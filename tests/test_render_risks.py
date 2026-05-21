"""Tests for chunk 4.7 — node-note Risks section.

Two parts:
1. Unit tests for `_render_risks` parsing/formatting logic.
   Feeds synthetic finding-annotation lists directly into the
   helper; no fixtures, no graph cache.
2. Integration test for `render_and_write_node_note` that
   exercises the full pull-from-graph path: parse a fixture,
   annotate a node with a curated `[<risk_name>] reason`
   description, render, assert the written file contains the
   wikilink.
"""

import shutil
import subprocess as sp
from pathlib import Path

import pytest

from src.analyzers.slither import run_slither
from src.render.obsidian import (
    _render_risks,
    render_and_write_node_note,
)
from src.tools import (
    annotate,
    augment_sarif,
    get_node,
    nodes_with_annotation,
    trailmark_parse,
)


# ----------------------------------------------------------------
# Unit tests — _render_risks parsing/formatting
# ----------------------------------------------------------------

_RISK_SYNTHESIZER_SOURCE = "risk-synthesizer"


def _curated(description: str) -> dict:
    """Build a curated finding-annotation dict (source set to
    `risk-synthesizer`, the value RiskSynthesizer's prompt
    requires). Used by tests that exercise the wikilink path
    of `_render_risks`."""
    return {
        "kind": "finding",
        "source": _RISK_SYNTHESIZER_SOURCE,
        "description": description,
    }


def _raw(description: str, source: str = "sarif:Slither") -> dict:
    """Build a raw SARIF finding-annotation dict. Matches the
    shape Trailmark's `augment_sarif` produces. Used by tests
    that exercise the plain-bullet path of `_render_risks`."""
    return {
        "kind": "finding",
        "source": source,
        "description": description,
    }


def _finding(description: str, source: str = "risk-synthesizer") -> dict:
    """Legacy helper kept for tests that don't care about the
    curated/raw split (empty-state, ordering, etc.). New
    tests should use `_curated` or `_raw` to make the source
    contract explicit."""
    return {"kind": "finding", "source": source, "description": description}


def test_risks_section_renders_curated_wikilink():
    """A `risk-synthesizer`-sourced annotation with a
    `[<risk_name>] reason`-shaped description renders as a
    wikilink to vault/risks/<risk_name>.md plus the reason."""
    ctx = {
        "finding_annotations": [
            _curated("[hotspots] swap holds state across external call"),
        ],
    }
    out = _render_risks(ctx)
    assert "## Risks" in out
    assert "[[risks/hotspots|hotspots]]" in out
    assert "swap holds state across external call" in out
    # The em-dash separator follows the wikilink.
    assert (
        "[[risks/hotspots|hotspots]] — swap holds state across "
        "external call" in out
    )


def test_risks_section_renders_sarif_finding_as_plain_text():
    """Raw SARIF findings (any source other than
    `risk-synthesizer`) render as plain bullets with NO
    wikilink, even when the description happens to contain
    bracketed text."""
    ctx = {
        "finding_annotations": [
            _raw("controlled-delegatecall: function delegates to user input"),
        ],
    }
    out = _render_risks(ctx)
    assert "## Risks" in out
    assert (
        "- controlled-delegatecall: function delegates to user input"
        in out
    )
    # No risks/ wikilink should be synthesized for SARIF items.
    assert "[[risks/" not in out


