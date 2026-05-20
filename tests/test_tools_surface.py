from src.tools import (
    attack_surface,
    complexity_hotspots,
    entrypoint_paths_to,
)


# ---------- attack_surface ----------


def test_attack_surface_includes_swap_mint_burn(tier1_graph_id):
    """Chunk 3.6 success criterion: swap, mint, burn appear as
    entrypoints in Tier 1's attack surface."""
    gid, cache_root = tier1_graph_id
    surface = attack_surface(gid, cache_root=cache_root)
    node_ids = {item["node_id"] for item in surface}
    assert "contracts.UniswapV2Pair:UniswapV2Pair.swap" in node_ids
    assert "contracts.UniswapV2Pair:UniswapV2Pair.mint" in node_ids
    assert "contracts.UniswapV2Pair:UniswapV2Pair.burn" in node_ids


def test_attack_surface_items_have_canonical_shape(tier1_graph_id):
    """Items must have the keys our docstring promises so
    callers can rely on them without defensive .get()."""
    gid, cache_root = tier1_graph_id
    surface = attack_surface(gid, cache_root=cache_root)
    assert surface, "Tier 1 must have at least one entrypoint"
    sample = surface[0]
    assert {
        "node_id",
        "trust_level",
        "kind",
        "description",
    } <= sample.keys()


def test_attack_surface_entries_describe_external_public(tier1_graph_id):
    """Trailmark's `description` encodes the visibility
    heuristic — the success criterion's "external/public"
    requirement is implicit in this field."""
    gid, cache_root = tier1_graph_id
    surface = attack_surface(gid, cache_root=cache_root)
    descriptions = {item["description"] for item in surface}
    assert any("external/public" in d for d in descriptions)


def test_attack_surface_excludes_internal_methods(tier1_graph_id):
    """Negative assertion (chunk 3.16, /review I5): Solidity
    `private`/`internal` methods must NOT appear in
    attack_surface. The positive tests above (swap/mint/burn
    present) don't catch a regression where visibility detection
    breaks and Trailmark surfaces every method — they'd still
    pass.

    Internals are confirmed present in the graph (via
    `list_nodes(kind='method')`) before asserting absence from
    the surface, so the negative assertion isn't vacuous."""
    from src.tools import list_nodes

    gid, cache_root = tier1_graph_id
    surface_ids = {
        item["node_id"]
        for item in attack_surface(gid, cache_root=cache_root)
    }
    method_ids = {
        n["id"]
        for n in list_nodes(gid, kind="method", cache_root=cache_root)
    }

    # UniswapV2Pair's private helpers (`_` prefix per Solidity
    # convention). Confirmed real method nodes in Tier 1.
    internal_methods = [
        "contracts.UniswapV2Pair:UniswapV2Pair._update",
        "contracts.UniswapV2Pair:UniswapV2Pair._safeTransfer",
        "contracts.UniswapV2Pair:UniswapV2Pair._mintFee",
    ]
    for nid in internal_methods:
        assert nid in method_ids, (
            f"fixture invariant broken: {nid} expected to exist "
            f"as a method node in Tier 1"
        )
        assert nid not in surface_ids, (
            f"internal method {nid} leaked into attack_surface — "
            f"Trailmark visibility detection regressed?"
        )


def test_attack_surface_count_is_bounded_for_tier1(tier1_graph_id):
    """Sanity bound on surface size. Tier 1 has 73 entrypoints
    (probed) and 91 methods total. Wide bounds catch regressions
    where the surface either collapses to zero or balloons to
    return every method (which the negative-assertion test above
    catches at the per-method level; this catches the aggregate
    failure mode)."""
    gid, cache_root = tier1_graph_id
    surface = attack_surface(gid, cache_root=cache_root)
    # Lower bound: at least the 3 main Pair entrypoints + interface
    # methods. Upper bound: well below the 91 total methods so a
    # "return everything" regression fails this.
    assert 50 <= len(surface) <= 90, (
        f"attack_surface size {len(surface)} outside expected "
        f"Tier 1 range [50, 90] — investigate"
    )


# ---------- entrypoint_paths_to ----------


def test_entrypoint_paths_to_internal_finds_paths(tier1_graph_id):
    """`_update` is called by swap/mint/burn entrypoints —
    Trailmark traces those paths."""
    gid, cache_root = tier1_graph_id
    sink = "contracts.UniswapV2Pair:UniswapV2Pair._update"
    paths = entrypoint_paths_to(gid, sink, cache_root=cache_root)
    assert len(paths) >= 3, (
        f"expected ≥3 paths from entrypoints to _update, got {len(paths)}"
    )
    for path in paths:
        assert path[-1] == sink, f"path doesn't end at sink: {path}"


def test_entrypoint_paths_to_entrypoint_is_empty(tier1_graph_id):
    """`swap` IS an entrypoint — no OTHER entrypoint reaches
    it. Empty list is correct semantics (not an error)."""
    gid, cache_root = tier1_graph_id
    paths = entrypoint_paths_to(
        gid,
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        cache_root=cache_root,
    )
    assert paths == []


# ---------- complexity_hotspots ----------


def test_complexity_hotspots_threshold_4_returns_three(tier1_graph_id):
    """Tier 1 has 3 methods at CC>=4: _mintFee (CC=6), swap
    (CC=4), Math.sqrt (CC=4). Matches the heatmap inventory
    from chunk 3.4."""
    gid, cache_root = tier1_graph_id
    hot = complexity_hotspots(gid, threshold=4, cache_root=cache_root)
    assert len(hot) == 3
    names = {n["name"] for n in hot}
    assert names == {"_mintFee", "swap", "sqrt"}


def test_complexity_hotspots_default_threshold_empty_on_tier1(
    tier1_graph_id,
):
    """Default threshold=10 — Tier 1 caps at CC=6 so no
    hotspots. Pins the default value AND the Tier 1 ceiling
    in one shot."""
    gid, cache_root = tier1_graph_id
    assert complexity_hotspots(gid, cache_root=cache_root) == []


def test_complexity_hotspots_returns_node_dicts(tier1_graph_id):
    """Unlike attack_surface, hotspots are full node dicts —
    callers can render them as graph nodes without extra
    lookups."""
    gid, cache_root = tier1_graph_id
    hot = complexity_hotspots(gid, threshold=4, cache_root=cache_root)
    assert hot
    sample = hot[0]
    assert {
        "id",
        "name",
        "kind",
        "cyclomatic_complexity",
    } <= sample.keys()
