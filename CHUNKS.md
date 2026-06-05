# meridian — Build Chunks

Implementation chunks derived from `PLAN.md`. Each chunk is scoped to fit in
one focused Claude session without context-overflow or hallucination risk:
single API surface, ≤ 3 files touched (with rare exceptions), explicit
success criteria, named dependencies on prior chunks.

Chunk IDs are `<phase>.<n>`. A downstream chunk may only start once every ID
listed under `Deps` has been completed.

Conventions used by every chunk:
- **Deliverable** — the concrete artifact produced.
- **Files** — the files created or modified.
- **Success** — how to verify the chunk works. Must be checkable without
  reading the source.
- **Deps** — prerequisite chunks.

---

## Phase 0 — Foundations

### 0.1 — Pin dependencies

- **Deliverable:** `pyproject.toml` updated with `trailmark`,
  `langchain-anthropic` (or confirm `langchain-openai`), and dev tools.
- **Files:** `pyproject.toml`, `uv.lock`.
- **Success:** `uv sync` succeeds; `uv run trailmark --version` prints a
  version ≥ 0.3.1.
- **Deps:** none.
- **Note:** `0.8.1` (used in an earlier draft) was the Claude Code plugin
  bundle version. The `trailmark` Python package on PyPI versions
  independently and is at `0.3.1`+ as of 2026-05.

### 0.2 — Test fixtures

- **Deliverable:** `tests/fixtures/tier0_erc4626/ERC4626.sol` (solmate copy)
  and `tests/fixtures/tier1_uniswap_v2/` with `Factory.sol`, `Pair.sol`,
  `ERC20.sol`.
- **Files:** `tests/fixtures/**`, plus a short `tests/fixtures/README.md`
  noting the upstream source + license for each file.
- **Success:** `uv run trailmark analyze --language solidity tests/fixtures/tier1_uniswap_v2` returns non-empty JSON.
- **Deps:** 0.1.

### 0.3 — Graph persistence

- **Deliverable:** `src/graph/persist.py` with `save_graph(query_engine,
  repo_hash)` and `load_graph(repo_hash)` that round-trip a `QueryEngine`
  via the CLI JSON dump (or pickle if the API permits).
- **Files:** `src/graph/__init__.py`, `src/graph/persist.py`,
  `tests/test_persist.py`.
- **Success:** Round-trip test on Tier 0 graph passes — `summary()` of
  loaded graph equals `summary()` of original.
- **Deps:** 0.1, 0.2.

### 0.4 — Trailmark core query tools

- **Deliverable:** `trailmark_parse`, `graph_summary`, `list_nodes`,
  `get_node` in `src/tools.py`.
- **Files:** `src/tools.py`, `tests/test_tools_core.py`.
- **Success:** Tests parse Tier 0, list all nodes, fetch one by ID, return
  expected fields (name, kind, file, location).
- **Deps:** 0.3.

### 0.5 — Trailmark traversal tools

- **Deliverable:** `callers_of`, `callees_of`, `ancestors_of`,
  `reachable_from`, `paths_between` in `src/tools.py`.
- **Files:** `src/tools.py`, `tests/test_tools_traversal.py`.
- **Success:** Tests verify caller/callee sets on Tier 1 against
  hand-computed expected sets for `UniswapV2Pair.swap` and
  `UniswapV2Pair._update`.
- **Deps:** 0.4.

### 0.6 — Obsidian vault skeleton + writer

- **Deliverable:** `ensure_vault(vault_path)` creates the directory tree
  from `PLAN.md`; `write_obsidian_note(rel_path, frontmatter, body)` writes
  with frontmatter rendered as YAML.
- **Files:** `src/render/__init__.py`, `src/render/obsidian.py`,
  `tests/test_obsidian_writer.py`.
- **Success:** Test writes a note with a multi-line body and verifies the
  produced file parses back to the same frontmatter dict.
- **Deps:** 0.1.

### 0.7 — Wikilink resolver

- **Deliverable:** `resolve_wikilink(graph_id, node_id) -> str` returning
  `[[contracts/UniswapV2Pair|UniswapV2Pair.swap]]`-style links based purely
  on node IDs. Stable under repeated calls.
- **Files:** `src/render/obsidian.py`, `tests/test_wikilink.py`.
- **Success:** Resolver returns identical strings on repeat runs; covers
  all `NodeKind` values from Trailmark.