def test_risks_section_does_not_collide_with_real_sarif_description_shape():
    """Cross-cutting review fix (C1): Trailmark's SARIF
    description format is `[WARNING] rule-id: msg (Tool)`.
    Uppercase makes the regex fail TODAY — but a future
    Trailmark release that drops the .upper() (or a custom
    SARIF tool that emits lowercase level) would silently
    flip every SARIF finding into a broken wikilink to
    `risks/warning.md`. Source-based filtering eliminates
    this fragility. Pin the no-collision contract."""
    ctx = {
        "finding_annotations": [
            # Real Trailmark SARIF shape with UPPERCASE level.
            _raw("[WARNING] 0-1-weak-prng: weak PRNG used (Slither)"),
            # Hypothetical future-Trailmark shape with lowercase.
            _raw("[warning] 0-1-weak-prng: weak PRNG used (Slither)"),
            # Description that EXACTLY matches the curated
            # prefix shape — but sourced from sarif.
            _raw("[hotspots] not actually curated; sourced as SARIF"),
        ],
    }
    out = _render_risks(ctx)
    # NONE of these should produce a wikilink. All three
    # render as plain bullets because source != "risk-synthesizer".
    assert "[[risks/warning" not in out
    assert "[[risks/hotspots" not in out
    # All three descriptions still appear as plain text bullets.
    assert "- [WARNING] 0-1-weak-prng" in out or "- [ [WARNING]" in out
    assert "- [warning] 0-1-weak-prng" in out or "- [ [warning]" in out
    assert "not actually curated" in out


def test_risks_section_orders_curated_before_raw():
    """RiskSynthesizer items must render before SARIF items
    regardless of input order — the auditor reads the LLM's
    prioritization first."""
    ctx = {
        "finding_annotations": [
            _raw("raw-slither-rule: low-level call without check"),
            _curated("[reentrancy-candidates] callback reentry surface"),
            _raw("another-raw: arbitrary send"),
            _curated("[hotspots] high CC + tainted"),
        ],
    }
    out = _render_risks(ctx)
    # Both curated items appear before either raw item.
    curated_pos = min(
        out.index("[[risks/reentrancy-candidates"),
        out.index("[[risks/hotspots"),
    )
    raw_pos = min(
        out.index("- raw-slither-rule"),
        out.index("- another-raw"),
    )
    assert curated_pos < raw_pos, (
        f"curated items at {curated_pos} must precede raw items "
        f"at {raw_pos}"
    )


def test_risks_section_renders_empty_when_no_findings():
    """Empty list, missing key, and all-empty-descriptions all
    produce the canonical empty-section message."""
    for ctx in (
        {},
        {"finding_annotations": []},
        {"finding_annotations": None},
        {"finding_annotations": [_finding(""), _finding("   ")]},
    ):
        out = _render_risks(ctx)
        assert out == "## Risks\n\n_No risks recorded._\n", (
            f"unexpected output for ctx={ctx!r}: {out!r}"
        )


def test_risks_section_parses_kebab_case_risk_names():
    """The full chunk 4.5 allowlist must parse: single token,
    single-hyphen, multi-hyphen kebab-case."""
    ctx = {
        "finding_annotations": [
            _finding("[hotspots] short name"),
            _finding("[delegatecall-sites] one-hyphen"),
            _finding("[reentrancy-candidates] two-hyphen"),
            _finding("[a1b2-c3d4-e5f6] alphanumeric kebab"),
        ],
    }
    out = _render_risks(ctx)
    assert "[[risks/hotspots|hotspots]]" in out
    assert "[[risks/delegatecall-sites|delegatecall-sites]]" in out
    assert (
        "[[risks/reentrancy-candidates|reentrancy-candidates]]" in out
    )
    assert "[[risks/a1b2-c3d4-e5f6|a1b2-c3d4-e5f6]]" in out


def test_risks_section_flattens_newlines_in_description():
    """Chunk 4.7 review fix (Findings 1+2): attacker-supplied
    Solidity comments propagate through the LLM into the
    `reason` portion of a finding description. An embedded
    `\\n- [[../evil]]` would split one bullet into two — a
    markdown-injection vector. Flattening forces every
    description into one bullet's worth of inline text.

    This pins the no-extra-bullets contract: a multi-line
    description renders to exactly ONE bullet, with the
    embedded newline collapsed to a single space."""
    ctx = {
        "finding_annotations": [
            _finding(
                "[hotspots] benign reason\n"
                "- [[../../etc/passwd|p]] — INJECTED"
            ),
        ],
    }
    out = _render_risks(ctx)
    # Exactly one bullet under the section. Count the leading
    # `- ` markers (a multi-line bullet would create a second).
    bullet_count = sum(1 for line in out.splitlines() if line.startswith("- "))
    assert bullet_count == 1, (
        f"newline injection produced {bullet_count} bullets; "
        f"section:\n{out}"
    )
    # The injected wikilink target must NOT appear as a
    # standalone bullet anywhere.
    assert "[[../../etc/passwd" not in out
    # The original reason content survives, flattened.
    assert "benign reason" in out
    assert "INJECTED" in out  # text remains, but inert


