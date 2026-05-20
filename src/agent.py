"""Main agent — the washable orchestrator.

Walks a parsed graph's topological order, dispatches NodeDocumenter
per node, then enumerates entrypoints and dispatches FlowTracer per
entrypoint.

Three public entry points:

- `build_agent(graph_id, vault_path)` constructs a deepagents agent
  wired to BOTH subagents (NodeDocumenter + FlowTracer). The LLM
  picks which subagent to dispatch based on the task message verb
  ("Document the node …" → NodeDocumenter; "Trace the entrypoint …"
  → FlowTracer).

- `dispatch_topo(graph_id, vault_path, ...)` is the level-gated
  loop for documenting nodes in topological order.

- `dispatch_flows(graph_id, vault_path, ...)` enumerates the
  attack-surface entrypoints and dispatches FlowTracer per
  entrypoint in a flat parallel pool (no ordering constraint).
"""

import concurrent.futures
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from deepagents import SubAgent, create_deep_agent
from langchain_openai import ChatOpenAI

from src.graph.persist import _validate_graph_id
from src.graph.topo import topo_levels, topo_order
from src.render.obsidian import (
    render_and_write_flow_note,
    render_and_write_node_note,
)
from src.subagents import FLOW_TRACER_SUBAGENT, NODE_DOCUMENTER_SUBAGENT
from src.tools import attack_surface, callees_of, graph_summary

_log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-5-mini"
DEFAULT_CONCURRENCY_CAP = 5
# Allowlist of characters permitted in node IDs entering the
# agent's LLM prompt surface (chunk 3.14). Today's Trailmark
# Solidity IDs match this exactly. If we add support for
# languages whose identifiers contain other characters, expand
# here AND add a regression test.
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_:.]+$")
# Bound the input so a pathological repo can't bloat the prompt
# or memory. Real Trailmark IDs cap around 100 chars; 500 is
# generous headroom.
_MAX_NODE_ID_LEN = 500
# Per-invocation timeout (seconds) for one agent.invoke() call.
# Chunk 3.11 closes the chunk 3.5 hang: gpt-5-mini calls typically
# take 5-15s; 600s is the deadline beyond which we declare the
# call hung and move on.
DEFAULT_PER_INVOKE_TIMEOUT = 600.0
# ChatOpenAI's HTTP request_timeout is set slightly below the
# orchestrator's per_invoke_timeout so HTTP fails first, the
# worker raises cleanly, and the future completes via the
# existing failure path. The orchestrator timeout is the backup
# for true indefinite hangs (langchain deadlock, etc).
_REQUEST_TIMEOUT_BUFFER = 60.0


def _build_system_prompt(graph_id: str, vault_path: str | Path) -> str:
    return f"""\
You are the washable orchestrator. Your job: document an entire parsed
codebase by walking its dependency graph and dispatching specialist
subagents in the correct order.

Context for this run (constants — do not change):
- graph_id   = {graph_id}
- vault_path = {vault_path}   (absolute)

The 9-step plan from PLAN.md (Phase column shows when each step
becomes real — until then, treat that step as a no-op):

1. Parse the repo with Trailmark. Persist the graph. [Phase 0 — DONE
   before you started; graph_id above is the result.]

2. Call `graph_summary(graph_id)` to get node counts, dependencies,
   and entrypoint count. Useful context for planning. [Phase 0]

3. Run `run_preanalysis(graph_id)` for security subgraphs (taint,
   blast radius, privilege boundaries). [Phase 4 — tool not yet
   available; skip silently for now.]

4. If the language is Solidity, run slither and call
   `augment_sarif(graph_id, sarif_path)` to merge findings into the
   graph. [Phase 4 — skip for now.]

5. Compute a topological order over the documentable node graph
   via `topo_order(graph_id)`. This returns node IDs in dependency
   order — bases come before derived contracts so wikilinks
   resolve. THIS IS THE ORDER YOU MUST DISPATCH NodeDocumenter IN.

6. NodeDocumenter dispatch: When invoked with a "Document the node
   `<id>`..." task message, dispatch the `node-documenter` subagent
   via the `task` tool (passing graph_id, node_id, vault_path), and
   return the absolute path the subagent wrote. The Python driver
   `dispatch_topo` walks the topo list; you do NOT loop. Do not
   invent extra dispatches.

7. FlowTracer dispatch: When invoked with a "Trace the entrypoint
   `<id>`..." task message, dispatch the `flow-tracer` subagent via
   the `task` tool (passing graph_id, entrypoint_node_id,
   vault_path), and return the absolute path the subagent wrote.
   The Python driver `dispatch_flows` walks the entrypoint list;
   you do NOT loop. Do not invent extra dispatches.

8. Dispatch the `risk-synthesizer` subagent ONCE over the
   augmented graph. [Phase 4 — skip for now.]

9. Write the root `README.md` map-of-content into vault_path with
   wikilinks into every populated section. [Chunk 2.4 — skip for
   now.]

Hard rules:
- Do NOT invent your own document structure. Subagents own per-note
  output via `render_and_write_node_note`; you orchestrate.
- Do NOT mutate graph_id or vault_path. They're set once, at build.
- If a tool you need isn't available yet (per the phase markers
  above), skip that step. Do not improvise.
"""


