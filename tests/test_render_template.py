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
    tuple doesn't accidentally break routing.

    Round-16 update: also stubs `get_node` (now called at the
    top of `render_and_write_node_note` for the refetch
    defense). Returns the fixture as a dict — tests register
    nodes by id (`stub_graph_dependencies[node['id']] = node`)
    so the refetch returns the test's synthesized dict."""
    nodes_by_id: dict[str, dict] = {}

    def _fake_get_node(_gid, node_id, *, cache_root=None):
        if node_id not in nodes_by_id:
            raise KeyError(node_id)
        return nodes_by_id[node_id]

    monkeypatch.setattr(
        "src.render.obsidian.get_node", _fake_get_node,
    )
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
    return nodes_by_id


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


def test_defang_link_list_item_preserves_canonical_wikilinks():
    """Codex round-14 fix: `_defang_link_list_item` must
    pass through the exact shape produced by
    `resolve_wikilink` so legitimate inheritance / caller
    bullets render with active wikilinks.

    Pin the shapes the writer emits today: bare path,
    pipe-disambiguated display, module-qualified path,
    method-qualified display."""
    from src.render.obsidian import _defang_link_list_item

    for canonical in (
        "[[contracts/Pair|Pair]]",
        "[[contracts/Pair|Pair.swap]]",
        "[[contracts/contracts.A.Vault|Vault]]",
        "[[libraries/SafeMath|SafeMath]]",
        "[[interfaces/IERC20|IERC20]]",
        "[[_meta/src.tokens.ERC20|src.tokens.ERC20]]",
    ):
        assert _defang_link_list_item(canonical) == canonical, (
            f"canonical wikilink defanged: {canonical}"
        )


def test_defang_link_list_item_defangs_injection():
    """Codex round-14 fix: a prompt-injected NodeDocumenter
    could emit raw HTML, vault-escape wikilinks, traversal
    targets, or trailing garbage after a legitimate-looking
    prefix. Each must be defanged (no active wikilink, no
    raw HTML, no line-start markers reachable)."""
    from src.render.obsidian import _defang_link_list_item

    hostile_inputs = [
        # 1. Vault traversal via wikilink.
        "[[../../etc/passwd]]",
        # 2. Trailing markdown injection after legit prefix.
        "[[contracts/Pair|Pair]] ## INJECTED",
        # 3. Raw HTML.
        '<iframe src="https://evil"></iframe>',
        # 4. Heading prefix.
        "## INJECTED HEADING",
        # 5. Bare URL.
        "https://attacker.example/x",
        # 6. Multi-line injection.
        "[[contracts/Pair|Pair]]\n## Pwned\n- bullet",
        # 7. Dangerous scheme.
        "javascript:alert(1)",
        # 8. Code fence.
        "```bash\nrm -rf $HOME\n```",
    ]
    for raw in hostile_inputs:
        out = _defang_link_list_item(raw)
        # No active wikilink targeting traversal.
        assert "[[../" not in out, (
            f"active traversal wikilink survived: {out!r}"
        )
        # No raw HTML tag.
        assert "<iframe" not in out
        # No line-start ## heading (flatten collapsed to one
        # line for non-wikilink content).
        assert "\n## " not in out
        # No active dangerous scheme.
        assert "javascript:" not in out
        # No raw 3-backtick fence.
        assert "```" not in out


