"""File-hash incremental cache for dispatch_topo.

After a successful run, the cache records the SHA-256 of
each source file whose nodes were dispatched. On the next
run, nodes whose files match the recorded hash are skipped.

Cache lives at `<vault>/.meridian/cache/files.json`. Vault-
scoped so each vault tracks its own work; deleting the
vault wipes the cache atomically.

Failure semantics: if any node from a file fails during a
dispatch, that file's hash is NOT recorded — the next run
re-dispatches all its nodes. Conservative; partial-success
files always retry.
"""

import hashlib
import json
import os
import threading
from collections.abc import Iterable
from pathlib import Path

# Relative to the vault root. Atomic-write tmp lands in the
# same directory so `os.replace` is filesystem-local.
_CACHE_REL_PATH = Path(".meridian/cache/files.json")


def _cache_path(vault_path: str | Path) -> Path:
    return Path(vault_path) / _CACHE_REL_PATH


def compute_file_hash(file_path: str | Path) -> str:
    """SHA-256 hex digest of the file's contents.

    Streams in 64 KiB chunks so memory stays bounded even
    on large fixture repos. Raises `FileNotFoundError` if
    the path doesn't exist."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_file_hashes(
    file_paths: Iterable[str | Path],
) -> dict[str, str]:
    """Bulk hash with dedup. Skips files that don't exist
    (returns no entry rather than raising) — a missing file
    means the node references a deleted source, and the
    caller treats absence as a cache miss (force dispatch)."""
    seen: dict[str, str] = {}
    for p in file_paths:
        sp = str(p)
        if sp in seen:
            continue
        try:
            seen[sp] = compute_file_hash(sp)
        except FileNotFoundError:
            continue
    return seen


def load_file_hash_cache(vault_path: str | Path) -> dict[str, str]:
    """Read `<vault>/.meridian/cache/files.json`. Returns an
    empty dict on missing file, corrupt JSON, or wrong shape
    — a corrupt cache is best treated as cold (force a fresh
    dispatch) rather than aborting.

    Defensively filters to str→str entries only, so an
    auditor hand-editing the cache can't crash the dispatch
    with a wrong-shape entry."""
    path = _cache_path(vault_path)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v
        for k, v in data.items()
        if isinstance(k, str) and isinstance(v, str)
    }


# Module-level lock so concurrent dispatch_topo invocations
# in the same process serialize their atomic writes. Today
# dispatch_topo is invoked sequentially per process, but the
# lock is cheap defense and matches the threading.Lock
# pattern in src/tools.py::_ANNOTATE_LOCK.
_CACHE_WRITE_LOCK = threading.Lock()


def save_file_hash_cache(
    vault_path: str | Path,
    cache: dict[str, str],
) -> None:
    """Atomic write via tmp+os.replace. Mirrors the pattern
    in `src/graph/persist.py::save_graph` — readers either
    see the old file or the new file, never a torn one.

    Sorted keys + 2-space indent so the on-disk JSON is
    diff-friendly when the auditor inspects it."""
    path = _cache_path(vault_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # PID + thread ID in the tmp name so concurrent
    # invocations from the same lock-holder don't collide
    # (defense in depth — the lock should serialize, but
    # belt + suspenders matches the writer pattern elsewhere).
    tmp_path = path.parent / (
        f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    )
    with _CACHE_WRITE_LOCK:
        tmp_path.write_text(
            json.dumps(cache, indent=2, sort_keys=True)
        )
        os.replace(tmp_path, path)
