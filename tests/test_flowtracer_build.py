from pathlib import Path

import pytest

from src.subagents import FLOW_TRACER_SUBAGENT


def test_subagent_has_required_keys():
    required = {"name", "description", "system_prompt", "tools"}
    assert required <= set(FLOW_TRACER_SUBAGENT.keys())
    assert FLOW_TRACER_SUBAGENT["name"] == "flow-tracer"
    assert len(FLOW_TRACER_SUBAGENT["description"]) > 50
    assert len(FLOW_TRACER_SUBAGENT["system_prompt"]) > 500


def test_subagent_tools_are_callable():
    tools = FLOW_TRACER_SUBAGENT["tools"]
    assert len(tools) >= 5
    for tool in tools:
        assert callable(tool), f"not callable: {tool!r}"


def test_subagent_tools_cover_required_capabilities():
    tool_names = {t.__name__ for t in FLOW_TRACER_SUBAGENT["tools"]}
    # Graph queries the prompt instructs the LLM to call
    assert "get_node" in tool_names
    assert "paths_between" in tool_names
    assert "reachable_from" in tool_names
    # Wikilink resolution (kept for future hop-narration)
    assert "resolve_wikilink" in tool_names
    # Combined render+write (the ONLY way to persist a flow note)
    assert "render_and_write_flow_note" in tool_names
    # Separable render/write must NOT be on the list — same
    # chunk 1.5 anti-pattern discipline as NodeDocumenter.
    assert "render_sequence" not in tool_names
    assert "write_obsidian_note" not in tool_names


def test_render_and_write_flow_note_rejects_fabricated_edges(
    tier1_graph_id, tmp_path, caplog,
):
    """Codex round-17 fix: even when every hop in a path is a
    REAL graph node, a fabricated edge (one where `(src, dst)`
    is not a real callees_of edge) must be rejected with a
    placeholder. Pre-fix, `render_sequence` was explicitly
    path-agnostic — it would draw any edge sequence the LLM
    supplied, including misleading edges that look
    authoritative in the rendered note.

    Reproduces the Codex repro:
      `[UniswapV2Pair.swap, UniswapV2ERC20.constructor]`
    where both nodes exist in the tier1 graph but `swap`
    doesn't call the constructor."""
    import logging

    from src.render.obsidian import render_and_write_flow_note
    from src.tools import callees_of, get_node, list_nodes

    gid, cache_root = tier1_graph_id
    swap_id = "contracts.UniswapV2Pair:UniswapV2Pair.swap"
    swap = get_node(gid, swap_id, cache_root=cache_root)

    # Find a real method node that swap does NOT call. We need
    # both nodes to exist (so the "hop not in graph" check
    # doesn't fire) but the edge to be fabricated.
    methods = list_nodes(gid, kind="method", cache_root=cache_root)
    swap_callees = {
        c["id"] for c in callees_of(gid, swap_id, cache_root=cache_root)
    }
    # Pick any method ≠ swap and not in swap's callees.
    non_callee = next(
        m for m in methods
        if m["id"] != swap_id and m["id"] not in swap_callees
    )
    fabricated_path = [swap_id, non_callee["id"]]

    caplog.set_level(logging.WARNING, logger="src.render.obsidian")
    out = render_and_write_flow_note(
        tmp_path,
        gid,
        swap,
        paths=[fabricated_path],
        overview="testing fabricated-edge rejection",
        cache_root=cache_root,
    )
    text = Path(out).read_text()

    # The fabricated path was rejected — no sequence diagram.
    assert "sequenceDiagram" not in text, (
        f"render_sequence ran on a fabricated edge: {text}"
    )
    assert "Path rejected" in text
    assert "not a real call edge" in text
    # The hop list was also short-circuited (would still
    # mislead).
    assert "**Hops:**" not in text
    # Warning fired with diagnostic detail.
    warnings = [r.getMessage() for r in caplog.records]
    assert any(
        "rejecting invalid flow path 1" in m
        and "not a real call edge" in m
        for m in warnings
    ), f"expected fabricated-edge warning; got {warnings}"


