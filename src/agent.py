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
import contextvars
import json
import logging
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from deepagents import SubAgent, create_deep_agent
from langchain_openai import ChatOpenAI

from src.graph.cache import (
    compute_file_hashes,
    load_file_hash_cache,
    save_file_hash_cache,
)
from src.graph.persist import _validate_graph_id, load_graph
from src.graph.topo import topo_levels, topo_order
from src.render.obsidian import (
    render_and_write_flow_note,
    render_and_write_node_note,
    render_and_write_risk_note,
)
from src.subagents import (
    FLOW_TRACER_SUBAGENT,
    NODE_DOCUMENTER_SUBAGENT,
    RISK_SYNTHESIZER_SUBAGENT,
)
from src.tools import (
    attack_surface,
    callees_of,
    clear_annotations_by_source,
    graph_summary,
    list_nodes,
)

_log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-5-mini"
DEFAULT_CONCURRENCY_CAP = 5
# Allowlist of characters permitted in node IDs entering the
# agent's LLM prompt surface. Today's Trailmark Solidity IDs
# match this exactly. If we add support for languages whose
# identifiers contain other characters, expand here AND add a
# regression test.
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_:.]+$")
# Bound the input so a pathological repo can't bloat the prompt
# or memory. Real Trailmark IDs cap around 100 chars; 500 is
# generous headroom.
_MAX_NODE_ID_LEN = 500
# Per-invocation timeout (seconds) for one agent.invoke() call.
# gpt-5-mini calls typically take 5-15s; 600s is the deadline
# beyond which we declare the call hung and move on.
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
   blast radius, privilege boundaries). [Phase 4 — the orchestrator
   script handles this BEFORE invoking you; the subgraphs are
   already registered on the graph by the time you receive a
   task.]

4. If the language is Solidity, run slither and call
   `augment_sarif(graph_id, sarif_path)` to merge findings into the
   graph. [Phase 4 — the orchestrator script handles this BEFORE
   invoking you; finding annotations are already attached to nodes
   by the time you receive a task.]

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

8. RiskSynthesizer dispatch: When invoked with a "Synthesize the
   three risk notes..." task message, dispatch the
   `risk-synthesizer` subagent via the `task` tool (passing
   graph_id), and return the JSON list of absolute file paths the
   subagent wrote. The Python driver `dispatch_risk_synthesis`
   triggers this once after dispatch_topo and dispatch_flows drain
   (RiskSynthesizer issues many `annotate` calls that would
   contend on _ANNOTATE_LOCK with concurrent workers).

9. Write the root `README.md` map-of-content into vault_path with
   wikilinks into every populated section. [Phase 2 — skip for now.]

Hard rules:
- Do NOT invent your own document structure. Subagents own per-note
  output via `render_and_write_node_note`; you orchestrate.
- Do NOT mutate graph_id or vault_path. They're set once, at build.
- If a tool you need isn't available yet (per the phase markers
  above), skip that step. Do not improvise.
