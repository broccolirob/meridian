# Tier 1 integration test

End-to-end runbook for Phase 2 — chunk 2.5.

## What it does

Runs the full washable pipeline against the Uniswap V2 core fixture:
parse → topological order → dispatch NodeDocumenter per node → write
MOCs. Then validates every expected file exists and every frontmatter
parses.

## Cost

Roughly $0.30 in OpenAI tokens per run (22 nodes × gpt-5-mini at
chunk 2.3's cap of 5). Not for CI; run it manually when you change:

- The dispatch loop (`src/agent.py`)
- The subagent prompt (`src/subagents.py`)
- The render template (`src/render/obsidian.py`)
- The MOC writer (`src/render/moc.py`)
- The topo sort (`src/graph/topo.py`)

## How to run

```bash
bash tests/integration/uniswap_v2/run.sh
```

Exits 0 on full success. Exits 1 if any expected file is missing
or any frontmatter block fails to parse.

## How to update the manifest

When Trailmark's parser changes or the graph layout shifts, the
expected paths may need to change. To regenerate the manifest:

1. Run `run.sh` once (cost: $0.30).
2. `ls -1` the produced vault to see what was written.
3. Replace `expected_files.txt` with the new list (keep the
   `# Comment` headers for context).

## What's in the vault

- 3 contracts: `UniswapV2ERC20`, `UniswapV2Factory`, `UniswapV2Pair`
- 5 interfaces: `IERC20`, `IUniswapV2Callee`, `IUniswapV2ERC20`,
  `IUniswapV2Factory`, `IUniswapV2Pair`
- 3 libraries: `Math`, `SafeMath`, `UQ112x112`
- 11 module-level notes in `_meta/`
- 5 MOC pages (root + 4 populated folder READMEs)