def test_render_and_write_flow_note_handles_empty_inner_path(
    tier1_graph_id, tmp_path,
):
    """Codex round-18 fix: `paths=[[]]` (a single empty list)
    used to crash with IndexError because `path[-1]` ran
    BEFORE `_validate_flow_path`. The validator had an
    explicit empty-path branch that was unreachable.

    Post-fix: validation runs first; the empty path is
    rejected with a generic placeholder; the note still
    ships. A prompt-injected FlowTracer that emits a
    malformed `paths` list can no longer fail the whole
    flow note."""
    from src.render.obsidian import render_and_write_flow_note
    from src.tools import get_node

    gid, cache_root = tier1_graph_id
    swap = get_node(
        gid,
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        cache_root=cache_root,
    )
    # Single empty inner path — the exact crash repro.
    out = render_and_write_flow_note(
        tmp_path,
        gid,
        swap,
        paths=[[]],
        overview="testing empty-path rejection",
        cache_root=cache_root,
    )
    text = Path(out).read_text()
    # Note shipped with a placeholder for the rejected path.
    assert "### Path 1 — invalid path" in text
    assert "Path rejected" in text
    assert "empty path" in text
    # No sequence diagram, no hop list.
    assert "sequenceDiagram" not in text
    assert "**Hops:**" not in text


def test_render_and_write_flow_note_rejects_path_with_wrong_start(
    tier1_graph_id, tmp_path,
):
    """Codex round-17 fix: a flow path whose `path[0]` isn't
    the dispatcher's bound entrypoint must be rejected. This
    catches the variant where an LLM, having received a task
    to trace entrypoint X, supplies a path starting at Y."""
    from src.render.obsidian import render_and_write_flow_note
    from src.tools import get_node

    gid, cache_root = tier1_graph_id
    swap_id = "contracts.UniswapV2Pair:UniswapV2Pair.swap"
    swap = get_node(gid, swap_id, cache_root=cache_root)

    # Path that doesn't start at swap.
    wrong_start_path = [
        "contracts.UniswapV2Pair:UniswapV2Pair._safeTransfer",
        "contracts.UniswapV2Pair:UniswapV2Pair._update",
    ]
    out = render_and_write_flow_note(
        tmp_path,
        gid,
        swap,
        paths=[wrong_start_path],
        overview="testing wrong-start rejection",
        cache_root=cache_root,
    )
    text = Path(out).read_text()
    assert "Path rejected" in text
    assert "expected entrypoint" in text
    assert "sequenceDiagram" not in text


def test_render_and_write_flow_note_refetches_entrypoint_discarding_forged_fields(
    tier1_graph_id, tmp_path,
):
    """Codex round-16 fix: the LLM-supplied `entrypoint_node`
    dict is treated as an ID carrier only. Forged name fields
    are silently overridden by the canonical refetch via
    `get_node(graph_id, entrypoint_node["id"], cache_root=...)`.

    Reproduced exploit: a prompt-injected FlowTracer supplies
    `entrypoint_node["name"]="../risks/flow-pwn"` with a valid
    id. Pre-fix, this flowed through the `rel_path` builder
    as `flows/../risks/flow-pwn.md` which `resolve()`d to
    `vault/risks/flow-pwn.md` — INSIDE the vault, so
    `write_obsidian_note`'s containment check passed. Because
    flow dispatch runs BEFORE risk synthesis and MOC
    generation, the attacker could plant an attacker-named
    note into `risks/` that the MOC writer would later index
    alongside curated risks.

    Post-fix: refetch returns the canonical swap entrypoint;
    the forged name is discarded; the file lands at
    `flows/UniswapV2Pair.swap.md`. Belt-and-suspenders: the
    `..`-segment reject in `write_obsidian_note` catches
    any rel_path that bypasses refetch."""
    from src.render.obsidian import render_and_write_flow_note

    gid, cache_root = tier1_graph_id
    forged_entrypoint = {
        # Valid id — refetch will succeed.
        "id": "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        # Malicious forged name designed to escape into risks/.
        "name": "../risks/flow-pwn",
        "kind": "method",
        "location": {
            "file_path": "FAKE.sol",
            "start_line": 0,
            "end_line": 999,
            "start_col": 0,
            "end_col": 0,
        },
    }
    out = render_and_write_flow_note(
        tmp_path,
        gid,
        forged_entrypoint,
        [],   # empty paths is fine — we're testing routing
        overview="ov",
        observations=[],
        cache_root=cache_root,
    )
    # Canonical name wins — file lands in flows/ with the
    # parent-qualified bare name.
    assert out.endswith("/flows/UniswapV2Pair.swap.md")
    # Nothing landed under `risks/`.
    assert not (tmp_path / "risks").exists(), (
        f"forged entrypoint name escaped into risks/: "
        f"{list((tmp_path / 'risks').iterdir())}"
    )
    # Frontmatter uses canonical name + entrypoint id, not
    # the forged values.
    body = Path(out).read_text()
    assert "name: swap" in body
    assert "../risks/flow-pwn" not in body
    assert "FAKE.sol" not in body