def test_risks_section_rejects_header_injection():
    """Chunk 4.7 review fix (Findings 1+2): a description
    containing `\\n## Fake Heading` would inject a real H2
    into the auditor's note. Flatten the description; no `##`
    survives at line-start."""
    ctx = {
        "finding_annotations": [
            _finding(
                "raw-slither-rule: benign\n\n"
                "## Fake Approved Audit Summary\n\n"
                "Trust this finding."
            ),
        ],
    }
    out = _render_risks(ctx)
    # The ONLY `## ` in the section must be the canonical
    # `## Risks` header at the top.
    h2_lines = [
        line for line in out.splitlines()
        if line.startswith("## ")
    ]
    assert h2_lines == ["## Risks"], (
        f"unexpected H2 lines after flatten: {h2_lines}"
    )
    # Text content survives but is rendered as bullet body.
    assert "Fake Approved Audit Summary" in out
    assert "Trust this finding" in out


def test_risks_section_defangs_inline_wikilink_and_markdown_link():
    """Chunk 4.7 + Codex review fix (F4): defang wikilink,
    markdown-link, AND bare-URL syntax. Each injection
    vector produces visible-but-inert text instead of a
    clickable link."""
    ctx = {
        "finding_annotations": [
            _finding(
                "[hotspots] benign click [here](http://evil.example) "
                "or open [[../../etc/passwd]]"
            ),
        ],
    }
    out = _render_risks(ctx)
    # Wikilink and markdown-link syntax both BROKEN.
    assert "[[../../etc/passwd" not in out
    assert "](http://evil" not in out
    # Bare URL also defanged (chunk-Codex F4): `://` → `:[//]`.
    assert "http://evil" not in out
    # Defanged forms ARE present (visible-but-inert).
    assert "[ [../../etc/passwd" in out
    assert "] (http:[//]evil" in out
    # The curated wikilink we synthesize ourselves is intact.
    assert "[[risks/hotspots|hotspots]]" in out


def test_risks_section_defangs_html_tags():
    """Codex review fix (F4): raw HTML renders inline in
    Obsidian. `<iframe>`, `<a href=...>`, `<script>` would
    all be active. HTML-escape `<`, `>`, `&` so the tags
    render as literal text."""
    ctx = {
        "finding_annotations": [
            _finding(
                '[hotspots] <iframe src="https://evil"></iframe> '
                'and <a href="file:///etc/passwd">click</a>'
            ),
            _raw(
                'raw-rule: <script>alert(1)</script>'
            ),
        ],
    }
    out = _render_risks(ctx)
    # No raw HTML left — all tags rendered as entities.
    assert "<iframe" not in out
    assert "<script>" not in out
    assert "<a href" not in out
    # Entity-encoded forms appear (visible, inert).
    assert "&lt;iframe" in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out


def test_risks_section_defangs_dangerous_uri_schemes():
    """Codex review fix (F4): schemes that don't use `://`
    must be defanged inline. `javascript:`, `data:`, `file:`,
    `obsidian:` are all click-handlers an attacker could
    point at malicious targets."""
    ctx = {
        "finding_annotations": [
            _finding(
                "[hotspots] visit javascript:alert(1) "
                "or data:text/html,<script>x</script> "
                "or file:///etc/passwd "
                "or obsidian://action?cmd=evil"
            ),
        ],
    }
    out = _render_risks(ctx)
    # All bare-scheme URIs defanged: `scheme:` → `scheme[:]`.
    assert "javascript:" not in out
    assert "data:" not in out
    assert "file:" not in out
    assert "obsidian:" not in out
    assert "javascript[:]" in out
    assert "data[:]" in out
    assert "file[:]" in out
    assert "obsidian[:]" in out


