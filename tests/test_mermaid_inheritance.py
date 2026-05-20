import pytest

from src.render.mermaid import render_inheritance

PAIR_ID = "contracts.UniswapV2Pair:UniswapV2Pair"
ERC20_ID = "contracts.UniswapV2ERC20:UniswapV2ERC20"


def test_pair_inherits_erc20_is_solid(tier1_graph_id):
    """Chunk 3.2 success criterion — literal substring from spec."""
    gid, cache_root = tier1_graph_id
    out = render_inheritance(gid, PAIR_ID, cache_root=cache_root)
    assert "UniswapV2ERC20 <|-- UniswapV2Pair" in out


def test_pair_implements_ipair_is_dashed(tier1_graph_id):
    """The interface side of `is IUniswapV2Pair, UniswapV2ERC20`
    must render with the dashed/implements style, not solid."""
    gid, cache_root = tier1_graph_id
    out = render_inheritance(gid, PAIR_ID, cache_root=cache_root)
    assert "IUniswapV2Pair <|.. UniswapV2Pair" in out


def test_output_is_a_fenced_classdiagram_block(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    out = render_inheritance(gid, PAIR_ID, cache_root=cache_root)
    assert out.startswith("```mermaid\n")
    assert out.rstrip().endswith("```")
    assert "classDiagram" in out


def test_erc20_has_pair_as_child(tier1_graph_id):
    """ERC20 is a base of Pair — rendering ERC20 must list Pair as
    a child. This tests the child-side traversal that requires
    phantom-target resolution to work."""
    gid, cache_root = tier1_graph_id
    out = render_inheritance(gid, ERC20_ID, cache_root=cache_root)
    assert "UniswapV2ERC20 <|-- UniswapV2Pair" in out


def test_unknown_node_raises_key_error(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    with pytest.raises(KeyError):
        render_inheritance(gid, "fake:DoesNotExist", cache_root=cache_root)