def _wrap_subagent_writers(
    subagent: SubAgent, vault_path: str | Path
) -> SubAgent:
    """Replace raw `render_and_write_*_note` functions in a
    subagent's tool list with closures that pre-bind `vault_path`.

    Chunk 3.17 (C-NEW-2 fix): the second-pass /review verified
    that LangChain's default schema generation exposed
    `vault_path` as an LLM-callable parameter on both writers,
    giving a prompt-injected agent a write-anywhere primitive.
    The closure approach:

    - Captures the orchestrator's trusted `vault_path` at build
      time so the LLM cannot supply its own.
    - Replaces the raw function in the subagent's tool list with
      a closure whose signature has NO `vault_path` arg, so the
      LLM tool schema for the closure cannot expose it.
    - Preserves the original function `__name__` so the LLM sees
      the same tool name in its system prompt.

    `cache_root` is closed differently — it stays on each tool's
    Python signature with `Annotated[Path, InjectedToolArg]` and
    a default value. The default makes it work without the
    orchestrator supplying it (which deepagents doesn't do for
    InjectedToolArg-marked params); `InjectedToolArg` hides it
    from the LLM schema. `vault_path` is a required arg with no
    sensible default, so it needs this closure approach instead.
    """
    def _safe_write_node_note(
        graph_id: str,
        node: dict[str, Any],
        graph_ctx: dict[str, Any] | None = None,
        body: str = "",
    ) -> str:
        """Write a node note. The vault root is supplied by the
        orchestrator at build time, not by the LLM. Same contract
        as `render_and_write_node_note` minus `vault_path`."""
        return render_and_write_node_note(
            vault_path, graph_id, node, graph_ctx, body
        )

    def _safe_write_flow_note(
        graph_id: str,
        entrypoint_node: dict[str, Any],
        paths: list[list[str]],
        overview: str = "",
        observations: list[str] | None = None,
    ) -> str:
        """Write a flow note. The vault root is supplied by the
        orchestrator at build time, not by the LLM. Same contract
        as `render_and_write_flow_note` minus `vault_path`."""
        return render_and_write_flow_note(
            vault_path,
            graph_id,
            entrypoint_node,
            paths,
            overview,
            observations,
        )

    # Match the LLM-visible tool name to the original. We can't
    # use functools.wraps because that would copy the full signature
    # back in (including vault_path), defeating the fix.
    _safe_write_node_note.__name__ = render_and_write_node_note.__name__
    _safe_write_node_note.__doc__ = render_and_write_node_note.__doc__
    _safe_write_flow_note.__name__ = render_and_write_flow_note.__name__
    _safe_write_flow_note.__doc__ = render_and_write_flow_note.__doc__

    def _replace_writer(t: Any) -> Any:
        if t is render_and_write_node_note:
            return _safe_write_node_note
        if t is render_and_write_flow_note:
            return _safe_write_flow_note
        return t

    wrapped: SubAgent = {
        **subagent,  # type: ignore[typeddict-item]
        "tools": [_replace_writer(t) for t in subagent["tools"]],
    }
    return wrapped