def test_risks_section_defangs_code_fences():
    """Codex review fix (F4): a fenced code block in the
    finding description could hide instruction-like text the
    auditor might copy-paste (`rm -rf $HOME/.ssh`). Three+
    backticks → HTML-entity backticks. The visual stays
    similar but the parser doesn't recognize a fence."""
    ctx = {
        "finding_annotations": [
            _raw(
                "rule: ```bash\nrm -rf $HOME/.ssh\n```"
            ),
        ],
    }
    out = _render_risks(ctx)
    # No 3-backtick run left — replaced with entity form.
    assert "```" not in out
    # Entity form present (renders as visible backticks but
    # NOT as code-fence delimiter).
    assert "&#x60;&#x60;&#x60;" in out
    # The instruction text survives (entity-escaped only
    # where it has HTML-special chars; `$` is preserved).
    assert "rm -rf" in out


def test_risks_section_defangs_dataview_inline_queries():
    """Codex round-19 fix: Obsidian's Dataview plugin
    EXECUTES single-backtick inline queries:
        `= some_dql_expression`
        `$= some_dataviewjs_expression`
    If enabled, an attacker-controlled finding description
    with such a payload runs arbitrary Dataview JavaScript
    in the auditor's vault. `_defang_text` now escapes ALL
    backticks (not just 3+ fenced runs), so the inline
    query never reaches the Dataview parser.

    This is the load-bearing test for the round-19
    single-backtick defense — three vectors covered:
    Dataview-DQL (`= ...`), DataviewJS (`$= ...`), and
    plain single-backtick code spans (which Dataview
    doesn't execute but other plugins might)."""
    ctx = {
        "finding_annotations": [
            _raw("DQL: `= file.size` snooping vault metadata"),
            _raw("DJS: `$= dv.pages('').file.path` exfil"),
            _raw("inline: `function_name()` reference"),
        ],
    }
    out = _render_risks(ctx)
    # No single backticks survived ANYWHERE.
    assert "`" not in out, (
        f"backtick reached the rendered output: {out}"
    )
    # Entity form present (renders as visible `` but the
    # parser doesn't recognize it as a code-span / Dataview
    # delimiter).
    assert "&#x60;= file.size&#x60;" in out
    assert "&#x60;$= dv.pages" in out
    assert "&#x60;function_name()&#x60;" in out


def test_risks_section_dedupes_duplicate_annotations():
    """Chunk 4.7 review fix (Finding 4): re-running
    RiskSynthesizer would attach duplicate annotations to the
    graph (Trailmark's annotate is append-only). Without
    dedup, every re-render doubles every bullet. The render
    layer collapses duplicates by flattened-description
    equality, preserving first-seen order."""
    ctx = {
        "finding_annotations": [
            _finding("[hotspots] dup reason"),
            _finding("raw-rule: also dup"),
            _finding("[hotspots] dup reason"),  # exact dup
            _finding("raw-rule: also dup"),  # exact dup
            _finding(
                "[hotspots]   dup reason  "
            ),  # whitespace-variant: also dup after flatten
        ],
    }
    out = _render_risks(ctx)
    # Each bullet appears exactly once.
    assert out.count("[[risks/hotspots|hotspots]] — dup reason") == 1
    assert out.count("- raw-rule: also dup") == 1
    # Total bullet count is 2 (one curated + one raw).
    bullet_count = sum(1 for line in out.splitlines() if line.startswith("- "))
    assert bullet_count == 2, (
        f"expected 2 deduped bullets; got {bullet_count}. "
        f"Section:\n{out}"
    )


