import threading

import pytest
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
    vault = ensure_vault(tmp_path / "vault")
    # `..` escape
    with pytest.raises(ValueError, match="escapes vault"):
        write_obsidian_note(vault, "../escaped.md", {}, "x")
    # absolute path escape (Path / absolute drops the left side)
    with pytest.raises(ValueError, match="escapes vault"):
        write_obsidian_note(vault, "/tmp/escaped.md", {}, "x")
    # Nothing got written outside the vault
    assert not (tmp_path / "escaped.md").exists()


# --- atomic write (chunk 3.13) ----------------------------------------


def test_write_leaves_no_tmp_files(tmp_path):
    """Atomic write uses a hidden tmp file in the same dir; it
    must be cleaned up (renamed into place) so no
    `.X.md.tmp.*` litter remains in the vault."""
    vault = ensure_vault(tmp_path / "vault")
    write_obsidian_note(
        vault, "contracts/Pair.md", {"name": "Pair"}, "body"
    )
    contracts = vault / "contracts"
    leftovers = [
        p.name for p in contracts.iterdir() if p.name != "Pair.md"
    ]
    assert leftovers == [], f"tmp files lingered: {leftovers}"


def test_write_atomic_failure_cleans_tmp(tmp_path, monkeypatch):
    """If os.replace fails (simulated), tmp file is cleaned up
    AND the target file is left alone (no torn write)."""
    vault = ensure_vault(tmp_path / "vault")
    # Pre-existing content the failed write must NOT corrupt.
    target_path = vault / "contracts" / "Pair.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("ORIGINAL CONTENT\n", encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("src.render.obsidian.os.replace", boom)

    with pytest.raises(OSError, match="simulated replace failure"):
        write_obsidian_note(
            vault, "contracts/Pair.md", {"name": "Pair"}, "NEW"
        )

    # Original content survives — no torn write.
    assert target_path.read_text() == "ORIGINAL CONTENT\n"
    # Tmp file cleaned up — no `.Pair.md.tmp.*` litter.
    contracts = vault / "contracts"
    leftovers = [
        p.name for p in contracts.iterdir() if p.name != "Pair.md"
    ]
    assert leftovers == [], f"tmp files lingered: {leftovers}"


def test_concurrent_writes_produce_one_intact_file(tmp_path):
    """10 threads writing the SAME target simultaneously. Final
    file content must be EXACTLY ONE of the writers' inputs
    (atomic os.replace) — never interleaved bytes from two
    writers. Chunk 3.10's collision guards eliminated most
    cross-node races, but any pair of threads on the same path
    (re-runs, edge cases) must still produce intact output."""
    vault = ensure_vault(tmp_path / "vault")
    bodies = [
        f"BODY-FROM-WRITER-{i}\n" * 30  # ~600 chars each
        for i in range(10)
    ]
    barrier = threading.Barrier(len(bodies))

    def writer(body):
        barrier.wait()  # release all threads simultaneously
        write_obsidian_note(
            vault, "contracts/Pair.md", {"name": "Pair"}, body
        )

    threads = [
        threading.Thread(target=writer, args=(b,)) for b in bodies
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = (vault / "contracts" / "Pair.md").read_text(
        encoding="utf-8"
    )
    # Final file is EXACTLY one of the input bodies (frontmatter
    # is identical across writers; body distinguishes them).
    distinct_signatures = set()
    for line in final.splitlines():
        if line.startswith("BODY-FROM-WRITER-"):
            distinct_signatures.add(line)
    assert len(distinct_signatures) == 1, (
        f"interleaved write — found {len(distinct_signatures)} "
        f"distinct writer signatures: {distinct_signatures}"
    )
    # No tmp file left behind.
    leftovers = [
        p.name
        for p in (vault / "contracts").iterdir()
        if p.name != "Pair.md"
    ]
    assert leftovers == []


# --- atomic-write tmp file sweep (chunk 3.27 / I-NEW-10) ---


def test_ensure_vault_sweeps_stale_tmp_files(tmp_path):
    """Chunk 3.27 / I-NEW-10: ensure_vault sweeps stale
    atomic-write tmp files (`.<name>.tmp.<pid>.<tid>`)
    older than 1 hour. These accumulate when a process is
    killed between `write_text` and `os.replace`; without
    a sweep, the vault grows unboundedly over crash-rerun
    cycles."""
    import os
    import time

    from src.render.obsidian import ensure_vault

    contracts = tmp_path / "contracts"
    contracts.mkdir(parents=True)

    stale = contracts / ".foo.md.tmp.12345.67890"
    stale.write_text("simulated partial write\n")
    # Backdate mtime to 2 hours ago (past the 1h threshold).
    old_mtime = time.time() - 7200
    os.utime(stale, (old_mtime, old_mtime))

    recent = contracts / ".bar.md.tmp.99999.11111"
    recent.write_text("recent partial — should survive sweep\n")

    # Real notes must always survive.
    real_note = contracts / "RealContract.md"
    real_note.write_text("# RealContract\nbody")

    ensure_vault(tmp_path)

    assert not stale.exists(), (
        "stale tmp orphan should be swept by ensure_vault"
    )
    assert recent.exists(), (
        "recent tmp must NOT be swept (sweep threshold is "
        "1 hour; this one is fresh)"
    )
    assert real_note.exists(), (
        "real note must NOT be swept (not a tmp orphan)"
    )


def test_ensure_vault_sweep_handles_missing_subdirs(tmp_path):
    """Sweep is best-effort; if a VAULT_SUBDIR doesn't
    exist (e.g., a brand-new vault before any writes),
    sweep short-circuits cleanly without erroring."""
    from src.render.obsidian import VAULT_SUBDIRS, ensure_vault

    # Brand-new vault — no subdirs exist yet.
    result = ensure_vault(tmp_path)

    # ensure_vault returns the vault path; subdirs are
    # created. No exception raised.
    assert result == tmp_path
    for subdir in VAULT_SUBDIRS:
        assert (tmp_path / subdir).exists()
