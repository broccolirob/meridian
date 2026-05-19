import sys
from pathlib import Path

# Make scripts/ importable for tests (matches the pattern used in
# document_one_node.py / document_repo.py).
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "scripts")
)

from validate_vault import (  # noqa: E402
    find_broken_links,
    main,
    strip_broken_links,
)


def _make_vault(tmp_path, files: dict[str, str]) -> Path:
    """Build a fake vault. `files` maps relative path -> body."""
    vault = tmp_path / "vault"
    for rel, body in files.items():
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return vault


def test_no_links_returns_empty(tmp_path):
    vault = _make_vault(tmp_path, {"contracts/A.md": "Just prose.\n"})
    assert find_broken_links(vault) == []


def test_resolved_path_link_is_not_broken(tmp_path):
    vault = _make_vault(
        tmp_path,
        {
            "contracts/A.md": "See [[contracts/B|B]].\n",
            "contracts/B.md": "I exist.\n",
        },
    )
    assert find_broken_links(vault) == []


def test_resolved_bare_link_is_not_broken(tmp_path):
    """Obsidian-style bare wikilinks should resolve via vault-wide
    search."""
    vault = _make_vault(
        tmp_path,
        {
            "contracts/A.md": "See [[B]].\n",
            "libraries/B.md": "I exist somewhere.\n",
        },
    )
    assert find_broken_links(vault) == []


def test_broken_path_link_is_reported(tmp_path):
    vault = _make_vault(
        tmp_path,
        {"contracts/A.md": "See [[contracts/Nope|Nope]].\n"},
    )
    broken = find_broken_links(vault)
    assert len(broken) == 1
    assert broken[0][1] == "contracts/Nope"


def test_strip_keeps_display_text(tmp_path):
    vault = _make_vault(
        tmp_path,
        {"contracts/A.md": "See [[contracts/Nope|the X function]].\n"},
    )
    broken = find_broken_links(vault)
    strip_broken_links(vault, broken)
    new = (vault / "contracts" / "A.md").read_text()
    assert new == "See the X function.\n"


def test_strip_bare_link_falls_back_to_target(tmp_path):
    vault = _make_vault(
        tmp_path, {"contracts/A.md": "See [[Nope]].\n"}
    )
    broken = find_broken_links(vault)
    strip_broken_links(vault, broken)
    new = (vault / "contracts" / "A.md").read_text()
    assert new == "See Nope.\n"


def test_main_exit_codes(tmp_path, monkeypatch):
    # 0: clean vault
    vault = _make_vault(
        tmp_path,
        {
            "contracts/A.md": "[[contracts/B|B]]",
            "contracts/B.md": "x",
        },
    )
    monkeypatch.setattr(sys, "argv", ["validate_vault.py", str(vault)])
    assert main() == 0

    # 1: broken link, no fix
    vault2 = _make_vault(
        tmp_path,
        {"a/X.md": "[[a/Y|Y]]"},
    )
    monkeypatch.setattr(sys, "argv", ["validate_vault.py", str(vault2)])
    assert main() == 1

    # 0 after --fix
    monkeypatch.setattr(
        sys, "argv", ["validate_vault.py", str(vault2), "--fix"]
    )
    assert main() == 0
    # Re-run to confirm clean
    monkeypatch.setattr(sys, "argv", ["validate_vault.py", str(vault2)])
    assert main() == 0

    # 2: vault not a directory
    monkeypatch.setattr(
        sys, "argv", ["validate_vault.py", str(tmp_path / "nope")]
    )
    assert main() == 2
