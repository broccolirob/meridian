# washable — Code Atlas

A deepagent that turns any codebase into an audit-ready Obsidian vault.
Trailmark provides the graph. deepagents provide the narrative and synthesis.
First-class target: Solidity. Designed-in target: any language Trailmark parses
(21 and counting).

## What we're building

A CLI:

```
washable <repo-path-or-url> <vault-path>
```

Output is a **code atlas**: an Obsidian vault with one note per code unit
(contract, function, struct, library), cross-cutting flow narratives, attack
surface MOCs, risk hotspots, and Mermaid diagrams. The vault opens in Obsidian
and reads like a researcher's notebook — wikilinks everywhere, frontmatter for
Dataview queries, diagrams inline.

The graph is the durable artifact. The Obsidian vault is one renderer of it.

## Why deepagents + Trailmark

**Trailmark gives us structure.** Tree-sitter parsing, rustworkx-backed
queryable graph, stable node IDs (`module:Contract.method`), built-in
pre-analysis for taint, blast radius, privilege boundaries, attack surface.
Multi-language. SARIF augmentation. Diff between commits.

**deepagents gives us context isolation.** Real codebases blow single-context
budgets. Each node gets a subagent. A planner walks the graph in dependency
order. A synthesizer reads what the per-node subagents wrote and produces the
cross-cutting notes.

Without Trailmark we'd be reinventing parsing and graph traversal. Without
deepagents we'd run out of context on the first mid-sized repo.

## Test targets

Three tiers. Climb the ladder as the pipeline gets stronger.

| Tier | Target | Files | Why |
| --- | --- | --- | --- |
| 0 (smoke) | `solmate/ERC4626.sol` | 1 | One file. Validates the single-node render path. |
| 1 (real) | Uniswap V2 core (`Factory`, `Pair`, `ERC20`) | 3 | Canonical small DeFi. Cross-contract calls, inheritance, callbacks. |
| 2 (stretch) | Morpho Blue or Compound V2 cToken set | 10-15 | Real-world complexity, modifier-heavy access control, oracle deps. |

Phase 1-2 build against Tier 0-1. Phase 3+ validate against Tier 2.

## Trail of Bits skills — incorporation policy

We use the `trailmark` Python package as a hard runtime dependency. We do
**not** depend on the Claude Code skills at runtime — instead we extract
their prompts, algorithms, and patterns into our own code as we need each
capability. The skill files at
`~/.claude/plugins/cache/trailofbits/trailmark/0.8.1/skills/<name>/SKILL.md`
are reference material we transcribe from.

| Skill | When we incorporate | What we extract |
| --- | --- | --- |
| `trailmark` | Phase 0 | Core parse/query patterns, language detection. |
| `trailmark-summary` | Phase 0 | Manifest shape — language list, entry-point count, deps. |
| `trailmark-structural` | Phase 4 | Pre-analysis invocation, hotspot/taint/blast-radius rendering. |
| `audit-augmentation` | Phase 4 | SARIF ingestion patterns for slither + semgrep output. |
| `diagramming-code` | Phase 3 | Mermaid emitters for call graphs, class hierarchies, containment. |
| `graph-evolution` | Phase 5 | Snapshot diff structure for "what changed between commits" notes. |

Skills we ignore (out of scope for the atlas): `genotoxic`, `vector-forge`,
`crypto-protocol-diagram`, `mermaid-to-proverif`.

## Vault shape

```
vault/
├── README.md                    # MOC root, links to everything
├── _meta/
│   ├── glossary.md              # ERC standards, DeFi terms, language-specific idioms
│   └── tags.md
├── contracts/                   # one note per contract (Solidity)
│   ├── UniswapV2Factory.md
│   ├── UniswapV2Pair.md
│   └── UniswapV2ERC20.md
├── interfaces/                  # interfaces split out for clarity
├── libraries/
├── flows/                       # cross-contract narratives, Mermaid sequence diagrams
│   ├── swap.md
│   ├── mint.md
│   └── burn.md
├── diagrams/
│   ├── inheritance.md
│   ├── call-graph.md
│   └── containment.md
├── attack-surface/              # MOC for entrypoints, per-entrypoint notes
│   ├── README.md
│   └── swap-entrypoint.md
└── risks/
    ├── hotspots.md
    ├── delegatecall-sites.md
    └── reentrancy-candidates.md
```

For languages other than Solidity the folder layout is generic: `modules/`,
`classes/`, `functions/`, plus the same cross-cutting folders.

### Note frontmatter (Dataview-queryable)

```yaml
---
type: contract               # contract | function | library | interface | struct | module | class
name: UniswapV2Pair
node_id: contracts/UniswapV2Pair.sol:UniswapV2Pair
file: contracts/UniswapV2Pair.sol
language: solidity
kind: contract
loc: 312
inherits: [[UniswapV2ERC20]]
implements: [[IUniswapV2Pair]]
uses: [[Math]], [[UQ112x112]], [[SafeMath]]
tags: [defi, amm, payable]
risk: medium
cyclomatic_max: 14
external_functions: 7
annotations:
  assumptions: 3
  findings: 1
---
```

