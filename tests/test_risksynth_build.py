"""Tests for RISK_SYNTHESIZER_SUBAGENT + render_and_write_risk_note.

Mirrors tests/test_flowtracer_build.py pattern: subagent dict
shape, tool allowlist coverage, anti-bypass discipline (no
separable render/write tools), hand-built render test (no LLM
invocation), path-traversal defense.
"""

from pathlib import Path

import pytest

from src.subagents import RISK_SYNTHESIZER_SUBAGENT


def test_subagent_has_required_keys():
    """deepagents.SubAgent contract: name, description,
    system_prompt, tools."""
    required = {"name", "description", "system_prompt", "tools"}
    assert required <= set(RISK_SYNTHESIZER_SUBAGENT.keys())
    assert RISK_SYNTHESIZER_SUBAGENT["name"] == "risk-synthesizer"
    assert len(RISK_SYNTHESIZER_SUBAGENT["description"]) > 50
    assert len(RISK_SYNTHESIZER_SUBAGENT["system_prompt"]) > 500


def test_subagent_tools_are_callable():
    tools = RISK_SYNTHESIZER_SUBAGENT["tools"]
    assert len(tools) >= 5
    for tool in tools:
        assert callable(tool), f"not callable: {tool!r}"


def test_subagent_tools_cover_required_capabilities():
    """Chunk 4.5 success criterion: tool allowlist references
    real symbols. Pins what's in and what's out — regression
    armor for the design decisions documented in the chunk
    plan."""
    tool_names = {t.__name__ for t in RISK_SYNTHESIZER_SUBAGENT["tools"]}

    # Data queries the prompt instructs the LLM to call.
    assert "nodes_with_annotation" in tool_names
    assert "complexity_hotspots" in tool_names
    assert "list_subgraph_nodes" in tool_names
    assert "get_node" in tool_names

    # Wikilink resolution + side effects.
    assert "resolve_wikilink" in tool_names
    assert "annotate" in tool_names

    # Combined render+write (the ONLY way to persist a risk note).
    assert "render_and_write_risk_note" in tool_names

    # Separable render/write must NOT be on the list — same
    # anti-bypass discipline as NodeDocumenter/FlowTracer.
    assert "render_node_note" not in tool_names
    assert "render_flow_note" not in tool_names
    assert "write_obsidian_note" not in tool_names

    # run_preanalysis is NOT a subagent tool — it's a write op
    # (deepcopy + save_graph per chunk 4.3) called by the main
    # agent (chunk 4.6) ONCE before dispatch. Subagent queries
    # individual subgraphs via list_subgraph_nodes (read-only).
    # Including run_preanalysis here would let the LLM trigger
    # redundant deepcopies.
    assert "run_preanalysis" not in tool_names


def test_render_and_write_risk_note_produces_valid_note(
    tier1_graph_id, tmp_path
):
    """Hand-built test — no LLM. Confirms body has the
    expected sections, file lands at vault/risks/<name>.md,
    overview/observations appear verbatim, involved-nodes
    render as wikilinks."""
    from src.render.obsidian import render_and_write_risk_note

    gid, cache_root = tier1_graph_id
    out = render_and_write_risk_note(
        tmp_path,
        gid,
        risk_name="hotspots",
        overview=(
            "UniswapV2Pair concentrates state mutations in "
            "swap/mint/burn. Auditors should review these "
            "first for reentrancy and price-manipulation "
            "vectors."
        ),
        involved_nodes=[
            "contracts.UniswapV2Pair:UniswapV2Pair.swap",
            "contracts.UniswapV2Pair:UniswapV2Pair.mint",
        ],
        observations=[
            "External callback in swap is the only re-entry "
            "surface visible in this fixture.",
        ],
        cache_root=cache_root,
    )
    written = Path(out)
    assert written.exists()
    assert written.parent.name == "risks"
    assert written.name == "hotspots.md"

    body = written.read_text()
    assert "## Overview" in body
    assert "UniswapV2Pair concentrates" in body
    assert "## Involved Nodes" in body
    # Wikilink syntax should be present for at least one
    # resolved node.
    assert "[[" in body and "]]" in body
    assert "## Observations" in body
    assert "External callback" in body