def build_agent(
    graph_id: str,
    vault_path: str | Path,
    *,
    model: str = DEFAULT_MODEL,
    request_timeout: float = DEFAULT_PER_INVOKE_TIMEOUT - _REQUEST_TIMEOUT_BUFFER,
) -> Any:
    """Build the washable main agent.

    `graph_id` is the 12-hex graph identifier from `trailmark_parse`.
    `vault_path` should be an absolute path (callers typically use
    `Path(vault).resolve()` so the LLM sees an unambiguous string).
    `request_timeout` (seconds) sets ChatOpenAI's per-HTTP-request
    timeout — slightly below the orchestrator's per-invoke timeout
    so HTTP fails first and worker threads exit cleanly. Set to
    None to disable (NOT recommended — see chunk 3.5 hang).

    Returns a langgraph `CompiledStateGraph` ready for `.invoke()`.
    The agent is wired with BOTH subagents (NodeDocumenter +
    FlowTracer). Each `.invoke()` carries one task — the LLM picks
    which subagent to dispatch based on the task message verb
    ("Document the node …" → NodeDocumenter; "Trace the
    entrypoint …" → FlowTracer). Python drivers (`dispatch_topo`
    and `dispatch_flows`) handle the enumeration loop.

    Subagent writer tools (`render_and_write_*_note`) are wrapped
    in closures that pre-bind `vault_path` so the LLM tool schema
    cannot expose it — chunk 3.17 C-NEW-2 fix.

    Chunk 3.22 / C-NEW-7: validates both `graph_id` and
    `vault_path` before building the system prompt that bakes
    them in. Fails fast with ValueError at construction
    rather than producing a corrupted system prompt that the
    LLM then operates on.
    """
    # Validate at the LLM trust boundary BEFORE constructing
    # the system prompt that interpolates these values.
    _validate_graph_id(graph_id)
    _validate_vault_path(vault_path)
    # mypy doesn't see pydantic dynamic fields on ChatOpenAI;
    # `request_timeout` is in model_fields (confirmed via probe).
    llm = ChatOpenAI(
        model=model,
        request_timeout=request_timeout,  # type: ignore[call-arg]
    )
    return create_deep_agent(
        model=llm,
        subagents=[
            _wrap_subagent_writers(NODE_DOCUMENTER_SUBAGENT, vault_path),
            _wrap_subagent_writers(FLOW_TRACER_SUBAGENT, vault_path),
        ],
        system_prompt=_build_system_prompt(graph_id, vault_path),
        tools=[
            graph_summary,
            topo_order,
        ],
    )


