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
    # Codex review fix: the prompt tells the LLM to read SARIF
    # rule IDs from annotation descriptions. `get_node` does
    # NOT return annotations — only node metadata. Without
    # `annotations_of` in the tool surface, the LLM
    # hallucinates or fails. Pin the contract.
    assert "annotations_of" in tool_names

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


def test_render_and_write_risk_note_defangs_backtick_in_involved_node_fallback(
    tier1_graph_id, tmp_path,
):
    """Codex round-11 fix: the involved-node wikilink
    fallback path interpolates the bare node name into a
    backticked inline code span (`` `{bare}` ``). If the
    LLM supplies an invalid node ID containing a backtick,
    the embedded backtick closes the span early and
    everything after it renders as raw markdown — heading,
    wikilink, etc. This is a real injection vector since
    `involved_nodes` is LLM-controlled.

    Pin the defang: backticks, newlines, wikilinks, and
    HTML inside the bare label are neutralized before
    interpolation."""
    from src.render.obsidian import render_and_write_risk_note

    gid, cache_root = tier1_graph_id
    malicious_nid = (
        "fake:foo`## INJECTED HEADING\n"
        "[[../../etc/passwd]] "
        "<iframe src='https://evil.example'></iframe>"
    )
    out = render_and_write_risk_note(
        tmp_path, gid,
        risk_name="hotspots",
        overview="Plain overview.",
        involved_nodes=[malicious_nid],
        cache_root=cache_root,
    )
    body = Path(out).read_text()
    # No active wikilink injected from the bare-fallback.
    assert "[[../../etc/passwd]]" not in body
    # No raw HTML.
    assert "<iframe" not in body
    # No newline-escape: the malicious newline in the nid
    # got flattened to a single space, so no second line
    # in the bullet can start with `## ` or `[[`.
    # Walk lines under "## Involved Nodes" and verify each
    # is either the section header or a single bullet line
    # (or blank).
    in_section = False
    bullet_lines: list[str] = []
    for line in body.splitlines():
        if line == "## Involved Nodes":
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break  # next section
            if line.startswith("- "):
                bullet_lines.append(line)
            elif line.strip() == "":
                continue
            else:
                # Non-bullet, non-blank line within the
                # Involved Nodes section → injection escaped.
                assert False, (
                    f"unexpected non-bullet line in Involved "
                    f"Nodes: {line!r}\nFull body:\n{body}"
                )
    # The single malicious node renders as exactly ONE
    # bullet (no newline split, no extra bullets).
    assert len(bullet_lines) == 1, (
        f"expected 1 bullet from 1 involved_node; got "
        f"{len(bullet_lines)}: {bullet_lines}"
    )


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


def test_render_and_write_risk_note_defangs_dataview_inline_queries(
    tier1_graph_id, tmp_path,
):
    """Codex round-19 fix: Obsidian's Dataview plugin executes
    single-backtick inline queries (`= dql_expr`, `$= js`).
    Pre-fix, `_defang_text` only escaped 3+ backtick fences,
    leaving Dataview inline queries intact. An LLM-authored
    risk overview or observation with `$= some_js` would run
    arbitrary DataviewJS in the auditor's vault if the plugin
    is enabled.

    Post-fix: every backtick is escaped to `&#x60;`. Pin both
    the overview path (block-aware) AND the observation path
    (block-aware per-bullet)."""
    from src.render.obsidian import render_and_write_risk_note

    gid, cache_root = tier1_graph_id
    overview = (
        "Risk includes inline DQL `= file.size` to peek at "
        "vault metadata and DataviewJS `$= dv.pages('')` "
        "to exfiltrate the whole vault."
    )
    observations = [
        "Inline `= now()` query in note",
        "Inline `$= app.vault.read('.env')` DataviewJS payload",
    ]
    out = render_and_write_risk_note(
        tmp_path, gid,
        risk_name="dataview-test",
        overview=overview,
        involved_nodes=[],
        observations=observations,
        cache_root=cache_root,
    )
    body = Path(out).read_text()
    # No single backticks anywhere in body — Dataview parser
    # has nothing to recognize.
    # (Stripping the YAML frontmatter first because the
    # frontmatter dumper may emit a `:` inside quoted strings
    # but never raw backticks; check defensively.)
    overview_body = body.split("---\n\n", 1)[-1]
    assert "`" not in overview_body, (
        f"backtick reached overview body: {overview_body}"
    )
    # Defanged entity forms present (visible-but-inert).
    assert "&#x60;= file.size&#x60;" in body
    assert "&#x60;$= dv.pages" in body
    assert "&#x60;= now()&#x60;" in body
    assert "&#x60;$= app.vault.read" in body