"""


# Codex round-17 fix: cross-node overwrite defense.
#
# The dispatcher sets these contextvars before invoking the
# agent for a specific node / entrypoint. The LLM-facing
# writer wrappers read them as the AUTHORITATIVE id; the
# LLM no longer supplies (or even sees) a `node` /
# `entrypoint_node` argument. A prompt-injected agent that
# tries to call the writer for a DIFFERENT node than the
# one being dispatched simply can't address the cross-node
# target — there is no `node` arg to forge.
#
# Why contextvars and not a closure-per-dispatch: `build_agent`
# constructs ONE agent shared across all worker threads in
# a `dispatch_topo` / `dispatch_flows` run (see the comment
# on line 806). Per-dispatch closures would require
# re-building the agent for every node, which is expensive.
# `contextvars.ContextVar` is the standard library's
# thread-local-with-proper-coroutine-semantics primitive;
# values set in one thread are isolated from other threads,
# and the agent's sync invoke chain inherits the context
# from the calling thread.
_EXPECTED_NODE_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_EXPECTED_NODE_ID",
)
_EXPECTED_ENTRYPOINT_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_EXPECTED_ENTRYPOINT_ID",
)


def _wrap_subagent_writers(
    subagent: SubAgent, vault_path: str | Path
) -> SubAgent:
    """Replace raw `render_and_write_*_note` functions in a
    subagent's tool list with closures that pre-bind `vault_path`.

    LangChain's default schema generation exposes any keyword-
    only param (including `vault_path`) as LLM-callable, which
    would give a prompt-injected agent a write-anywhere
    primitive. The closure approach:

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
        graph_ctx: dict[str, Any] | None = None,
        body: str = "",
    ) -> str:
        """Write a node note. The vault root AND the target
        node id are supplied by the orchestrator at dispatch
        time, not by the LLM.

        Codex round-17 fix: the LLM no longer passes `node`.
        The node id comes from `_EXPECTED_NODE_ID` (set by
        `_invoke_one` before agent.invoke). The LLM-controlled
        surface here is: graph_ctx + body. If the agent is
        prompt-injected to "document a different node", it has
        no argument through which to address the cross-node
        target — the wrapper writes to the dispatched node
        only."""
        try:
            expected_id = _EXPECTED_NODE_ID.get()
        except LookupError as e:
            raise RuntimeError(
                "render_and_write_node_note called outside a "
                "dispatch context (no _EXPECTED_NODE_ID bound). "
                "Set the contextvar in the dispatcher before "
                "invoking the agent."
            ) from e
        return render_and_write_node_note(
            vault_path,
            graph_id,
            {"id": expected_id},
            graph_ctx,
            body,
        )

    def _safe_write_flow_note(
        graph_id: str,
        paths: list[list[str]],
        overview: str = "",
        observations: list[str] | None = None,
    ) -> str:
        """Write a flow note. The vault root AND the entrypoint
        id are supplied by the orchestrator at dispatch time,
        not by the LLM.

        Codex round-17 fix: the LLM no longer passes
        `entrypoint_node`. The entrypoint id comes from
        `_EXPECTED_ENTRYPOINT_ID` (set by `_invoke_one` before
        agent.invoke). Same cross-node defense as
        `_safe_write_node_note` — a prompt-injected FlowTracer
        cannot redirect the write to a different entrypoint's
        flow note."""
        try:
            expected_id = _EXPECTED_ENTRYPOINT_ID.get()
        except LookupError as e:
            raise RuntimeError(
                "render_and_write_flow_note called outside a "
                "dispatch context (no _EXPECTED_ENTRYPOINT_ID "
                "bound). Set the contextvar in the dispatcher "
                "before invoking the agent."
            ) from e
        return render_and_write_flow_note(
            vault_path,
            graph_id,
            {"id": expected_id},
            paths,
            overview,
            observations,
        )

    def _safe_write_risk_note(
        graph_id: str,
        risk_name: str,
        overview: str,
        involved_nodes: list[str],
        observations: list[str] | None = None,
    ) -> str:
        """Write a risk note. The vault root is supplied by the
        orchestrator at build time, not by the LLM. Same contract
        as `render_and_write_risk_note` minus `vault_path`."""
        return render_and_write_risk_note(
            vault_path,
            graph_id,
            risk_name,
            overview,
            involved_nodes,
            observations,
        )

    # Match the LLM-visible tool name to the original. We can't
    # use functools.wraps because that would copy the full signature
    # back in (including vault_path), defeating the fix.
    _safe_write_node_note.__name__ = render_and_write_node_note.__name__
    _safe_write_node_note.__doc__ = render_and_write_node_note.__doc__
    _safe_write_flow_note.__name__ = render_and_write_flow_note.__name__
    _safe_write_flow_note.__doc__ = render_and_write_flow_note.__doc__
    _safe_write_risk_note.__name__ = render_and_write_risk_note.__name__
    _safe_write_risk_note.__doc__ = render_and_write_risk_note.__doc__

    def _replace_writer(t: Any) -> Any:
        if t is render_and_write_node_note:
            return _safe_write_node_note
        if t is render_and_write_flow_note:
            return _safe_write_flow_note
        if t is render_and_write_risk_note:
            return _safe_write_risk_note
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
    None to disable (NOT recommended — see DEFAULT_PER_INVOKE_TIMEOUT
    comment for the hang scenario this protects against).

    Returns a langgraph `CompiledStateGraph` ready for `.invoke()`.
    The agent is wired with BOTH subagents (NodeDocumenter +
    FlowTracer). Each `.invoke()` carries one task — the LLM picks
    which subagent to dispatch based on the task message verb
    ("Document the node …" → NodeDocumenter; "Trace the
    entrypoint …" → FlowTracer). Python drivers (`dispatch_topo`
    and `dispatch_flows`) handle the enumeration loop.

    Subagent writer tools (`render_and_write_*_note`) are wrapped
    in closures that pre-bind `vault_path` so the LLM tool schema
    cannot expose it (see `_wrap_subagent_writers`).

    Validates both `graph_id` and `vault_path` before building
    the system prompt that bakes them in. Fails fast with
    ValueError at construction rather than producing a corrupted
    system prompt that the LLM then operates on.
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
            _wrap_subagent_writers(RISK_SYNTHESIZER_SUBAGENT, vault_path),
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
    reads it to detect hung workers.

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

    def _drain_completion(future: concurrent.futures.Future) -> None:
        """Process a `done()` future as a completion: resolve
        its result (translating worker exceptions to a "fail"
        tuple), call on_done with safety, discard from pending.

        Used by both the completion branch (futures that wait()
        returned in `done`) and the race-recovery path inside
        the timeout branch (for futures that became done between
        wait() and the deadline check).
        """
        nid = futures_map[future]
        try:
            try:
                status, info = future.result(timeout=0)
            except Exception as e:
                status, info = "fail", f"{type(e).__name__}: {e}"
            # A failure HERE (e.g., caller-supplied on_progress
            # hit a broken pipe) is logged and swallowed so the
            # gather loop drains the rest of pending instead of
            # abandoning in-flight workers.
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

    while pending:
        size_before = len(pending)
        done, _still_pending = concurrent.futures.wait(
            pending,
            timeout=poll_interval,
            return_when=concurrent.futures.FIRST_COMPLETED,
        )
        # Completion branch.
        for future in done:
            _drain_completion(future)

        # Per-invoke timeout branch with race recovery: a
        # future may have completed between wait() and now.
        # Check done() first so we process race-completed
        # futures as completions rather than mis-marking them
        # as TimeoutErrors.
        now = time.monotonic()
        for future in list(pending):
            if future.done():
                # Race: completed between wait() and this
                # check. Its side effect (e.g., file write)
                # is already on disk; on_done must reflect
                # the real outcome, not a spurious timeout.
                _drain_completion(future)
                continue
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
    LLM prompt surface. Defends against prompt injection via
    backticks, newlines, or other chars that could escape the
    backtick-quoted fence in `_invoke_one` task messages.

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
    surfaces.

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
    task_template: str = _NODE_DOC_TEMPLATE,
) -> dict[str, Any]:
    """Single agent invocation. Validates `node_id` and
    `graph_id` at the LLM trust boundary, then dispatches via
    `task_template`. Exceptions bubble to the caller's dispatch
    loop, which records them per-node.

    `task_template` defaults to the NodeDocumenter prompt;
    pass `_FLOW_TRACE_TEMPLATE` for the FlowTracer dispatch.

    vault_path is intentionally NOT a parameter: it's already
    baked into `agent` via build_agent's system prompt and the
    _wrap_subagent_writers closures. The dispatcher binds vault
    once at agent construction; per-invoke routing isn't needed.

    Codex round-17 fix: binds `_EXPECTED_NODE_ID` /
    `_EXPECTED_ENTRYPOINT_ID` for the agent's invoke. The
    writer wrappers read from these contextvars instead of
    accepting `node` / `entrypoint_node` from the LLM —
    closes the cross-node overwrite vector. Which var to set
    is determined by the task template (NodeDocumenter vs
    FlowTracer).
    """
    _validate_node_id(node_id)
    # graph_id is interpolated into the task template, so it's
    # part of the LLM prompt surface. Validate at this boundary
    # (defense-in-depth — load_graph in persist.py already
    # validates, but a future refactor could bypass that path).
    _validate_graph_id(graph_id)
    task_msg = task_template.format(
        node_id=node_id, graph_id=graph_id
    )
    if task_template is _NODE_DOC_TEMPLATE:
        node_token = _EXPECTED_NODE_ID.set(node_id)
        entry_token = None
    elif task_template is _FLOW_TRACE_TEMPLATE:
        entry_token = _EXPECTED_ENTRYPOINT_ID.set(node_id)
        node_token = None
    else:
        # Future templates (e.g., risk synthesis) don't bind
        # either contextvar — they don't go through the
        # node/flow writer wrappers.
        node_token = None
        entry_token = None
    try:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": task_msg}]}
        )
    finally:
        if node_token is not None:
            _EXPECTED_NODE_ID.reset(node_token)
        if entry_token is not None:
            _EXPECTED_ENTRYPOINT_ID.reset(entry_token)
    last = result["messages"][-1].content
    return {"node_id": node_id, "agent_reply": last}


