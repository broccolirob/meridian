from pathlib import Path

import pytest

from src.tools import read_file_range

ERC20_PATH = (
    Path("tests/fixtures/tier0_erc4626/src/tokens/ERC20.sol").resolve()
)


def test_reads_single_line():
    line = read_file_range(ERC20_PATH, 1, 1)
    assert line.strip().startswith("// SPDX-License-Identifier")


def test_reads_inclusive_range():
    out = read_file_range(ERC20_PATH, 1, 3)
    assert out.count("\n") >= 2
    assert "SPDX" in out
    assert "pragma" in out


def test_out_of_range_upper_clamps():
    full = read_file_range(ERC20_PATH, 1, 99_999)
    assert "abstract contract ERC20" in full


def test_reversed_range_returns_empty():
    assert read_file_range(ERC20_PATH, 50, 10) == ""


@pytest.mark.parametrize(
    "start,end",
    [(0, 5), (1, 0), (-1, 10), (5, -1)],
)
def test_zero_or_negative_lines_raises(start, end):
    with pytest.raises(ValueError, match="line numbers must be >= 1"):
        read_file_range(ERC20_PATH, start, end)


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        read_file_range("does/not/exist.sol", 1, 5)


# --- read_node_source (scoped, agent-safe wrapper) ------------------

from src.tools import read_node_source  # noqa: E402


def test_read_node_source_returns_node_range(tier0_graph_id):
    """Agent-facing wrapper reads the node's full source — no path
    argument means no prompt-injection vector for arbitrary file
    reads."""
    gid, cache_root = tier0_graph_id
    text = read_node_source(
        gid, "src.tokens.ERC4626:ERC4626", cache_root=cache_root
    )
    # ERC4626 contract body — includes 'abstract contract ERC4626'
    assert "abstract contract ERC4626" in text
    # And not noise from elsewhere in the file
    assert "abstract contract ERC20" not in text


def test_read_node_source_method_returns_method_only(tier0_graph_id):
    gid, cache_root = tier0_graph_id
    text = read_node_source(
        gid, "src.tokens.ERC4626:ERC4626.deposit", cache_root=cache_root
    )
    # deposit body mentions safeTransferFrom and previewDeposit
    assert "deposit" in text
    # And shouldn't pull in unrelated method bodies like beforeWithdraw
    assert "beforeWithdraw" not in text


def test_read_node_source_unknown_node_raises(tier0_graph_id):
    gid, cache_root = tier0_graph_id
    with pytest.raises(KeyError):
        read_node_source(gid, "does.not:Exist", cache_root=cache_root)
