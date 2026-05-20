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

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from deepagents import create_deep_agent
from langchain_openai import ChatOpenAI

from src.graph.topo import topo_levels, topo_order
from src.subagents import FLOW_TRACER_SUBAGENT, NODE_DOCUMENTER_SUBAGENT
from src.tools import attack_surface, callees_of, graph_summary

_log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-5-mini"
DEFAULT_CONCURRENCY_CAP = 5


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


def build_agent(
    graph_id: str,
    vault_path: str | Path,
    *,
    model: str = DEFAULT_MODEL,
) -> Any:
    """Build the washable main agent.

    `graph_id` is the 12-hex graph identifier from `trailmark_parse`.
    `vault_path` should be an absolute path (callers typically use
    `Path(vault).resolve()` so the LLM sees an unambiguous string).

    Returns a langgraph `CompiledStateGraph` ready for `.invoke()`.
    The agent is wired with BOTH subagents (NodeDocumenter +
    FlowTracer). Each `.invoke()` carries one task — the LLM picks
    which subagent to dispatch based on the task message verb
    ("Document the node …" → NodeDocumenter; "Trace the
    entrypoint …" → FlowTracer). Python drivers (`dispatch_topo`
    and `dispatch_flows`) handle the enumeration loop.
    """
    llm = ChatOpenAI(model=model)
    return create_deep_agent(
        model=llm,
        subagents=[NODE_DOCUMENTER_SUBAGENT, FLOW_TRACER_SUBAGENT],
        system_prompt=_build_system_prompt(graph_id, vault_path),
        tools=[
            graph_summary,
            topo_order,
        ],
    )


def _invoke_one(
    agent: Any, graph_id: str, node_id: str, vault_path: str
) -> dict[str, Any]:
    """Single agent invocation for one node. Exceptions bubble up
    to dispatch_topo, which records them per-node."""
    task_msg = (
        f"Document the node `{node_id}` in graph `{graph_id}`. "
        f"vault_path (absolute) = {vault_path}. "
        f"Dispatch the `node-documenter` subagent via the `task` "
        f"tool — do not generate note content yourself."
    )
    result = agent.invoke(
        {"messages": [{"role": "user", "content": task_msg}]}
    )
    last = result["messages"][-1].content
    return {"node_id": node_id, "agent_reply": last}


def dispatch_topo(
    graph_id: str,
    vault_path: str,
    *,
    model: str = DEFAULT_MODEL,
    concurrency_cap: int = DEFAULT_CONCURRENCY_CAP,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Walk the topological order for `graph_id` and dispatch
    NodeDocumenter per node. Returns a summary of what shipped.

    `vault_path` should be an absolute path. `concurrency_cap` caps
    parallel agent invocations via a ThreadPoolExecutor — the
    lost-update race in `annotate` is handled by the threading.Lock
    in `src/tools.py`.

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

    # One agent shared across all worker threads. Safe because
    # langgraph's CompiledStateGraph keeps no mutable instance state
    # across `.invoke()` calls — the run state lives in the input
    # dict, and the underlying ChatOpenAI client is stateless.
    # Empirically verified at cap=5 on Tier 0 (8 nodes) and Tier 1
    # (22 nodes) without state corruption.
    agent = build_agent(graph_id, vault_path, model=model)
    levels = topo_levels(graph_id)
    order = [nid for level in levels for nid in level]

    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    progress_idx = 0

    def _try_one(node_id: str) -> tuple[str, Any]:
        try:
            return ("ok", _invoke_one(agent, graph_id, node_id, vault_path))
        except Exception as e:
            _log.exception("dispatch failed for %s", node_id)
            return ("fail", f"{type(e).__name__}: {e}")

    # Dispatch one level at a time. Within a level, all nodes are
    # independent (verified by topo_levels); they run in parallel up
    # to concurrency_cap. Across levels, we wait for the level to
    # finish before starting the next — that's what guarantees a
    # derived contract's wikilink targets exist on disk by the time
    # the derived contract's NodeDocumenter runs.
    for level in levels:
        with ThreadPoolExecutor(max_workers=concurrency_cap) as pool:
            futures = {pool.submit(_try_one, nid): nid for nid in level}
            for future in as_completed(futures):
                node_id = futures[future]
                status, info = future.result()
                progress_idx += 1
                if on_progress is not None:
                    on_progress(progress_idx, len(order), node_id)
                if status == "ok":
                    successes.append(info)
                else:
                    failures.append({"node_id": node_id, "error": info})

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
    """Single agent invocation for one entrypoint. Exceptions bubble
    up to dispatch_flows, which records them per-entrypoint."""
    task_msg = (
        f"Trace the entrypoint `{entrypoint_id}` in graph "
        f"`{graph_id}`. vault_path (absolute) = {vault_path}. "
        f"Dispatch the `flow-tracer` subagent via the `task` tool — "
        f"do not generate the flow note content yourself."
    )
    result = agent.invoke(
        {"messages": [{"role": "user", "content": task_msg}]}
    )
    last = result["messages"][-1].content
    return {"node_id": entrypoint_id, "agent_reply": last}


def dispatch_flows(
    graph_id: str,
    vault_path: str,
    *,
    model: str = DEFAULT_MODEL,
    concurrency_cap: int = DEFAULT_CONCURRENCY_CAP,
    on_progress: Callable[[int, int, str], None] | None = None,
    skip_leaf_entrypoints: bool = True,
) -> dict[str, Any]:
    """Enumerate entrypoints via `attack_surface` and dispatch
    FlowTracer per entrypoint. Returns a summary of what shipped.

    `skip_leaf_entrypoints` (default True): filter out entrypoints
    with no outgoing callees (leaf functions like simple getters).
    Tracing them produces an empty-paths placeholder note —
    wasteful at scale.

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

    entrypoints = attack_surface(graph_id)
    if skip_leaf_entrypoints:
        entrypoints = [
            e for e in entrypoints
            if callees_of(graph_id, e["node_id"])
        ]
    order = [e["node_id"] for e in entrypoints]

    agent = build_agent(graph_id, vault_path, model=model)
    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    def _try_one(eid: str) -> tuple[str, Any]:
        try:
            return (
                "ok",
                _invoke_one_flow(agent, graph_id, eid, vault_path),
            )
        except Exception as e:
            _log.exception("flow dispatch failed for %s", eid)
            return ("fail", f"{type(e).__name__}: {e}")

    # Entrypoints are independent — flat parallel pool, no
    # level-gating (unlike dispatch_topo's inheritance-aware walk).
    progress_idx = 0
    with ThreadPoolExecutor(max_workers=concurrency_cap) as pool:
        futures = {pool.submit(_try_one, eid): eid for eid in order}
        for future in as_completed(futures):
            eid = futures[future]
            status, info = future.result()
            progress_idx += 1
            if on_progress is not None:
                on_progress(progress_idx, len(order), eid)
            if status == "ok":
                successes.append(info)
            else:
                failures.append({"node_id": eid, "error": info})

    return {
        "graph_id": graph_id,
        "entrypoint_count": len(order),
        "successes": successes,
        "failures": failures,
        "order": order,
    }
