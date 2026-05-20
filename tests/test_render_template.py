from pathlib import Path

import pytest

from src.render.obsidian import render_and_write_node_note, render_node_note
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


@pytest.fixture
def stub_graph_dependencies(monkeypatch):
    """Stub the graph-touching helpers used by
    `render_and_write_node_note` so tests can exercise the
    routing/template logic without relying on the silent
    `except Exception` fallbacks in the diagram block (chunk
    3.5) and the disambiguation block (chunk 3.10).

    Pre-3.16 the routing tests passed a fake graph_id and
    depended on both fallbacks firing silently. If a future
    cleanup narrows either `except Exception`, the routing
    tests would all fail without the routing logic itself
    having regressed. This fixture decouples those tests."""
    monkeypatch.setattr(
        "src.render.obsidian.list_nodes",
        lambda *a, **k: [],  # no collisions → bare path
    )
    monkeypatch.setattr(
        "src.render.obsidian.render_inheritance",
        lambda *a, **k: "",  # no-op diagram
    )
    monkeypatch.setattr(
        "src.render.obsidian._pick_primary_method",
        lambda *a, **k: None,  # no primary method → skip call graph
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


# --- render_and_write_node_note (the agent's keystone tool) ---------


def test_render_and_write_returns_string_path(
    erc20_contract, tier0_graph_id, tmp_path
):
    gid, cache_root = tier0_graph_id
    out = render_and_write_node_note(
        tmp_path, gid, erc20_contract, {}, "ov", cache_root=cache_root
    )
    assert isinstance(out, str), "agents expect a string path, not Path"
    assert out.endswith("/contracts/ERC20.md")


@pytest.mark.parametrize(
    "kind,name,expected_subdir",
    [
        ("contract", "Pair", "contracts"),
        ("library", "SafeMath", "libraries"),
        ("interface", "IERC20", "interfaces"),
        ("trait", "Iterator", "interfaces"),
        ("module", "src.tokens.ERC20", "_meta"),
        ("function", "doStuff", "contracts"),
        ("struct", "Reserve", "contracts"),
        ("enum", "Status", "contracts"),
        ("namespace", "Foo", "contracts"),
        ("class", "Auth", "contracts"),
        ("not-a-known-kind", "Mystery", "contracts"),  # default fallback
    ],
)
def test_render_and_write_routes_by_kind(
    kind, name, expected_subdir, tmp_path, stub_graph_dependencies
):
    """KIND_TO_FOLDER routing for every Trailmark node kind.
    Uses `stub_graph_dependencies` so the test exercises the
    real routing logic without depending on the silent
    `except Exception` fallbacks (chunk 3.16, /review I6)."""
    fake_node = {
        "id": f"src.fake:{name}",
        "name": name,
        "kind": kind,
        "location": {
            "file_path": "fake.sol",
            "start_line": 1,
            "end_line": 10,
            "start_col": 0,
            "end_col": 0,
        },
        "parameters": [],
        "return_type": None,
        "exception_types": [],
        "cyclomatic_complexity": None,
        "branches": [],
        "docstring": None,
    }
    out = render_and_write_node_note(
        tmp_path, "abc012345678", fake_node, {}, "ov"
    )
    assert out.endswith(f"/{expected_subdir}/{name}.md")
    assert (tmp_path / expected_subdir / f"{name}.md").exists()


def test_render_and_write_rejects_method_nodes(
    transfer_method, tier0_graph_id, tmp_path
):
    gid, cache_root = tier0_graph_id
    with pytest.raises(ValueError, match="method nodes are documented"):
        render_and_write_node_note(
            tmp_path, gid, transfer_method, {}, "ov",
            cache_root=cache_root,
        )
    # And nothing was written
    assert not any(tmp_path.rglob("*.md"))


def test_render_and_write_produces_canonical_template(
    erc20_contract, tier0_graph_id, tmp_path
):
    gid, cache_root = tier0_graph_id
    out = render_and_write_node_note(
        tmp_path, gid, erc20_contract, {}, "ov", cache_root=cache_root
    )
    text = Path(out).read_text(encoding="utf-8")
    # Frontmatter is canonical (not LLM-invented title/path keys)
    assert text.startswith("---\nname: ERC20\nkind: contract\n")
    # All 7 sections present
    for heading in (
        "## Overview",
        "## Graph context",
        "## State",
        "## Functions",
        "## Events / Errors / Modifiers",
        "## Annotations",
        "## Risks",
    ):
        assert heading in text, f"missing {heading}"


# --- diagram embedding (chunk 3.5) -----------------------------------


def test_render_and_write_embeds_inheritance_diagram(
    erc20_contract, tier0_graph_id, tmp_path
):
    """Every contract note must include an inheritance diagram —
    render_inheritance always produces something (at least a
    `class <Name>` block) so this is guaranteed."""
    gid, cache_root = tier0_graph_id
    out_path = render_and_write_node_note(
        tmp_path, gid, erc20_contract, {}, "ov", cache_root=cache_root
    )
    text = Path(out_path).read_text()
    assert "### Inheritance diagram" in text
    assert "```mermaid" in text
    assert "classDiagram" in text


def test_render_and_write_embeds_call_graph_when_methods_exist(
    erc20_contract, tier0_graph_id, tmp_path
):
    """ERC20 has methods → call graph appears, centered on the
    highest-CC method via _pick_primary_method."""
    gid, cache_root = tier0_graph_id
    out_path = render_and_write_node_note(
        tmp_path, gid, erc20_contract, {}, "ov", cache_root=cache_root
    )
    text = Path(out_path).read_text()
    assert "### Call graph" in text
    assert "graph TD" in text
    # Exactly two fenced mermaid blocks (inheritance + call graph).
    assert text.count("```mermaid") == 2


def test_diagrams_appear_above_link_lists(
    erc20_contract, tier0_graph_id, tmp_path
):
    """Visual structure (diagrams) before text lists. Auditors
    scan diagrams first; the lists are the index."""
    gid, cache_root = tier0_graph_id
    out_path = render_and_write_node_note(
        tmp_path, gid, erc20_contract, {}, "ov", cache_root=cache_root
    )
    text = Path(out_path).read_text()
    inheritance_diag_pos = text.find("### Inheritance diagram")
    inherits_list_pos = text.find("### Inheritance\n")
    call_graph_pos = text.find("### Call graph")
    assert inheritance_diag_pos < call_graph_pos < inherits_list_pos


# --- C2: filename disambiguation on collision (chunk 3.10) -------


def test_node_note_qualifies_filename_on_bare_name_collision(
    monkeypatch, tier0_graph_id, tmp_path
):
    """Two nodes routing to the same folder with the same bare
    name must produce distinct files. Tier 0 has no real
    collisions, so we synthesize one by patching list_nodes."""
    from src.render import obsidian
    from src.render.obsidian import render_and_write_node_note
    from src.tools import get_node

    gid, cache_root = tier0_graph_id
    erc20 = get_node(
        gid, "src.tokens.ERC20:ERC20", cache_root=cache_root
    )
    # Synthesize a second contract that shares ERC20's bare name
    # but lives in a different module. The disambiguation logic
    # only inspects bare name + kind + module, so a minimal dict
    # suffices.
    sibling = {
        "id": "vendored.ERC20:ERC20",
        "name": "ERC20",
        "kind": "contract",
    }

    real_list_nodes = obsidian.list_nodes

    def patched(graph_id, *, kind=None, cache_root=None):
        nodes = real_list_nodes(
            graph_id, kind=kind, cache_root=cache_root
        )
        return nodes + [sibling]

    monkeypatch.setattr(obsidian, "list_nodes", patched)

    out = render_and_write_node_note(
        tmp_path, gid, erc20, {}, "ov", cache_root=cache_root
    )
    # Qualified with the real ERC20's module prefix.
    assert out.endswith("/contracts/src.tokens.ERC20.ERC20.md")
    # And the qualified file actually exists (no silent overwrite).
    assert Path(out).exists()


def test_resolve_wikilink_returns_bare_path_when_no_collision(
    tier0_graph_id,
):
    """Common case: no collision → wikilink uses bare path. This
    pins the regression armor for the no-collision case (Tier
    0/1 production runs)."""
    from src.render.obsidian import resolve_wikilink

    gid, cache_root = tier0_graph_id
    link = resolve_wikilink(
        gid, "src.tokens.ERC20:ERC20", cache_root=cache_root
    )
    # Unchanged from pre-3.10: no qualification because ERC20 is
    # unique among contract-folder nodes in Tier 0.
    assert link == "[[contracts/ERC20|ERC20]]"


# --- silent-fallback regression armor (chunk 3.16, /review I6) ------


def test_render_and_write_logs_warning_when_graph_unavailable(
    erc20_contract, tmp_path, caplog
):
    """When the graph cache file doesn't exist (bad gid), the
    diagram block in `render_and_write_node_note` logs a warning
    and ships the note without diagrams. The disambiguation block
    similarly falls back to the bare path.

    The routing tests above use `stub_graph_dependencies` so they
    DON'T depend on this fallback — but the fallback itself is
    still real behavior we ship. This test pins it independently
    so a future refactor that drops the safety net (e.g.,
    narrowing `except Exception` per /review I17) surfaces here,
    not via mysterious failures in the routing tests."""
    import logging

    caplog.set_level(logging.WARNING, logger="src.render.obsidian")
    out = render_and_write_node_note(
        tmp_path,
        "deadbeef0000",  # valid 12-hex shape, not an actual cache
        erc20_contract,
        {},
        "ov",
    )
    # Note still ships
    assert Path(out).exists()
    assert out.endswith("/contracts/ERC20.md")

    # Warning fired for the diagram failure (chunk 3.5) AND/OR
    # the disambiguation failure (chunk 3.10). Either logged
    # message indicates the fallback engaged.
    warnings = [r.getMessage() for r in caplog.records]
    assert any(
        "diagram computation failed" in m
        or "disambiguation failed" in m
        for m in warnings
    ), f"expected fallback warning; got {warnings}"

    # Note body has NO diagram blocks (fallback worked).
    text = Path(out).read_text()
    assert "```mermaid" not in text
