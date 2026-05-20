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
    routing/template logic without depending on the diagram
    block's or disambiguation block's narrow-except fallbacks.

    Decouples routing tests from the graph-loading code path,
    so a future cleanup that narrows or widens either except
    tuple doesn't accidentally break routing."""
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
    fallbacks in the diagram or disambiguation blocks."""
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


# --- diagram embedding -----------------------------------------------


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


# --- filename disambiguation on collision ----------------------------


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


# --- silent-fallback regression armor --------------------------------


def test_render_and_write_logs_warning_when_graph_unavailable(
    erc20_contract, tmp_path, caplog
):
    """When the graph cache file doesn't exist (bad gid), the
    diagram block in `render_and_write_node_note` logs a warning
    and ships the note without diagrams. The disambiguation block
    similarly falls back to the bare path.

    The routing tests above use `stub_graph_dependencies` so
    they DON'T depend on this fallback — but the fallback
    itself is still real behavior we ship. This test pins it
    independently so a future refactor that drops the safety
    net surfaces here, not via mysterious routing-test failures."""
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

    # Warning fired for the diagram failure AND/OR the
    # disambiguation failure. Either logged message indicates
    # the fallback engaged.
    warnings = [r.getMessage() for r in caplog.records]
    assert any(
        "diagram computation failed" in m
        or "disambiguation failed" in m
        for m in warnings
    ), f"expected fallback warning; got {warnings}"

    # Note body has NO diagram blocks (fallback worked).
    text = Path(out).read_text()
    assert "```mermaid" not in text


# --- narrowed-except regression armor --------------------------------


def test_diagram_block_propagates_coding_bugs(
    erc20_contract, tmp_path, monkeypatch
):
    """The diagram block's narrow except catches expected
    graph-lookup failures. TypeError from a coding bug must
    propagate — a broad `except Exception` would swallow it
    silently and ship a diagram-less note, hiding the
    regression."""

    def _raise_type_error(*args, **kwargs):
        raise TypeError("simulated coding bug in render_inheritance")

    monkeypatch.setattr(
        "src.render.obsidian.render_inheritance",
        _raise_type_error,
    )
    # Stub disambiguation so it doesn't ALSO fail (we want to
    # isolate the diagram block's behavior).
    monkeypatch.setattr(
        "src.render.obsidian.list_nodes",
        lambda *a, **k: [],
    )

    with pytest.raises(TypeError, match="simulated coding bug"):
        render_and_write_node_note(
            tmp_path,
            "abc123456789",
            erc20_contract,
            {},
            "ov",
        )


def test_disambiguation_block_propagates_coding_bugs(
    erc20_contract, tmp_path, monkeypatch
):
    """Same narrowing principle applied to the disambiguation
    block. AttributeError from a coding bug in
    `_disambiguated_path` propagates instead of silently falling
    back to the bare path."""

    def _raise_attribute_error(*args, **kwargs):
        raise AttributeError("simulated coding bug in _disambiguated_path")

    # Stub the diagram block dependencies so we isolate the
    # disambiguation block's behavior.
    monkeypatch.setattr(
        "src.render.obsidian.render_inheritance",
        lambda *a, **k: "",
    )
    monkeypatch.setattr(
        "src.render.obsidian._pick_primary_method",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "src.render.obsidian._disambiguated_path",
        _raise_attribute_error,
    )

    with pytest.raises(AttributeError, match="simulated coding bug"):
        render_and_write_node_note(
            tmp_path,
            "abc123456789",
            erc20_contract,
            {},
            "ov",
        )


# --- _pick_primary_method direct unit tests --------------------------
#
# Only exercised indirectly through the diagram block in
# `render_and_write_node_note`. Logic bugs in `_pick_primary_method`
# that don't raise (e.g., a wrong tiebreak direction) would
# silently pick the wrong method without any other test failing.
# Direct tests on a synthetic `list_nodes` response give
# explicit control of CC values, names, and the ID-prefix
# relationship.


def test_pick_primary_method_returns_none_when_no_methods(monkeypatch):
    """Empty list_nodes result → None. A container with no
    methods (interface stub, library-only, or a synthetic node
    in a test fixture) is the most common None path."""
    from src.render.obsidian import _pick_primary_method

    monkeypatch.setattr(
        "src.render.obsidian.list_nodes",
        lambda *a, **k: [],
    )
    assert _pick_primary_method("abc012345678", "src.X:X") is None


def test_pick_primary_method_returns_none_when_no_id_prefix_match(
    monkeypatch,
):
    """list_nodes returns methods but NONE start with
    `container_id.` — none belong to this container. This
    catches the wrong-prefix bug class (e.g., returning a
    method of a DIFFERENT contract that happens to be in the
    list)."""
    from src.render.obsidian import _pick_primary_method

    # Methods belong to a DIFFERENT contract (different module).
    monkeypatch.setattr(
        "src.render.obsidian.list_nodes",
        lambda *a, **k: [
            {
                "id": "src.Y:Y.foo",
                "name": "foo",
                "cyclomatic_complexity": 5,
                "kind": "method",
            },
            {
                "id": "src.Y:Y.bar",
                "name": "bar",
                "cyclomatic_complexity": 3,
                "kind": "method",
            },
        ],
    )
    assert _pick_primary_method("abc012345678", "src.X:X") is None


def test_pick_primary_method_picks_highest_cc(monkeypatch):
    """Highest cyclomatic_complexity wins. Pins the primary
    sort direction (descending)."""
    from src.render.obsidian import _pick_primary_method

    monkeypatch.setattr(
        "src.render.obsidian.list_nodes",
        lambda *a, **k: [
            {
                "id": "src.X:X.simple",
                "name": "simple",
                "cyclomatic_complexity": 1,
                "kind": "method",
            },
            {
                "id": "src.X:X.complex",
                "name": "complex",
                "cyclomatic_complexity": 8,
                "kind": "method",
            },
            {
                "id": "src.X:X.medium",
                "name": "medium",
                "cyclomatic_complexity": 4,
                "kind": "method",
            },
        ],
    )
    assert (
        _pick_primary_method("abc012345678", "src.X:X")
        == "src.X:X.complex"
    )


def test_pick_primary_method_tiebreaks_by_name_ascending(monkeypatch):
    """Equal CC → alphabetical by name (ascending). Pins the
    tiebreak direction — a wrong tiebreak would silently
    change which method shows up in the call graph diagram."""
    from src.render.obsidian import _pick_primary_method

    monkeypatch.setattr(
        "src.render.obsidian.list_nodes",
        lambda *a, **k: [
            {
                "id": "src.X:X.zebra",
                "name": "zebra",
                "cyclomatic_complexity": 5,
                "kind": "method",
            },
            {
                "id": "src.X:X.apple",
                "name": "apple",
                "cyclomatic_complexity": 5,
                "kind": "method",
            },
            {
                "id": "src.X:X.mango",
                "name": "mango",
                "cyclomatic_complexity": 5,
                "kind": "method",
            },
        ],
    )
    # `apple` wins on alphabetical tiebreak.
    assert (
        _pick_primary_method("abc012345678", "src.X:X")
        == "src.X:X.apple"
    )


def test_pick_primary_method_treats_missing_or_none_cc_as_zero(
    monkeypatch,
):
    """Trailmark sometimes omits `cyclomatic_complexity` or
    sets it to None for trivial methods (getters, fallbacks).
    The sort key uses `m.get("cyclomatic_complexity") or 0`,
    so missing/None methods rank lowest. This test exercises
    BOTH the `.get()` default AND the `or 0` (None coalesce)
    paths — a refactor that switched to `m["cyclomatic_complexity"]`
    would KeyError on missing fields; a refactor that dropped
    the `or 0` would TypeError on None comparisons."""
    from src.render.obsidian import _pick_primary_method

    monkeypatch.setattr(
        "src.render.obsidian.list_nodes",
        lambda *a, **k: [
            # Missing CC field entirely.
            {
                "id": "src.X:X.no_cc",
                "name": "no_cc",
                "kind": "method",
            },
            # Explicit None CC.
            {
                "id": "src.X:X.none_cc",
                "name": "none_cc",
                "cyclomatic_complexity": None,
                "kind": "method",
            },
            # Real CC=1.
            {
                "id": "src.X:X.real",
                "name": "real",
                "cyclomatic_complexity": 1,
                "kind": "method",
            },
        ],
    )
    # `real` wins (CC=1 > 0); the other two tie at 0 and would
    # have been tiebroken alphabetically if `real` weren't there.
    assert (
        _pick_primary_method("abc012345678", "src.X:X")
        == "src.X:X.real"
    )


def test_pick_primary_method_requires_dot_separator(monkeypatch):
    """ID-prefix match requires the trailing `.` — a method
    of `src.XY:XY` does NOT belong to `src.X:X` even though
    the ID starts with `src.X`. Pins the off-by-one mistake
    that startswith() would make without the explicit dot."""
    from src.render.obsidian import _pick_primary_method

    monkeypatch.setattr(
        "src.render.obsidian.list_nodes",
        lambda *a, **k: [
            # Same module prefix, different contract.
            # `src.XY:XY.foo`.startswith("src.X:X.") is False
            # (good); the dot guard does its job.
            {
                "id": "src.XY:XY.foo",
                "name": "foo",
                "cyclomatic_complexity": 10,
                "kind": "method",
            },
        ],
    )
    assert _pick_primary_method("abc012345678", "src.X:X") is None


# --- widened-except for legitimate cache failures --------------------


@pytest.mark.parametrize(
    "exc",
    [EOFError, OSError],
    ids=["EOFError", "OSError"],
)
def test_diagram_block_recovers_from_load_graph_failures(
    erc20_contract, tmp_path, monkeypatch, exc
):
    """The diagram block's narrow-except tuple must include
    legitimate filesystem failures from load_graph: EOFError
    (torn pickle), pickle.UnpicklingError (corrupted cache),
    OSError (NFS hiccup, permission denied). These should
    degrade gracefully (note ships without diagrams), not
    propagate up and abort the entire writer."""
    def _raise(*args, **kwargs):
        raise exc(f"simulated {exc.__name__}")

    monkeypatch.setattr("src.render.obsidian.render_inheritance", _raise)
    # Stub disambiguation so it doesn't ALSO fail.
    monkeypatch.setattr(
        "src.render.obsidian.list_nodes",
        lambda *a, **k: [],
    )

    out = render_and_write_node_note(
        tmp_path,
        "abc123456789",
        erc20_contract,
        {},
        "ov",
    )
    assert Path(out).exists()
    assert "```mermaid" not in Path(out).read_text()


def test_diagram_block_recovers_from_unpickling_error(
    erc20_contract, tmp_path, monkeypatch
):
    """pickle.UnpicklingError variant — separate (not
    parametrized) because pickle isn't a top-level test-file
    import."""
    import pickle

    def _raise(*args, **kwargs):
        raise pickle.UnpicklingError("simulated UnpicklingError")

    monkeypatch.setattr("src.render.obsidian.render_inheritance", _raise)
    monkeypatch.setattr(
        "src.render.obsidian.list_nodes",
        lambda *a, **k: [],
    )

    out = render_and_write_node_note(
        tmp_path,
        "abc123456789",
        erc20_contract,
        {},
        "ov",
    )
    assert Path(out).exists()
    assert "```mermaid" not in Path(out).read_text()


@pytest.mark.parametrize(
    "exc",
    [EOFError, OSError],
    ids=["EOFError", "OSError"],
)
def test_disambiguation_block_recovers_from_load_graph_failures(
    erc20_contract, tmp_path, monkeypatch, exc
):
    """Same widening for the filename-disambiguation block —
    if `_disambiguated_path` raises an OSError / EOFError /
    UnpicklingError (via its `list_nodes` → `load_graph` call
    chain), fall back to the bare path rather than aborting
    the writer."""
    def _raise(*args, **kwargs):
        raise exc(f"simulated {exc.__name__}")

    monkeypatch.setattr(
        "src.render.obsidian.render_inheritance",
        lambda *a, **k: "",
    )
    monkeypatch.setattr(
        "src.render.obsidian._pick_primary_method",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "src.render.obsidian._disambiguated_path",
        _raise,
    )

    out = render_and_write_node_note(
        tmp_path,
        "abc123456789",
        erc20_contract,
        {},
        "ov",
    )
    assert Path(out).exists()
    assert out.endswith("/contracts/ERC20.md")


def test_disambiguation_block_recovers_from_unpickling_error(
    erc20_contract, tmp_path, monkeypatch
):
    """pickle.UnpicklingError variant for the disambiguation
    block."""
    import pickle

    def _raise(*args, **kwargs):
        raise pickle.UnpicklingError("simulated UnpicklingError")

    monkeypatch.setattr(
        "src.render.obsidian.render_inheritance",
        lambda *a, **k: "",
    )
    monkeypatch.setattr(
        "src.render.obsidian._pick_primary_method",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "src.render.obsidian._disambiguated_path",
        _raise,
    )

    out = render_and_write_node_note(
        tmp_path,
        "abc123456789",
        erc20_contract,
        {},
        "ov",
    )
    assert Path(out).exists()
    assert out.endswith("/contracts/ERC20.md")