### Body sections (consistent across notes)

1. **Overview** — 2-3 sentences from the LLM
2. **Graph context** — inheritance + containment Mermaid, callers/callees lists
3. **State** — table of state variables, types, mutability
4. **Functions** — grouped by visibility, each with signature, NatSpec/docstring, callers, callees, complexity
5. **Events / Errors / Modifiers**
6. **Annotations** — LLM-written assumptions, invariants, findings (pulled back from the graph)
7. **Risks** — embedded from `risks/`

## Architecture

```
┌───────────────────────────────────────────────────────────────┐
│  Main agent (planner + synthesizer)                           │
│  - parse → preanalysis → augment → topo-walk → dispatch       │
│  - synthesis pass at the end                                  │
└───────────────────────────────────────────────────────────────┘
        │                  │                       │
        ▼                  ▼                       ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│ NodeDocumenter   │ │ FlowTracer       │ │ RiskSynthesizer  │
│ subagent         │ │ subagent         │ │ subagent         │
│ - one node →     │ │ - entrypoint →   │ │ - SARIF +        │
│   one note       │ │   sink trace +   │ │   preanalysis +  │
│                  │ │   sequence diag  │ │   annotations    │
└──────────────────┘ └──────────────────┘ └──────────────────┘
        │                  │                       │
        ▼                  ▼                       ▼
┌───────────────────────────────────────────────────────────────┐
│  Tools (deterministic, NOT LLM)                               │
│  Trailmark wrappers + Obsidian writers + slither/semgrep      │
└───────────────────────────────────────────────────────────────┘
```

## Tools layer (`src/tools.py`)

```python
# --- Trailmark wrappers ---
def trailmark_parse(repo_path: str, language: str = "auto") -> str
    # parse + persist QueryEngine to disk under .washable/graph/, return graph_id

def graph_summary(graph_id: str) -> dict
def list_nodes(graph_id: str, kind: str | None = None) -> list[dict]
def get_node(graph_id: str, node_id: str) -> dict
def callers_of(graph_id: str, node_id: str) -> list[dict]
def callees_of(graph_id: str, node_id: str) -> list[dict]
def ancestors_of(graph_id: str, node_id: str) -> list[dict]
def reachable_from(graph_id: str, node_id: str) -> list[dict]
def paths_between(graph_id: str, src: str, dst: str) -> list[list[str]]
def entrypoint_paths_to(graph_id: str, node_id: str) -> list[list[str]]
# Note: neighbor/reachability queries return full Trailmark node dicts
# (not just IDs) so subagents don't have to re-call get_node per hop.
# Path queries stay as list[str] — paths are usually walked, not inspected.
def attack_surface(graph_id: str) -> list[dict]
def complexity_hotspots(graph_id: str, threshold: int = 10) -> list[dict]

# --- Annotations (agent writes findings back to the graph) ---
def annotate(graph_id: str, node_id: str, kind: str, description: str) -> None
def annotations_of(graph_id: str, node_id: str) -> list[dict]
def nodes_with_annotation(graph_id: str, kind: str) -> list[str]

# --- Pre-analysis + augmentation ---
def run_preanalysis(graph_id: str) -> dict       # taint, blast radius, attack surface
def augment_sarif(graph_id: str, sarif_path: str) -> None
def diff_graphs(before_id: str, after_id: str) -> dict

# --- External analyzers (Phase 4) ---
def run_slither(repo_path: str, out_sarif: str) -> str   # Solidity → SARIF
def run_semgrep(repo_path: str, rules: str, out_sarif: str) -> str  # multi-lang → SARIF

# --- Obsidian writers ---
def ensure_vault(vault_path: str) -> None
def write_obsidian_note(vault_rel_path: str, frontmatter: dict, body: str) -> str
def append_to_moc(moc_rel_path: str, entry: str) -> None
def resolve_wikilink(graph_id: str, node_id: str) -> str   # deterministic via node_id
def render_mermaid_call_graph(graph_id: str, node_id: str, depth: int = 2) -> str
def render_mermaid_inheritance(graph_id: str, node_id: str) -> str
def render_mermaid_sequence(graph_id: str, path: list[str]) -> str
```

The agent never sees raw source unless it explicitly reads a node. Everything
structural goes through Trailmark.

## Subagents (`src/subagents.py`)

### NodeDocumenter

Input: `graph_id`, `node_id`, plus a manifest of already-documented dependencies.
Tools: read-only Trailmark queries + `read_file_range` + `write_obsidian_note` +
`annotate` + `resolve_wikilink`.
Output: one Obsidian note. Side effect: may add `assumption`/`invariant`
annotations to the graph.

### FlowTracer

Input: `graph_id`, an entrypoint `node_id` from `attack_surface()`.
Tools: `entrypoint_paths_to`, `paths_between`, `get_node`, `render_mermaid_sequence`,
`write_obsidian_note`.
Output: one `flows/<entrypoint>.md` note narrating the path, embedding the
sequence diagram, linking each hop to its NodeDocumenter note.

### RiskSynthesizer

