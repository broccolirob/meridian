import pickle

import pytest

from src.graph.topo import topo_order


def test_tier1_uniswap_v2_erc20_precedes_pair(tier1_graph_id):
    """The canonical CHUNKS.md success criterion."""
    gid, cache_root = tier1_graph_id
    order = topo_order(gid, cache_root=cache_root)
    erc20 = "contracts.UniswapV2ERC20:UniswapV2ERC20"
    actual_pair = "contracts.UniswapV2Pair:UniswapV2Pair"
    assert erc20 in order
    assert actual_pair in order
    assert order.index(erc20) < order.index(actual_pair)


def test_tier1_interfaces_precede_implementers(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    order = topo_order(gid, cache_root=cache_root)
    iface = "contracts.interfaces.IUniswapV2ERC20:IUniswapV2ERC20"
    impl = "contracts.UniswapV2ERC20:UniswapV2ERC20"
    assert order.index(iface) < order.index(impl)


def test_tier0_erc20_precedes_erc4626(tier0_graph_id):
    """Solmate's ERC4626 extends ERC20 — ERC20 must come first."""
    gid, cache_root = tier0_graph_id
    order = topo_order(gid, cache_root=cache_root)
    erc20 = "src.tokens.ERC20:ERC20"
    erc4626 = "src.tokens.ERC4626:ERC4626"
    assert order.index(erc20) < order.index(erc4626)


def test_order_contains_only_documentable_kinds(tier1_graph_id):
    """No methods — only contract/library/interface/module."""
    gid, cache_root = tier1_graph_id
    order = topo_order(gid, cache_root=cache_root)
    for nid in order:
        # Top-level node IDs have no `.` after the final `:`
        # (methods look like `module:Class.method`)
        tail = nid.rsplit(":", 1)[-1] if ":" in nid else nid
        assert "." not in tail.split(":")[-1] or ":" not in nid, (
            f"non-top-level node in order: {nid}"
        )


def test_order_covers_every_documentable_node(tier1_graph_id):
    """Every documentable node must appear — no losses to filtering."""
    gid, cache_root = tier1_graph_id
    order = topo_order(gid, cache_root=cache_root)
    # Tier 1: 3 contracts + 5 interfaces + 3 libraries + 11 modules
    assert len(order) == 22
    assert len(set(order)) == len(order)


def test_order_is_deterministic(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    a = topo_order(gid, cache_root=cache_root)
    b = topo_order(gid, cache_root=cache_root)
    assert a == b


def test_tier0_excludes_methods(tier0_graph_id):
    gid, cache_root = tier0_graph_id
    order = topo_order(gid, cache_root=cache_root)
    # Tier 0: 50 nodes total, 42 methods. Topo set = 50 - 42 = 8
    # (4 modules + 2 contracts + 2 libraries).
    assert len(order) == 8


def _fake_node(name: str) -> dict:
    return {
        "id": f"m:{name}",
        "name": name,
        "kind": "contract",
        "location": {
            "file_path": f"/fake/{name.lower()}.sol",
            "start_line": 1,
            "end_line": 10,
            "start_col": 0,
            "end_col": 0,
        },
        "parameters": [],
        "return_type": None,
        "exception_types": [],
        "cyclomatic_complexity": None,
        "branches": [],
        "docstring": None,
    }


def test_cycle_detection(tmp_path, monkeypatch):
    """Hand-construct a cyclic graph to verify the algorithm raises.

    We monkeypatch load_graph rather than pickling a fake engine —
    classes defined inside test functions don't have a stable module
    path, so pickle can't round-trip them.
    """
    import json

    fake_graph = {
        "language": "solidity",
        "root_path": "/fake",
        "summary": {},
        "nodes": {"m:A": _fake_node("A"), "m:B": _fake_node("B")},
        "edges": [
            {
                "source": "m:A",
                "target": "m:B",
                "kind": "inherits",
                "confidence": "certain",
            },
            {
                "source": "m:B",
                "target": "m:A",
                "kind": "inherits",
                "confidence": "certain",
            },
        ],
        "subgraphs": {},
    }

    class _FakeEngine:
        def to_json(self):
            return json.dumps(fake_graph)

    monkeypatch.setattr(
        "src.graph.topo.load_graph",
        lambda graph_id, *, cache_root: _FakeEngine(),
    )

    with pytest.raises(ValueError, match="cycle"):
        topo_order("abc012345678", cache_root=tmp_path)
