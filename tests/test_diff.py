"""Tests for src/tools.py::diff_graphs.

Each test copies tier1_uniswap_v2/contracts/ to two tmp dirs
(before/, after/), mutates the `after` copy in code, parses
both into a shared cache, then diffs.

Mutation-at-test-time keeps the test intent local (the
mutation IS what the test verifies) and avoids a checked-in
duplicate fixture tree.
"""

import shutil
from pathlib import Path

import pytest

from src.tools import diff_graphs, trailmark_parse

TIER1_CONTRACTS = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "tier1_uniswap_v2"
    / "contracts"
)


def _parse_two_snapshots(
    before_dir: Path, after_dir: Path, cache_root: Path
) -> tuple[str, str]:
    """Parse both dirs into the same cache_root; return
    (before_id, after_id). Sharing one cache_root lets
    `diff_graphs` look up both via the same load_graph path."""
    before_id = trailmark_parse(
        str(before_dir), language="solidity", cache_root=cache_root,
    )
    after_id = trailmark_parse(
        str(after_dir), language="solidity", cache_root=cache_root,
    )
    return before_id, after_id


@pytest.fixture
def tier1_before_after(tmp_path):
    """Two tmp copies of Tier 1 contracts + a cache dir.
    Tests mutate the `after` copy then call
    `_parse_two_snapshots`."""
    before = tmp_path / "before"
    after = tmp_path / "after"
    shutil.copytree(TIER1_CONTRACTS, before)
    shutil.copytree(TIER1_CONTRACTS, after)
    return before, after, tmp_path / "cache"


def test_diff_identity_returns_empty_deltas(tier1_before_after):
    """Diffing a graph against itself returns empty
    add/remove/modify lists and an empty summary_delta.
    Pins the "no change" baseline so a future refactor that
    accidentally surfaces phantom diffs gets caught."""
    before, _after, cache_root = tier1_before_after
    gid = trailmark_parse(
        str(before), language="solidity", cache_root=cache_root,
    )
    result = diff_graphs(gid, gid, cache_root=cache_root)

    # summary_delta only carries keys whose count changed —
    # identity diff => empty dict.
    assert result["summary_delta"] == {}
    assert result["nodes"]["added"] == []
    assert result["nodes"]["removed"] == []
    assert result["nodes"]["modified"] == []
    assert result["edges"]["added"] == []
    assert result["edges"]["removed"] == []
    assert result["entrypoints"]["added"] == []
    assert result["entrypoints"]["removed"] == []
    assert result["entrypoints"]["modified"] == []


def test_diff_detects_added_contract(tier1_before_after):
    """`after` gains a brand-new contract file. Diff must
    surface the new contract under nodes.added and reflect
    a positive node-count delta in summary_delta."""
    before, after, cache_root = tier1_before_after
    (after / "Extra.sol").write_text(
        "pragma solidity =0.5.16;\n"
        "contract Extra {\n"
        "    function ping() external pure returns (uint) { return 42; }\n"
        "}\n"
    )
    bid, aid = _parse_two_snapshots(before, after, cache_root)
    result = diff_graphs(bid, aid, cache_root=cache_root)

    added_names = {n["name"] for n in result["nodes"]["added"]}
    assert "Extra" in added_names, (
        f"Extra contract missing from added: {added_names}"
    )
    assert result["nodes"]["removed"] == []
    # summary_delta["nodes"] is only present when the count
    # changed; the new file adds at least the contract + its
    # method node, so the delta is strictly positive.
    assert result["summary_delta"].get("nodes", {}).get("delta", 0) > 0


def test_diff_detects_removed_contract(tier1_before_after):
    """`after` has one contract file deleted. Diff must
    surface the deleted contract under nodes.removed and
    reflect a negative node-count delta."""
    before, after, cache_root = tier1_before_after
    (after / "UniswapV2ERC20.sol").unlink()
    bid, aid = _parse_two_snapshots(before, after, cache_root)
    result = diff_graphs(bid, aid, cache_root=cache_root)

    removed_names = {n["name"] for n in result["nodes"]["removed"]}
    assert "UniswapV2ERC20" in removed_names, (
        f"UniswapV2ERC20 missing from removed: {removed_names}"
    )
    # The deleted file owned many nodes (contract + methods);
    # at minimum the contract itself must be removed.
    assert result["summary_delta"].get("nodes", {}).get("delta", 0) < 0