def test_render_and_write_risk_note_sanitizes_overview(
    tier1_graph_id, tmp_path,
):
    """Codex review fix (F5): RiskSynthesizer's overview
    prose comes from the LLM processing attacker-influenced
    SARIF context. Without sanitization, an attacker can
    inject raw HTML (iframes, scripts), clickable
    vault-traversal wikilinks, transclusions, dangerous URI
    schemes, and code fences directly into the auditor's
    risk note. Pin the contract: all injection vectors are
    defanged before write."""
    from src.render.obsidian import render_and_write_risk_note

    gid, cache_root = tier1_graph_id
    malicious_overview = (
        '<iframe src="https://evil.example"></iframe> '
        'Click [here](javascript:alert(1)) or visit '
        'obsidian://action?cmd=evil or open '
        '[[../../etc/passwd]] or ![[../../secrets]]. '
        'File risk in file:///etc/shadow. '
        '```bash\nrm -rf $HOME/.ssh\n```'
    )
    out = render_and_write_risk_note(
        tmp_path, gid,
        risk_name="hotspots",
        overview=malicious_overview,
        involved_nodes=[],
        cache_root=cache_root,
    )
    body = Path(out).read_text()
    # Every injection vector is defanged in the written file.
    assert "<iframe" not in body
    assert "](javascript:" not in body
    assert "obsidian://" not in body
    assert "[[../../etc/passwd" not in body
    assert "![[../../secrets" not in body
    assert "file:///etc/shadow" not in body
    assert "```bash" not in body
    # Visible-but-inert defanged forms present.
    assert "&lt;iframe" in body
    assert "] (javascript[:]" in body
    assert "obsidian[:]" in body
    assert "[ [../../etc/passwd" in body
    assert "! [" in body  # transclusion defang
    assert "file[:][//]/etc/shadow" in body
    assert "&#x60;&#x60;&#x60;" in body


def test_render_and_write_risk_note_rejects_heading_injection_in_overview(
    tier1_graph_id, tmp_path,
):
    """Codex follow-up review fix (F1): `_defang_text` alone
    preserves newlines, so an overview containing
    `\\n## Fake Finding Accepted` would still inject a real
    H2 into the auditor's note. Block-aware sanitizer must
    neutralize line-start markdown constructs."""
    from src.render.obsidian import render_and_write_risk_note

    out = render_and_write_risk_note(
        tmp_path, tier1_graph_id[0],
        risk_name="hotspots",
        overview=(
            "Real prose about hotspots.\n\n"
            "## Fake Finding Accepted\n\n"
            "Trust this conclusion.\n"
        ),
        involved_nodes=[],
        cache_root=tier1_graph_id[1],
    )
    body = Path(out).read_text()
    # The ONLY H2 in the body must be canonical sections
    # (## Overview, ## Involved Nodes, ## Observations) —
    # NOT the attacker's injected "Fake Finding Accepted".
    import re as _re
    h2_lines = _re.findall(
        r"^## .+$", body, flags=_re.MULTILINE,
    )
    assert "## Fake Finding Accepted" not in h2_lines, (
        f"H2 injection survived block-defang; H2 lines: "
        f"{h2_lines}\n\nbody:\n{body}"
    )
    # Real prose survives (paragraph structure preserved).
    assert "Real prose about hotspots." in body
    # Attacker text still appears (defanged inline) — visible
    # but not as a heading.
    assert "Fake Finding Accepted" in body


