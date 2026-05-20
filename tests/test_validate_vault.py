import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "validate_vault.py"
)


@pytest.fixture
def validate_vault():
    """Fresh import of validate_vault.py per test.
    scripts/ is not a Python package; this matches the
    pattern in test_document_one_node.py et al."""
    spec = importlib.util.spec_from_file_location(
        "validate_vault_under_test", str(SCRIPT_PATH)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_vault(tmp_path, files: dict[str, str]) -> Path:
    """Build a fake vault. `files` maps relative path -> body."""
    vault = tmp_path / "vault"
    for rel, body in files.items():
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return vault


def test_no_links_returns_empty(validate_vault, tmp_path):
    vault = _make_vault(tmp_path, {"contracts/A.md": "Just prose.\n"})
    assert validate_vault.find_broken_links(vault) == []


def test_resolved_path_link_is_not_broken(validate_vault, tmp_path):
    vault = _make_vault(
        tmp_path,
        {
            "contracts/A.md": "See [[contracts/B|B]].\n",
            "contracts/B.md": "I exist.\n",
        },
    )
    assert validate_vault.find_broken_links(vault) == []


def test_resolved_bare_link_is_not_broken(validate_vault, tmp_path):
    """Obsidian-style bare wikilinks should resolve via vault-wide
    search."""
    vault = _make_vault(
        tmp_path,
        {
            "contracts/A.md": "See [[B]].\n",
            "libraries/B.md": "I exist somewhere.\n",
        },
    )
    assert validate_vault.find_broken_links(vault) == []


def test_broken_path_link_is_reported(validate_vault, tmp_path):
    vault = _make_vault(
        tmp_path,
        {"contracts/A.md": "See [[contracts/Nope|Nope]].\n"},
    )
    broken = validate_vault.find_broken_links(vault)
    assert len(broken) == 1
    assert broken[0][1] == "contracts/Nope"


def test_strip_keeps_display_text(validate_vault, tmp_path):
    vault = _make_vault(
        tmp_path,
        {"contracts/A.md": "See [[contracts/Nope|the X function]].\n"},
    )
    broken = validate_vault.find_broken_links(vault)
    validate_vault.strip_broken_links(vault, broken)
    new = (vault / "contracts" / "A.md").read_text()
    assert new == "See the X function.\n"


def test_strip_bare_link_falls_back_to_target(validate_vault, tmp_path):
    vault = _make_vault(
        tmp_path, {"contracts/A.md": "See [[Nope]].\n"}
    )
    broken = validate_vault.find_broken_links(vault)
    validate_vault.strip_broken_links(vault, broken)
    new = (vault / "contracts" / "A.md").read_text()
    assert new == "See Nope.\n"


def test_main_exit_codes(validate_vault, tmp_path, monkeypatch):
    # 0: clean vault
    vault = _make_vault(
        tmp_path,
        {
            "contracts/A.md": "[[contracts/B|B]]",
            "contracts/B.md": "x",
        },
    )
    monkeypatch.setattr(sys, "argv", ["validate_vault.py", str(vault)])
    assert validate_vault.main() == 0

    # 1: broken link, no fix
    vault2 = _make_vault(
        tmp_path,
        {"a/X.md": "[[a/Y|Y]]"},
    )
    monkeypatch.setattr(sys, "argv", ["validate_vault.py", str(vault2)])
    assert validate_vault.main() == 1

    # 0 after --fix
    monkeypatch.setattr(
        sys, "argv", ["validate_vault.py", str(vault2), "--fix"]
    )
    assert validate_vault.main() == 0
    # Re-run to confirm clean
    monkeypatch.setattr(sys, "argv", ["validate_vault.py", str(vault2)])
    assert validate_vault.main() == 0

    # 2: vault not a directory
    monkeypatch.setattr(
        sys, "argv", ["validate_vault.py", str(tmp_path / "nope")]
    )
    assert validate_vault.main() == 2
