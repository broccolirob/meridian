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
    """Chunk 3.10: when two nodes share a bare name in the same
    folder, resolve_wikilink must point at the qualified
    filename — otherwise the link breaks for the second node."""
    from src.render import obsidian

    gid, cache_root = tier0_graph_id
    sibling = {
        "id": "vendored.ERC20:ERC20",
        "name": "ERC20",
        "kind": "contract",
    }
    real_list_nodes = obsidian.list_nodes

    def patched(graph_id, *, kind=None, cache_root=None):
        return real_list_nodes(
            graph_id, kind=kind, cache_root=cache_root
        ) + [sibling]

    monkeypatch.setattr(obsidian, "list_nodes", patched)

    link = resolve_wikilink(
        gid, "src.tokens.ERC20:ERC20", cache_root=cache_root
    )
    # Qualified target, bare display label.
    assert link == "[[contracts/src.tokens.ERC20.ERC20|ERC20]]"
