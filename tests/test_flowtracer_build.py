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

    assert out.endswith("/flows/swap.md")
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
