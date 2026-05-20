import pytest

from src.render.obsidian import KIND_TO_FOLDER, resolve_wikilink


def test_contract_wikilink(tier0_graph_id):
    gid, cache_root = tier0_graph_id
    link = resolve_wikilink(
        gid, "src.tokens.ERC20:ERC20", cache_root=cache_root
    )
    assert link == "[[contracts/ERC20|ERC20]]"


def test_library_wikilink(tier0_graph_id):
    gid, cache_root = tier0_graph_id
    link = resolve_wikilink(
        gid,
        "src.utils.FixedPointMathLib:FixedPointMathLib",
        cache_root=cache_root,
    )
    assert link == "[[libraries/FixedPointMathLib|FixedPointMathLib]]"


def test_interface_wikilink(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    link = resolve_wikilink(
        gid,
        "contracts.interfaces.IUniswapV2Pair:IUniswapV2Pair",
        cache_root=cache_root,
    )
    assert link == "[[interfaces/IUniswapV2Pair|IUniswapV2Pair]]"


def test_method_wikilink_targets_parent_contract(tier0_graph_id):
    gid, cache_root = tier0_graph_id
    link = resolve_wikilink(
        gid, "src.tokens.ERC20:ERC20.transfer", cache_root=cache_root
    )
    assert link == "[[contracts/ERC20|ERC20.transfer]]"


def test_module_wikilink(tier0_graph_id):
    gid, cache_root = tier0_graph_id
    link = resolve_wikilink(
        gid, "src.tokens.ERC20", cache_root=cache_root
    )
    assert link == "[[_meta/src.tokens.ERC20|src.tokens.ERC20]]"


def test_resolver_is_stable_under_repeat_calls(tier0_graph_id):
    gid, cache_root = tier0_graph_id
    link1 = resolve_wikilink(
        gid, "src.tokens.ERC20:ERC20.transfer", cache_root=cache_root
    )
    link2 = resolve_wikilink(
        gid, "src.tokens.ERC20:ERC20.transfer", cache_root=cache_root
    )
    assert link1 == link2


def test_unknown_node_raises_keyerror(tier0_graph_id):
    gid, cache_root = tier0_graph_id
    with pytest.raises(KeyError):
        resolve_wikilink(gid, "does.not.exist", cache_root=cache_root)


def test_routing_table_covers_all_trailmark_top_level_kinds():
    expected = {
        "function",
        "class",
        "module",
        "struct",
        "interface",
        "trait",
        "enum",
        "namespace",
        "contract",
        "library",
    }
    assert expected <= set(KIND_TO_FOLDER.keys())


def test_routing_table_does_not_route_method():
    assert "method" not in KIND_TO_FOLDER


def test_resolve_wikilink_qualifies_target_on_collision(
    monkeypatch, tier0_graph_id
):
    """When two nodes share a bare name in the same folder,
    resolve_wikilink must point at the qualified filename —
    otherwise the link breaks for the second node."""
    from src.graph.persist import _load_graph_cached
    from src.render import obsidian

    gid, cache_root = tier0_graph_id
    sibling = {
        "id": "vendored.ERC20:ERC20",
        "name": "ERC20",
        "kind": "contract",
    }
    real_build = obsidian._build_collision_map

    def patched_build(engine):
        collision_map = real_build(engine)
        collision_map.setdefault(
            ("contracts", sibling["name"]), set()
        ).add(sibling["id"])
        return collision_map

    monkeypatch.setattr(
        obsidian, "_build_collision_map", patched_build
    )
    # Force a fresh engine so patched_build actually fires.
    # The injected sibling mutates the cached map on the
    # engine instance; clear again on teardown so the
    # mutation doesn't leak into subsequent tests using
    # the same session-scoped engine.
    _load_graph_cached.cache_clear()
    try:
        link = resolve_wikilink(
            gid, "src.tokens.ERC20:ERC20", cache_root=cache_root
        )
        # Qualified target, bare display label.
        assert link == "[[contracts/src.tokens.ERC20.ERC20|ERC20]]"
    finally:
        _load_graph_cached.cache_clear()


def test_disambiguation_caches_collision_map_per_engine(
    monkeypatch, tier1_graph_id
):
    """Performance regression armor: the collision map must
    be built once per engine instance, not per
    resolve_wikilink call. Without the cache, dispatch over
    N nodes with K hops/note becomes O(N²) — measurable on
    real codebases (Tier 1 ~30 nodes survives; hundreds-of-
    contracts repos grind)."""
    from src.graph.persist import _load_graph_cached
    from src.render import obsidian

    gid, cache_root = tier1_graph_id
    call_count = {"n": 0}
    original = obsidian._build_collision_map

    def counting_build(engine):
        call_count["n"] += 1
        return original(engine)

    monkeypatch.setattr(
        obsidian, "_build_collision_map", counting_build
    )
    # Force a fresh engine so we observe the FIRST build,
    # not a no-op cache hit from a prior test.
    _load_graph_cached.cache_clear()
    for _ in range(10):
        resolve_wikilink(
            gid,
            "contracts.UniswapV2Pair:UniswapV2Pair",
            cache_root=cache_root,
        )
    assert call_count["n"] == 1, (
        f"collision map rebuilt {call_count['n']} times in "
        f"10 resolves — cache broken; fix would result in "
        f"O(N²) dispatch cost on large graphs"
    )


def test_disambiguation_uses_per_engine_collision_map(
    tier0_graph_id, tier1_graph_id,
):
    """Two different engines (different graph_ids → different
    cached instances) get distinct collision maps. Proves the
    lazy-attach is per-instance, not smuggled across engines
    via module-global state."""
    from src.graph.persist import load_graph

    gid0, root0 = tier0_graph_id
    gid1, root1 = tier1_graph_id
    e0 = load_graph(gid0, cache_root=root0)
    e1 = load_graph(gid1, cache_root=root1)

    resolve_wikilink(
        gid0, "src.tokens.ERC20:ERC20", cache_root=root0
    )
    resolve_wikilink(
        gid1,
        "contracts.UniswapV2Pair:UniswapV2Pair",
        cache_root=root1,
    )

    m0 = getattr(e0, "_washable_collision_map", None)
    m1 = getattr(e1, "_washable_collision_map", None)
    assert m0 is not None and m1 is not None
    assert m0 is not m1, (
        "each engine instance must own its own collision "
        "map — sharing one across engines would corrupt "
        "lookups when graphs have different node sets"
    )
    # Tier 0 (ERC4626) has no UniswapV2Pair; Tier 1 does.
    assert ("contracts", "UniswapV2Pair") in m1
    assert ("contracts", "UniswapV2Pair") not in m0