def test_graph_ctx_inherits_defangs_injection(erc20_contract):
    """Codex round-14 fix: `graph_ctx["inherits" | ... |
    "callees"]` is LLM-authored. A poisoned NodeDocumenter
    could inject headings, raw HTML, or traversal wikilinks
    into the rendered list. Verify each list defangs
    end-to-end via render_node_note."""
    ctx = {
        "inherits": ["[[../../etc/passwd]]"],
        "implements": ['<iframe src="https://evil"></iframe>'],
        "uses": ["## INJECTED HEADING"],
        "callers": [
            "[[contracts/Pair|Pair]]\n## Trailing pwn\n- evil"
        ],
        "callees": ["javascript:alert(1)"],
    }
    _, body = render_node_note(erc20_contract, ctx, "")
    # No active traversal wikilinks.
    assert "[[../../etc/passwd]]" not in body
    # No raw HTML.
    assert "<iframe" not in body
    # No line-start H2 injection from any list item — only
    # the canonical section headings should be H2.
    import re as _re
    h2 = set(_re.findall(r"(?m)^## .+$", body))
    canonical = {
        "## Overview", "## Graph context", "## State",
        "## Functions", "## Events / Errors / Modifiers",
        "## Annotations", "## Risks",
    }
    assert h2 == canonical, (
        f"non-canonical H2 survived: {h2 - canonical}"
    )
    # No active dangerous scheme.
    assert "javascript:alert" not in body
    # The defanged form of the legit-prefix item must still
    # render (proves the multi-line attacker payload was
    # flattened on failed-match path, not silently dropped).
    assert "Trailing pwn" in body


def test_render_functions_defangs_wikilink_signature_docstring(
    erc20_contract,
):
    """Codex round-14 fix: `_render_functions` interpolates
    `wikilink`, `signature`, and `docstring` from each
    function entry — all LLM-authored. Each field must be
    defanged in its own rendering context (wikilink whitelist,
    inline-code backtick-escape, italic-prose flatten)."""
    ctx = {
        "functions": [
            {
                "name": "evil",
                "visibility": "external",
                # Hostile wikilink — not the resolve_wikilink
                # shape, must defang.
                "wikilink": (
                    "[[../../etc/passwd]] "
                    '<iframe src="https://evil"></iframe>'
                ),
                # Backtick + line-start heading injection
                # inside an inline-code-span context.
                "signature": (
                    "function evil() `\n## Pwned heading\n"
                    "[[../../escape]]"
                ),
                "cyclomatic_complexity": 1,
                "callers_count": 0,
                "callees_count": 0,
                # Multi-line italic docstring with heading +
                # wikilink injection.
                "docstring": (
                    "First line of doc.\n## INJECTED\n"
                    "[[../../etc/passwd]]"
                ),
            },
        ],
    }
    _, body = render_node_note(erc20_contract, ctx, "")
    # No active traversal wikilink anywhere.
    assert "[[../../etc/passwd]]" not in body
    assert "[[../../escape]]" not in body
    # No raw HTML.
    assert "<iframe" not in body
    # No line-start H2 injection.
    import re as _re
    h2 = set(_re.findall(r"(?m)^## .+$", body))
    canonical = {
        "## Overview", "## Graph context", "## State",
        "## Functions", "## Events / Errors / Modifiers",
        "## Annotations", "## Risks",
    }
    assert h2 == canonical, (
        f"non-canonical H2 from functions section: "
        f"{h2 - canonical}"
    )
    # Inline-code-span backtick injection neutralized: the
    # embedded backtick rendered as HTML entity, so the
    # signature span is still one contiguous code span.
    assert "&#x60;" in body
    # The signature MUST be wrapped in a code span — check
    # the literal `function evil()` substring appears inside
    # backticks somewhere on a Functions-section bullet.
    assert "`function evil()" in body


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
    # Round-16 refetch contract: register the synthesized
    # node so the stubbed `get_node` returns it. With this
    # registration, the test exercises the same routing
    # logic as before — the refetch returns the registered
    # dict, and routing flows through normally.
    stub_graph_dependencies[fake_node["id"]] = fake_node
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

    from src.graph.persist import _load_graph_cached

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
    # Force a fresh engine so patched_build fires. The
    # injected sibling mutates the cached map on the engine
    # instance; clear again on teardown so the mutation
    # doesn't leak into subsequent tests using the same
    # session-scoped engine.
    _load_graph_cached.cache_clear()
    try:
        out = render_and_write_node_note(
            tmp_path, gid, erc20, {}, "ov", cache_root=cache_root
        )
        # Qualified with the real ERC20's module prefix.
        assert out.endswith("/contracts/src.tokens.ERC20.ERC20.md")
        # And the qualified file actually exists (no silent overwrite).
        assert Path(out).exists()
    finally:
        _load_graph_cached.cache_clear()


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


