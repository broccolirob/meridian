"""Tests for src/render/diff_md.py + main.py CLI.

Reuses the mutate-at-test-time pattern from tests/test_diff.py
(chunk 5.1): copy tier1_uniswap_v2/contracts/ to two tmp dirs,
mutate the `after` copy, parse both, render the diff note.
"""

import shutil
from pathlib import Path

import pytest

from main import _resolve_to_graph_id, cli
from src.render.diff_md import (
    render_and_write_diff_note,
    render_diff_note,
)
from src.tools import diff_graphs, trailmark_parse

TIER1_CONTRACTS = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "tier1_uniswap_v2"
    / "contracts"
)


@pytest.fixture
def tier1_before_after(tmp_path):
    """Two tmp copies of Tier 1 contracts + a cache dir.
    Tests mutate the `after` copy then parse both into the
    shared cache."""
    before = tmp_path / "before"
    after = tmp_path / "after"
    shutil.copytree(TIER1_CONTRACTS, before)
    shutil.copytree(TIER1_CONTRACTS, after)
    return before, after, tmp_path / "cache"


def _parse_two(before: Path, after: Path, cache: Path) -> tuple[str, str]:
    bid = trailmark_parse(
        str(before), language="solidity", cache_root=cache,
    )
    aid = trailmark_parse(
        str(after), language="solidity", cache_root=cache,
    )
    return bid, aid


def test_render_diff_note_pure_function():
    """Pure-function contract: same diff dict → same output.
    Pin frontmatter keys + every canonical body heading."""
    synthetic_diff = {
        "summary_delta": {
            "nodes": {"before": 10, "after": 12, "delta": 2},
        },
        "nodes": {
            "added": [
                {
                    "id": "mod:Foo",
                    "name": "Foo",
                    "kind": "contract",
                    "file": "Foo.sol",
                    "cyclomatic_complexity": None,
                },
            ],
            "removed": [],
            "modified": [],
        },
        "edges": {"added": [], "removed": []},
        "entrypoints": {
            "added": [],
            "removed": [],
            "modified": [],
        },
    }
    fm1, body1 = render_diff_note(
        synthetic_diff, before_label="aaaa1111", after_label="bbbb2222",
    )
    fm2, body2 = render_diff_note(
        synthetic_diff, before_label="aaaa1111", after_label="bbbb2222",
    )
    # Deterministic.
    assert fm1 == fm2
    assert body1 == body2
    # Frontmatter shape.
    required_keys = {
        "type", "before", "after",
        "nodes_added", "nodes_removed", "nodes_modified",
        "edges_added", "edges_removed",
        "entrypoints_added", "entrypoints_removed",
        "entrypoints_modified",
    }
    assert required_keys <= set(fm1.keys())
    assert fm1["type"] == "diff"
    assert fm1["nodes_added"] == 1
    assert fm1["entrypoints_added"] == 0
    # Canonical body headings present.
    for heading in [
        "# Diff: aaaa1111 → bbbb2222",
        "## Summary",
        "## Attack surface changes",
        "### Added entrypoints",
        "### Removed entrypoints",
        "### Modified entrypoints (trust / asset shifts)",
        "## Structural changes",
        "### Added nodes",
        "### Removed nodes",
        "### Modified nodes",
        "## Edge changes",
        "### Added edges",
        "### Removed edges",
    ]:
        assert heading in body1, f"missing heading: {heading}"
    # Empty subsections render as `_None._` placeholders.
    assert body1.count("_None._") >= 6


def test_render_and_write_diff_note_writes_under_diffs_subdir(
    tier1_before_after, tmp_path,
):
    """End-to-end: parse two Tier 1 snapshots, diff, write.
    File lands at vault/diffs/<before8>-<after8>.md."""
    before, after, cache = tier1_before_after
    # Mutation: add a new contract so there's a meaningful diff.
    (after / "Extra.sol").write_text(
        "pragma solidity =0.5.16;\n"
        "contract Extra {\n"
        "    function newEntrypoint(uint x) external pure returns (uint) {\n"
        "        return x + 1;\n"
        "    }\n"
        "}\n"
    )
    bid, aid = _parse_two(before, after, cache)
    diff = diff_graphs(bid, aid, cache_root=cache)

    vault = tmp_path / "vault"
    written = render_and_write_diff_note(
        vault, diff, before_id=bid, after_id=aid,
    )
    path = Path(written)
    assert path.exists()
    # Filename is <before8>-<after8>.md under diffs/.
    assert path.parent.name == "diffs"
    assert path.name == f"{bid[:8]}-{aid[:8]}.md"
    # Valid YAML+markdown: frontmatter block + body.
    text = path.read_text()
    assert text.startswith("---\n")
    assert "type: diff" in text
    assert "# Diff:" in text


