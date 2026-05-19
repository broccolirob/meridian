"""Main agent — the washable orchestrator.

Walks a parsed graph's topological order and dispatches NodeDocumenter
per node. Chunk 2.2 is the *skeleton*: the prompt describes the plan,
the tool list is wired up, but dispatch logic doesn't exist yet
(chunk 2.3 owns that).
"""

from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from langchain_openai import ChatOpenAI

from src.graph.topo import topo_order
from src.subagents import NODE_DOCUMENTER_SUBAGENT
from src.tools import graph_summary

DEFAULT_MODEL = "gpt-5-mini"


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

6. For each node in the topological order, dispatch the
   `node-documenter` subagent via the `task` tool. Pass:
     - graph_id (the constant above)
     - node_id (the id from the topo list)
     - vault_path (the constant above)
     - overview_hint (optional, leave empty unless the user provided one)
   Each subagent writes ONE Obsidian note. Cap parallelism to 5.
   [Phase 2 chunk 2.3 — for now this chunk is a skeleton, dispatch
   loop will be wired then.]

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
    `vault_path` should be an absolute path (the harness in chunk 1.4
    pattern: `Path(vault).resolve()`).

    Returns a langgraph `CompiledStateGraph` ready for `.invoke()`.

    NOTE: chunk 2.2 is the *skeleton*. The system prompt describes
    the 9-step plan but no dispatch logic is wired here. Calling
    `.invoke()` on this agent today would produce a planning-only
    response. Real dispatch lands in chunk 2.3.
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