def test_risks_section_falls_back_on_malformed_prefix():
    """LLM hallucinations of prefixes that violate the chunk
    4.5 kebab-case allowlist fall through to plain-text
    rendering — never produce broken wikilinks. Same defensive
    posture as the `_RISK_NAME_RE` allowlist enforces."""
    bad_prefixes = (
        "[Hotspots] uppercase rejected",
        "[has space] space rejected",
        "[trailing-] trailing hyphen rejected",
        "[-leading] leading hyphen rejected",
        "[a--b] double hyphen rejected",
        "[a_b] underscore rejected",
        "[a.b] dot rejected",
        "no brackets at all",
        "[] empty brackets",
    )
    for desc in bad_prefixes:
        ctx = {"finding_annotations": [_finding(desc)]}
        out = _render_risks(ctx)
        # The description appears as plain text in a bullet.
        assert f"- {desc}" in out, (
            f"expected plain bullet for {desc!r}, got: {out!r}"
        )
        # And no wikilink was synthesized for it.
        assert "[[risks/" not in out, (
            f"malformed prefix {desc!r} produced a wikilink: "
            f"{out!r}"
        )


# ----------------------------------------------------------------
# Integration — render_and_write_node_note pulls from the graph
# ----------------------------------------------------------------

@pytest.fixture
def fresh_tier0(tier0_dir, tmp_path):
    """Fresh parse + per-test cache. Mutations don't leak;
    mirrors tests/test_annotations.py fresh_tier0 fixture."""
    cache_root = tmp_path / "cache"
    gid = trailmark_parse(
        str(tier0_dir), language="solidity", cache_root=cache_root
    )
    return gid, cache_root


def test_node_overview_defangs_markdown_injection(
    fresh_tier0, tmp_path,
):
    """Codex round-13 fix: `_render_overview` interpolates
    LLM-authored body verbatim, and node notes go through
    this path. Block-level + inline injection (heading,
    wikilink, HTML) must be neutralized inside the
    overview."""
    from src.render.obsidian import render_and_write_node_note
    from src.tools import get_node

    gid, cache_root = fresh_tier0
    node = get_node(
        gid, "src.tokens.ERC20:ERC20", cache_root=cache_root,
    )
    malicious_body = (
        "Real prose.\n\n"
        "## INJECTED HEADING\n\n"
        "[[../../etc/passwd]] "
        '<iframe src="https://evil"></iframe>'
    )
    out = render_and_write_node_note(
        tmp_path, gid, node, {}, malicious_body,
        cache_root=cache_root,
    )
    body = Path(out).read_text()
    # No active wikilink / HTML / line-start H2.
    assert "[[../../etc/passwd]]" not in body
    assert "<iframe" not in body
    import re as _re
    # The only H2s should be canonical section headings,
    # not the attacker's "INJECTED HEADING".
    injected = _re.findall(
        r"^## INJECTED .*$", body, flags=_re.MULTILINE,
    )
    assert injected == [], (
        f"line-start H2 injection survived; lines: {injected}"
    )


def test_render_annotations_defangs_field_injection():
    """Codex round-13 fix: `_render_annotations`
    interpolates LLM-authored `kind`, `source`, and
    `description` into bullet lines. Defang each so
    embedded markdown/HTML/URI-scheme content can't break
    out of the bullet."""
    from src.render.obsidian import _render_annotations

    ctx = {
        "annotations": [
            {
                "kind": "assumption",
                "source": "<iframe src=https://evil>",
                "description": (
                    "Plain text.\n## INJECTED HEADING\n"
                    "[[../../etc/passwd]] "
                    "Click file:///etc/shadow"
                ),
            },
        ],
    }
    out = _render_annotations(ctx)
    # No raw HTML, wikilink, or dangerous URI scheme.
    assert "<iframe" not in out
    assert "[[../../etc/passwd]]" not in out
    assert "file:///etc/shadow" not in out
    # No line-start H2 (the multi-line description was
    # flattened to one line, so no `\n## ` survives).
    import re as _re
    h2_lines = _re.findall(
        r"^## .+$", out, flags=_re.MULTILINE,
    )
    assert h2_lines == ["## Annotations"], (
        f"unexpected H2s: {h2_lines}"
    )
    # Defanged inline forms survive (visible but inert).
    assert "&lt;iframe" in out
    assert "[ [../../etc/passwd]]" in out
    assert "file[:]" in out