def test_diff_detects_added_call_edge(tier1_before_after):
    """`after` modifies `transfer` to call `_approve` (which
    it does not call in `before`). Diff must surface the new
    `(transfer, _approve, call)` edge under edges.added.

    The same mutation also changes `transfer`'s body shape
    (line_span / CC), so nodes.modified will likely include
    the transfer entry too — that's incidental, the test
    pins the edge addition only."""
    before, after, cache_root = tier1_before_after
    src = (after / "UniswapV2ERC20.sol").read_text()
    # `transfer` currently only calls `_transfer`. Inject an
    # extra call to `_approve` so a NEW (transfer, _approve)
    # edge appears in the diff. `_approve(msg.sender, to, 0)`
    # is side-effect-free for value=0 (writes 0 to the
    # allowance map; semantics don't matter for the diff
    # test).
    mutated = src.replace(
        "    function transfer(address to, uint value) external returns (bool) {\n"
        "        _transfer(msg.sender, to, value);\n"
        "        return true;\n"
        "    }",
        "    function transfer(address to, uint value) external returns (bool) {\n"
        "        _transfer(msg.sender, to, value);\n"
        "        _approve(msg.sender, to, 0);\n"
        "        return true;\n"
        "    }",
    )
    assert mutated != src, "transfer-body mutation did not apply"
    (after / "UniswapV2ERC20.sol").write_text(mutated)

    bid, aid = _parse_two_snapshots(before, after, cache_root)
    result = diff_graphs(bid, aid, cache_root=cache_root)

    # Look for any call edge whose target is `_approve` and
    # whose source is the transfer method. Edge kind is
    # `calls` (plural) — Trailmark's enum value, not `call`.
    added_edges = result["edges"]["added"]
    transfer_to_approve = [
        e for e in added_edges
        if e["kind"] == "calls"
        and e["target"].endswith(":UniswapV2ERC20._approve")
        and e["source"].endswith(":UniswapV2ERC20.transfer")
    ]
    assert transfer_to_approve, (
        f"missing transfer→_approve call edge; added edges: "
        f"{added_edges}"
    )


def test_diff_detects_added_entrypoint(tier1_before_after):
    """`after` adds a new `external` function. Diff must
    surface it under entrypoints.added — the attack-surface
    delta that the graph-evolution skill targets.

    Uses a brand-new contract+function pair rather than
    changing an existing function's visibility, because
    Trailmark may treat visibility change as a node-modified
    event without re-classifying the existing node as a new
    entrypoint."""
    before, after, cache_root = tier1_before_after
    (after / "Extra.sol").write_text(
        "pragma solidity =0.5.16;\n"
        "contract Extra {\n"
        "    function newEntrypoint(uint x) external pure returns (uint) {\n"
        "        return x + 1;\n"
        "    }\n"
        "}\n"
    )
    bid, aid = _parse_two_snapshots(before, after, cache_root)
    result = diff_graphs(bid, aid, cache_root=cache_root)

    added_ep_ids = [e["id"] for e in result["entrypoints"]["added"]]
    assert any(
        eid.endswith("Extra.newEntrypoint") for eid in added_ep_ids
    ), (
        f"newEntrypoint missing from entrypoints.added: "
        f"{added_ep_ids}"
    )


def test_diff_detects_modified_node_complexity(tier1_before_after):
    """`after` mutates `transfer` to add a branch (bumps
    cyclomatic complexity). Diff must surface the function
    in `nodes.modified` with a `cyclomatic_complexity` change
    entry showing before/after values.

    Pins the documented shape of modified-node entries (the
    `_compare_units` covers CC, parameters, line_span). Without
    this, a Trailmark API drift that changes the modified-entry
    structure would land silently and break chunk 5.2's renderer
    far from the root cause."""
    before, after, cache_root = tier1_before_after
    src = (after / "UniswapV2ERC20.sol").read_text()
    # Add an if/else branch to `transfer` — bumps the
    # cyclomatic complexity from 1 to 2 without changing
    # the function's contract.
    mutated = src.replace(
        "    function transfer(address to, uint value) external returns (bool) {\n"
        "        _transfer(msg.sender, to, value);\n"
        "        return true;\n"
        "    }",
        "    function transfer(address to, uint value) external returns (bool) {\n"
        "        if (value > 0) {\n"
        "            _transfer(msg.sender, to, value);\n"
        "        }\n"
        "        return true;\n"
        "    }",
    )
    assert mutated != src, "transfer-body CC-mutation did not apply"
    (after / "UniswapV2ERC20.sol").write_text(mutated)

    bid, aid = _parse_two_snapshots(before, after, cache_root)
    result = diff_graphs(bid, aid, cache_root=cache_root)

    # Find the transfer entry in modified nodes.
    transfer_mods = [
        m for m in result["nodes"]["modified"]
        if m["id"].endswith(":UniswapV2ERC20.transfer")
    ]
    assert transfer_mods, (
        f"transfer not in modified nodes: "
        f"{[m['id'] for m in result['nodes']['modified']]}"
    )
    changes = transfer_mods[0]["changes"]
    # The shape promised by the docstring + Trailmark's
    # _compare_units: cyclomatic_complexity with before/after.
    assert "cyclomatic_complexity" in changes, (
        f"missing cyclomatic_complexity change: {changes}"
    )
    cc = changes["cyclomatic_complexity"]
    assert "before" in cc and "after" in cc, (
        f"CC change missing before/after keys: {cc}"
    )
    assert cc["after"] > cc["before"], (
        f"CC should have risen with new branch: {cc}"
    )


def test_diff_rejects_invalid_graph_id(tier1_before_after):
    """Invalid graph_id (13 chars instead of 12) is rejected
    by `load_graph._validate_graph_id` before the diff runs.
    Mirrors the validation-defense tests in test_persist.py —
    pin the contract that bad input fails loud at the wrapper
    boundary, not silently downstream."""
    _before, _after, cache_root = tier1_before_after
    with pytest.raises(ValueError, match="graph_id"):
        diff_graphs(
            "abcdef0123456",  # 13 chars
            "abcdef012345",
            cache_root=cache_root,
        )
