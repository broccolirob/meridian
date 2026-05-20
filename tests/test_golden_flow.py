"""Structural regression tests for the Tier 1 flow-note golden.

The golden file is produced by an LLM dispatch (manual step:
`uv run python scripts/trace_one_flow.py …`). These tests
assert STRUCTURE not bytes — LLM prose varies, but frontmatter
shape, sequence-diagram presence, and hop-wikilink syntax are
deterministic invariants.

Tests skip cleanly when the golden hasn't been captured yet, so
this file is safe to land before the manual step.
"""

import re
from pathlib import Path

import pytest
import yaml

GOLDEN_FLOW = (
    Path(__file__).parent
    / "golden"
    / "flows"
    / "UniswapV2Pair.swap.md"
)

REQUIRED_FRONTMATTER_KEYS = {
    "type",
    "name",
    "entrypoint",
    "path_count",
}


def _split(text: str) -> tuple[dict, str]:
    end = text.index("\n---\n", 4)
    fm = yaml.safe_load(text[4:end])
    body = text[end + 5 :]
    return fm, body


def _require_golden() -> str:
    if not GOLDEN_FLOW.exists():
        pytest.skip(
            "flow golden missing — regenerate with "
            "`uv run python scripts/regenerate_swap_golden.py` "
            "(deterministic, no LLM cost). Or capture a real "
            "LLM-driven note with `uv run python "
            "scripts/trace_one_flow.py` then copy to "
            "tests/golden/flows/UniswapV2Pair.swap.md."
        )
    return GOLDEN_FLOW.read_text(encoding="utf-8")


def test_golden_swap_has_canonical_frontmatter():
    text = _require_golden()
    assert text.startswith("---\n"), "missing frontmatter"
    fm, _ = _split(text)
    missing = REQUIRED_FRONTMATTER_KEYS - set(fm.keys())
    assert not missing, f"missing frontmatter keys: {missing}"
    assert fm["type"] == "flow"
    assert fm["name"] == "swap"


def test_golden_swap_overview_is_real_prose():
    text = _require_golden()
    _, body = _split(text)
    assert "## Overview" in body
    assert "_Overview not yet written._" not in body


def test_golden_swap_has_at_least_one_path():
    """Sequence diagram renders in Obsidian (success criterion 2)."""
    text = _require_golden()
    _, body = _split(text)
    assert "## Paths" in body
    assert re.search(r"^### Path \d+", body, re.MULTILINE)
    assert "sequenceDiagram" in body


def test_golden_swap_every_path_has_hops_list():
    """Every hop must wikilink to its contract note — so every
    ### Path subsection must carry a **Hops:** list."""
    text = _require_golden()
    _, body = _split(text)
    path_count = len(re.findall(r"^### Path \d+", body, re.MULTILINE))
    hops_count = body.count("**Hops:**")
    assert hops_count >= 1
    assert hops_count == path_count, (
        f"each ### Path subsection must have a **Hops:** list "
        f"({path_count} paths but {hops_count} Hops sections)"
    )


def test_golden_swap_hops_are_wikilinks():
    """Every hop line must use [[…]] wikilink syntax. The
    only fallback exception (`bare-name (no contract note)`)
    must NOT appear in Tier 1 since every method has a real
    parent contract."""
    text = _require_golden()
    _, body = _split(text)
    blocks = re.findall(
        r"\*\*Hops:\*\*\n\n((?:\d+\. .+\n)+)", body
    )
    assert blocks, "no Hops blocks parsed"
    for block in blocks:
        for line in block.strip().splitlines():
            assert "[[" in line and "]]" in line, (
                f"hop line missing wikilink: {line!r}"
            )


def test_golden_swap_hops_target_uniswap_v2_pair():
    """The swap entrypoint lives in UniswapV2Pair — at least
    one hop must wikilink to its parent contract's note."""
    text = _require_golden()
    _, body = _split(text)
    assert "contracts/UniswapV2Pair" in body
