"""Main agent — the washable orchestrator.

Walks a parsed graph's topological order and dispatches NodeDocumenter
per node.

Two public entry points:

- `build_agent(graph_id, vault_path)` constructs a deepagents agent
  configured for single-node dispatch (the LLM receives one node per
  user message and forwards to NodeDocumenter).

- `dispatch_topo(graph_id, vault_path, ...)` is the Python-driven
  loop: it builds the agent once, walks `topo_order`, and invokes
  the agent per node through a ThreadPoolExecutor (default cap = 5).
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from deepagents import create_deep_agent
from langchain_openai import ChatOpenAI

from src.graph.topo import topo_order
from src.subagents import NODE_DOCUMENTER_SUBAGENT
from src.tools import graph_summary

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

6. You'll be invoked once per node by the Python dispatch loop in
   `dispatch_topo`. Each invocation: receive a node_id in the user
   message, dispatch the `node-documenter` subagent via the `task`
   tool (passing graph_id, node_id, vault_path), and return the
   absolute path the subagent wrote. The Python driver walks the
   topo list; you do NOT loop. Do not invent extra dispatches.

7. For each entrypoint returned by `attack_surface(graph_id)`,
   dispatch the `flow-tracer` subagent. [Phase 3 — skip for now.]

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
    The agent is configured for single-node dispatch: each invocation
    receives one node in the user message and forwards to the
    node-documenter subagent. The Python driver in `dispatch_topo`
    handles the walk across the topological order.
    """
    llm = ChatOpenAI(model=model)
    return create_deep_agent(
        model=llm,
        subagents=[NODE_DOCUMENTER_SUBAGENT],
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
    order = topo_order(graph_id)

    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    def _try_one(node_id: str) -> tuple[str, Any]:
        try:
            return ("ok", _invoke_one(agent, graph_id, node_id, vault_path))
        except Exception as e:
            _log.exception("dispatch failed for %s", node_id)
            return ("fail", f"{type(e).__name__}: {e}")

    with ThreadPoolExecutor(max_workers=concurrency_cap) as pool:
        futures = {pool.submit(_try_one, nid): nid for nid in order}
        for i, future in enumerate(as_completed(futures), 1):
            node_id = futures[future]
            status, info = future.result()
            if on_progress is not None:
                on_progress(i, len(order), node_id)
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
