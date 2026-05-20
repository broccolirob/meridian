import functools
import hashlib
import os
import pickle
import re
import threading
from pathlib import Path

from trailmark.query.api import QueryEngine

CACHE_ROOT = Path(".washable/graph")

# Module-level lock that serializes _load_graph_cached
# invocations. CPython's lru_cache is thread-safe for the
# cache MAP but does NOT serialize the wrapped function call
# on a miss — two concurrent callers on the same key both
# enter pickle.load, and the loser's instance is held briefly
# by its caller while the cache stores the winner's. This
# lock makes concurrent misses deterministic: only one
# pickle.load per (graph_id, mtime_ns), and all concurrent
# callers receive the same cached instance.
#
# Cost: hit lookups also acquire the lock, but hits run in
# microseconds — contention is negligible at washable's
# typical concurrency (cap=5 workers, ~100 load_graph calls
# per dispatch). The thundering-herd fix saves 4×pickle.load
# of wasted I/O on a cold cache.
_LOAD_LOCK = threading.Lock()

# Graph IDs are the first 12 hex chars of a sha256 — see repo_hash().
# Validating shape on save/load defends pickle.load against an attacker-
# (or LLM-)controlled graph_id that escapes the cache via "../" or
# resolves to an attacker-written file elsewhere on disk.
_GRAPH_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _validate_graph_id(graph_id: str) -> None:
    if not _GRAPH_ID_RE.fullmatch(graph_id):
        raise ValueError(
            f"invalid graph_id {graph_id!r}: expected 12 lowercase hex chars"
        )


def repo_hash(repo_path: str | Path) -> str:
    abs_path = str(Path(repo_path).expanduser().resolve())
    return hashlib.sha256(abs_path.encode()).hexdigest()[:12]


def save_parse_root(
    parse_root: str | Path,
    graph_id: str,
    cache_root: Path = CACHE_ROOT,
) -> Path:
    """Persist the absolute `parse_root` alongside the engine.

    `read_node_source` validates that file paths it reads are
    under this root, defending against symlinked exfiltration
    (e.g., an adversarial repo planting
    `evil.sol -> /etc/passwd`).

    The file is written to `cache_root/<gid>/parse_root.txt`
    so it's co-located with `engine.pkl` and gets invalidated
    on cache wipe.
    """
    _validate_graph_id(graph_id)
    out_dir = Path(cache_root) / graph_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "parse_root.txt"
    abs_root = str(Path(parse_root).resolve())
    out_path.write_text(abs_root + "\n", encoding="utf-8")
    return out_path


def load_parse_root(
    graph_id: str,
    cache_root: Path = CACHE_ROOT,
) -> Path | None:
    """Return the absolute `parse_root` saved alongside
    `engine.pkl`, or None if not set (legacy graphs).

    The None return preserves backward-compat — legacy caches
    have no parse_root.txt; callers that need the validation
    should treat None as "skip the check" rather than "reject".
    """
    _validate_graph_id(graph_id)
    parse_root_file = Path(cache_root) / graph_id / "parse_root.txt"
    if not parse_root_file.exists():
        return None
    return Path(parse_root_file.read_text().strip())


def save_graph(
    engine: QueryEngine,
    graph_id: str,
    cache_root: Path = CACHE_ROOT,
) -> Path:
    _validate_graph_id(graph_id)
    out_dir = Path(cache_root) / graph_id
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / "engine.pkl"
    # Atomic write: pickle.dump into a same-directory tmp file, then
    # os.replace into place. Eliminates the partial-read race where a
    # reader (e.g. another tool call) opens engine.pkl while a writer
    # is mid-pickle.dump and hits EOFError. PID in the tmp name keeps
    # concurrent writers from clobbering each other's tmp.
    # Lost-update (two writers racing on the same graph) is handled by
    # _ANNOTATE_LOCK in src/tools.py; cross-process locking is out of
    # scope.
    tmp_path = out_dir / f".engine.pkl.tmp.{os.getpid()}"
    try:
        with open(tmp_path, "wb") as f:
            pickle.dump(engine, f)
        os.replace(tmp_path, final_path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    # Invalidate the load cache explicitly. _load_graph_cached keys
    # on (graph_id, cache_root, mtime_ns), but coarse-resolution
    # filesystems (HFS+, ext3, some NFS mounts) only tick mtime per
    # second. Two saves within the same second produce identical
    # mtime_ns → next load hits the stale cache → silent lost-update
    # inside _ANNOTATE_LOCK. cache_clear() makes invalidation
    # deterministic regardless of FS clock resolution. Cost is
    # negligible — maxsize=4, at most 4 entries evicted.
    _load_graph_cached.cache_clear()
    return final_path


@functools.lru_cache(maxsize=4)
def _load_graph_cached(
    graph_id: str,
    cache_root: Path,
    mtime_ns: int,
) -> QueryEngine:
    """Inner cache layer. Keyed by (graph_id, cache_root, mtime_ns).

    The mtime_ns component is necessary but not sufficient for
    invalidation — coarse-resolution filesystems can produce
    identical mtime_ns for back-to-back saves. save_graph compensates
    by calling cache_clear() after each write, so the next call is
    guaranteed a fresh pickle.load regardless of FS clock behavior.

    Private — validation already happened in the outer load_graph.
    """
    engine_path = cache_root / graph_id / "engine.pkl"
    with open(engine_path, "rb") as f:
        return pickle.load(f)


def load_graph(
    graph_id: str,
    cache_root: Path = CACHE_ROOT,
) -> QueryEngine:
    """Load the persisted QueryEngine for `graph_id`.

    Memoized via an mtime-aware lru_cache: identical (graph_id,
    cache_root) pairs return the SAME in-process QueryEngine
    instance until the underlying file is rewritten by
    save_graph. save_graph calls cache_clear() after its atomic
    os.replace, so invalidation is deterministic even on
    coarse-mtime filesystems (HFS+, ext3, some NFS mounts) where
    two saves within the same second would otherwise produce
    identical mtime_ns and collide on the cache key.

    Cache size: 4 entries (see _load_graph_cached). Sufficient
    for the common case (one graph per dispatch); rare cross-
    tier sessions evict in LRU order.

    Concurrency: _ANNOTATE_LOCK in src/tools.py serializes the
    only mutator path. _LOAD_LOCK (this module) serializes
    cache misses so concurrent readers on a cold cache get the
    SAME instance, not divergent copies. Readers without writes
    see the same cached instance; readers that arrive after a
    write get a cleared cache → fresh pickle.load.

    Raises:
        ValueError: if `graph_id` doesn't match the 12-hex pattern.
        FileNotFoundError: if the cache file doesn't exist.
    """
    _validate_graph_id(graph_id)
    # Normalize to positional Path so the inner cache key is
    # consistent regardless of how the caller passed cache_root.
    cache_root = Path(cache_root)
    engine_path = cache_root / graph_id / "engine.pkl"
    if not engine_path.exists():
        raise FileNotFoundError(
            f"graph not in cache: {engine_path}"
        )
    mtime_ns = engine_path.stat().st_mtime_ns
    # Serialize cache access — hits are ~10us, the lock cost
    # is microseconds, and concurrent misses become
    # deterministic (no thundering herd).
    with _LOAD_LOCK:
        return _load_graph_cached(graph_id, cache_root, mtime_ns)