- **Deps:** 0.4, 0.6.
- **Known limitation (deferred):** Wikilink paths use `node["name"]`, so
  two contracts/libraries with the same simple name in different modules
  (e.g. two `ERC20`s) collide on the same `.md` path. Tier 0/1 fixtures
  don't exercise this. Fix when a real fixture or user repo triggers a
  collision — likely needs a graph-wide uniqueness scan + qualified
  fallback. Tracked here so it doesn't get lost.

### 0.8 — End-to-end smoke script

- **Deliverable:** `scripts/smoke_parse.py` that parses Tier 0, dumps a
  JSON manifest of `{node_id, kind, name, file, callers_count,
  callees_count}` for every node.
- **Files:** `scripts/smoke_parse.py`.
- **Success:** Running `uv run python scripts/smoke_parse.py` against
  Tier 0 prints a non-empty manifest with every expected ERC4626 function.
- **Deps:** 0.4, 0.5.

---

## Phase 1 — Single-node MVP

### 1.1 — Note template

- **Deliverable:** `src/render/obsidian.py::render_node_note(node, graph_ctx,
  body)` that renders the canonical body structure from `PLAN.md`
  (Overview → Graph context → State → Functions → Events → Annotations →
  Risks) with empty sections as placeholders.
- **Files:** `src/render/obsidian.py`, `tests/test_render_template.py`.
- **Success:** Snapshot test: rendering a fixture node produces a
  deterministic byte-equal note across runs.
- **Deps:** 0.6.

### 1.2 — Annotation tools

- **Deliverable:** `annotate`, `annotations_of`, `nodes_with_annotation`,
  `clear_annotations` wrappers in `src/tools.py` over Trailmark's
  annotation API.
- **Files:** `src/tools.py`, `tests/test_annotations.py`.
- **Success:** Test adds an `ASSUMPTION` annotation to a Tier 0 node,
  reads it back, lists it via `nodes_with_annotation`.
- **Deps:** 0.4.

### 1.3 — NodeDocumenter subagent definition

- **Deliverable:** Subagent dict in `src/subagents.py` with name,
  description, instruction prompt (referencing graph queries it should run
  and the body sections it must produce), and tool allowlist.
- **Files:** `src/subagents.py`.
- **Success:** Module loads, exports `NODE_DOCUMENTER_SUBAGENT`; tool list
  references only symbols that exist in `src/tools.py`.
- **Deps:** 0.4, 0.5, 1.1, 1.2.

### 1.4 — Single-node harness

- **Deliverable:** `scripts/document_one_node.py <node_id>` that loads
  the Tier 0 graph, constructs the agent with only `NodeDocumenter`,
  runs it against the given node, writes the note.
- **Files:** `scripts/document_one_node.py`.
- **Success:** Manual review of the produced note for the ERC4626 `deposit`
  function: passes Obsidian render, frontmatter parses, body sections
  populated.
- **Deps:** 1.3.

### 1.5 — Note quality pass

- **Deliverable:** Iterated prompt + template adjustments until the
  single-node output for `ERC4626.deposit` and one library function looks
  publication-ready in Obsidian.
- **Files:** `src/subagents.py`, `src/render/obsidian.py`.
- **Success:** Two reference notes committed to `tests/golden/` as goldens
  for future regression checks.
- **Deps:** 1.4.

---

## Phase 2 — Main agent + topo walk

### 2.1 — Topological ordering

- **Deliverable:** `src/graph/topo.py::topo_order(graph_id) -> list[str]`
  that orders nodes by `inherits` → `uses`/`imports` → `contains`,
  treating `calls` as soft (not part of ordering).
- **Files:** `src/graph/topo.py`, `tests/test_topo.py`.
- **Success:** On Tier 1, `UniswapV2ERC20` precedes `UniswapV2Pair`;
  test asserts this and the absence of cycles.
- **Deps:** 0.5.

### 2.2 — Main agent skeleton

- **Deliverable:** `src/agent.py::build_agent(graph_id, vault_path)`
  returning a `deepagents.create_deep_agent`-built agent wired to the
  tools and a static instruction prompt that follows steps 1-9 from
  `PLAN.md`. No dispatch logic yet — the prompt just describes the plan.
- **Files:** `src/agent.py`, `tests/test_agent_build.py`.
- **Success:** Agent constructs without error, tool list is non-empty,
  instructions contain the literal phrase "topological order".
- **Deps:** 0.4, 0.6, 1.3.

### 2.3 — Dispatch loop

- **Deliverable:** Update `build_agent` so the main agent walks
  `topo_order` and dispatches `NodeDocumenter` per node. Includes a
  concurrency cap constant (default 5).
