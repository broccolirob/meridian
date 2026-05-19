import pytest

from src.render.obsidian import render_node_note
from src.tools import get_node


@pytest.fixture
def erc20_contract(tier0_graph_id):
    gid, cache_root = tier0_graph_id
    return get_node(gid, "src.tokens.ERC20:ERC20", cache_root=cache_root)


@pytest.fixture
def transfer_method(tier0_graph_id):
    gid, cache_root = tier0_graph_id
    return get_node(
        gid, "src.tokens.ERC20:ERC20.transfer", cache_root=cache_root
    )


def test_render_is_deterministic(erc20_contract):
    fm1, body1 = render_node_note(erc20_contract, {}, "")
    fm2, body2 = render_node_note(erc20_contract, {}, "")
    assert fm1 == fm2
    assert body1 == body2


def test_render_returns_tuple_of_dict_and_str(erc20_contract):
    result = render_node_note(erc20_contract, {}, "")
    assert isinstance(result, tuple)
    fm, body = result
    assert isinstance(fm, dict)
    assert isinstance(body, str)


def test_body_contains_all_seven_top_level_sections(erc20_contract):
    _, body = render_node_note(erc20_contract, {}, "")
    for heading in [
        "## Overview",
        "## Graph context",
        "## State",
        "## Functions",
        "## Events / Errors / Modifiers",
        "## Annotations",
        "## Risks",
    ]:
        assert heading in body, f"missing heading: {heading}"


def test_empty_graph_ctx_produces_placeholders(erc20_contract):
    _, body = render_node_note(erc20_contract, {}, "")
    assert "_Overview not yet written._" in body
    assert "_No callers in this graph._" in body
    assert "_No annotations yet._" in body
    assert "_No risks recorded._" in body
    assert "Trailmark does not extract" in body


def test_body_overrides_overview_placeholder(erc20_contract):
    overview = "ERC20 implementation with EIP-2612 permit."
    _, body = render_node_note(erc20_contract, {}, overview)
    assert overview in body
    assert "_Overview not yet written._" not in body


def test_graph_ctx_callers_render_as_wikilinks(erc20_contract):
    ctx = {
        "callers": [
            "[[contracts/ERC4626|ERC4626.deposit]]",
            "[[contracts/ERC4626|ERC4626.withdraw]]",
        ]
    }
    _, body = render_node_note(erc20_contract, ctx, "")
    assert "[[contracts/ERC4626|ERC4626.deposit]]" in body
    assert "[[contracts/ERC4626|ERC4626.withdraw]]" in body
    assert "_No callers in this graph._" not in body


def test_frontmatter_has_expected_keys(erc20_contract):
    fm, _ = render_node_note(erc20_contract, {}, "")
    required = {"name", "kind", "node_id", "file", "lines", "loc"}
    assert required <= set(fm.keys())
    assert fm["name"] == "ERC20"
    assert fm["kind"] == "contract"
    assert fm["node_id"] == "src.tokens.ERC20:ERC20"
    assert fm["file"].endswith("ERC20.sol")


def test_method_node_renders_too(transfer_method):
    fm, body = render_node_note(transfer_method, {}, "")
    assert fm["kind"] == "method"
    assert fm["cyclomatic_complexity"] == 1
    assert "## Overview" in body
    assert "## Functions" in body