def test_render_and_write_risk_note_rejects_path_traversal(
    tier1_graph_id, tmp_path
):
    """Defense: attacker-LLM (prompt injection from
    adversarial Solidity comments) passing
    `risk_name="../../etc/passwd"` must NOT escape the vault.
    risk_name is validated against the kebab-case allowlist."""
    from src.render.obsidian import render_and_write_risk_note

    gid, cache_root = tier1_graph_id
    for bad in (
        "../escape",
        "/etc/passwd",
        "..",
        "with space",
        "UPPER",
        "trailing.dot",
        "with/slash",
        # Tightened regex (chunk 4.5 review fix): the original
        # [a-z0-9-]+ pattern accepted these degenerate forms
        # and produced ugly/ambiguous filenames. The
        # post-review pattern ^[a-z0-9]+(-[a-z0-9]+)*$ rejects
        # them.
        "-",
        "--",
        "-foo",
        "foo-",
        "a--b",
    ):
        with pytest.raises(ValueError, match="risk_name"):
            render_and_write_risk_note(
                tmp_path,
                gid,
                risk_name=bad,
                overview="x",
                involved_nodes=[],
                cache_root=cache_root,
            )


def test_render_and_write_risk_note_accepts_involved_nodes_none(
    tier1_graph_id, tmp_path
):
    """Chunk 4.5 review fix: deepagents tool serialization
    sometimes converts [] -> null. `involved_nodes=None` must
    NOT crash with TypeError on len(); function should coerce
    to [] and emit a placeholder Involved Nodes section."""
    from src.render.obsidian import render_and_write_risk_note

    gid, cache_root = tier1_graph_id
    out = render_and_write_risk_note(
        tmp_path,
        gid,
        risk_name="hotspots",
        overview="No nodes pinned yet.",
        involved_nodes=None,  # type: ignore[arg-type]
        cache_root=cache_root,
    )
    written = Path(out)
    assert written.exists()
    body = written.read_text()
    assert "## Involved Nodes" in body
    # Frontmatter nodes_count reflects the coerced empty list.
    assert "nodes_count: 0" in body


def test_render_and_write_risk_note_falls_back_on_garbage_node_ids(
    tier1_graph_id, tmp_path
):
    """Chunk 4.5 review fix: pins the graceful-degradation
    contract. When the LLM hallucinates node IDs that don't
    exist (or hands us colon-free garbage), the note still
    writes — unresolvable hops render as backticked bare
    names rather than crashing the whole synthesis."""
    from src.render.obsidian import render_and_write_risk_note

    gid, cache_root = tier1_graph_id
    out = render_and_write_risk_note(
        tmp_path,
        gid,
        risk_name="hotspots",
        overview="Mix of real + garbage IDs.",
        involved_nodes=[
            "garbage-no-colon",
            "fake:NotReal.method",
            "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        ],
        cache_root=cache_root,
    )
    written = Path(out)
    assert written.exists()
    body = written.read_text()
    assert "## Involved Nodes" in body
    # Garbage IDs must appear in the note (backticked
    # fallback) — proves rendering didn't abort on the bad
    # entries before reaching the good one.
    assert "garbage-no-colon" in body or "`garbage-no-colon`" in body
    assert "NotReal.method" in body


def test_list_subgraph_nodes_returns_empty_for_unknown_name(
    tier1_graph_id,
):
    """Chunk 4.5 review fix: list_subgraph_nodes used to
    surface Trailmark's KeyError as a tool-error string. LLM
    mental model expects "empty means nothing matched" —
    matches sibling tools like nodes_with_annotation. Pin
    the no-raise contract so a future refactor doesn't
    regress this."""
    from src.tools import list_subgraph_nodes

    gid, cache_root = tier1_graph_id
    result = list_subgraph_nodes(
        gid, "definitely-not-a-real-subgraph-name", cache_root=cache_root
    )
    assert result == []