- **Files:** `src/agent.py`.
- **Success:** Running the agent on Tier 0 writes one note per top-level
  node in `vault/contracts/` or appropriate folder.
- **Deps:** 2.1, 2.2.

### 2.4 — Map-of-content generator

- **Deliverable:** `src/render/moc.py::write_root_moc(vault_path,
  graph_id)` that writes `vault/README.md` listing every section with
  per-section node counts and links into MOC sub-pages.
- **Files:** `src/render/moc.py`, `tests/test_moc.py`.
- **Success:** Generated `vault/README.md` contains a wikilink to every
  populated folder MOC; opening it in Obsidian shows no broken links.
- **Deps:** 0.7, 2.3.

### 2.5 — Tier 1 end-to-end

- **Deliverable:** Runbook + saved transcript in `tests/integration/uniswap_v2/`
  proving the agent produces a complete vault for Uniswap V2 core.
- **Files:** `tests/integration/uniswap_v2/run.sh`,
  `tests/integration/uniswap_v2/expected_files.txt`.
- **Success:** `run.sh` produces a vault with at least three contract
  notes, frontmatter parses on each, and `expected_files.txt` is satisfied.
- **Deps:** 2.3, 2.4.

### 2.6 — Wikilink validation pass

- **Deliverable:** `scripts/validate_vault.py <vault>` that scans every
  `.md` for `[[…]]` references and checks each target exists. Optional
  `--fix` removes orphans.
- **Files:** `scripts/validate_vault.py`.
- **Success:** Running on the Tier 1 vault reports zero broken links.
- **Deps:** 2.5.

---

## Phase 3 — Flow notes + Mermaid

### 3.1 — Mermaid call-graph renderer

- **Deliverable:** `src/render/mermaid.py::render_call_graph(graph_id,
  node_id, depth=2) -> str` producing a `graph TD` block with callers
  upstream and callees downstream, depth-limited.
- **Files:** `src/render/mermaid.py`, `tests/test_mermaid_callgraph.py`.
- **Success:** Snapshot test — running on `UniswapV2Pair.swap` produces a
  diagram containing `swap`, `_update`, and `safeTransfer`.
- **Deps:** 0.5.

### 3.2 — Mermaid inheritance renderer

- **Deliverable:** `render_inheritance(graph_id, node_id) -> str` producing
  a `classDiagram` with inherits / implements edges for one contract.
- **Files:** `src/render/mermaid.py`, `tests/test_mermaid_inheritance.py`.
- **Success:** On `UniswapV2Pair`, output includes `UniswapV2ERC20 <|--
  UniswapV2Pair`.
- **Deps:** 0.5.

### 3.3 — Mermaid sequence renderer

- **Deliverable:** `render_sequence(graph_id, path: list[str]) -> str`
  producing a `sequenceDiagram` where each participant is the contract
  containing each hop and arrows are call edges along the path.
- **Files:** `src/render/mermaid.py`, `tests/test_mermaid_sequence.py`.
- **Success:** Renders a known `swap` path with three or more participants
  and ordered arrows.
- **Deps:** 0.5.

### 3.4 — Incorporate diagramming-code skill patterns

- **Deliverable:** Re-read
  `~/.claude/plugins/cache/trailofbits/trailmark/0.8.1/skills/diagramming-code/SKILL.md`
  and port any node-styling, complexity-heatmap, or containment patterns
  into `src/render/mermaid.py` that we don't already have.
- **Files:** `src/render/mermaid.py`, `src/render/mermaid_styles.py`.
- **Success:** Heatmap renderer outputs a Mermaid graph colored by
  cyclomatic complexity buckets; tested against Tier 1.
- **Deps:** 3.1, 3.2, 3.3.

### 3.5 — Embed diagrams in node notes

- **Deliverable:** Update `NodeDocumenter` prompt + `render_node_note`
  template to embed a call-graph and inheritance diagram inside each
  contract note's "Graph context" section.
- **Files:** `src/subagents.py`, `src/render/obsidian.py`.
- **Success:** Re-running Tier 1 produces contract notes containing both
  diagrams; goldens updated.
- **Deps:** 1.5, 3.1, 3.2.

### 3.6 — Attack-surface tool wrappers

- **Deliverable:** `attack_surface(graph_id)`,
  `entrypoint_paths_to(graph_id, node_id)`,
  `complexity_hotspots(graph_id, threshold)` in `src/tools.py`.
- **Files:** `src/tools.py`, `tests/test_tools_surface.py`.
- **Success:** On Tier 1, `attack_surface()` returns `swap`, `mint`,
  `burn` (all `external`/`public`).
- **Deps:** 0.5.

