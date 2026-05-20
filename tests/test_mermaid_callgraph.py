import pytest

from src.render.mermaid import render_call_graph

# Tier 1 fixture: the swap function is the canonical entrypoint
# the chunk's success criterion is written against.
SWAP_ID = "contracts.UniswapV2Pair:UniswapV2Pair.swap"


def test_swap_diagram_contains_required_nodes(tier1_graph_id):
    """Chunk 3.1 success criterion: swap + _update + _safeTransfer
    all present in the call graph at default depth."""
    gid, cache_root = tier1_graph_id
    out = render_call_graph(gid, SWAP_ID, cache_root=cache_root)
    assert "swap" in out
    assert "_update" in out
    assert "_safeTransfer" in out


def test_output_is_a_fenced_mermaid_block(tier1_graph_id):
    """Consumers embed the return value directly in Markdown."""
    gid, cache_root = tier1_graph_id
    out = render_call_graph(gid, SWAP_ID, cache_root=cache_root)
    assert out.startswith("```mermaid\n")
    assert out.rstrip().endswith("```")
    assert "graph TD" in out


def test_depth_zero_returns_root_only(tier1_graph_id):
    """depth=0 = just the focus node, no callers or callees."""
    gid, cache_root = tier1_graph_id
    out = render_call_graph(gid, SWAP_ID, depth=0, cache_root=cache_root)
    assert "-->" not in out
    assert "swap" in out


def test_negative_depth_raises(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    with pytest.raises(ValueError, match="depth must be >= 0"):
        render_call_graph(gid, SWAP_ID, depth=-1, cache_root=cache_root)


def test_output_is_byte_stable(tier1_graph_id):
    """Sorted-alias assignment + sorted-edge output must produce
    identical bytes across runs — required for snapshot tests in
    later chunks (3.5 goldens)."""
    gid, cache_root = tier1_graph_id
    a = render_call_graph(gid, SWAP_ID, cache_root=cache_root)
    b = render_call_graph(gid, SWAP_ID, cache_root=cache_root)
    assert a == b