def test_render_annotations_filters_out_finding_kind():
    """Cross-cutting review fix (I1): the Annotations section
    must NOT render `kind="finding"` entries. Those are owned
    by the Risks section, populated automatically by
    `render_and_write_node_note` from the graph. If an LLM
    accidentally passes finding-kind entries in
    `graph_ctx["annotations"]`, filter them out at render
    time rather than producing double bullets across the two
    sections."""
    from src.render.obsidian import _render_annotations

    ctx = {
        "annotations": [
            {"kind": "assumption", "source": "node-documenter",
             "description": "transfer assumes prior approval"},
            {"kind": "finding", "source": "risk-synthesizer",
             "description": "[hotspots] would-double-render"},
            {"kind": "invariant", "source": "node-documenter",
             "description": "totalSupply >= sum(balances)"},
            {"kind": "finding", "source": "sarif:Slither",
             "description": "raw sarif finding that snuck in"},
        ],
    }
    out = _render_annotations(ctx)
    # Non-finding kinds appear.
    assert "**assumption**" in out
    assert "transfer assumes prior approval" in out
    assert "**invariant**" in out
    # Codex round-13 fix: annotation description is now
    # HTML-escaped (`>` → `&gt;`). Visually identical in
    # rendered HTML but the source is defanged so a
    # malicious `>` followed by attacker HTML can't break
    # out of the bullet body.
    assert "totalSupply &gt;= sum(balances)" in out
    # Finding-kind entries are SILENTLY DROPPED here (rendered
    # by _render_risks instead). They must not appear under
    # the Annotations section.
    assert "**finding**" not in out
    assert "would-double-render" not in out
    assert "raw sarif finding" not in out


def test_method_findings_bubble_up_to_parent_contract(
    fresh_tier0, tmp_path,
):
    """Codex review fix (F3): slither/semgrep attach findings
    at method-level granularity (e.g.,
    `contracts.UniswapV2Pair:UniswapV2Pair._update`). Methods
    are documented INSIDE their parent's note —
    render_and_write_node_note rejects method-kind nodes. So
    method-level findings must bubble up to the parent
    container's Risks section, otherwise the auditor never
    sees them.

    Pin the contract: annotate a method, render its parent
    contract, assert the finding appears in the parent
    note."""
    gid, cache_root = fresh_tier0
    method_id = "src.tokens.ERC20:ERC20.transfer"
    parent_id = "src.tokens.ERC20:ERC20"

    # Method-level finding (raw SARIF-shape).
    annotate(
        gid, method_id, "finding",
        "1-1-reentrancy-no-eth: external call before state write",
        source="sarif:Slither", cache_root=cache_root,
    )

    parent_node = get_node(gid, parent_id, cache_root=cache_root)
    vault = tmp_path / "vault"
    out_path = render_and_write_node_note(
        vault, gid, parent_node, {}, "Overview.",
        cache_root=cache_root,
    )
    body = Path(out_path).read_text()

    # Method finding bubbles up — appears in parent's Risks
    # section as a plain bullet (SARIF source).
    assert "## Risks" in body
    assert "_No risks recorded._" not in body
    assert "1-1-reentrancy-no-eth" in body, (
        f"method-level finding must bubble up to parent note; "
        f"body:\n{body}"
    )