def _gather_with_per_invoke_timeout(
    futures_map: dict[concurrent.futures.Future, str],
    *,
    start_times: dict[str, float],
    per_invoke_timeout: float,
    on_done: Callable[[str, str, Any], None],
) -> None:
    """Drain `futures_map` with a per-future deadline keyed off
    WORKER START TIME (not submission). `start_times` is written
    by the worker thread in `_try_one` as its first action; gather
    reads it to detect hung workers (chunk 3.20 / C-NEW-5).

    Three drain paths per polling iteration:

    1. Completion: future is `done`. on_done invoked with the
       result (or a "fail" tuple if `future.result()` raised).
    2. Per-invoke timeout (running worker hang): item_id is in
       `start_times` and `now - start_times[item_id]` exceeds
       `per_invoke_timeout`. on_done invoked with a TimeoutError
       describing the per-invoke breach; future.cancel() is best-
       effort (Python can't kill running threads — daemon workers
       die when the process exits).
    3. Pool deadlock (all workers wedged): pending hasn't
       decreased in `2 × per_invoke_timeout` seconds. Remaining
       pending futures (queued AND any still-running) marked as
       TimeoutError. Catches the case where workers hang and
       their replacements can't start, so the dispatch returns
       a complete summary instead of looping forever.

    Queued futures (item_id NOT in `start_times`) have no per-
    invoke deadline — they're not running, so the per-invoke
    timeout doesn't apply. The deadlock detector catches the
    pathological "queue never advances" case.

    Polls with `concurrent.futures.wait` so completed futures are
    drained promptly, and hung futures are detected within
    poll-interval seconds of crossing their deadline.
    """
    pending = set(futures_map.keys())
    poll_interval = min(2.0, max(0.05, per_invoke_timeout / 4))
    # Deadlock detector: time of last `pending` decrease.
    last_progress = time.monotonic()

    while pending:
        size_before = len(pending)
        done, _still_pending = concurrent.futures.wait(
            pending,
            timeout=poll_interval,
            return_when=concurrent.futures.FIRST_COMPLETED,
        )
        # Completion branch.
        for future in done:
            nid = futures_map[future]
            try:
                # Resolve the worker's outcome first. Worker
                # exceptions translate to a ("fail", "...")
                # tuple so we still call on_done with a real
                # status.
                try:
                    status, info = future.result(timeout=0)
                except Exception as e:
                    status, info = "fail", f"{type(e).__name__}: {e}"
                # Then call on_done exactly once. A failure
                # HERE (e.g., caller-supplied on_progress hit a
                # broken pipe) is logged and swallowed so the
                # gather loop drains the rest of pending instead
                # of abandoning in-flight workers (chunk 3.19,
                # /review C-NEW-4).
                try:
                    on_done(nid, status, info)
                except Exception:
                    _log.exception(
                        "on_done raised for %s; continuing to "
                        "drain remaining futures",
                        nid,
                    )
            finally:
                pending.discard(future)

        # Per-invoke timeout branch: only RUNNING workers have a
        # deadline. Queued futures (not in start_times) wait.
        now = time.monotonic()
        for future in list(pending):
            nid = futures_map[future]
            if nid not in start_times:
                # Future hasn't been picked up by a worker yet
                # (queued behind a saturated pool). No invocation
                # to time out. The deadlock detector below will
                # catch a genuinely wedged pool.
                continue
            if now - start_times[nid] > per_invoke_timeout:
                try:
                    on_done(
                        nid,
                        "fail",
                        f"TimeoutError: invocation exceeded "
                        f"per_invoke_timeout={per_invoke_timeout}s",
                    )
                except Exception:
                    _log.exception(
                        "on_done raised for %s during timeout "
                        "handling; continuing",
                        nid,
                    )
                finally:
                    future.cancel()
                    pending.discard(future)

        # Pool deadlock detector: if no future has been
        # discarded in 2× per_invoke_timeout seconds, the pool
        # is wedged (workers hung AND replacements can't start).
        # Mark remaining pending as failed so the dispatch
        # returns a complete summary.
        if len(pending) < size_before:
            last_progress = time.monotonic()
        elif (
            pending
            and time.monotonic() - last_progress
            > 2 * per_invoke_timeout
        ):
            deadlock_msg = (
                f"TimeoutError: pool made no progress for "
                f"2 × per_invoke_timeout={2 * per_invoke_timeout}s "
                f"(queue blocked behind hung workers)"
            )
            for future in list(pending):
                nid = futures_map[future]
                try:
                    on_done(nid, "fail", deadlock_msg)
                except Exception:
                    _log.exception(
                        "on_done raised for %s during deadlock "
                        "handling; continuing",
                        nid,
                    )
                finally:
                    future.cancel()
                    pending.discard(future)


def _validate_node_id(node_id: str) -> None:
    """Allowlist validation for node IDs entering the agent's
    LLM prompt surface (chunk 3.14). Defends against prompt
    injection via backticks, newlines, or other chars that could
    escape the backtick-quoted fence in `_invoke_one*` task
    messages.

    Today's Solidity IDs match `[A-Za-z0-9_:.]+` exactly (probed
    against Tier 0/1 fixtures). If Trailmark grows other-language
    support whose identifiers contain other characters, expand
    `_NODE_ID_RE` AND add a regression test for the new shape.

    Raises ValueError on any mismatch — the calling try/except in
    `dispatch_*._try_one` records it as a per-node failure and
    the dispatch continues.
    """
    if not node_id or len(node_id) > _MAX_NODE_ID_LEN:
        raise ValueError(
            f"invalid node_id length: {len(node_id)} (must be "
            f"1..{_MAX_NODE_ID_LEN})"
        )
    if not _NODE_ID_RE.fullmatch(node_id):
        raise ValueError(
            f"invalid node_id {node_id!r}: must match "
            f"{_NODE_ID_RE.pattern}"
        )


# Reject control chars (U+0000..U+001F, U+007F) and unicode
# line/paragraph separators (U+2028, U+2029) in vault_path —
# any of these could inject new prompt lines into the LLM
# system message that interpolates vault_path. Backticks are
# allowed: POSIX paths can legitimately contain them, and the
# system prompt doesn't backtick-fence vault_path (uses
# `vault_path = {value}` directly, no fence to escape).
_VAULT_PATH_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f  ]")
_MAX_VAULT_PATH_LEN = 4096


