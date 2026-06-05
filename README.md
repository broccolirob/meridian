# meridian

> Turn any codebase into an audit-ready Obsidian vault. Trailmark
> provides the graph; deepagents provide the narrative.

![status: pre-release](https://img.shields.io/badge/status-pre--release-orange)
![python: 3.12+](https://img.shields.io/badge/python-3.12+-blue)

**Status: pre-release.** Phase 5 polish in progress. APIs and the
CLI surface may shift before 1.0.

## What it does

A deepagent that turns any codebase into an audit-ready Obsidian
vault. Trailmark provides the graph. deepagents provide the
narrative and synthesis. First-class target: Solidity. Designed-in
target: any language Trailmark parses (21 and counting).

For an auditor, the output is a code atlas: one note per
contract/library/interface, cross-cutting flow narratives,
attack-surface MOCs, risk hotspots, and Mermaid diagrams — all
wikilinked so you navigate by clicking.

## Quickstart

Requirements:

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- `OPENAI_API_KEY` in `.env` (defaults to `gpt-5-mini`)
- For Solidity audits: `slither-analyzer` (installed via `uv sync`)
  and a matching `solc` (see [Setup notes](#setup-notes))

Clone and run against the bundled Tier 1 fixture (Uniswap V2):

```bash
git clone https://github.com/<your-org>/meridian.git
cd meridian
uv sync
cp .env.example .env
# edit .env to set OPENAI_API_KEY

# Orchestrate end-to-end: parse → slither → augment → document
# nodes → trace flows → synthesize risks → write MOC → validate.
uv run python scripts/document_repo.py \
    --repo tests/fixtures/tier1_uniswap_v2 \
    --vault .meridian/vaults/quickstart
```

After ~2-5 minutes (model latency dominates), open the vault in
Obsidian:

```bash
open -a Obsidian .meridian/vaults/quickstart
```

You should see three contract notes (`UniswapV2Factory`,
`UniswapV2Pair`, `UniswapV2ERC20`), flow notes under `flows/`,
risk notes under `risks/`, and a root `README.md` MOC linking
everything.

> **Note on the CLI:** the one-command orchestrator above lives in
> `scripts/document_repo.py` today. The polished `meridian`
> console-script (chunk 5.4) exposes `parse`, `diff`, and
> `validate` subcommands but doesn't yet wrap orchestration as a
> single `run` command. That's coming in a future chunk; the
> script form is stable and ships the full pipeline.

## What you get

```
<vault>/
├── README.md              Map-of-content for the whole vault
├── _meta/                 Glossary, tags
├── contracts/             One note per contract/library/interface
├── flows/                 Per-entrypoint call-chain narratives
├── attack-surface/        Entrypoints ranked by trust + asset value
├── risks/                 Hotspots, delegatecall sites, reentrancy
├── diffs/                 (meridian diff output)
└── diagrams/              Inheritance + call-graph Mermaid blocks
```

Each note has:

- **YAML frontmatter** with type, node_id, file, location,
  cyclomatic complexity, callers/callees counts,
  inherits/implements/uses, finding annotations.
- **7 canonical sections**: Overview, Graph context, State,
  Functions, Events / Errors / Modifiers, Annotations, Risks.

See [`PLAN.md`](PLAN.md) for the full layout specification.

## Screenshot

_Coming soon — see [`docs/SCREENSHOTS.md`](docs/SCREENSHOTS.md) for
capture instructions._

When ready, the canonical screenshot lives at
`docs/screenshot-note.png` and shows the `UniswapV2Pair` contract
note as rendered in Obsidian's default theme.

## CLI

The `meridian` console-script exposes three subcommands today:

```bash
uv run meridian --help                          # list subcommands
uv run meridian parse <repo>                    # cache a parsed graph
uv run meridian diff <before> <after>           # write a diff note
uv run meridian validate                        # check vault wikilinks
```

Both `diff` and `validate` need `--vault-path PATH` at the top level:

```bash
uv run meridian --vault-path ./my-vault validate
uv run meridian --vault-path ./my-vault diff <bid> <aid>
```

The full repo→vault orchestration is `scripts/document_repo.py`
(see [Quickstart](#quickstart) above). A future chunk wraps it as
`meridian run`.

## Setup notes

**Solidity (slither):** `uv sync` installs `slither-analyzer` via
`[project].dependencies`. You still need a matching `solc` — for
Tier 1 (Uniswap V2 pinned to `0.5.16`):

```bash
solc-select install 0.5.16
solc-select use 0.5.16
```

**Other languages:** `meridian parse` accepts `--language` (default
`auto`). Trailmark supports 21+ languages; for non-Solidity targets,
use `run_semgrep` from `src/analyzers/semgrep.py` as the SARIF
producer instead of slither.

## Project status

- **Phases 0-4:** complete. Foundations, single-node MVP, main
  agent + topo walk, flow notes + Mermaid, risk pass + SARIF.
- **Phase 5:** complete. Diff mode, file-hash incremental cache,
  CLI polish (parse/diff/validate), this README, Obsidian Canvas
  export.
- **Threat model:** designed for accountable inputs — customer
  audits, vetted bounty targets. Running against unaccountable
  code (random GitHub clones) requires sandboxing; see
  `src/analyzers/slither.py`'s docstring for the build-tool RCE
  surface that crytic-compile exposes.

See [`PLAN.md`](PLAN.md) for architecture depth and
[`CHUNKS.md`](CHUNKS.md) for the phase breakdown.

## License

Not yet specified.