def test_render_and_write_flow_note_produces_valid_note(
    tier1_graph_id, tmp_path
):
    """Hand-built path test — no LLM. Confirms the body has
    sequence diagrams, the file lands at the expected path, and
    overview/observations appear verbatim."""
    from src.render.obsidian import render_and_write_flow_note
    from src.tools import get_node

    gid, cache_root = tier1_graph_id
    swap = get_node(
        gid,
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        cache_root=cache_root,
    )
    paths = [
        [
            "contracts.UniswapV2Pair:UniswapV2Pair.swap",
            "contracts.UniswapV2Pair:UniswapV2Pair._safeTransfer",
        ],
        [
            "contracts.UniswapV2Pair:UniswapV2Pair.swap",
            "contracts.UniswapV2Pair:UniswapV2Pair._update",
        ],
    ]

    out = render_and_write_flow_note(
        tmp_path,
        gid,
        swap,
        paths,
        overview="Test overview prose",
        observations=["obs one", "obs two"],
        cache_root=cache_root,
    )

    # Filename qualified with containing contract (so
    # UniswapV2Pair.swap, not bare swap, when other contracts
    # also expose a `swap` entrypoint).
    assert out.endswith("/flows/UniswapV2Pair.swap.md")
    text = Path(out).read_text()
    # Frontmatter shape
    assert "type: flow" in text
    assert "name: swap" in text
    assert "path_count: 2" in text
    # Body content
    assert "## Paths" in text
    assert text.count("sequenceDiagram") == 2  # one per path
    assert "_safeTransfer" in text
    assert "_update" in text
    assert "## Overview" in text
    assert "Test overview prose" in text
    assert "## Observations" in text
    assert "obs one" in text
    assert "obs two" in text
    # Per-path Hops list with method-level wikilinks.
    assert text.count("**Hops:**") == 2  # one per path
    assert "[[contracts/UniswapV2Pair|UniswapV2Pair.swap]]" in text
    assert (
        "[[contracts/UniswapV2Pair|UniswapV2Pair._safeTransfer]]"
        in text
    )
    assert (
        "[[contracts/UniswapV2Pair|UniswapV2Pair._update]]" in text
    )


def test_render_and_write_flow_note_empty_paths_emits_placeholder(
    tier1_graph_id, tmp_path
):
    """Empty paths list = placeholder section, not crash.
    Required for the chunk-3.8 dispatch loop to survive
    entrypoints with no reachable sinks."""
    from src.render.obsidian import render_and_write_flow_note
    from src.tools import get_node

    gid, cache_root = tier1_graph_id
    swap = get_node(
        gid,
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        cache_root=cache_root,
    )
    out = render_and_write_flow_note(
        tmp_path,
        gid,
        swap,
        paths=[],
        overview="x",
        cache_root=cache_root,
    )
    text = Path(out).read_text()
    assert "## Paths" in text
    assert "No multi-hop paths" in text
    assert "sequenceDiagram" not in text
    assert "path_count: 0" in text


