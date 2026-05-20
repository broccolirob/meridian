import pytest

from src.render.mermaid import render_sequence

# A hand-built 4-hop path with 3 distinct containing classes —
# satisfies the chunk 3.3 success criterion. Each node is a real
# Tier 1 node ID (validated by the renderer via load_graph).
# This isn't a real call chain in the graph (uniswapV2Call
# doesn't actually call getReserves), but render_sequence is
# path-agnostic by design — see plan's design decision #1.
SWAP_PATH = [
    "contracts.UniswapV2Pair:UniswapV2Pair.swap",
    "contracts.interfaces.IUniswapV2Callee:IUniswapV2Callee.uniswapV2Call",
    "contracts.interfaces.IUniswapV2Pair:IUniswapV2Pair.getReserves",
    "contracts.UniswapV2Pair:UniswapV2Pair._update",
]


def test_renders_three_plus_participants(tier1_graph_id):
    """Chunk 3.3 success criterion: a known swap path renders
    with three or more participants."""
    gid, cache_root = tier1_graph_id
    out = render_sequence(gid, SWAP_PATH, cache_root=cache_root)
    assert out.count("    participant ") >= 3
    assert "participant UniswapV2Pair" in out
    assert "participant IUniswapV2Callee" in out
    assert "participant IUniswapV2Pair" in out


def test_arrows_are_in_path_order(tier1_graph_id):
    """Arrow order must match path order — the sequence narrative
    depends on it."""
    gid, cache_root = tier1_graph_id
    out = render_sequence(gid, SWAP_PATH, cache_root=cache_root)
    arrows = [
        line
        for line in out.splitlines()
        if "->>" in line and "participant" not in line
    ]
    assert len(arrows) == 3
    assert "uniswapV2Call" in arrows[0]
    assert "getReserves" in arrows[1]
    assert "_update" in arrows[2]


def test_output_is_a_fenced_sequencediagram_block(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    out = render_sequence(gid, SWAP_PATH, cache_root=cache_root)
    assert out.startswith("```mermaid\n")
    assert out.rstrip().endswith("```")
    assert "sequenceDiagram" in out


def test_self_call_renders_as_self_loop(tier1_graph_id):
    """Two consecutive nodes in the same contract = self-loop in
    Mermaid (A->>A: method)."""
    gid, cache_root = tier1_graph_id
    path = [
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        "contracts.UniswapV2Pair:UniswapV2Pair._safeTransfer",
    ]
    out = render_sequence(gid, path, cache_root=cache_root)
    assert "UniswapV2Pair->>UniswapV2Pair: _safeTransfer" in out
    assert out.count("    participant ") == 1


def test_empty_path_raises(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    with pytest.raises(ValueError, match="path must be non-empty"):
        render_sequence(gid, [], cache_root=cache_root)


def test_unknown_node_raises_key_error(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    bad_path = [
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        "fake:DoesNotExist.method",
    ]
    with pytest.raises(KeyError):
        render_sequence(gid, bad_path, cache_root=cache_root)
