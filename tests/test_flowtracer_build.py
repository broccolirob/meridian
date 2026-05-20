from pathlib import Path

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

    # Chunk 3.10: filename qualified with containing contract
    # (UniswapV2Pair.swap, not bare swap).
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
    # Chunk 3.9: per-path Hops list with method-level wikilinks
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


def test_flow_note_filename_qualified_with_contract(
    tier1_graph_id, tmp_path
):
    """Chunk 3.10: two Tier 1 entrypoints sharing the bare name
    `swap` (UniswapV2Pair.swap and IUniswapV2Pair.swap on the
    attack surface) must produce distinct files. Without
    qualification the second write would silently overwrite
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


# --- per-path render failure (chunk 3.16, /review I8) -----------------


def test_render_and_write_flow_note_continues_past_bad_path(
    tier1_graph_id, tmp_path, caplog
):
    """One bad path in a multi-path list must NOT abort the
    flow note. The chunk 3.7 design adds an inline
    `_Sequence diagram unavailable_` placeholder + warning so
    the dispatch loop can still ship a partial note.

    Two failure modes are exercised in one test:
      1. `render_sequence` raises (unknown node in the path
         → KeyError in its validation loop) → inline placeholder.
      2. `resolve_wikilink` raises KeyError for an individual
         hop → backticked bare-name fallback in Hops list.

    Pre-3.16 neither branch was tested — a regression that
    re-raised instead of falling back would silently break
    dispatch_flows for any flow containing one bad path."""
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
    assert "### Path 2 — swap → NotARealNode" in text
    assert "Sequence diagram unavailable" in text

    # Both paths still have Hops sections — the Hops loop runs
    # independently of the sequence-render try/except.
    assert text.count("**Hops:**") == 2

    # Path 2's Hops list falls back to backticked bare-name for
    # the unresolvable hop (resolve_wikilink raised KeyError).
    assert "`NotARealNode` (no contract note)" in text
    # Path 1's first hop still resolves (it's a real method).
    assert "[[contracts/UniswapV2Pair|UniswapV2Pair.swap]]" in text

    # Warning fired with diagnostic detail (path index + reason).
    warnings = [r.getMessage() for r in caplog.records]
    assert any(
        "sequence render failed for path 2" in m
        and "NotARealNode" in m
        for m in warnings
    ), f"expected per-path failure warning; got {warnings}"