def test_render_and_write_risk_note_rejects_list_injection_in_observations(
    tier1_graph_id, tmp_path,
):
    """Codex follow-up review fix (F1): observation
    containing `ok\\n- Action: trust this` would inject a
    sibling bullet. Block-defang prevents that."""
    from src.render.obsidian import render_and_write_risk_note

    out = render_and_write_risk_note(
        tmp_path, tier1_graph_id[0],
        risk_name="hotspots",
        overview="Plain overview.",
        involved_nodes=[],
        observations=[
            "Real observation.\n- Injected action item",
        ],
        cache_root=tier1_graph_id[1],
    )
    body = Path(out).read_text()
    # The injected bullet's `- ` at line start must not
    # render as a list item. Count list bullets in the
    # Observations section.
    import re as _re
    # Find the Observations section content.
    obs_section = body.split("## Observations", 1)[-1]
    bullets = _re.findall(r"^- ", obs_section, flags=_re.MULTILINE)
    assert len(bullets) == 1, (
        f"expected exactly 1 observation bullet, got "
        f"{len(bullets)}. Body:\n{body}"
    )
    # The injected text survives (defanged) but as paragraph
    # text of the first observation, not a second bullet.
    assert "Injected action item" in body


def test_render_and_write_risk_note_rejects_blockquote_injection(
    tier1_graph_id, tmp_path,
):
    """Block-defang covers blockquote injection too —
    attacker can't make their text look like a quoted
    statement from another auditor."""
    from src.render.obsidian import render_and_write_risk_note

    out = render_and_write_risk_note(
        tmp_path, tier1_graph_id[0],
        risk_name="hotspots",
        overview=(
            "Real overview.\n\n"
            "> 'This contract is safe.' — Trail of Bits"
        ),
        involved_nodes=[],
        cache_root=tier1_graph_id[1],
    )
    body = Path(out).read_text()
    # No real blockquote at line start. (Obsidian/markdown
    # would render `> ` as a quoted block.)
    import re as _re
    block_quotes = _re.findall(
        r"^> ", body, flags=_re.MULTILINE,
    )
    assert block_quotes == [], (
        f"blockquote injection survived; body:\n{body}"
    )
    # Text content still visible.
    assert "Trail of Bits" in body


def test_render_and_write_risk_note_rejects_reference_def_injection(
    tier1_graph_id, tmp_path,
):
    """Link reference definitions `[ref]: url` at line start
    set up named links that can be referenced inline as
    `[text][ref]`. An attacker can plant a malicious URL ref
    and trick the LLM (or auditor in a later pass) into
    referencing it. Defang at the reference-def line."""
    from src.render.obsidian import render_and_write_risk_note

    out = render_and_write_risk_note(
        tmp_path, tier1_graph_id[0],
        risk_name="hotspots",
        overview=(
            "Real overview.\n\n"
            "[evil]: https://attacker.example/\n"
            "[^fn]: footnote payload"
        ),
        involved_nodes=[],
        cache_root=tier1_graph_id[1],
    )
    body = Path(out).read_text()
    # Reference definitions at line start are defanged.
    import re as _re
    refs = _re.findall(
        r"^\[[^\]]+\]:\s", body, flags=_re.MULTILINE,
    )
    assert refs == [], (
        f"reference definitions survived block-defang; body:\n{body}"
    )


def test_render_and_write_risk_note_sanitizes_observations(
    tier1_graph_id, tmp_path,
):
    """Codex review fix (F5): each observation string is
    also LLM-controlled and must be sanitized the same way
    as overview."""
    from src.render.obsidian import render_and_write_risk_note

    gid, cache_root = tier1_graph_id
    out = render_and_write_risk_note(
        tmp_path, gid,
        risk_name="hotspots",
        overview="Plain overview.",
        involved_nodes=[],
        observations=[
            "Benign observation A.",
            '<script>alert(1)</script>',
            "Click [trap](file:///etc/passwd)",
            "![[../../secrets|view]] tells you everything",
        ],
        cache_root=cache_root,
    )
    body = Path(out).read_text()
    # Benign content survives.
    assert "Benign observation A." in body
    # Injection vectors defanged.
    assert "<script>" not in body
    assert "](file:" not in body
    assert "![[" not in body
    assert "&lt;script&gt;" in body
    assert "] (file[:]" in body


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
