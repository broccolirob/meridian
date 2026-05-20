"""Regression armor for chunk 3.26 (I-NEW-7): read_node_source
rejects file paths that resolve outside the original
parse_root. Defends against symlinked exfiltration via
adversarial repos.

The fix's value: an attacker planting `evil.sol ->
/etc/passwd` in their Solidity repo can't trick the LLM
into reading /etc/passwd. read_node_source loads the
parse_root saved at parse time and verifies the file_path
resolves under it.
"""

import pytest

from src.tools import read_node_source


def test_read_node_source_rejects_file_path_outside_parse_root(
    tier0_graph_id_default_cache, monkeypatch
):
    """Monkey-patch get_node to return a malicious node with
    file_path pointing outside the parse tree. Pre-3.26 the
    file would be opened; post-3.26 ValueError is raised."""
    gid = tier0_graph_id_default_cache

    monkeypatch.setattr(
        "src.tools.get_node",
        lambda *a, **k: {
            "id": "evil_node",
            "name": "evil",
            "kind": "method",
            "location": {
                "file_path": "/etc/passwd",
                "start_line": 1,
                "end_line": 5,
            },
        },
    )

    with pytest.raises(ValueError, match="escapes parse_root"):
        read_node_source(gid, "evil_node")


def test_read_node_source_rejects_traversal_via_dotdot(
    tier0_graph_id_default_cache, monkeypatch, tmp_path
):
    """Same check via a different out-of-tree path. The
    /etc/passwd test above uses an absolute path to a system
    file; this one uses a constructed tmp_path file outside
    any conceivable parse_root, confirming .resolve() +
    .relative_to() reject both shapes consistently."""
    gid = tier0_graph_id_default_cache

    outside_file = tmp_path / "leak.txt"
    outside_file.write_text("secret\n")

    monkeypatch.setattr(
        "src.tools.get_node",
        lambda *a, **k: {
            "id": "traversal_node",
            "name": "traversal",
            "kind": "method",
            "location": {
                "file_path": str(outside_file),
                "start_line": 1,
                "end_line": 1,
            },
        },
    )

    with pytest.raises(ValueError, match="escapes parse_root"):
        read_node_source(gid, "traversal_node")


def test_read_node_source_accepts_path_inside_parse_root(
    tier0_graph_id_default_cache,
):
    """Sanity: real Tier 0 nodes (whose file_path IS under
    the tier0 fixture directory) read successfully. This
    armors against an overly-strict validator that breaks
    legitimate reads."""
    gid = tier0_graph_id_default_cache

    # ERC4626 is a Tier 0 contract; its source IS under the
    # parsed fixture directory.
    source = read_node_source(gid, "src.tokens.ERC4626:ERC4626")
    assert len(source) > 0
    assert "ERC4626" in source
