"""Regenerate the chunk-3.9 swap flow golden deterministically.

No LLM cost. Uses `render_and_write_flow_note` with hand-crafted
inputs to produce a flow note that satisfies the structural
invariants in `tests/test_golden_flow.py`:
  - canonical YAML frontmatter (type=flow, name, entrypoint,
    path_count)
  - non-placeholder Overview prose
  - one or more `### Path N — ...` subsections
  - each path followed by a Mermaid sequenceDiagram block
  - each path followed by a `**Hops:**` numbered list of
    wikilinks
  - at least one wikilink targeting `contracts/UniswapV2Pair`

The golden is hand-crafted (not LLM-captured) on purpose: the
6 structural tests don't validate semantic LLM-output quality,
so a deterministic golden is cheaper, more reproducible, AND
provides actual regression armor on the renderer pipeline.

If you also want to validate LLM output quality (chunk 3.9's
"describes the swap path correctly" subjective criterion), run
`scripts/trace_one_flow.py` separately and eyeball the output
— that's an independent workflow.

Usage:
    uv run python scripts/regenerate_swap_golden.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.render.obsidian import render_and_write_flow_note  # noqa: E402
from src.tools import get_node, trailmark_parse  # noqa: E402


FIXTURE = "tests/fixtures/tier1_uniswap_v2"
GOLDEN_VAULT = Path("tests/golden")
SWAP_ID = "contracts.UniswapV2Pair:UniswapV2Pair.swap"

# Hand-crafted overview — describes what `swap` does in
# UniswapV2Pair. Auditor-reviewed prose, line citations omitted
# (the structural tests don't require them).
_OVERVIEW = (
    "swap is the AMM entrypoint that exchanges one input token "
    "for another. It validates output amounts (lines 159-162), "
    "acquires the lock modifier for reentrancy safety, transfers "
    "the requested output to the recipient via _safeTransfer, "
    "optionally invokes the recipient's IUniswapV2Callee.uniswapV2Call "
    "hook for flash-loan-style callbacks, and finally calls "
    "_update to commit new reserves and emit the Swap event "
    "(lines 159-200). The constant-product invariant is enforced "
    "after the callback returns and balances are sampled, so a "
    "callback that fails to repay reverts the entire swap."
)

# Hand-crafted paths. Two of swap's three real callees — chosen
# to exercise both intra-contract self-loops (in the sequence
# diagram) and the Hops wikilink resolver.
_PATHS = [
    [
        SWAP_ID,
        "contracts.UniswapV2Pair:UniswapV2Pair._safeTransfer",
    ],
    [
        SWAP_ID,
        "contracts.UniswapV2Pair:UniswapV2Pair._update",
    ],
]

# Auditor observations the test asserts in spirit (they appear
# in the "Observations" section verbatim).
_OBSERVATIONS = [
    "lock modifier enforces no reentrancy across the swap call",
    "Swap event emitted after _update commits new reserves",
    (
        "uniswapV2Call callback executes BEFORE the K-invariant "
        "check, so a malicious callback must restore balances"
    ),
]


def main() -> int:
    print(f"[1/3] Parsing {FIXTURE}...")
    graph_id = trailmark_parse(FIXTURE, language="solidity")
    print(f"      graph_id = {graph_id}")

    print(f"[2/3] Fetching entrypoint node: {SWAP_ID}")
    swap = get_node(graph_id, SWAP_ID)
    print(f"      kind={swap['kind']} name={swap['name']}")

    GOLDEN_VAULT.mkdir(parents=True, exist_ok=True)
    (GOLDEN_VAULT / "flows").mkdir(parents=True, exist_ok=True)

    print("[3/3] Rendering deterministic flow note...")
    out = render_and_write_flow_note(
        GOLDEN_VAULT,
        graph_id,
        swap,
        _PATHS,
        overview=_OVERVIEW,
        observations=_OBSERVATIONS,
    )
    print(f"GOLDEN WRITTEN: {out}")
    print(f"      size: {Path(out).stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
