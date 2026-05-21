"""Tests for src/tools.py::run_preanalysis.

Trailmark preanalysis is pure Python (no external analyzer),
so these tests run unconditionally — no slither/solc setup
required, unlike test_slither.py / test_augment_sarif.py.
"""

from pathlib import Path

import pytest

from src.graph.persist import load_graph
from src.tools import run_preanalysis, trailmark_parse

TIER1_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "tier1_uniswap_v2"
)


@pytest.fixture
def tier1_gid(tmp_path):
    """Parse Tier 1 into a fresh tmp cache per test. Hermetic
    — each test gets its own (gid, cache_root); the wrapper's
    save_graph mutations don't leak across tests."""
    cache_root = tmp_path / "cache"
    gid = trailmark_parse(
        str(TIER1_FIXTURE),
        language="solidity",
        cache_root=cache_root,
    )
    return gid, cache_root


def test_run_preanalysis_returns_expected_subgraphs(tier1_gid):
    """Chunk 4.3 success criterion: result contains the
    security-relevant subgraphs. Empirical Tier 1 produces
    `tainted`, `entrypoints`, `entrypoint_reachable`,
    `high_blast_radius`, `privilege_boundary`. The latter two
    may be empty (count=0) on simpler codebases; what matters
    is that they're registered (key present in dict)."""
    gid, cache_root = tier1_gid
    result = run_preanalysis(gid, cache_root=cache_root)

    expected_subgraphs = {
        "tainted",
        "entrypoints",
        "entrypoint_reachable",
        "high_blast_radius",
        "privilege_boundary",
    }
    missing = expected_subgraphs - set(result.keys())
    assert not missing, (
        f"missing subgraphs: {missing}. Got: {list(result)}"
    )


def test_run_preanalysis_shape_is_count_plus_sample_ids(
    tier1_gid,
):
    """Every subgraph entry is {count: int, sample_ids: list[str]}."""
    gid, cache_root = tier1_gid
    result = run_preanalysis(gid, cache_root=cache_root)

    for name, entry in result.items():
        assert set(entry.keys()) == {"count", "sample_ids"}, (
            f"subgraph {name!r} has unexpected shape: {entry}"
        )
        assert isinstance(entry["count"], int)
        assert isinstance(entry["sample_ids"], list)
        # sample_ids never exceeds the count.
        assert len(entry["sample_ids"]) <= entry["count"]


def test_run_preanalysis_populates_sample_ids_for_nonempty(
    tier1_gid,
):
    """Non-empty subgraphs include up to `sample_size` IDs.
    Tier 1's `tainted` has 80 nodes — verifies default
    sample_size=25 caps correctly."""
    gid, cache_root = tier1_gid
    result = run_preanalysis(gid, cache_root=cache_root)

    tainted = result["tainted"]
    assert tainted["count"] >= 1
    assert len(tainted["sample_ids"]) == min(
        tainted["count"], 25
    )
    # IDs are Trailmark node IDs (strings with `:` or `.`).
    for nid in tainted["sample_ids"]:
        assert isinstance(nid, str)
        assert len(nid) > 0


def test_run_preanalysis_handles_zero_node_subgraphs(tier1_gid):
    """Empty subgraphs (Tier 1's high_blast_radius and
    privilege_boundary) return count=0, sample_ids=[]. The
    keys are still present — this is the "subgraph registered
    but empty" case the trailmark-structural skill explicitly
    documents."""
    gid, cache_root = tier1_gid
    result = run_preanalysis(gid, cache_root=cache_root)

    for empty_subgraph in (
        "high_blast_radius",
        "privilege_boundary",
    ):
        entry = result[empty_subgraph]
        assert entry["count"] == 0, (
            f"expected {empty_subgraph} empty on Tier 1; "
            f"got count={entry['count']}"
        )
        assert entry["sample_ids"] == []


def test_run_preanalysis_persists_subgraphs_to_engine(tier1_gid):
    """After the wrapper returns, a fresh load_graph reveals
    the engine has the subgraphs registered (proves
    save_graph happened, not just in-memory mutation).
    Subsequent LLM calls can query them without re-running
    preanalysis."""
    from src.graph.persist import _load_graph_cached

    gid, cache_root = tier1_gid
    run_preanalysis(gid, cache_root=cache_root)

    # Force a fresh load from disk.
    _load_graph_cached.cache_clear()
    engine = load_graph(gid, cache_root=cache_root)
    registered = set(engine.subgraph_names())
    assert "tainted" in registered
    assert "entrypoints" in registered


def test_run_preanalysis_sample_size_caps_ids(tier1_gid):
    """Custom sample_size param overrides the default 25."""
    gid, cache_root = tier1_gid
    result = run_preanalysis(
        gid, sample_size=5, cache_root=cache_root
    )
    assert len(result["tainted"]["sample_ids"]) == 5


def test_run_preanalysis_raises_on_malformed_graph_id(tmp_path):
    """Standard graph_id validation (via load_graph) surfaces
    before any preanalysis work."""
    with pytest.raises(ValueError, match="invalid graph_id"):
        run_preanalysis("not-hex!", cache_root=tmp_path)


def test_run_preanalysis_raises_on_negative_sample_size(tier1_gid):
    """Negative sample_size triggers Python's negative-slice
    semantics on `nodes[:sample_size]` — a 10-node subgraph
    with sample_size=-5 would silently return the first 5
    nodes instead of an empty list or error. Explicit
    validation surfaces the misuse before any work."""
    gid, cache_root = tier1_gid
    with pytest.raises(ValueError, match="sample_size must be >= 0"):
        run_preanalysis(
            gid, sample_size=-1, cache_root=cache_root
        )


def test_run_preanalysis_is_idempotent(tier1_gid):
    """Calling run_preanalysis twice must NOT change subgraph
    counts. Trailmark's preanalysis is internally idempotent
    (verified empirically: counts match exactly across two
    runs on Tier 1). Pins the contract so a future Trailmark
    change that made preanalysis additive would surface here
    instead of silently doubling subgraph membership."""
    gid, cache_root = tier1_gid
    first = run_preanalysis(gid, cache_root=cache_root)
    second = run_preanalysis(gid, cache_root=cache_root)

    assert set(first.keys()) == set(second.keys()), (
        f"second call subgraphs differ: first={sorted(first)}, "
        f"second={sorted(second)}"
    )
    for name in first:
        assert first[name]["count"] == second[name]["count"], (
            f"subgraph {name!r} count drifted: "
            f"first={first[name]['count']}, "
            f"second={second[name]['count']} — likely a "
            f"regression in Trailmark's preanalysis idempotency"
        )