def test_render_diff_note_summary_table_omits_unchanged_metrics():
    """summary_delta keys are sparse — only present when the
    count changed. Renderer must show `_unchanged_` for
    omitted metrics so the table layout stays stable."""
    # Only entrypoints changed; nodes + edges unchanged.
    synthetic_diff = {
        "summary_delta": {
            "entrypoints": {
                "before": 5, "after": 6, "delta": 1,
            },
        },
        "nodes": {"added": [], "removed": [], "modified": []},
        "edges": {"added": [], "removed": []},
        "entrypoints": {
            "added": [
                {
                    "id": "mod:Foo.ep", "name": "ep",
                    "kind": "function", "file": "Foo.sol",
                    "cyclomatic_complexity": 1,
                },
            ],
            "removed": [],
            "modified": [],
        },
    }
    _fm, body = render_diff_note(
        synthetic_diff, before_label="a", after_label="b",
    )
    # nodes + edges show _unchanged_; entrypoints shows the
    # numeric row.
    assert "| nodes | — | — | _unchanged_ |" in body
    assert "| edges | — | — | _unchanged_ |" in body
    assert "| entrypoints | 5 | 6 | +1 |" in body


def test_render_diff_note_added_entrypoint_appears(
    tier1_before_after,
):
    """CHUNKS.md success criterion: diff note lists changed
    entrypoints. Mutate Tier 1 to add a new external function,
    assert the function name surfaces under Added entrypoints."""
    before, after, cache = tier1_before_after
    (after / "Extra.sol").write_text(
        "pragma solidity =0.5.16;\n"
        "contract Extra {\n"
        "    function newEntrypoint(uint x) external pure returns (uint) {\n"
        "        return x + 1;\n"
        "    }\n"
        "}\n"
    )
    bid, aid = _parse_two(before, after, cache)
    diff = diff_graphs(bid, aid, cache_root=cache)
    _fm, body = render_diff_note(
        diff, before_label="before", after_label="after",
    )
    # Locate the Added entrypoints section.
    added_section = body.split("### Added entrypoints", 1)[1]
    next_section = added_section.split("### ", 1)[0]
    assert "newEntrypoint" in next_section, (
        f"newEntrypoint missing under Added entrypoints: "
        f"{next_section}"
    )


def test_cli_diff_writes_note_with_path_args(
    tier1_before_after, tmp_path,
):
    """CLI accepts directory paths for before + after, parses
    both on the fly, writes the diff note. Exit code 0."""
    before, after, _cache = tier1_before_after
    # Mutation so the diff is non-trivial.
    (after / "Extra.sol").write_text(
        "pragma solidity =0.5.16;\n"
        "contract Extra { function ping() external pure {} }\n"
    )
    vault = tmp_path / "vault"
    # Chunk 5.4: vault moved from positional to top-level
    # --vault-path flag. Diff now takes only `before` +
    # `after` positionals.
    rc = cli([
        "--vault-path", str(vault),
        "diff", str(before), str(after),
    ])
    assert rc == 0
    diff_files = list((vault / "diffs").glob("*.md"))
    assert len(diff_files) == 1
    text = diff_files[0].read_text()
    assert "type: diff" in text


def test_cli_diff_accepts_graph_id_args(
    tier1_before_after, tmp_path,
):
    """CLI accepts 12-hex graph_id args for before + after
    when graphs are pre-parsed into the default cache.

    Uses the default `.meridian/graph/` cache (no explicit
    cache_root override at CLI layer in 5.2). Cleans up after
    itself so the test doesn't leak state across runs."""
    before, after, _cache_unused = tier1_before_after
    (after / "Extra.sol").write_text(
        "pragma solidity =0.5.16;\n"
        "contract Extra { function ping() external pure {} }\n"
    )
    # Pre-parse into the DEFAULT cache so the CLI's
    # _resolve_to_graph_id (which lacks a cache_root arg)
    # finds the graphs.
    bid = trailmark_parse(str(before), language="solidity")
    aid = trailmark_parse(str(after), language="solidity")
    try:
        vault = tmp_path / "vault"
        # Chunk 5.4: --vault-path is top-level.
        rc = cli(["--vault-path", str(vault), "diff", bid, aid])
        assert rc == 0
        diff_files = list((vault / "diffs").glob("*.md"))
        assert len(diff_files) == 1
        assert diff_files[0].name == f"{bid[:8]}-{aid[:8]}.md"
    finally:
        # Best-effort cache cleanup — default cache lives at
        # .meridian/graph/<gid>/ relative to cwd.
        from src.graph.persist import CACHE_ROOT
        for gid in (bid, aid):
            shutil.rmtree(
                CACHE_ROOT / gid, ignore_errors=True,
            )


def test_resolve_to_graph_id_rejects_nonexistent_path():
    """`_resolve_to_graph_id` raises SystemExit with a clear
    message when the arg is neither a 12-hex graph_id nor an
    existing directory."""
    with pytest.raises(SystemExit, match="neither a 12-hex"):
        _resolve_to_graph_id("not-a-path-or-hex")