def test_flow_note_defangs_backtick_in_hop_fallback(
    tier1_graph_id, tmp_path,
):
    """Codex round-11 fix: per-path Hops list contains
    backticked bare names when resolve_wikilink can't find
    the hop. LLM-controlled `paths` arg can supply a hop_id
    with embedded backticks, newlines, headings, wikilinks.
    Without defang the backtick closes the code span and
    everything after renders as raw markdown.

    Pin the defang: injection content survives only as
    inert escaped text."""
    from src.render.obsidian import render_and_write_flow_note
    from src.tools import get_node

    gid, cache_root = tier1_graph_id
    swap = get_node(
        gid,
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        cache_root=cache_root,
    )
    # A path whose second hop is a malicious unresolvable id.
    malicious_hop = (
        "fake:foo`## INJECTED HEADING\n"
        "[[../../etc/passwd]] "
        "<iframe src='https://evil.example'></iframe>"
    )
    paths = [
        [
            "contracts.UniswapV2Pair:UniswapV2Pair.swap",
            malicious_hop,
        ],
    ]
    out = render_and_write_flow_note(
        tmp_path, gid, swap, paths,
        overview="overview",
        cache_root=cache_root,
    )
    text = Path(out).read_text()

    # No active wikilink and no raw HTML — both came from
    # the malicious hop_id.
    assert "[[../../etc/passwd]]" not in text
    assert "<iframe" not in text

    # The malicious hop's newline got flattened: no second
    # line under "**Hops:**" can start with `## ` or any
    # markdown structural character.
    in_hops = False
    for line in text.splitlines():
        if line.startswith("**Hops:**"):
            in_hops = True
            continue
        if in_hops:
            if line.strip() == "":
                # blank line ends hops list
                continue
            if line.startswith("##") or line.startswith("###"):
                # next heading — out of section
                break
            stripped = line.lstrip()
            # Each hop line begins with an integer + ". "
            assert stripped[0:1].isdigit() or stripped == "", (
                f"unexpected non-hop line under **Hops:**: "
                f"{line!r}\nFull body:\n{text}"
            )


def test_flow_note_defangs_path_heading_against_newline_injection(
    tier1_graph_id, tmp_path,
):
    """Codex round-11 fix: the `### Path {i} — {bare} → {sink_bare}`
    heading line interpolates LLM-controlled bare names. A
    newline in either lets the line wrap and inject a real
    H2/H3 on the following line."""
    from src.render.obsidian import render_and_write_flow_note
    from src.tools import get_node

    gid, cache_root = tier1_graph_id
    swap = get_node(
        gid,
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        cache_root=cache_root,
    )
    # Last hop has injected newline + heading. sink_bare
    # gets the basename after the last `.`, so the dot in
    # the rsplit keeps the rest. We use a name that ends
    # with `.evil\n## INJECTED` to force the newline into
    # sink_bare.
    paths = [
        [
            "contracts.UniswapV2Pair:UniswapV2Pair.swap",
            "fake:foo.evil\n## INJECTED PATH H2",
        ],
    ]
    out = render_and_write_flow_note(
        tmp_path, gid, swap, paths,
        overview="overview",
        cache_root=cache_root,
    )
    text = Path(out).read_text()
    # Find the Path-heading line and confirm it didn't
    # split into two lines (no H2 injection).
    h2_lines = [
        line for line in text.splitlines()
        if line.startswith("## ") and "INJECTED" in line
    ]
    assert h2_lines == [], (
        f"H2 injection from path heading; lines: {h2_lines}"
    )


def test_flow_note_overview_defangs_markdown_injection(
    tier1_graph_id, tmp_path,
):
    """Codex round-13 fix: flow-note overview body is
    LLM-authored. Heading/wikilink/HTML injection must be
    neutralized inside the overview."""
    from src.render.obsidian import render_and_write_flow_note
    from src.tools import get_node

    gid, cache_root = tier1_graph_id
    swap = get_node(
        gid,
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        cache_root=cache_root,
    )
    out = render_and_write_flow_note(
        tmp_path, gid, swap, paths=[],
        overview=(
            "Real overview.\n\n"
            "## INJECTED HEADING\n\n"
            "[[../../etc/passwd]] <iframe src='https://evil'></iframe>"
        ),
        cache_root=cache_root,
    )
    text = Path(out).read_text()
    assert "[[../../etc/passwd]]" not in text
    assert "<iframe" not in text
    import re as _re
    injected = _re.findall(
        r"^## INJECTED.*$", text, flags=_re.MULTILINE,
    )
    assert injected == []


def test_flow_note_observations_defang_markdown_injection(
    tier1_graph_id, tmp_path,
):
    """Codex round-13 fix: each flow observation is
    LLM-authored prose. Defang per observation before
    interpolation."""
    from src.render.obsidian import render_and_write_flow_note
    from src.tools import get_node

    gid, cache_root = tier1_graph_id
    swap = get_node(
        gid,
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        cache_root=cache_root,
    )
    out = render_and_write_flow_note(
        tmp_path, gid, swap, paths=[],
        overview="overview",
        observations=[
            "Benign A.",
            (
                "Malicious B.\n"
                "## INJECTED OBS HEADING\n"
                "[[../../etc/passwd]] "
                "<iframe src='https://evil'></iframe>"
            ),
        ],
        cache_root=cache_root,
    )
    text = Path(out).read_text()
    assert "Benign A." in text
    assert "[[../../etc/passwd]]" not in text
    assert "<iframe" not in text
    import re as _re
    injected = _re.findall(
        r"^## INJECTED.*$", text, flags=_re.MULTILINE,
    )
    assert injected == []