def test_render_and_write_node_note_refetches_node_discarding_forged_fields(
    tier0_graph_id, tmp_path,
):
    """Codex round-16 fix: the LLM-supplied `node` dict is
    treated as an ID carrier only. Forged name/kind/location
    fields are silently overridden by the canonical refetch
    via `get_node(graph_id, node["id"], cache_root=...)`.

    Reproduced exploit: a prompt-injected NodeDocumenter
    supplies `node["name"]="../risks/node-pwn"` (valid id,
    forged name). Pre-fix, this flowed through
    `_disambiguated_path` into `rel_path
    contracts/../risks/node-pwn.md` which `resolve()`d to
    `vault/risks/node-pwn.md` — INSIDE the vault, so
    `write_obsidian_note`'s containment check passed. The
    attacker got an in-vault arbitrary-write primitive.

    Post-fix: refetch returns the canonical ERC20 dict; the
    forged name is discarded; the file lands at the canonical
    path. Belt-and-suspenders: even if a future change
    bypasses refetch, `write_obsidian_note`'s `..`-segment
    reject catches the rel_path."""
    gid, cache_root = tier0_graph_id
    forged_node = {
        # Valid id — refetch will succeed.
        "id": "src.tokens.ERC20:ERC20",
        # Malicious forged fields.
        "name": "../risks/node-pwn",
        "kind": "contract",
        "location": {
            "file_path": "FAKE.sol",
            "start_line": 0,
            "end_line": 999,
            "start_col": 0,
            "end_col": 0,
        },
        "parameters": [],
        "return_type": None,
        "exception_types": [],
        "cyclomatic_complexity": 9999,
        "branches": [],
        "docstring": None,
    }
    out = render_and_write_node_note(
        tmp_path, gid, forged_node, {}, "ov",
        cache_root=cache_root,
    )
    # Canonical name wins — file lands at the legit path.
    assert out.endswith("/contracts/ERC20.md")
    # Nothing landed under `risks/`.
    assert not (tmp_path / "risks").exists(), (
        f"forged name escaped into risks/: "
        f"{list((tmp_path / 'risks').iterdir())}"
    )
    # Frontmatter uses the canonical name, not the forged
    # one — even though YAML safe_dump would have quoted the
    # `../risks/...` string, having it in the frontmatter
    # is auditor-misleading. Pin that the refetch wins.
    body = Path(out).read_text()
    assert "name: ERC20" in body
    assert "../risks/node-pwn" not in body
    # The forged location/cc didn't reach the rendered note.
    assert "FAKE.sol" not in body
    assert "9999" not in body


def test_render_and_write_node_note_belt_and_suspenders_rel_path_reject():
    """If a future change ever lets a `..`-bearing rel_path
    reach `write_obsidian_note`, the `..`-segment reject
    fires as the catch-all. This pins the second layer of
    the round-16 defense independent of the refetch."""
    from src.render.obsidian import write_obsidian_note

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        vault = Path(d) / "vault"
        vault.mkdir()
        # Simulate a `_disambiguated_path` that produced
        # `contracts/../risks/pwn.md` — refetch was bypassed
        # somehow, this is the last gate.
        with pytest.raises(ValueError, match="traversal segment"):
            write_obsidian_note(
                vault, "contracts/../risks/pwn.md", {}, "x",
            )
        assert not (vault / "risks").exists()


