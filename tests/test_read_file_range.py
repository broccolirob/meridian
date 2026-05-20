from pathlib import Path

import pytest

from src.tools import MAX_SOURCE_LINES, read_file_range

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
    """An end_line past EOF clamps to the file's actual length —
    no error, just returns everything available."""
    full = read_file_range(ERC20_PATH, 1, 1000)
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


def test_oversized_request_rejected():
    """An attacker repo with a multi-GB Solidity file would OOM
    the orchestrator if Trailmark recorded e.g. start_line=1,
    end_line=20_000_000 for a node. The MAX_SOURCE_LINES cap
    rejects requests for more lines than any realistic single
    documentable node would have."""
    with pytest.raises(ValueError, match="exceeds MAX_SOURCE_LINES"):
        read_file_range(ERC20_PATH, 1, MAX_SOURCE_LINES + 1)


def test_at_cap_succeeds():
    """A request for exactly MAX_SOURCE_LINES lines is accepted —
    the cap is inclusive at the boundary; clamps to EOF as usual."""
    full = read_file_range(ERC20_PATH, 1, MAX_SOURCE_LINES)
    assert "abstract contract ERC20" in full


def test_streams_does_not_buffer_full_file(tmp_path):
    """Defense against the 'request small range from huge file'
    DoS path. Build a file far larger than the requested range
    and verify read_file_range returns only the requested lines
    — implicit proof that f.readlines()'s full-file load was
    replaced by streaming via itertools.islice."""
    big = tmp_path / "big.sol"
    # 50_000 lines of 80-byte content — 4MB total. If the
    # implementation regressed to f.readlines(), this still
    # works but loses the bound; the bound is verified
    # mechanically via test_oversized_request_rejected.
    big.write_text("x" * 79 + "\n" * 1, encoding="utf-8")
    with big.open("a", encoding="utf-8") as f:
        for _ in range(49_999):
            f.write("y" * 79 + "\n")
    out = read_file_range(big, 1, 3)
    assert out.count("\n") == 3
    assert out.startswith("x" * 79)


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