def test_finding_bubble_filters_to_container_descendants(
    fresh_tier0, tmp_path,
):
    """Codex review fix (F3): the prefix filter must NOT
    accidentally pick up unrelated nodes. ID prefix matching
    is strict: `<container_id>.` (with trailing dot)."""
    gid, cache_root = fresh_tier0
    # Attach finding to an UNRELATED method (different parent).
    annotate(
        gid, "src.tokens.ERC4626:ERC4626.deposit", "finding",
        "unrelated-finding-do-not-bubble",
        source="sarif:Slither", cache_root=cache_root,
    )
    # Render ERC20 (the OTHER contract). Its Risks section
    # should be empty because the finding is on ERC4626.
    parent_node = get_node(
        gid, "src.tokens.ERC20:ERC20", cache_root=cache_root,
    )
    vault = tmp_path / "vault"
    out_path = render_and_write_node_note(
        vault, gid, parent_node, {}, "Overview.",
        cache_root=cache_root,
    )
    body = Path(out_path).read_text()
    assert "unrelated-finding-do-not-bubble" not in body, (
        f"prefix filter leaked finding from another container; "
        f"body:\n{body}"
    )
    assert "_No risks recorded._" in body


def test_render_and_write_node_note_fetches_finding_annotations(
    fresh_tier0, tmp_path,
):
    """End-to-end pin for chunk 4.7: annotate a node with a
    curated finding, render through the public entry point,
    read the written file, assert the Risks section contains
    the wikilink to the risk note. This is the canonical proof
    for CHUNKS.md's success criterion ('A Tier 1 contract note
    with a slither finding shows the finding inline')."""
    gid, cache_root = fresh_tier0
    node_id = "src.tokens.ERC20:ERC20"

    # Curated annotation (matches RiskSynthesizer's contract).
    annotate(
        gid, node_id, "finding",
        "[hotspots] balanceOf + transfer concentrate state mutations",
        source="risk-synthesizer", cache_root=cache_root,
    )
    # Raw SARIF-style annotation (no bracketed prefix).
    annotate(
        gid, node_id, "finding",
        "controlled-delegatecall: function delegates to user input",
        source="sarif:Slither", cache_root=cache_root,
    )

    node = get_node(gid, node_id, cache_root=cache_root)
    vault = tmp_path / "vault"
    out_path = render_and_write_node_note(
        vault, gid, node, {}, "Overview body.",
        cache_root=cache_root,
    )

    body = Path(out_path).read_text()
    assert "## Risks" in body
    # Curated item rendered as wikilink.
    assert (
        "[[risks/hotspots|hotspots]] — balanceOf + transfer "
        "concentrate state mutations" in body
    )
    # SARIF item rendered as plain bullet (no wikilink).
    assert (
        "- controlled-delegatecall: function delegates to user input"
        in body
    )
    # No "No risks recorded" placeholder.
    assert "_No risks recorded._" not in body


# ----------------------------------------------------------------
# Tier 1 binary-gated integration test (cross-cutting review I4)
# ----------------------------------------------------------------

TIER1_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "tier1_uniswap_v2"
)


