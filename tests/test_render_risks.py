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

from pathlib import Path

import pytest

from src.render.obsidian import (
    _render_risks,
    render_and_write_node_note,
)
from src.tools import annotate, get_node, trailmark_parse


# ----------------------------------------------------------------
# Unit tests — _render_risks parsing/formatting
# ----------------------------------------------------------------

def _finding(description: str, source: str = "test") -> dict:
    """Build a finding-annotation dict in the shape
    `annotations_of(kind="finding")` returns."""
    return {"kind": "finding", "source": source, "description": description}


def test_risks_section_renders_curated_wikilink():
    """A `[<risk_name>] reason`-shaped description renders as a
    wikilink to vault/risks/<risk_name>.md plus the reason."""
    ctx = {
        "finding_annotations": [
            _finding("[hotspots] swap holds state across external call"),
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
    """An annotation whose description lacks the bracketed
    kebab-case prefix (raw SARIF output from slither) renders
    as a plain bullet with NO wikilink."""
    ctx = {
        "finding_annotations": [
            _finding("controlled-delegatecall: function delegates to user input"),
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


def test_risks_section_orders_curated_before_raw():
    """RiskSynthesizer items must render before SARIF items
    regardless of input order — the auditor reads the LLM's
    prioritization first."""
    ctx = {
        "finding_annotations": [
            _finding("raw-slither-rule: low-level call without check"),
            _finding("[reentrancy-candidates] callback reentry surface"),
            _finding("another-raw: arbitrary send"),
            _finding("[hotspots] high CC + tainted"),
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
    """Chunk 4.7 review fix: even after whitespace flattening,
    attacker-chosen wikilink targets `[[../../etc/passwd]]`
    and markdown links `[trusted](http://evil)` embedded in a
    single-line description would still render as clickable
    links. Defang both inline by inserting a space inside the
    syntax markers — visible text survives, link is inert."""
    ctx = {
        "finding_annotations": [
            _finding(
                "[hotspots] benign click [here](http://evil.example) "
                "or open [[../../etc/passwd]]"
            ),
        ],
    }
    out = _render_risks(ctx)
    # The injected wikilink/markdown-link syntax is BROKEN:
    # `[[` becomes `[ [` and `](` becomes `] (`.
    assert "[[../../etc/passwd" not in out
    assert "](http://evil" not in out
    # Defanged forms ARE present (visible-but-inert).
    assert "[ [../../etc/passwd" in out
    assert "] (http://evil" in out
    # The curated wikilink we synthesize ourselves is intact.
    assert "[[risks/hotspots|hotspots]]" in out


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