def test_render_and_write_strips_llm_supplied_diagram_fields(
    erc20_contract, tier0_graph_id, tmp_path, monkeypatch,
):
    """Codex round-15 fix: an LLM-supplied
    `graph_ctx["inheritance_mermaid"]` / `["call_graph_mermaid"]`
    must NEVER reach the rendered note. The renderer pops both
    keys before invoking the trusted Mermaid generators; if
    generation fails, the keys stay absent, so a hostile
    Mermaid payload can't fall through.

    Round-16: real graph_id so the round-16 refetch defense
    succeeds; monkeypatch `render_inheritance` to raise so
    the diagram-block fallback path engages. The pre-try
    strip + the failure-path strip together ensure the LLM's
    hostile Mermaid never reaches the file."""
    gid, cache_root = tier0_graph_id

    def _raise(*args, **kwargs):
        raise OSError("simulated diagram-generation failure")

    monkeypatch.setattr(
        "src.render.obsidian.render_inheritance", _raise,
    )
    malicious_mermaid = (
        "graph TD\n"
        "  A --> B\n"
        "## INJECTED DIAGRAM HEADING\n"
        "[[../../etc/passwd]]\n"
        '<iframe src="https://evil"></iframe>\n'
    )
    malicious_call_graph = (
        "## ANOTHER INJECTED HEADING\n"
        "[[../../secrets/db]]\n"
        '<script>alert(1)</script>\n'
    )
    out = render_and_write_node_note(
        tmp_path,
        gid,
        erc20_contract,
        {
            "inheritance_mermaid": malicious_mermaid,
            "call_graph_mermaid": malicious_call_graph,
        },
        "ov",
        cache_root=cache_root,
    )
    body = Path(out).read_text()

    # None of the malicious markers survived.
    assert "INJECTED DIAGRAM HEADING" not in body
    assert "ANOTHER INJECTED HEADING" not in body
    assert "[[../../etc/passwd]]" not in body
    assert "[[../../secrets/db]]" not in body
    assert "<iframe" not in body
    assert "<script>" not in body

    # No diagram subsections at all (regen failed; LLM
    # strings discarded).
    assert "### Inheritance diagram" not in body
    assert "### Call graph" not in body
    # No mermaid fence reached the note either.
    assert "```mermaid" not in body


def test_render_and_write_raises_when_graph_unavailable(
    erc20_contract, tmp_path,
):
    """Codex round-16 contract change: the refetch defense
    REQUIRES the graph to be loadable. A missing cache file
    (bad gid) raises `FileNotFoundError` rather than degrading
    to "ship a note without diagrams".

    Why the contract had to change: the prior degradation
    path used the LLM-supplied `node` dict verbatim when the
    graph couldn't be loaded. An attacker who forged
    `node["name"]="../risks/node-pwn"` AND a fake graph_id
    `"deadbeef0000"` (intentionally not in cache) would hit
    the fallback path, where the forged name flows into
    `_disambiguated_path` and gives an in-vault arbitrary-
    write primitive. Strict-raise closes that.

    Tests that previously exercised the fallback have been
    rewritten to use `tier0_graph_id` (real graph) +
    monkeypatched `render_inheritance` / `_disambiguated_path`
    to simulate the specific failure they pin."""
    with pytest.raises(FileNotFoundError, match="graph not in cache"):
        render_and_write_node_note(
            tmp_path,
            "deadbeef0000",  # valid 12-hex shape, not an actual cache
            erc20_contract,
            {},
            "ov",
        )
    # Nothing was written — the refetch raised before
    # disambiguation or write_obsidian_note ran.
    assert not any(tmp_path.rglob("*.md"))


# --- narrowed-except regression armor --------------------------------


def test_diagram_block_propagates_coding_bugs(
    erc20_contract, tier0_graph_id, tmp_path, monkeypatch
):
    """The diagram block's narrow except catches expected
    graph-lookup failures. TypeError from a coding bug must
    propagate — a broad `except Exception` would swallow it
    silently and ship a diagram-less note, hiding the
    regression.

    Round-16: uses `tier0_graph_id` (real graph) so the
    pre-diagram refetch succeeds; the monkeypatch ensures
    the FAILURE we're pinning fires INSIDE the diagram block."""
    gid, cache_root = tier0_graph_id

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
            gid,
            erc20_contract,
            {},
            "ov",
            cache_root=cache_root,
        )


