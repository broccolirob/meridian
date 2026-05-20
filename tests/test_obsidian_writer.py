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
