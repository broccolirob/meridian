import yaml

from src.render.obsidian import (
    VAULT_SUBDIRS,
    ensure_vault,
    write_obsidian_note,
)


def test_ensure_vault_creates_skeleton(tmp_path):
    vault = ensure_vault(tmp_path / "vault")
    assert vault.is_dir()
    for sub in VAULT_SUBDIRS:
        assert (vault / sub).is_dir(), f"missing subdir: {sub}"


def test_ensure_vault_is_idempotent(tmp_path):
    vault_path = tmp_path / "vault"
    ensure_vault(vault_path)
    ensure_vault(vault_path)
    for sub in VAULT_SUBDIRS:
        assert (vault_path / sub).is_dir()


def test_write_obsidian_note_creates_file_at_rel_path(tmp_path):
    vault = ensure_vault(tmp_path / "vault")
    path = write_obsidian_note(
        vault, "contracts/Pair.md", {"name": "Pair"}, "Body."
    )
    assert path == vault / "contracts" / "Pair.md"
    assert path.exists()


def test_frontmatter_round_trips(tmp_path):
    vault = ensure_vault(tmp_path / "vault")
    fm = {
        "type": "contract",
        "name": "Pair",
        "node_id": "contracts.Pair:Pair",
        "language": "solidity",
        "tags": ["defi", "amm"],
        "annotations": {"assumptions": 3, "findings": 1},
    }
    path = write_obsidian_note(vault, "contracts/Pair.md", fm, "body")
    text = path.read_text(encoding="utf-8")

    assert text.startswith("---\n")
    end = text.index("\n---\n", 4)
    parsed = yaml.safe_load(text[4:end])
    assert parsed == fm


def test_multiline_body_preserved(tmp_path):
    vault = ensure_vault(tmp_path / "vault")
    body = "# Heading\n\nFirst para.\n\nSecond para with `code`.\n"
    path = write_obsidian_note(
        vault, "contracts/Pair.md", {"name": "Pair"}, body
    )
    text = path.read_text(encoding="utf-8")
    assert text.endswith(body)


def test_write_creates_parent_dirs_lazily(tmp_path):
    raw_vault = tmp_path / "raw"
    path = write_obsidian_note(
        raw_vault, "flows/deep/nested/swap.md", {}, "content"
    )
    assert path.exists()
    assert path.read_text(encoding="utf-8") == "content\n"


def test_write_rejects_path_traversal(tmp_path):
    import pytest

    vault = ensure_vault(tmp_path / "vault")
    # `..` escape
    with pytest.raises(ValueError, match="escapes vault"):
        write_obsidian_note(vault, "../escaped.md", {}, "x")
    # absolute path escape (Path / absolute drops the left side)
    with pytest.raises(ValueError, match="escapes vault"):
        write_obsidian_note(vault, "/tmp/escaped.md", {}, "x")
    # Nothing got written outside the vault
    assert not (tmp_path / "escaped.md").exists()