def test_disambiguation_block_propagates_coding_bugs(
    erc20_contract, tier0_graph_id, tmp_path, monkeypatch
):
    """Same narrowing principle applied to the disambiguation
    block. AttributeError from a coding bug in
    `_disambiguated_path` propagates instead of silently falling
    back to the bare path.

    Round-16: uses real graph so refetch succeeds; the
    monkeypatched `_disambiguated_path` fires the failure
    we pin."""
    gid, cache_root = tier0_graph_id

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
            gid,
            erc20_contract,
            {},
            "ov",
            cache_root=cache_root,
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
    erc20_contract, tier0_graph_id, tmp_path, monkeypatch, exc
):
    """The diagram block's narrow-except tuple must include
    legitimate filesystem failures from load_graph: EOFError
    (torn pickle), pickle.UnpicklingError (corrupted cache),
    OSError (NFS hiccup, permission denied). These should
    degrade gracefully (note ships without diagrams), not
    propagate up and abort the entire writer.

    Round-16: real graph for the refetch; monkeypatch
    `render_inheritance` to raise the cache-layer failure
    AFTER refetch succeeds — that's what the diagram block's
    except tuple is for."""
    gid, cache_root = tier0_graph_id

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
        gid,
        erc20_contract,
        {},
        "ov",
        cache_root=cache_root,
    )
    assert Path(out).exists()
    assert "```mermaid" not in Path(out).read_text()


def test_diagram_block_recovers_from_unpickling_error(
    erc20_contract, tier0_graph_id, tmp_path, monkeypatch
):
    """pickle.UnpicklingError variant — separate (not
    parametrized) because pickle isn't a top-level test-file
    import.

    Round-16: real graph for refetch."""
    import pickle

    gid, cache_root = tier0_graph_id

    def _raise(*args, **kwargs):
        raise pickle.UnpicklingError("simulated UnpicklingError")

    monkeypatch.setattr("src.render.obsidian.render_inheritance", _raise)
    monkeypatch.setattr(
        "src.render.obsidian.list_nodes",
        lambda *a, **k: [],
    )

    out = render_and_write_node_note(
        tmp_path,
        gid,
        erc20_contract,
        {},
        "ov",
        cache_root=cache_root,
    )
    assert Path(out).exists()
    assert "```mermaid" not in Path(out).read_text()


@pytest.mark.parametrize(
    "exc",
    [EOFError, OSError],
    ids=["EOFError", "OSError"],
)
def test_disambiguation_block_recovers_from_load_graph_failures(
    erc20_contract, tier0_graph_id, tmp_path, monkeypatch, exc
):
    """Same widening for the filename-disambiguation block —
    if `_disambiguated_path` raises an OSError / EOFError /
    UnpicklingError (via its `list_nodes` → `load_graph` call
    chain), fall back to the bare path rather than aborting
    the writer.

    Round-16: real graph for refetch."""
    gid, cache_root = tier0_graph_id

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
        gid,
        erc20_contract,
        {},
        "ov",
        cache_root=cache_root,
    )
    assert Path(out).exists()
    assert out.endswith("/contracts/ERC20.md")


def test_disambiguation_block_recovers_from_unpickling_error(
    erc20_contract, tier0_graph_id, tmp_path, monkeypatch
):
    """pickle.UnpicklingError variant for the disambiguation
    block.

    Round-16: real graph for refetch."""
    import pickle

    gid, cache_root = tier0_graph_id

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
        gid,
        erc20_contract,
        {},
        "ov",
        cache_root=cache_root,
    )
    assert Path(out).exists()
    assert out.endswith("/contracts/ERC20.md")