def _validate_vault_path(vault_path: str | Path) -> None:
    """Reject vault paths that could inject into LLM prompt
    surfaces (chunk 3.22 / C-NEW-7).

    Disallowed:
      - empty string
      - length > 4096 chars
      - any control char (U+0000..U+001F, U+007F)
      - unicode line/paragraph separators (U+2028, U+2029)
      - non-absolute paths

    Allowed: spaces, parentheses, backticks, hyphens, all
    standard POSIX path characters. Build_agent is the only
    caller; ValueError there means agent construction failed
    cleanly before any LLM-facing surface saw the bad value.
    """
    s = str(vault_path)
    if not s:
        raise ValueError("invalid vault_path: empty")
    if len(s) > _MAX_VAULT_PATH_LEN:
        raise ValueError(
            f"invalid vault_path length: {len(s)} (must be "
            f"1..{_MAX_VAULT_PATH_LEN})"
        )
    if _VAULT_PATH_BAD_CHARS.search(s):
        raise ValueError(
            "invalid vault_path: contains control chars or "
            "unicode line separators (rejects \\n / \\r / \\t / "
            "U+2028 / U+2029 to prevent LLM prompt injection)"
        )
    if not Path(s).is_absolute():
        raise ValueError(
            f"invalid vault_path {s!r}: must be absolute"
        )


# Task-message templates for the two dispatch verbs. The main
# agent's system prompt steers on the verb at the start of the
# task message ("Document the node ..." → NodeDocumenter;
# "Trace the entrypoint ..." → FlowTracer). Keep both literal
# verbs intact when editing — the orchestrator's prompt is
# coupled to them.
_NODE_DOC_TEMPLATE = (
    "Document the node `{node_id}` in graph `{graph_id}`. "
    "Dispatch the `node-documenter` subagent via the `task` "
    "tool — do not generate note content yourself."
)
_FLOW_TRACE_TEMPLATE = (
    "Trace the entrypoint `{node_id}` in graph "
    "`{graph_id}`. "
    "Dispatch the `flow-tracer` subagent via the `task` tool — "
    "do not generate the flow note content yourself."
)


def _invoke_one(
    agent: Any,
    graph_id: str,
    node_id: str,
    vault_path: str,
    task_template: str = _NODE_DOC_TEMPLATE,
) -> dict[str, Any]:
    """Single agent invocation. Validates `node_id` at the trust
    boundary (chunk 3.14) then dispatches via `task_template`.
    Exceptions bubble to the caller's dispatch loop, which records
    them per-node.

    Pre-3.16 this was duplicated as a separate `_invoke_one_flow`
    (byte-identical except for the verb in the task message);
    chunk 3.16's /review I15 collapsed them by parameterizing
    the template. The current `_invoke_one_flow` is a one-line
    wrapper preserved for call-site readability in dispatch_flows.
    """
    _validate_node_id(node_id)
    # Chunk 3.22 / C-NEW-7: graph_id is interpolated into the
    # task template, so it's part of the LLM prompt surface.
    # Validate at this boundary (defense-in-depth — load_graph
    # in persist.py already validates, but a future refactor
    # could bypass that path).
    _validate_graph_id(graph_id)
    task_msg = task_template.format(
        node_id=node_id, graph_id=graph_id
    )
    result = agent.invoke(
        {"messages": [{"role": "user", "content": task_msg}]}
    )
    last = result["messages"][-1].content
    return {"node_id": node_id, "agent_reply": last}


def _make_recorder(
    successes: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    *,
    total: int,
    on_progress: Callable[[int, int, str], None] | None,
) -> Callable[[str, str, Any], None]:
    """Build the on_done callback shared by both dispatchers
    (chunk 3.16, /review I16). Appends successful invokes to
    `successes`, failures to `failures` (with a `"node_id"` key
    even for entrypoints — the failure dict shape is uniform),
    and fires `on_progress(idx, total, item_id)` per completed
    item.

    The returned closure owns its own progress index that
    accumulates across multiple `_run_pool` calls — this is
    what makes `dispatch_topo`'s per-level loop emit a single
    1..N progress sequence across all levels rather than
    restarting at 1 per level.

    The recorder runs in the main thread (driven by
    `_gather_with_per_invoke_timeout`'s drain loop), so the
    `nonlocal progress_idx` mutation is single-threaded — no
    lock needed.
    """
    progress_idx = 0

    def _record(item_id: str, status: str, info: Any) -> None:
        nonlocal progress_idx
        progress_idx += 1
        # Record the outcome BEFORE notifying the caller. A
        # broken on_progress callback (chunk 3.19 / C-NEW-4)
        # must not lose the outcome tracking — the dispatch's
        # whole point is to return a complete summary. Notify
        # second so that even if on_progress raises and the
        # exception escapes this closure (up to _gather's
        # try/except), the successes/failures lists are
        # already populated for this item.
        if status == "ok":
            successes.append(info)
        else:
            failures.append({"node_id": item_id, "error": info})
        if on_progress is not None:
            on_progress(progress_idx, total, item_id)

    return _record