### 3.7 — FlowTracer subagent

- **Deliverable:** Subagent dict in `src/subagents.py` with prompt
  instructing the agent to call `entrypoint_paths_to`, narrate the path,
  embed a sequence diagram, and write to `flows/<entrypoint>.md`.
- **Files:** `src/subagents.py`, `tests/test_flowtracer_build.py`.
- **Success:** Subagent loads; tool allowlist references only real symbols.
- **Deps:** 3.3, 3.6.

### 3.8 — Main agent dispatches FlowTracer

- **Deliverable:** Update `build_agent` to enumerate entrypoints via
  `attack_surface` and dispatch `FlowTracer` per entrypoint after the
  `NodeDocumenter` pass.
- **Files:** `src/agent.py`.
- **Success:** Re-running Tier 1 produces `vault/flows/swap.md`,
  `vault/flows/mint.md`, `vault/flows/burn.md`.
- **Deps:** 2.3, 3.7.

### 3.9 — Tier 1 flow notes review

- **Deliverable:** Manual review + golden snapshot of one flow note.
- **Files:** `tests/golden/flows/swap.md`.
- **Success:** Note describes the swap path correctly, sequence diagram
  renders in Obsidian, every hop wikilinks to its contract note.
- **Deps:** 3.8.

---

## Phase 4 — Risk pass + SARIF

### 4.1 — Slither runner

- **Deliverable:** `src/analyzers/slither.py::run_slither(repo_path,
  out_sarif)` shelling out to `slither <repo> --sarif <out>`.
- **Files:** `src/analyzers/__init__.py`, `src/analyzers/slither.py`,
  `tests/test_slither.py`.
- **Success:** Running on Tier 1 produces a valid SARIF file with at
  least one finding.
- **Deps:** 0.1 (slither-analyzer dep added here).

### 4.2 — SARIF augmentation wrapper

- **Deliverable:** `augment_sarif(graph_id, sarif_path)` tool that calls
  Trailmark's `augment_sarif` and persists.
- **Files:** `src/tools.py`, `tests/test_augment_sarif.py`.
- **Success:** After augmentation, `findings()` on the graph returns at
  least one node tagged with a finding annotation.
- **Deps:** 0.4, 4.1.

### 4.3 — Pre-analysis wrapper

- **Deliverable:** `run_preanalysis(graph_id) -> dict` calling
  `QueryEngine.preanalysis()` and returning subgraph names and node
  counts. Incorporate patterns from `trailmark-structural/SKILL.md`.
- **Files:** `src/tools.py`, `tests/test_preanalysis.py`.
- **Success:** Returns subgraphs including `attack_surface`, `tainted`,
  `blast_radius`, `privilege_boundaries` for Tier 1.
- **Deps:** 0.4.

### 4.4 — Incorporate audit-augmentation skill patterns

- **Deliverable:** Port mapping logic from
  `~/.claude/plugins/cache/trailofbits/trailmark/0.8.1/skills/audit-augmentation/SKILL.md`
  into `src/analyzers/sarif_map.py` so findings without explicit node IDs
  still attach to graph nodes by file+line overlap.
- **Files:** `src/analyzers/sarif_map.py`,
  `tests/test_sarif_mapping.py`.
- **Success:** A SARIF finding pointing at a line inside `swap` attaches
  to the `swap` node annotation.
- **Deps:** 4.2.

### 4.5 — RiskSynthesizer subagent

- **Deliverable:** Subagent dict producing notes under `vault/risks/` —
  `hotspots.md`, `delegatecall-sites.md`, `reentrancy-candidates.md` —
  from `run_preanalysis` + `findings` + `complexity_hotspots`. Adds
  back-annotations on involved nodes.
- **Files:** `src/subagents.py`, `tests/test_risksynth_build.py`.
- **Success:** Subagent loads; tool allowlist references real symbols.
- **Deps:** 4.2, 4.3.

### 4.6 — Main agent dispatches RiskSynthesizer

- **Deliverable:** Update `build_agent` to run slither → SARIF →
  augment → preanalysis → RiskSynthesizer before final synthesis.
- **Files:** `src/agent.py`.
- **Success:** Re-run Tier 1 produces populated `vault/risks/` folder
  with at least `hotspots.md`.
- **Deps:** 4.1, 4.5.

### 4.7 — Re-render notes with embedded findings

- **Deliverable:** Update `render_node_note` to include a "Risks" section
  populated from each node's finding annotations.
- **Files:** `src/render/obsidian.py`.
- **Success:** A Tier 1 contract note with a slither finding shows the
  finding inline; goldens updated.
