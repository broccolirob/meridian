"""Tests for the meridian CLI subcommands and global flags.

Pattern: import `cli` from `main`, call with an argv list,
assert exit code + side effects (file written, stdout
content). For paths that argparse exits via SystemExit
(`--version`, `--help`, missing required args), use
`pytest.raises(SystemExit)` and inspect captured stdout/
stderr via `capsys`.

Diff CLI tests live in `tests/test_diff_md.py` (chunk 5.2,
updated for `--vault-path` in chunk 5.4). This file covers
the parse, validate, version, and help surface.
"""

import shutil
import re

import pytest

from main import _project_version, cli
from src.graph.persist import CACHE_ROOT


# ---- --version + --help -----------------------------------


def test_cli_version_prints_project_version(capsys):
    """`meridian --version` exits 0 after printing the
    package version. argparse's `action="version"` raises
    SystemExit(0) by design."""
    with pytest.raises(SystemExit) as exc:
        cli(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "meridian " in out
    assert _project_version() in out


def test_cli_help_lists_all_subcommands(capsys):
    """`meridian --help` lists every subcommand. Pins the
    CHUNKS.md 5.4 success criterion: `parse`, `diff`,
    `validate` all visible in the help output."""
    with pytest.raises(SystemExit) as exc:
        cli(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for sub in ("parse", "diff", "validate"):
        assert sub in out, (
            f"`{sub}` missing from --help output:\n{out}"
        )


# ---- parse ------------------------------------------------


def test_cli_parse_writes_graph_and_prints_gid(
    tier0_dir, tmp_path, capsys,
):
    """`meridian parse <repo>` parses the source tree into
    the default graph cache and prints a 12-hex graph_id
    to stdout.

    Uses a `tmp_path` COPY of tier0_dir (not tier0_dir itself)
    so the resulting graph_id is unique to this test. Cleaning
    up `tier0_dir`'s cache would nuke the session-scoped
    `tier0_graph_id_default_cache` fixture and poison every
    downstream test that depends on it."""
    fresh_src = tmp_path / "tier0_copy"
    shutil.copytree(tier0_dir, fresh_src)
    rc = cli(["parse", str(fresh_src)])
    assert rc == 0
    out = capsys.readouterr().out
    gid = out.strip()
    try:
        assert re.fullmatch(r"[0-9a-f]{12}", gid), (
            f"expected 12-hex graph_id; got {gid!r}"
        )
        assert (CACHE_ROOT / gid).exists()
    finally:
        shutil.rmtree(CACHE_ROOT / gid, ignore_errors=True)


def test_cli_parse_rejects_missing_path(tmp_path):
    """`parse /nonexistent` exits with a clear error before
    invoking trailmark_parse."""
    missing = tmp_path / "nonexistent"
    with pytest.raises(SystemExit) as exc:
        cli(["parse", str(missing)])
    assert "does not exist" in str(exc.value)


def test_cli_parse_rejects_file_path(tmp_path):
    """`parse <file>` rejects when the path is a file
    rather than a directory; trailmark_parse needs a tree."""
    file_path = tmp_path / "single.sol"
    file_path.write_text("contract C {}")
    with pytest.raises(SystemExit) as exc:
        cli(["parse", str(file_path)])
    assert "not a directory" in str(exc.value)


# ---- validate ---------------------------------------------


def _make_vault(tmp_path, files: dict[str, str]):
    """Build a tiny vault with the given relative-path →
    content mapping. Returns the vault root."""
    vault = tmp_path / "vault"
    for rel, content in files.items():
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return vault


def test_cli_validate_reports_clean_vault(tmp_path, capsys):
    """Vault with a self-resolving wikilink reports OK
    and exits 0."""
    vault = _make_vault(tmp_path, {
        "a.md": "Refs [[b]] for context.",
        "b.md": "Just a note.",
    })
    rc = cli(["--vault-path", str(vault), "validate"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK: 0 broken" in out
    assert "2 notes" in out


def test_cli_validate_reports_broken_links(tmp_path, capsys):
    """Vault with a non-resolving wikilink exits 1 and
    reports the broken target."""
    vault = _make_vault(tmp_path, {
        "a.md": "Refs [[ghost]] which does not exist.",
    })
    rc = cli(["--vault-path", str(vault), "validate"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "FOUND 1 broken" in out
    assert "ghost" in out
    assert "a.md" in out


def test_cli_validate_fix_strips_broken_links(
    tmp_path, capsys,
):
    """`validate --fix` rewrites the source note to strip
    the broken wikilink. Re-running validate after the fix
    reports clean."""
    vault = _make_vault(tmp_path, {
        "a.md": "Refs [[ghost]] which does not exist.",
    })
    # First run with --fix: strip the broken link, exit 0.
    rc = cli([
        "--vault-path", str(vault), "validate", "--fix",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "FIXED: stripped" in out
    # The source note no longer contains the broken wikilink.
    rewritten = (vault / "a.md").read_text(encoding="utf-8")
    assert "[[ghost]]" not in rewritten
    assert "ghost" in rewritten  # display text kept as bare word
    # Second run without --fix: clean.
    capsys.readouterr()  # discard prior output
    rc2 = cli(["--vault-path", str(vault), "validate"])
    assert rc2 == 0
    assert "OK: 0 broken" in capsys.readouterr().out


# ---- --vault-path requirement -----------------------------


def test_cli_diff_requires_vault_path():
    """`diff` without `--vault-path` exits with a clear
    error that names the missing flag."""
    with pytest.raises(SystemExit) as exc:
        cli(["diff", "abc012345678", "def012345678"])
    msg = str(exc.value)
    assert "requires --vault-path" in msg
    assert "diff" in msg


def test_cli_validate_requires_vault_path():
    """`validate` without `--vault-path` exits with a clear
    error that names the missing flag."""
    with pytest.raises(SystemExit) as exc:
        cli(["validate"])
    msg = str(exc.value)
    assert "requires --vault-path" in msg
    assert "validate" in msg


def test_cli_validate_rejects_nonexistent_vault(tmp_path):
    """Review-round fix: a typo'd `--vault-path` for
    `validate` exits with a clear error INSTEAD of silently
    creating the phantom vault and reporting `OK: 0 broken`.

    Pin: the would-be path is NOT created on disk. If
    `ensure_vault` ran, it would mkdir the path — assert
    the path stays missing."""
    typo_path = tmp_path / "voult-typo"  # doesn't exist
    with pytest.raises(SystemExit) as exc:
        cli(["--vault-path", str(typo_path), "validate"])
    msg = str(exc.value)
    assert "vault does not exist" in msg
    assert str(typo_path) in msg
    # Critically: the phantom vault was NOT created.
    assert not typo_path.exists(), (
        f"validate created the phantom vault: {typo_path}"
    )


def test_cli_validate_rejects_file_as_vault(tmp_path):
    """Edge case: `--vault-path` points at an existing
    FILE (not a directory). validate exits with a clear
    error instead of crashing inside ensure_vault's
    mkdir."""
    not_a_dir = tmp_path / "looks-like-a-vault"
    not_a_dir.write_text("oops, this is a file")
    with pytest.raises(SystemExit) as exc:
        cli(["--vault-path", str(not_a_dir), "validate"])
    msg = str(exc.value)
    assert "not a directory" in msg


def test_cli_diff_still_creates_vault_on_demand(
    tmp_path, capsys,
):
    """Pin the asymmetry: `diff` keeps create-on-demand
    semantics (it writes into the vault, so creation is
    intentional). Only `validate` got the must_exist gate.

    This test calls diff with a path that doesn't exist
    AND uses two valid-format graph_ids that aren't cached
    — diff_graphs will fail with FileNotFoundError, but
    the failure should come from the cache lookup, NOT
    from a vault-missing error."""
    fresh_vault = tmp_path / "fresh-vault"  # doesn't exist
    # diff_graphs will fail on the missing graph cache, but
    # the vault itself should be auto-created first.
    with pytest.raises(Exception) as exc:
        cli([
            "--vault-path", str(fresh_vault),
            "diff", "abc012345678", "def012345678",
        ])
    # The failure is graph-cache-related, NOT vault-missing.
    msg = str(exc.value)
    assert "vault does not exist" not in msg
    # And the vault WAS auto-created.
    assert fresh_vault.exists()
