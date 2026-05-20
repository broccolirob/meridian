from pathlib import Path

import pytest
import yaml

GOLDEN_DIR = Path(__file__).parent / "golden"

REQUIRED_FRONTMATTER_KEYS = {
    "name",
    "kind",
    "node_id",
    "file",
    "lines",
    "loc",
    "callers_count",
    "callees_count",
}

REQUIRED_HEADINGS = [
    "## Overview",
    "## Graph context",
    "## State",
    "## Functions",
    "## Events / Errors / Modifiers",
    "## Annotations",
    "## Risks",
]


def _split_note(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        pytest.fail(f"{path.name}: missing leading --- frontmatter marker")
    end = text.index("\n---\n", 4)
    fm = yaml.safe_load(text[4:end])
    body = text[end + 5 :]
    return fm, body


def _golden_paths() -> list[Path]:
    return sorted(GOLDEN_DIR.glob("*.md"))


@pytest.mark.parametrize(
    "golden_path",
    _golden_paths(),
    ids=lambda p: p.name,
)
def test_golden_has_canonical_frontmatter(golden_path):
    fm, _ = _split_note(golden_path)
    missing = REQUIRED_FRONTMATTER_KEYS - set(fm.keys())
    assert not missing, f"missing frontmatter keys: {missing}"


@pytest.mark.parametrize(
    "golden_path",
    _golden_paths(),
    ids=lambda p: p.name,
)
def test_golden_has_all_seven_sections(golden_path):
    _, body = _split_note(golden_path)
    for heading in REQUIRED_HEADINGS:
        assert heading in body, (
            f"{golden_path.name}: missing heading {heading!r}"
        )


@pytest.mark.parametrize(
    "golden_path",
    _golden_paths(),
    ids=lambda p: p.name,
)
def test_golden_overview_is_real(golden_path):
    _, body = _split_note(golden_path)
    assert "_Overview not yet written._" not in body, (
        f"{golden_path.name}: Overview still has placeholder text"
    )


def _strip_fenced_blocks(text: str) -> str:
    """Drop ```...``` regions. Mermaid blocks in Graph context
    use alias decls + classDef lines that legitimately contain
    no wikilinks — they're diagrams, not link lists. The
    populated-lines check below targets the
    Inheritance/Implements/Uses/Callers/Callees lists only."""
    out = []
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return "\n".join(out)


@pytest.mark.parametrize(
    "golden_path",
    _golden_paths(),
    ids=lambda p: p.name,
)
def test_golden_graph_context_wikilinks_when_populated(golden_path):
    """If any Graph context list-subsection (Inheritance, Implements,
    Uses, Callers, Callees) is populated (not just an italic
    placeholder), it must use wikilink syntax — not plain text.

    Genuinely isolated nodes (e.g. a leaf library called only via
    Solidity's `using for` syntax that Trailmark doesn't capture as
    edges) legitimately have all-placeholder lists. Don't fail on
    those."""
    _, body = _split_note(golden_path)
    start = body.index("## Graph context")
    end = body.index("\n## ", start + 1)
    section = _strip_fenced_blocks(body[start:end])
    populated_lines = [
        ln for ln in section.splitlines()
        if ln.strip()
        and not ln.startswith("#")
        and not (ln.startswith("_") and ln.endswith("_"))
    ]
    if not populated_lines:
        pytest.skip(
            f"{golden_path.name}: Graph context lists are entirely "
            f"placeholders (isolated node — no wikilinks required)"
        )
    assert "[[" in section and "]]" in section, (
        f"{golden_path.name}: Graph context has populated entries "
        f"but no wikilink syntax — likely plain-text references"
    )
