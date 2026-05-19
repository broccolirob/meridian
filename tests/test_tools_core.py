import pytest

from src.tools import (
    get_node,
    graph_summary,
    list_nodes,
    trailmark_parse,
)


@pytest.fixture(scope="module")
def tier0_graph_id(tier0_dir, tmp_path_factory):
    cache_root = tmp_path_factory.mktemp("cache")
    gid = trailmark_parse(
        str(tier0_dir), language="solidity", cache_root=cache_root
    )
    return gid, cache_root


def test_trailmark_parse_returns_graph_id(tier0_graph_id):
    gid, cache_root = tier0_graph_id
    assert isinstance(gid, str)
    assert len(gid) == 12
    assert (cache_root / gid / "engine.pkl").exists()


def test_graph_summary_matches_tier0(tier0_graph_id):
    gid, cache_root = tier0_graph_id
    s = graph_summary(gid, cache_root=cache_root)
    assert s["total_nodes"] == 50
    assert s["functions"] == 42
    assert s["entrypoints"] == 19
    assert set(s["dependencies"]) == {
        "ERC20",
        "SafeTransferLib",
        "FixedPointMathLib",
    }


def test_list_nodes_no_filter_returns_all(tier0_graph_id):
    gid, cache_root = tier0_graph_id
    nodes = list_nodes(gid, cache_root=cache_root)
    assert len(nodes) == 50
    for n in nodes:
        assert {"id", "name", "kind", "location"} <= set(n.keys())
        assert "file_path" in n["location"]


def test_list_nodes_filtered_by_kind(tier0_graph_id):
    gid, cache_root = tier0_graph_id
    contracts = list_nodes(gid, kind="contract", cache_root=cache_root)
    assert len(contracts) == 2
    names = {c["name"] for c in contracts}
    assert any("ERC20" in n for n in names)


def test_get_node_by_id_returns_expected_fields(tier0_graph_id):
    gid, cache_root = tier0_graph_id
    node = get_node(
        gid, "src.tokens.ERC20:ERC20", cache_root=cache_root
    )
    assert node["kind"] == "contract"
    assert node["location"]["file_path"].endswith("ERC20.sol")
    assert node["location"]["start_line"] >= 1


def test_get_node_missing_raises(tier0_graph_id):
    gid, cache_root = tier0_graph_id
    with pytest.raises(KeyError):
        get_node(gid, "does.not.exist", cache_root=cache_root)
