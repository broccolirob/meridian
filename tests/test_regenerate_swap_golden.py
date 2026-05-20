"""Tests for scripts/regenerate_swap_golden.py.

Deterministic no-LLM script that regenerates the golden flow
note from the Tier 1 fixture. The only meaningful invariant:
output must satisfy the same assertions
`tests/test_golden_flow.py` runs against the committed golden.

If this script breaks, the documented recovery path
(`test_golden_flow.py`'s skip-message says "regenerate with
scripts/regenerate_swap_golden.py") no longer works — a
regression that lost the golden would silently turn the
golden tests into skips forever.
"""

import importlib.util
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "regenerate_swap_golden.py"
)


@pytest.fixture
def script_module():
    """Fresh import of regenerate_swap_golden.py per test."""
    spec = importlib.util.spec_from_file_location(
        "regenerate_swap_golden_under_test", str(SCRIPT_PATH)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_main_produces_golden_satisfying_existing_assertions(
    script_module, monkeypatch, tmp_path
):
    """Run regenerate_swap_golden's main() against a
    tmp_path target. The produced file must satisfy the
    same invariants `test_golden_flow.py` asserts against
    the committed golden — co-armoring the regenerator and
    the golden's consumer. A future drift in either side
    fails this test."""
    # Redirect the script's output path to tmp_path so the
    # test doesn't overwrite the committed golden.
    monkeypatch.setattr(script_module, "GOLDEN_VAULT", tmp_path)

    rc = script_module.main()
    assert rc == 0

    # The script writes to GOLDEN_VAULT/flows/<name>.md.
    flow_files = list((tmp_path / "flows").glob("*.md"))
    assert len(flow_files) == 1, (
        f"expected exactly one flow file in "
        f"{tmp_path / 'flows'}; got {flow_files}"
    )
    out = flow_files[0]
    text = out.read_text()

    # Mirror tests/test_golden_flow.py's invariants:
    # frontmatter exists, body has Overview + Paths + Hops
    # sections, at least one sequenceDiagram block.
    assert text.startswith("---\n"), (
        f"missing YAML frontmatter; file starts with: "
        f"{text[:50]!r}"
    )
    assert "## Overview" in text
    assert "## Paths" in text
    assert "**Hops:**" in text
    assert "sequenceDiagram" in text
    # Tier 1's UniswapV2Pair wikilink appears in Hops
    # (mirrors test_golden_flow.py's wikilink assertion).
    assert "UniswapV2Pair" in text