def _make_recorder(
    successes: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    *,
    total: int,
    on_progress: Callable[[int, int, str], None] | None,
) -> Callable[[str, str, Any], None]:
    """Build the on_done callback shared by both dispatchers.
    Appends successful invokes to `successes`, failures to
    `failures` (with a `"node_id"` key even for entrypoints —
    the failure dict shape is uniform), and fires
    `on_progress(idx, total, item_id)` per completed item.

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
        # broken on_progress callback must not lose the outcome
        # tracking — the dispatch's whole point is to return a
        # complete summary. Notify second so that even if
        # on_progress raises and the exception escapes this
        # closure (up to _gather's try/except), the
        # successes/failures lists are already populated for
        # this item.
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
    per-invocation timeout. For each item:

      - `invoke_fn(item)` returns → `on_done(item, "ok", result_dict)`
      - any exception           → `on_done(item, "fail", "ExcType: msg")`
      - exceeds per_invoke_timeout → `on_done(..., "fail", "TimeoutError: ...")`

    `dispatch_topo` calls this once per topological level (the
    inheritance-aware level boundary survives in the caller);
    `dispatch_flows` calls it once with the flat entrypoint list.

    Pool uses explicit shutdown(wait=False, cancel_futures=True)
    instead of the context manager because the `with` form blocks
    on hung workers — the dispatch summary returns promptly even
    if some invocations are wedged.

    Process-exit caveat: ThreadPoolExecutor workers are NOT
    daemon threads (Python default). A truly hung worker (e.g.,
    wedged HTTP call) will keep the Python process alive after
    main() returns, because the interpreter waits for all non-
    daemon threads. The per-invoke timeout fires and on_done
    records "fail" correctly, so the dispatch summary is right;
    but the operator may need to ctrl-C if every retry path is
    wedged. Script entry points (scripts/document_*.py) work
    around this with `os._exit(rc)` after flushing stdio.

    `log_kind` distinguishes log messages between dispatchers
    ("dispatch" vs "flow dispatch"). Kept as a parameter — not
    derived from invoke_fn — so log greps for "flow dispatch
    failed" continue to match.
    """
    # Worker-stamped actual start times, keyed by item_id (not
    # Future identity, because the worker thread doesn't know
    # its own Future). The per-invoke deadline checks against
    # this so tail items queued behind saturated workers don't
    # burn their per_invoke_timeout budget on queue wait alone.
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
    600s = 10 minutes (motivated by a real 17-minute indefinite
    block from an early production run).

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

    Level-gating limitation: the per-level barrier ensures level
    N+1 only STARTS after every level-N future has been drained
    from `pending` (via completion or per-invoke timeout).
    However, `pool.shutdown(wait=False, cancel_futures=True)`
    lets a per-invoke-timed-out worker keep running in its daemon
    thread (Python can't kill threads). If the worker's
    `agent.invoke()` eventually completes — e.g., a hung LLM API
    call that recovers AFTER the timeout fired — it may write a
    file to vault AFTER level N+1 has already started.

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
    will block until all workers complete (re-introducing hang
    exposure).

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

    # File-hash incremental cache (chunk 5.3):
    # 1. Map every node to its owning file via list_nodes.
    # 2. Hash each unique file's current contents.
    # 3. Compare to prior cache; nodes whose files are
    #    unchanged from a previous successful run are
    #    skipped.
    # 4. After dispatch, record fresh hashes for files
    #    whose dispatched nodes all succeeded (per-file
    #    all-or-nothing). Files no longer in the graph
    #    (deleted source) are pruned.
    node_files: dict[str, str] = {
        n["id"]: n["location"]["file_path"]
        for n in list_nodes(graph_id)
    }
    ordered_files: set[str] = {
        node_files[nid] for nid in order if nid in node_files
    }
    prior_cache = load_file_hash_cache(vault_path)
    current_hashes = compute_file_hashes(ordered_files)
    skip_set: set[str] = {
        nid for nid in order
        if (fp := node_files.get(nid)) is not None
        and current_hashes.get(fp) is not None
        and prior_cache.get(fp) == current_hashes[fp]
    }
    skipped: list[dict[str, Any]] = [
        {"node_id": nid, "reason": "file unchanged"}
        for nid in order if nid in skip_set
    ]

    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    record = _make_recorder(
        successes, failures,
        total=len(order) - len(skip_set),
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
    #
    # Skipped nodes are filtered out per-level BEFORE
    # _run_pool. An entire level may become empty if all its
    # nodes are cached — skip the pool call entirely in
    # that case.
    for level in levels:
        pending = [nid for nid in level if nid not in skip_set]
        if not pending:
            continue
        _run_pool(
            pending,
            lambda nid: _invoke_one(agent, graph_id, nid),
            concurrency_cap=concurrency_cap,
            per_invoke_timeout=per_invoke_timeout,
            on_done=record,
            log_kind="dispatch",
        )

    # Per-file all-or-nothing cache update. A file's hash is
    # recorded only if NO node from that file failed during
    # this dispatch. Files no longer in the graph (deleted
    # source) are pruned. Files that were entirely skipped
    # (all their nodes were cache-hits) keep their prior
    # hash (re-recorded from current_hashes, which equals
    # prior_cache for those files by construction).
    failed_files: set[str] = {
        node_files[f["node_id"]]
        for f in failures
        if f["node_id"] in node_files
    }
    new_cache: dict[str, str] = {
        k: v for k, v in prior_cache.items() if k in ordered_files
    }
    for s in successes:
        fp = node_files.get(s["node_id"])
        # `fp in current_hashes` guard: a node CAN succeed
        # even when its file wasn't hashable at dispatch
        # start — e.g., the source file appeared between
        # `compute_file_hashes` and NodeDocumenter's
        # `read_node_source`. Without the guard, the index
        # raises KeyError and aborts the whole cache-update
        # path. The skipped-files loop below has the same
        # guard for the symmetric reason.
        if (
            fp is not None
            and fp not in failed_files
            and fp in current_hashes
        ):
            new_cache[fp] = current_hashes[fp]
    for nid in skip_set:
        fp = node_files.get(nid)
        if fp is not None and fp in current_hashes:
            new_cache[fp] = current_hashes[fp]
    save_file_hash_cache(vault_path, new_cache)

    return {
        "graph_id": graph_id,
        "node_count": len(order),
        "successes": successes,
        "failures": failures,
        "skipped": skipped,
        "order": order,
    }


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
    `dispatch_topo` for the underlying rationale.

    `entrypoint_filter` is an optional callable that scopes the
    dispatch to a subset of the attack surface.
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
        lambda eid: _invoke_one(
            agent, graph_id, eid, _FLOW_TRACE_TEMPLATE
        ),
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


_RISK_SYNTHESIS_TEMPLATE = (
    "Synthesize the three risk notes (hotspots, "
    "delegatecall-sites, reentrancy-candidates) for graph "
    "`{graph_id}` by dispatching the `risk-synthesizer` "
    "subagent ONCE via the `task` tool — do not generate the "
    "risk note content yourself. Return the JSON list of "
    "absolute file paths the subagent wrote."
)


def dispatch_risk_synthesis(
    graph_id: str,
    vault_path: str | Path,
    *,
    model: str = DEFAULT_MODEL,
    per_invoke_timeout: float = DEFAULT_PER_INVOKE_TIMEOUT,
) -> dict[str, Any]:
    """Invoke the risk-synthesizer subagent ONCE.

    PRECONDITION: caller MUST complete `dispatch_topo` and
    `dispatch_flows` BEFORE calling this. RiskSynthesizer
    issues 15-45 `annotate` calls in a single invocation;
    concurrent NodeDocumenter/FlowTracer workers would
    serialize on `_ANNOTATE_LOCK` and stall the dispatch
    pool. See `src/subagents.py:RISK_SYNTHESIZER_SUBAGENT`
    description for the full constraint.

    Also enforced at runtime: a fail-fast precondition check
    verifies `run_preanalysis` has registered the "tainted"
    subgraph on this graph. Without preanalysis subgraphs,
    RiskSynthesizer would silently produce empty/wrong risk
    notes — better to error out concretely.

    Single invocation wrapped in a 1-future ThreadPoolExecutor
    so the orchestrator-level `per_invoke_timeout` catches
    langgraph/deepagents deadlocks where no HTTP request is
    in flight (request_timeout alone is insufficient — same
    defense as dispatch_topo/dispatch_flows; see
    DEFAULT_PER_INVOKE_TIMEOUT comment for the hang scenario).
    Catches all exceptions and reports via the result dict so
    the orchestrator script can degrade gracefully (rc=2,
    advisory) instead of aborting the whole run when risk
    synthesis fails.

    PARTIAL-STATE WARNING: a mid-flight failure (timeout, LLM
    crash, network) can leave the engine in a partial state:
    N of M `annotate` calls already saved to disk
    (atomically), but 0 of 3 risk notes written. Chunk 4.7's
    node-note "Risks" section would then cite finding-
    annotations whose corresponding risk note doesn't exist.
    A rerun is approximately idempotent (annotations
    deduplicate on description equality), but operators
    should check `<vault>/risks/` is populated before
    treating a partial run as canonical.

    Returns:
        {graph_id, ok, reply, error, staging_root}.
        `ok=True` means the agent returned without raising.

        `staging_root` is the absolute path to the per-run
        staging directory the agent's writes were bound to
        (`<vault>/.audit/risk-staging/<run_id>/`), or None
        if dispatch failed before staging was created
        (precondition_failed).

        `reply` is a JSON list of file paths the agent
        wrote into staging (NOT into the final vault). The
        CALLER is responsible for verifying the staged
        inventory against `should_have_notes` and any other
        gates, THEN atomically promoting allowlisted
        filenames into the final `vault/risks/`. On
        verification failure, the caller should `rmtree`
        `staging_root` and leave `vault/risks/` untouched.

        Codex round-10 fix: the dispatcher previously
        promoted on success before the script's trust gate
        ran. A clean-graph reply that wrote all-three notes
        passed `ok=True` and got promoted; only AFTER did
        the script's gate notice the "clean graph, no notes
        expected" violation and wipe. The promotion-to-wipe
        window was exposed to crashes/SIGKILLs/Obsidian
        watchers. Now: the dispatcher returns staging
        metadata only. Final vault/risks/ remains untouched
        until the caller's gate passes.
    """
    _validate_graph_id(graph_id)
    _validate_vault_path(vault_path)
    if per_invoke_timeout <= 0:
        raise ValueError(
            f"per_invoke_timeout must be > 0 "
            f"(got {per_invoke_timeout})"
        )

    # Precondition guard: refuse to run if preanalysis hasn't
    # registered the expected subgraphs. Otherwise the LLM
    # silently synthesizes garbage from empty subgraphs.
    engine = load_graph(graph_id)
    registered = set(engine.subgraph_names())
    if "tainted" not in registered:
        return {
            "graph_id": graph_id,
            "ok": False,
            "reply": "",
            "error": (
                "precondition_failed: run_preanalysis has not "
                "been called for this graph (no 'tainted' "
                "subgraph registered). Risk synthesis depends "
                "on preanalysis subgraphs."
            ),
            # No staging created yet — caller has nothing
            # to clean up.
            "staging_root": None,
        }

    # Idempotency guard: clear prior risk-synthesizer
    # annotations BEFORE invoking the agent. Trailmark's
    # annotate API is append-only; without this, a re-run
    # multiplies the on-disk annotations 1×→2×→3× per run
    # (the render-side `dict.fromkeys` dedup hides this from
    # node-note output but the engine.pkl grows monotonically
    # and any tool counting `len(annotations_of(...))` gets
    # wrong numbers). Mirrors `clear_augmented("sarif")`
    # Trailmark uses internally for augment_sarif.
    # `_RISK_SYNTHESIZER_SOURCE` value is the contract
    # documented in src/subagents.py:_RISK_SYNTHESIZER_PROMPT
    # step 6.
    clear_annotations_by_source(
        graph_id, "risk-synthesizer", kind="finding",
    )

    # Staging-based write isolation (Codex rounds 9 + 10).
    # The agent writes via a closure bound to staging, NOT
    # to the real vault. The dispatcher RETURNS staging
    # metadata; the caller verifies the staged inventory
    # against trust gates (should_have_notes etc.) and
    # promotes only allowlisted files into vault/risks/ on
    # full pass. On failure, the caller `rmtree`s staging
    # and vault/risks/ stays untouched.
    #
    # Round 9 (initial staging) closed the late-write race
    # (timed-out workers reach staging only). Round 10
    # moves promotion to the caller so the trust gate runs
    # BEFORE any file reaches vault/risks/ — closing the
    # promotion-to-wipe exposure window where a crash or
    # Obsidian sync could surface attacker-controlled
    # content between promote and rejection.
    vault_root = Path(vault_path).resolve()
    run_id = secrets.token_hex(8)
    staging_root = (
        vault_root / ".audit" / "risk-staging" / run_id
    )
    (staging_root / "risks").mkdir(parents=True, exist_ok=True)

    agent = build_agent(
        graph_id,
        staging_root,  # staging, NOT real vault
        model=model,
        request_timeout=per_invoke_timeout - _REQUEST_TIMEOUT_BUFFER,
    )
    task_msg = _RISK_SYNTHESIS_TEMPLATE.format(graph_id=graph_id)

    def _invoke() -> str:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": task_msg}]}
        )
        return result["messages"][-1].content

    # Explicit shutdown(wait=False) — matches _run_pool's
    # pattern (src/agent.py:_run_pool). A `with` block would
    # call shutdown(wait=True) on exit and stall on the
    # wedged daemon worker; using try/finally with wait=False
    # lets the function return promptly while the orphan
    # worker dies with the process (daemon thread, killed
    # by os._exit at script end).
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_invoke)
        try:
            # We ignore the LLM's reply content. The caller
            # inventories `staging_root/risks/` directly —
            # that's the only trustworthy source of "what
            # was written".
            future.result(timeout=per_invoke_timeout)
            # SUCCESS: report which files landed in staging.
            # No promotion here; the caller verifies + moves.
            staging_risks = staging_root / "risks"
            staged: list[str] = []
            if staging_risks.is_dir():
                for src in sorted(staging_risks.glob("*.md")):
                    staged.append(str(src))
            return {
                "graph_id": graph_id,
                "ok": True,
                "reply": json.dumps(staged),
                "error": None,
                "staging_root": str(staging_root),
            }
        except concurrent.futures.TimeoutError:
            future.cancel()
            return {
                "graph_id": graph_id,
                "ok": False,
                "reply": "",
                "error": (
                    f"per_invoke_timeout ({per_invoke_timeout}s) "
                    f"exceeded — likely langgraph/deepagents "
                    f"deadlock"
                ),
                "staging_root": str(staging_root),
            }
        except Exception as e:
            return {
                "graph_id": graph_id,
                "ok": False,
                "reply": "",
                "error": str(e),
                "staging_root": str(staging_root),
            }
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        # NOTE: NO staging cleanup here. The caller owns
        # the staging lifecycle (they need to inspect it
        # for verification). They MUST `rmtree`
        # `staging_root` on either success (after
        # promotion) or failure (rejection). If they
        # forget, the leftover lives under
        # `.audit/risk-staging/<run_id>/` and gets wiped
        # at the next process start (document_repo.py
        # wipes the parent on startup).
