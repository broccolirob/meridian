import pytest

from src.tools import (
    ancestors_of,
    callees_of,
    callers_of,
    paths_between,
    reachable_from,
    trailmark_parse,
)

SWAP = "contracts.UniswapV2Pair:UniswapV2Pair.swap"
UPDATE = "contracts.UniswapV2Pair:UniswapV2Pair._update"


@pytest.fixture(scope="module")
def tier1_graph_id(tier1_dir, tmp_path_factory):
    cache_root = tmp_path_factory.mktemp("cache-tier1")
    gid = trailmark_parse(
        str(tier1_dir), language="solidity", cache_root=cache_root
    )
    return gid, cache_root


def test_callers_of_swap_is_empty(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    assert callers_of(gid, SWAP, cache_root=cache_root) == []


def test_callees_of_swap_is_expected_three(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    ids = {n["id"] for n in callees_of(gid, SWAP, cache_root=cache_root)}
    assert ids == {
        "contracts.UniswapV2Pair:UniswapV2Pair._update",
        "contracts.UniswapV2Pair:UniswapV2Pair._safeTransfer",
        "contracts.interfaces.IUniswapV2Pair:IUniswapV2Pair.getReserves",
    }


def test_callers_of_update_is_expected_four(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    ids = {n["id"] for n in callers_of(gid, UPDATE, cache_root=cache_root)}
    assert ids == {
        "contracts.UniswapV2Pair:UniswapV2Pair.sync",
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        "contracts.UniswapV2Pair:UniswapV2Pair.burn",
        "contracts.UniswapV2Pair:UniswapV2Pair.mint",
    }


def test_callees_of_update_is_empty(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    assert callees_of(gid, UPDATE, cache_root=cache_root) == []


def test_paths_between_swap_and_update_is_two_hop(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    paths = paths_between(gid, SWAP, UPDATE, cache_root=cache_root)
    assert paths == [[SWAP, UPDATE]]


def test_ancestors_and_reachable_counts(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    assert len(ancestors_of(gid, UPDATE, cache_root=cache_root)) == 4
    assert len(reachable_from(gid, SWAP, cache_root=cache_root)) == 3


def test_missing_node_returns_empty(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    assert callers_of(gid, "does.not.exist", cache_root=cache_root) == []
    assert callees_of(gid, "does.not.exist", cache_root=cache_root) == []