def _solc_516_available() -> bool:
    """Mirrors test_augment_sarif.py + test_slither.py. Tier 1
    (UniswapV2) compiles with solc 0.5.16."""
    if shutil.which("solc-select") is None:
        return False
    try:
        proc = sp.run(
            ["solc-select", "versions"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        return "0.5.16" in proc.stdout
    except (sp.TimeoutExpired, FileNotFoundError):
        return False


@pytest.mark.skipif(
    shutil.which("slither") is None,
    reason="slither not on PATH",
)
@pytest.mark.skipif(
    not _solc_516_available(),
    reason="solc 0.5.16 not installed",
)
def test_tier1_real_slither_finding_renders_as_plain_bullet(
    tmp_path, monkeypatch,
):
    """Cross-cutting review fix (I4): pins the CHUNKS.md 4.7
    success criterion against REAL slither output, not
    synthetic annotations.

    Real Trailmark SARIF descriptions are shaped like
    `"[WARNING] 0-1-weak-prng: ... (Slither)"` — uppercase
    bracketed prefix. The current `_render_risks` filters by
    `source` (chunk-4.7 cross-cutting fix C1), so SARIF
    findings always route to the plain-bullet path regardless
    of description shape. This test pins that contract
    end-to-end: parse Tier 1, run slither, augment_sarif,
    render a node note for a contract with findings, assert
    the Risks section contains a real slither rule rendered
    as a plain bullet (NO wikilink). Catches any regression
    where SARIF findings accidentally match the curated path
    (e.g., a future Trailmark version that drops the .upper()
    on the level prefix)."""
    monkeypatch.setenv("SOLC_VERSION", "0.5.16")
    # TEST-ONLY: bypass build_analyzer_env's HOME isolation
    # so slither can reach ~/.solc-select/artifacts/ (Codex
    # follow-up F1 removed the symlink). Production runs use
    # the isolated env.
    import os
    monkeypatch.setattr(
        "src.analyzers.slither.build_analyzer_env",
        lambda **_kw: os.environ.copy(),
    )
    repo = tmp_path / "tier1"
    shutil.copytree(TIER1_FIXTURE, repo)
    cache_root = tmp_path / "cache"
    gid = trailmark_parse(
        str(repo), language="solidity", cache_root=cache_root,
    )
    sarif = tmp_path / "tier1.sarif"
    run_slither(
        repo / "contracts", sarif,
        project_root=repo, timeout=120.0,
    )
    result = augment_sarif(gid, sarif, cache_root=cache_root)
    assert result["matched_findings"] >= 1, (
        f"expected slither findings to attach; got {result}"
    )

    # Pin the cross-cutting Codex review fix (F3): slither
    # attaches findings at METHOD-level granularity, e.g.,
    # `contracts.UniswapV2Pair:UniswapV2Pair._update`.
    # render_and_write_node_note rejects method-kind nodes
    # (they document inside parents). So we must render the
    # CONTAINER and assert method findings bubble up.
    finding_nodes = nodes_with_annotation(
        gid, "finding", cache_root=cache_root,
    )
    assert finding_nodes, "expected ≥1 node with findings"

    # Find a method-kind finding node, then render its
    # parent container.
    method_nid = None
    for n in finding_nodes:
        if n.get("kind") == "method":
            method_nid = n["id"]
            break
    assert method_nid is not None, (
        f"expected ≥1 method-kind finding on Tier 1; got "
        f"{[(n.get('kind'), n.get('id')) for n in finding_nodes]}"
    )
    # Method ID is `<container>.<method_name>` — strip the
    # last `.<segment>` to get the container.
    container_nid = method_nid.rsplit(".", 1)[0]
    container_node = get_node(
        gid, container_nid, cache_root=cache_root,
    )

    vault = tmp_path / "vault"
    out_path = render_and_write_node_note(
        vault, gid, container_node, {}, "Overview body.",
        cache_root=cache_root,
    )
    body = Path(out_path).read_text()

    # Section is populated (not the empty placeholder) —
    # proves the method-level finding bubbled up to the
    # parent container's note.
    assert "## Risks" in body
    assert "_No risks recorded._" not in body, (
        f"expected method finding to bubble up to parent "
        f"container note. method_nid={method_nid}, "
        f"container_nid={container_nid}, body:\n{body}"
    )

    # Real slither descriptions are bracketed but UPPERCASE
    # and source="sarif:Slither" — must render as plain
    # bullet, NOT as a `[[risks/...]]` wikilink.
    assert "[[risks/" not in body, (
        f"SARIF findings must NOT produce risks/ wikilinks; "
        f"body:\n{body}"
    )

    # At least one slither-shaped rule ID appears in the
    # rendered output. Slither rule IDs match `\d-\d-...`
    # (e.g., 0-1-weak-prng, 1-1-reentrancy-no-eth).
    import re
    assert re.search(r"\d-\d-[a-z-]+", body), (
        f"expected at least one slither rule ID in Risks "
        f"section; body:\n{body}"
    )