def _run_pool(
    items: list[str],
    invoke_fn: Callable[[str], dict[str, Any]],
    *,
    concurrency_cap: int,
    per_invoke_timeout: float,
    on_done: Callable[[str, str, Any], None],
    log_kind: str,
) -> None:
    """Run one batch of items through a ThreadPoolExecutor with
    per-invocation timeout (chunk 3.11). For each item:

      - `invoke_fn(item)` returns → `on_done(item, "ok", result_dict)`
      - any exception           → `on_done(item, "fail", "ExcType: msg")`
      - exceeds per_invoke_timeout → `on_done(..., "fail", "TimeoutError: ...")`

    `dispatch_topo` calls this once per topological level (the
    inheritance-aware level boundary survives in the caller);
    `dispatch_flows` calls it once with the flat entrypoint list.
    Chunk 3.16's /review I16 extracted the pool plumbing from
    both dispatchers — it was byte-identical except for the
    log-kind prefix in the exception path.

    Pool uses explicit shutdown(wait=False, cancel_futures=True)
    instead of context manager because the `with` form blocks
    on hung workers (chunk 3.5 hang). Hung workers are daemon
    threads and die when the process exits.

    `log_kind` distinguishes log messages between dispatchers
    ("dispatch" vs "flow dispatch"). Kept as a parameter — not
    derived from invoke_fn — so log greps for "flow dispatch
    failed" continue to match.
    """
    # Worker-stamped actual start times, keyed by item_id (not
    # Future identity, because the worker thread doesn't know
    # its own Future). Pre-3.20 the per-invoke deadline was set
    # at SUBMISSION, so tail items queued behind saturated
    # workers would burn their entire per_invoke_timeout
    # budget on queue wait. Now the worker stamps this on
    # entry; _gather uses it for the deadline check (chunk 3.20
    # / C-NEW-5).
    start_times: dict[str, float] = {}

    def _try_one(item_id: str) -> tuple[str, Any]:
        # FIRST action: stamp the actual worker start time so
        # _gather's deadline check is against execution time,
        # not submission time. CPython dict ops are atomic per-
        # key — no lock needed for this single-key write paired
        # with single-key reads in _gather.
        start_times[item_id] = time.monotonic()
        try:
            return ("ok", invoke_fn(item_id))
        except Exception as e:
            _log.exception("%s failed for %s", log_kind, item_id)
            return ("fail", f"{type(e).__name__}: {e}")

    pool = ThreadPoolExecutor(max_workers=concurrency_cap)
    try:
        futures_map = {
            pool.submit(_try_one, item): item for item in items
        }
        _gather_with_per_invoke_timeout(
            futures_map,
            start_times=start_times,
            per_invoke_timeout=per_invoke_timeout,
            on_done=on_done,
        )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def dispatch_topo(
    graph_id: str,
    vault_path: str,
    *,
    model: str = DEFAULT_MODEL,
    concurrency_cap: int = DEFAULT_CONCURRENCY_CAP,
    on_progress: Callable[[int, int, str], None] | None = None,
    per_invoke_timeout: float = DEFAULT_PER_INVOKE_TIMEOUT,
) -> dict[str, Any]:
    """Walk the topological order for `graph_id` and dispatch
    NodeDocumenter per node. Returns a summary of what shipped.

    `vault_path` should be an absolute path. `concurrency_cap` caps
    parallel agent invocations via a ThreadPoolExecutor — the
    lost-update race in `annotate` is handled by the threading.Lock
    in `src/tools.py`.

    `per_invoke_timeout` (seconds) bounds each NodeDocumenter
    invocation. Hung invocations are recorded as TimeoutError
    failures after the deadline; the dispatch continues. Default
    600s = 10 minutes (the chunk 3.5 production hang motivating
    this was 17 minutes of indefinite block).

    Per-node exceptions are caught and recorded in `failures`; the
    loop never aborts mid-walk.

    Cache root: ALWAYS the default `.washable/graph/`. We don't
    expose an override because subagent tools (get_node, callers_of,
    annotate, etc.) bind their `cache_root` default at module-import
    time on the LLM agent's tool list — there's no clean way to
    thread a per-call override through that surface without
    signature-stripping closures. trailmark_parse + dispatch_topo +
    every subagent tool all read/write the default, so consistency
    holds for real runs. Tools-layer tests that need isolation
    should call the tool functions directly with explicit
    `cache_root=` instead of going through dispatch_topo.

    Level-gating limitation (chunk 3.24 / I-NEW-4): the per-level
    barrier ensures level N+1 only STARTS after every level-N
    future has been drained from `pending` (via completion or
    per-invoke timeout). However, `pool.shutdown(wait=False,
    cancel_futures=True)` lets a per-invoke-timed-out worker keep
    running in its daemon thread (Python can't kill threads). If
    the worker's `agent.invoke()` eventually completes — e.g., a
    hung LLM API call that recovers AFTER the timeout fired — it
    may write a file to vault AFTER level N+1 has already started.

    Observable effects:
    - The dispatch summary records the node as failed (CORRECT —
      the timeout fired and `pending` was discarded; the summary
      reflects what the orchestrator saw).
    - A file may nevertheless appear on disk after the failure
      was recorded. Downstream consumers should treat the run
      summary as authoritative; orphan late-writes are harmless
      (atomic, well-formed) but contradict the failure record.

    Rate in practice: low. `per_invoke_timeout=600s` is generous
    vs. typical 15-60s invokes. A worker completing in the
    (600s, 600s+epsilon) window requires a hung LLM API call that
    recovers in a narrow window. If this becomes a real
    production issue, change `wait=False` → `wait=True` in
    `_run_pool`'s shutdown call, accepting that the dispatcher
    will block until all workers complete (re-introducing the
    chunk 3.5 hang exposure).

    Returns:
        {
            "graph_id":    str,
            "node_count":  int,
            "successes":   [{"node_id", "agent_reply"}, ...],
            "failures":    [{"node_id", "error"}, ...],
            "order":       [node_id, ...],
        }
    """
    if concurrency_cap < 1:
        raise ValueError(
            f"concurrency_cap must be >= 1 (got {concurrency_cap})"
        )
    if per_invoke_timeout <= 0:
        raise ValueError(
            f"per_invoke_timeout must be > 0 "
            f"(got {per_invoke_timeout})"
        )

    # One agent shared across all worker threads. Safe because
    # langgraph's CompiledStateGraph keeps no mutable instance state
    # across `.invoke()` calls — the run state lives in the input
    # dict, and the underlying ChatOpenAI client is stateless.
    # Empirically verified at cap=5 on Tier 0 (8 nodes) and Tier 1
    # (22 nodes) without state corruption.
    agent = build_agent(
        graph_id,
        vault_path,
        model=model,
        request_timeout=per_invoke_timeout - _REQUEST_TIMEOUT_BUFFER,
    )
    levels = topo_levels(graph_id)
    order = [nid for level in levels for nid in level]

    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    record = _make_recorder(
        successes, failures,
        total=len(order),
        on_progress=on_progress,
    )

    # Dispatch one level at a time. Within a level, nodes are
    # independent (verified by topo_levels) — _run_pool fans them
    # out up to concurrency_cap. Across levels, we wait for the
    # level to finish before starting the next; that's what
    # guarantees a derived contract's wikilink targets exist on
    # disk by the time the derived contract's NodeDocumenter
    # runs. (Reusing one `record` across levels keeps the
    # progress index 1..N rather than restarting per level.)
    for level in levels:
        _run_pool(
            level,
            lambda nid: _invoke_one(agent, graph_id, nid, vault_path),
            concurrency_cap=concurrency_cap,
            per_invoke_timeout=per_invoke_timeout,
            on_done=record,
            log_kind="dispatch",
        )

    return {
        "graph_id": graph_id,
        "node_count": len(order),
        "successes": successes,
        "failures": failures,
        "order": order,
    }