Input: `graph_id` after preanalysis + SARIF augmentation.
Tools: `run_preanalysis`, `nodes_with_annotation`, `complexity_hotspots`,
`write_obsidian_note`, `annotate`.
Output: notes under `risks/` plus back-annotations on the graph
(`AnnotationKind.FINDING`) so individual node notes can embed their findings on
next render.

## Main agent (`src/agent.py`)

Built with `deepagents.create_deep_agent`. Instructions in plain English:

1. Parse the repo with Trailmark. Persist the graph.
2. Run `graph_summary` and `attack_surface` to get a sense of scale.
3. Run `run_preanalysis` for the security subgraphs.
4. If Solidity is present, run slither and `augment_sarif`. (Phase 4+)
5. Compute a topological order over the node graph (leaves first).
6. For each node, dispatch `NodeDocumenter`. Cap parallelism.
7. For each entrypoint from `attack_surface`, dispatch `FlowTracer`.
8. Dispatch `RiskSynthesizer` once over the augmented graph.
9. Write the root `README.md` MOC with links into every section.

## Phased build plan

### Phase 0 — Foundations

- Add `trailmark`, `slither-analyzer` (optional in Phase 4) to `pyproject.toml`.
- Build `trailmark_parse`, `graph_summary`, `list_nodes`, `get_node`,
  `callers_of`, `callees_of`.
- Persist `QueryEngine` to disk under `.washable/graph/<repo-hash>/`.
- Build `ensure_vault`, `write_obsidian_note` with frontmatter + body
  templating.
- Smoke test: parse `solmate/ERC4626.sol`, dump a JSON manifest of nodes.

### Phase 1 — Single-node MVP

- Build `NodeDocumenter` subagent.
- Hardcode it to document one contract from Tier 0.
- Goal: open the produced note in Obsidian and have it feel right.
- This is the demo. Polish it before scaling.

### Phase 2 — Main agent + topo walk

- Build the planner: topological sort of nodes by dependency edges
  (`inherits`, `uses`, `imports` first; `calls` last).
- Dispatch `NodeDocumenter` per node in order.
- Run end-to-end on Tier 1 (Uniswap V2 core).
- Vault should have `contracts/UniswapV2Factory.md`,
  `contracts/UniswapV2Pair.md`, `contracts/UniswapV2ERC20.md` plus a root MOC.

### Phase 3 — Flow notes + Mermaid

- Build `FlowTracer` subagent + `render_mermaid_sequence`,
  `render_mermaid_call_graph`, `render_mermaid_inheritance`.
- Incorporate `diagramming-code` skill patterns into our renderers.
- Run on Tier 1 entrypoints (`swap`, `mint`, `burn`).
- Now the vault feels like a notebook, not just docs.

### Phase 4 — Risk pass + SARIF

- Wire `run_slither` → SARIF → `augment_sarif`.
- Incorporate `trailmark-structural` patterns into a `run_preanalysis` wrapper.
- Build `RiskSynthesizer`.
- Validate on Tier 1, then graduate to Tier 2.

### Phase 5 — Diff mode + polish

- Build `diff_graphs` wrapper. Incorporate `graph-evolution` patterns.
- Add `washable diff <vault> <new-commit>` to render attack-surface deltas.
- CLI polish via `uv run washable` / `uvx washable`.
- Incremental: hash files, skip unchanged node subgraphs.
- Pretty Obsidian Canvas file for the inheritance + call graph?

## Open questions to revisit

1. **Model split.** `pyproject.toml` currently pins `langchain-openai`. For
   Solidity reasoning, GPT-5-class for synthesis main agent, smaller for
   per-node subagents. Or swap in `langchain-anthropic` for Claude
   Sonnet + Opus. Decide before Phase 1.
2. **Concurrency.** How many `NodeDocumenter` subagents in flight? Per-node
   token cost × node count vs. wall-clock. Default cap at 5, tune from there.
3. **Vault overwrites.** Re-running on the same repo: clobber or merge?
   Initial behaviour: clobber the vault but preserve any `*.md` files
   prefixed `notes-` (user-authored, sacred).
4. **Annotation persistence.** Trailmark annotations are stored in the graph,
   not the vault. On re-parse, do we replay annotations from the prior graph,
   or drop them? Decide before Phase 4.

## Repository layout (target)

```
washable/
├── PLAN.md                     # this file
├── README.md
├── pyproject.toml
├── main.py                     # CLI entry
├── src/
│   ├── agent.py                # main agent (create_deep_agent)
│   ├── subagents.py            # NodeDocumenter, FlowTracer, RiskSynthesizer
│   ├── tools.py                # Trailmark wrappers + Obsidian writers + analyzers
│   ├── render/
│   │   ├── obsidian.py         # frontmatter + body templates
│   │   ├── mermaid.py          # diagram emitters (from diagramming-code)
│   │   └── moc.py              # map-of-content generation
│   ├── analyzers/
│   │   ├── slither.py          # SARIF runner
│   │   └── semgrep.py
│   └── graph/
│       ├── persist.py          # QueryEngine save/load
│       └── topo.py             # topological ordering for node dispatch
└── tests/
    └── fixtures/
        ├── tier0_erc4626/
        └── tier1_uniswap_v2/
```