def test_flow_note_filename_qualified_with_contract(
    tier1_graph_id, tmp_path
):
    """Two Tier 1 entrypoints sharing the bare name `swap`
    (UniswapV2Pair.swap and IUniswapV2Pair.swap on the attack
    surface) must produce distinct files. Without qualification
    the second write would silently overwrite
    the first."""
    from src.render.obsidian import render_and_write_flow_note
    from src.tools import get_node

    gid, cache_root = tier1_graph_id
    pair_swap = get_node(
        gid,
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        cache_root=cache_root,
    )
    iface_swap = get_node(
        gid,
        "contracts.interfaces.IUniswapV2Pair:IUniswapV2Pair.swap",
        cache_root=cache_root,
    )

    out_a = render_and_write_flow_note(
        tmp_path, gid, pair_swap, paths=[], overview="contract side",
        cache_root=cache_root,
    )
    out_b = render_and_write_flow_note(
        tmp_path, gid, iface_swap, paths=[], overview="interface side",
        cache_root=cache_root,
    )

    assert out_a.endswith("/flows/UniswapV2Pair.swap.md")
    assert out_b.endswith("/flows/IUniswapV2Pair.swap.md")
    assert out_a != out_b
    # Both files exist (no silent overwrite)
    assert Path(out_a).exists()
    assert Path(out_b).exists()


# --- per-path render failure -----------------------------------------


def test_render_and_write_flow_note_continues_past_bad_path(
    tier1_graph_id, tmp_path, caplog
):
    """One bad path in a multi-path list must NOT abort the
    flow note. The design adds an inline
    `_Sequence diagram unavailable_` placeholder + warning so
    the dispatch loop can still ship a partial note.

    Two failure modes are exercised in one test:
      1. `render_sequence` raises (unknown node in the path
         → KeyError in its validation loop) → inline placeholder.
      2. `resolve_wikilink` raises KeyError for an individual
         hop → backticked bare-name fallback in Hops list.

    Armors against a regression that re-raises instead of
    falling back, which would silently break dispatch_flows for
    any flow containing one bad path."""
    import logging

    from src.render.obsidian import render_and_write_flow_note
    from src.tools import get_node

    gid, cache_root = tier1_graph_id
    swap = get_node(
        gid,
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        cache_root=cache_root,
    )

    good_path = [
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        "contracts.UniswapV2Pair:UniswapV2Pair._safeTransfer",
    ]
    # Second hop is unknown — render_sequence raises KeyError;
    # resolve_wikilink on the same hop also raises KeyError.
    bad_path = [
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        "module.fake:NotARealNode",
    ]

    caplog.set_level(logging.WARNING, logger="src.render.obsidian")

    out = render_and_write_flow_note(
        tmp_path,
        gid,
        swap,
        paths=[good_path, bad_path],
        overview="testing partial-failure resilience",
        cache_root=cache_root,
    )

    text = Path(out).read_text()

    # The note shipped with both paths represented.
    assert "path_count: 2" in text

    # Path 1: real sequence diagram rendered.
    assert "### Path 1 — swap → _safeTransfer" in text
    # Exactly ONE sequenceDiagram block in the entire note
    # (path 2 fell back to the placeholder).
    assert text.count("sequenceDiagram") == 1

    # Path 2: inline placeholder (NOT a sequence diagram).
    # Round-17: the pre-render validator now catches the
    # fabricated hop EARLIER (before render_sequence runs),
    # so the placeholder text is "Path rejected: hop ... not
    # in graph" instead of the older "Sequence diagram
    # unavailable" message. Stricter rejection, same shipping
    # behavior.
    # Round-18: heading no longer dereferences `path[-1]`
    # (would have crashed on `paths=[[]]`), so invalid paths
    # now show the generic "invalid path" heading instead of
    # `swap → NotARealNode`. Rejection reason still names
    # the bad hop.
    assert "### Path 2 — invalid path" in text
    assert "Path rejected" in text
    assert "NotARealNode" in text

    # Path 1 still has its Hops section. Path 2 does NOT —
    # round-17 short-circuits invalid paths entirely (rendering
    # hops of a fabricated path would still be misleading).
    assert text.count("**Hops:**") == 1
    # Path 1's first hop still resolves (it's a real method).
    assert "[[contracts/UniswapV2Pair|UniswapV2Pair.swap]]" in text

    # Warning fired with diagnostic detail (path index + reason).
    warnings = [r.getMessage() for r in caplog.records]
    assert any(
        "rejecting invalid flow path 2" in m
        and "NotARealNode" in m
        for m in warnings
    ), f"expected per-path rejection warning; got {warnings}"