def _invoke_one_flow(
    agent: Any, graph_id: str, entrypoint_id: str, vault_path: str
) -> dict[str, Any]:
    """Thin wrapper for FlowTracer dispatch (chunk 3.16). Routes
    to `_invoke_one` with the flow-tracing task template; kept
    as a named function so `dispatch_flows` reads clearly and the
    chunk 3.14 validation test pattern stays unchanged."""
    return _invoke_one(
        agent, graph_id, entrypoint_id, vault_path, _FLOW_TRACE_TEMPLATE
    )


def dispatch_flows(
    graph_id: str,
    vault_path: str,
    *,
    model: str = DEFAULT_MODEL,
    concurrency_cap: int = DEFAULT_CONCURRENCY_CAP,
    on_progress: Callable[[int, int, str], None] | None = None,
    skip_leaf_entrypoints: bool = True,
    per_invoke_timeout: float = DEFAULT_PER_INVOKE_TIMEOUT,
    entrypoint_filter: Callable[
        [list[dict[str, Any]]], list[dict[str, Any]]
    ]
    | None = None,
) -> dict[str, Any]:
    """Enumerate entrypoints via `attack_surface` and dispatch
    FlowTracer per entrypoint. Returns a summary of what shipped.

    `skip_leaf_entrypoints` (default True): filter out entrypoints
    with no outgoing callees (leaf functions like simple getters).
    Tracing them produces an empty-paths placeholder note —
    wasteful at scale.

    `per_invoke_timeout` (seconds) bounds each FlowTracer
    invocation. Hung invocations are recorded as TimeoutError
    failures after the deadline; the dispatch continues. See
    `dispatch_topo` for the rationale (chunk 3.11 / chunk 3.5
    hang).

    `entrypoint_filter` (chunk 3.15) is an optional callable that
    scopes the dispatch to a subset of the attack surface.
    Applied AFTER `attack_surface()` and BEFORE
    `skip_leaf_entrypoints`, so callers can pre-narrow without
    losing the leaf filter or compose both for selective runs.
    `scripts/trace_one_flow.py` uses this to run one entrypoint
    without monkey-patching module globals.

    Per-entrypoint exceptions are caught and recorded in `failures`;
    the loop never aborts mid-walk. Mirrors `dispatch_topo`'s shape
    so a future orchestrator can compose both passes.

    Cache root: ALWAYS the default `.washable/graph/` (same
    constraint as `dispatch_topo` — subagent tools bind their
    cache_root default at module-import time on the LLM tool list).

    Returns:
        {
            "graph_id":         str,
            "entrypoint_count": int,
            "successes":        [{"node_id", "agent_reply"}, ...],
            "failures":         [{"node_id", "error"}, ...],
            "order":            [entrypoint_id, ...],
        }
    """
    if concurrency_cap < 1:
        raise ValueError(
            f"concurrency_cap must be >= 1 (got {concurrency_cap})"
        )
    if per_invoke_timeout <= 0:
        raise ValueError(
            f"per_invoke_timeout must be > 0 "
            f"(got {per_invoke_timeout})"
        )

    entrypoints = attack_surface(graph_id)
    if entrypoint_filter is not None:
        entrypoints = entrypoint_filter(entrypoints)
    if skip_leaf_entrypoints:
        entrypoints = [
            e for e in entrypoints
            if callees_of(graph_id, e["node_id"])
        ]
    order = [e["node_id"] for e in entrypoints]

    agent = build_agent(
        graph_id,
        vault_path,
        model=model,
        request_timeout=per_invoke_timeout - _REQUEST_TIMEOUT_BUFFER,
    )
    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    record = _make_recorder(
        successes, failures,
        total=len(order),
        on_progress=on_progress,
    )

    # Entrypoints are independent — one flat _run_pool call, no
    # level-gating (unlike dispatch_topo's inheritance-aware walk).
    _run_pool(
        order,
        lambda eid: _invoke_one_flow(agent, graph_id, eid, vault_path),
        concurrency_cap=concurrency_cap,
        per_invoke_timeout=per_invoke_timeout,
        on_done=record,
        log_kind="flow dispatch",
    )

    return {
        "graph_id": graph_id,
        "entrypoint_count": len(order),
        "successes": successes,
        "failures": failures,
        "order": order,
    }