- **Deps:** 4.6.

### 4.8 — Semgrep runner (optional, multi-language)

- **Deliverable:** `src/analyzers/semgrep.py::run_semgrep(repo, rules,
  out_sarif)` for any Trailmark-supported language.
- **Files:** `src/analyzers/semgrep.py`,
  `tests/test_semgrep.py`.
- **Success:** Runs on a small Python fixture and produces SARIF that
  `augment_sarif` ingests.
- **Deps:** 4.2.

---

## Phase 5 — Diff mode + polish

### 5.1 — Graph diff tool

- **Deliverable:** `diff_graphs(before_id, after_id) -> dict` wrapping
  `QueryEngine.diff_against`. Incorporate `graph-evolution/SKILL.md`
  patterns for surfacing attack-surface deltas.
- **Files:** `src/tools.py`, `tests/test_diff.py`.
- **Success:** Diffing two intentionally-different Tier 1 snapshots
  returns expected added/removed nodes and edges.
- **Deps:** 0.4.

### 5.2 — CLI subcommand: diff

- **Deliverable:** `meridian diff <vault> <new-commit>` parses both
  states and writes `vault/diffs/<short-sha>.md`.
- **Files:** `main.py` (CLI entry), `src/render/diff_md.py`.
- **Success:** Running against two Tier 1 snapshots produces a diff
  note listing changed entrypoints.
- **Deps:** 5.1.

### 5.3 — File-hash incremental cache

- **Deliverable:** `.meridian/cache/files.json` mapping file path →
  content hash. Subagent dispatch skips nodes whose owning files'
  hashes are unchanged from the last successful run.
- **Files:** `src/graph/cache.py`, `src/agent.py`,
  `tests/test_cache.py`.
- **Success:** Two consecutive runs on unchanged Tier 1 — first
  dispatches subagents, second dispatches zero.
- **Deps:** 2.3.

### 5.4 — CLI polish

- **Deliverable:** `main.py` exposes `meridian parse`, `meridian diff`,
  `meridian validate`, plus `--version`, `--help`, and a `--vault-path`
  flag. Pyproject `[project.scripts]` wires `meridian = "main:cli"`.
- **Files:** `main.py`, `pyproject.toml`.
- **Success:** `uv run meridian --help` prints all subcommands; `uvx
  --from . meridian --version` prints the project version.
- **Deps:** 5.2.

### 5.5 — README + quickstart

- **Deliverable:** `README.md` with one-paragraph pitch, quickstart
  (`uvx meridian <repo> <vault>`), and a screenshot of an Obsidian note.
- **Files:** `README.md`, `docs/screenshot-note.png`.
- **Success:** A reader following the quickstart against Tier 1
  produces a vault.
- **Deps:** 5.4.

### 5.6 — Obsidian Canvas export (stretch)

- **Deliverable:** `src/render/canvas.py` emits a `.canvas` file with
  contract nodes positioned by inheritance + call-graph layout.
- **Files:** `src/render/canvas.py`, `tests/test_canvas.py`.
- **Success:** Produced `.canvas` opens in Obsidian and shows all
  Tier 1 contracts with inheritance edges visible.
- **Deps:** 2.5.

---

## Chunk-to-phase coverage map

| Phase | Chunks | Count |
| --- | --- | --- |
| 0 — Foundations | 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8 | 8 |
| 1 — Single-node MVP | 1.1, 1.2, 1.3, 1.4, 1.5 | 5 |
| 2 — Main agent + topo | 2.1, 2.2, 2.3, 2.4, 2.5, 2.6 | 6 |
| 3 — Flow notes + Mermaid | 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9 | 9 |
| 4 — Risk pass + SARIF | 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8 | 8 |
| 5 — Diff + polish | 5.1, 5.2, 5.3, 5.4, 5.5, 5.6 | 6 |
| **Total** | | **42** |

## Suggested execution order

The default order is `0.1 → 0.2 → … → 5.6` (within-phase numeric, then
next phase). Two parallelizable lanes exist when the team has bandwidth:

- **Render lane:** 0.6 → 0.7 → 1.1 → 3.1 → 3.2 → 3.3 → 3.4 can run
  in parallel with the tool-layer lane once 0.5 is complete.
- **Analyzer lane:** 4.1 + 4.8 can run in parallel with 4.3 + 4.4 since
  they only converge at 4.5.

Stop at every chunk's **Success** line before moving on. If a chunk's
success criteria can't be met without scope creep, log the gap in a new
chunk rather than fattening the current one.