def test_sequence_render_propagates_coding_bugs(
    tier1_graph_id, tmp_path, monkeypatch
):
    """The per-path sequence-render block's narrow except
    catches expected per-path failures (bad node ID →
    KeyError, missing graph cache → FileNotFoundError, etc.)
    so partial flow notes still ship. Coding bugs (TypeError,
    AttributeError) propagate up so they surface in
    dispatch_flows's failure recorder instead of being
    silently swallowed mid-flow."""
    import pytest

    from src.render.obsidian import render_and_write_flow_note
    from src.tools import get_node

    gid, cache_root = tier1_graph_id
    swap = get_node(
        gid,
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        cache_root=cache_root,
    )

    def _raise_type_error(*args, **kwargs):
        raise TypeError("simulated coding bug in render_sequence")

    monkeypatch.setattr(
        "src.render.obsidian.render_sequence",
        _raise_type_error,
    )

    good_path = [
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        "contracts.UniswapV2Pair:UniswapV2Pair._safeTransfer",
    ]

    with pytest.raises(TypeError, match="simulated coding bug"):
        render_and_write_flow_note(
            tmp_path,
            gid,
            swap,
            paths=[good_path],
            cache_root=cache_root,
        )


# --- widened-except for legitimate cache failures --------------------


@pytest.mark.parametrize(
    "exc",
    [EOFError, OSError],
    ids=["EOFError", "OSError"],
)
def test_sequence_render_recovers_from_load_graph_failures(
    tier1_graph_id, tmp_path, monkeypatch, exc
):
    """Per-path sequence rendering's narrow catch tuple must
    include legitimate load_graph-style exceptions (EOFError,
    OSError, UnpicklingError). With one path failing on one
    of these, the flow note must still ship with an inline
    placeholder for that path rather than aborting the entire
    flow-note write."""
    from src.render.obsidian import render_and_write_flow_note
    from src.tools import get_node

    gid, cache_root = tier1_graph_id
    swap = get_node(
        gid,
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        cache_root=cache_root,
    )

    def _raise(*args, **kwargs):
        raise exc(f"simulated {exc.__name__}")

    monkeypatch.setattr(
        "src.render.obsidian.render_sequence",
        _raise,
    )

    good_path = [
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        "contracts.UniswapV2Pair:UniswapV2Pair._safeTransfer",
    ]

    out = render_and_write_flow_note(
        tmp_path,
        gid,
        swap,
        paths=[good_path],
        cache_root=cache_root,
    )
    assert Path(out).exists()
    assert "Sequence diagram unavailable" in Path(out).read_text()


def test_sequence_render_recovers_from_unpickling_error(
    tier1_graph_id, tmp_path, monkeypatch
):
    """pickle.UnpicklingError variant for the sequence-render
    block."""
    import pickle

    from src.render.obsidian import render_and_write_flow_note
    from src.tools import get_node

    gid, cache_root = tier1_graph_id
    swap = get_node(
        gid,
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        cache_root=cache_root,
    )

    def _raise(*args, **kwargs):
        raise pickle.UnpicklingError("simulated UnpicklingError")

    monkeypatch.setattr(
        "src.render.obsidian.render_sequence",
        _raise,
    )

    good_path = [
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        "contracts.UniswapV2Pair:UniswapV2Pair._safeTransfer",
    ]

    out = render_and_write_flow_note(
        tmp_path,
        gid,
        swap,
        paths=[good_path],
        cache_root=cache_root,
    )
    assert Path(out).exists()
    assert "Sequence diagram unavailable" in Path(out).read_text()
