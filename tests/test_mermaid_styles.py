import pytest

from src.render.mermaid import (
    render_complexity_heatmap,
    render_containment,
)
from src.render.mermaid_styles import (
    COMPLEXITY_CLASSDEFS,
    bucket_for_complexity,
)

PAIR_ID = "contracts.UniswapV2Pair:UniswapV2Pair"
ERC20_ID = "contracts.UniswapV2ERC20:UniswapV2ERC20"


# ---------- bucket_for_complexity ----------


def test_bucket_low_includes_zero_and_low():
    assert bucket_for_complexity(1) == "low"
    assert bucket_for_complexity(4) == "low"


def test_bucket_medium_at_boundaries():
    assert bucket_for_complexity(5) == "medium"
    assert bucket_for_complexity(10) == "medium"


def test_bucket_high_above_ten():
    assert bucket_for_complexity(11) == "high"
    assert bucket_for_complexity(50) == "high"


def test_bucket_none_defaults_to_low():
    """Trailmark uses None when CC isn't computed — render code
    shouldn't crash on it."""
    assert bucket_for_complexity(None) == "low"


# ---------- render_complexity_heatmap ----------


def test_heatmap_includes_multiple_buckets(tier1_graph_id):
    """Chunk 3.4 success criterion: a Tier 1 heatmap with
    threshold=4 surfaces both low (CC=4) and medium (CC=6)
    buckets. Default threshold=5 would only show medium."""
    gid, cache_root = tier1_graph_id
    out = render_complexity_heatmap(
        gid, threshold=4, cache_root=cache_root
    )
    assert ":::low" in out
    assert ":::medium" in out
    for classdef in COMPLEXITY_CLASSDEFS:
        assert classdef in out


def test_heatmap_labels_include_cc_value(tier1_graph_id):
    """The auditor needs to see the CC number alongside the color
    — "yellow" is meaningless without the value."""
    gid, cache_root = tier1_graph_id
    out = render_complexity_heatmap(
        gid, threshold=4, cache_root=cache_root
    )
    assert "CC=6" in out
    assert "CC=4" in out


def test_heatmap_output_is_a_fenced_flowchart(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    out = render_complexity_heatmap(
        gid, threshold=4, cache_root=cache_root
    )
    assert out.startswith("```mermaid\n")
    assert "flowchart TB" in out
    assert out.rstrip().endswith("```")


def test_heatmap_empty_when_threshold_too_high(tier1_graph_id):
    """Threshold above the max CC in the graph produces an
    explanatory empty diagram, not garbage output."""
    gid, cache_root = tier1_graph_id
    out = render_complexity_heatmap(
        gid, threshold=999, cache_root=cache_root
    )
    assert "No methods with CC >= 999" in out
    assert out.startswith("```mermaid\n")


def test_heatmap_rejects_negative_threshold(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    with pytest.raises(ValueError, match="threshold must be >= 0"):
        render_complexity_heatmap(
            gid, threshold=-1, cache_root=cache_root
        )


# ---------- render_containment ----------


def test_containment_lists_pair_methods(tier1_graph_id):
    """UniswapV2Pair's containment diagram should list its
    documented methods — swap, mint, burn, _update, etc."""
    gid, cache_root = tier1_graph_id
    out = render_containment(gid, PAIR_ID, cache_root=cache_root)
    assert "class UniswapV2Pair {" in out
    for method in ("swap", "mint", "burn", "_update", "_safeTransfer"):
        assert f"+{method}()" in out


def test_containment_output_is_a_fenced_classdiagram(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    out = render_containment(gid, ERC20_ID, cache_root=cache_root)
    assert out.startswith("```mermaid\n")
    assert "classDiagram" in out
    assert out.rstrip().endswith("```")


def test_containment_unknown_node_raises(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    with pytest.raises(KeyError):
        render_containment(
            gid, "fake:DoesNotExist", cache_root=cache_root
        )


def test_containment_renders_return_type_name_not_dict(tier1_graph_id):
    """Trailmark's `return_type` is a dict like
    `{'name': 'uint', 'module': None, 'generic_args': []}`. The
    renderer must extract `name`, not str()-format the dict."""
    gid, cache_root = tier1_graph_id
    out = render_containment(gid, PAIR_ID, cache_root=cache_root)
    # `burn` returns `uint` in Tier 1 — appears as `+burn() uint`.
    assert "+burn() uint" in out
    # And the raw dict form must NOT appear anywhere.
    assert "'name':" not in out
    assert "generic_args" not in out
