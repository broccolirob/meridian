import os

import pytest

from src.graph.persist import (
    _load_graph_cached,
    load_graph,
    repo_hash,
    save_graph,
)


@pytest.fixture(autouse=True)
def _clear_load_graph_cache_each_test():
    """Ensure every test in this file starts AND ends with an
    empty `_load_graph_cached`. The lru_cache is module-global,
    so without teardown its entries persist into subsequent
    tests. Currently harmless because tests use distinct
    graph_ids, but a future test reusing an existing gid would
    inherit stale cache state.

    Autouse means this runs around every test in the file
    without per-test boilerplate."""
    _load_graph_cached.cache_clear()
    yield
    _load_graph_cached.cache_clear()


def test_repo_hash_stable_and_unique():
    h1 = repo_hash("/tmp/foo")
    h2 = repo_hash("/tmp/foo")
    h3 = repo_hash("/tmp/bar")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 12


def test_save_load_round_trip(tier0_engine, tier0_dir, tmp_path):
    rh = repo_hash(str(tier0_dir))
    path = save_graph(tier0_engine, rh, cache_root=tmp_path)
    assert path.exists()
    assert path.name == "engine.pkl"

    loaded = load_graph(rh, cache_root=tmp_path)
    assert loaded.summary() == tier0_engine.summary()


def test_save_leaves_no_tmp_files(tier0_engine, tier0_dir, tmp_path):
    """Atomic write uses a tmp file in the same dir; it must be cleaned
    up (renamed into place) so we don't leak `.engine.pkl.tmp.*` litter
    in the cache directory."""
    rh = repo_hash(str(tier0_dir))
    save_graph(tier0_engine, rh, cache_root=tmp_path)
    out_dir = tmp_path / rh
    leftovers = [p.name for p in out_dir.iterdir() if p.name != "engine.pkl"]
    assert leftovers == [], f"tmp files lingered: {leftovers}"


def test_save_is_idempotent_under_repeat(tier0_engine, tier0_dir, tmp_path):
    """Re-saving over an existing engine.pkl must succeed atomically —
    os.replace overwrites the destination."""
    rh = repo_hash(str(tier0_dir))
    save_graph(tier0_engine, rh, cache_root=tmp_path)
    save_graph(tier0_engine, rh, cache_root=tmp_path)  # must not raise
    loaded = load_graph(rh, cache_root=tmp_path)
    assert loaded.summary() == tier0_engine.summary()


def test_load_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_graph("deadbeef0123", cache_root=tmp_path)


@pytest.mark.parametrize(
    "bad_id",
    [
        "../escape",
        "../../etc/passwd",
        "abc",  # too short
        "0123456789abcdef",  # too long
        "DEADBEEF1234",  # uppercase
        "0123456789gz",  # non-hex
        "",  # empty
        "0123 56789ab",  # whitespace
    ],
)
def test_load_rejects_malformed_graph_id(bad_id, tmp_path):
    with pytest.raises(ValueError, match="invalid graph_id"):
        load_graph(bad_id, cache_root=tmp_path)


@pytest.mark.parametrize(
    "bad_id",
    ["../escape", "DEADBEEF1234", "0123456789gz", ""],
)
def test_save_rejects_malformed_graph_id(tier0_engine, bad_id, tmp_path):
    before = set(tmp_path.iterdir())
    with pytest.raises(ValueError, match="invalid graph_id"):
        save_graph(tier0_engine, bad_id, cache_root=tmp_path)
    # Validator runs before mkdir — no new entries under tmp_path
    assert set(tmp_path.iterdir()) == before


# --- mtime-aware lru_cache -------------------------------------------


def test_load_graph_caches_repeated_calls(tier0_engine, tmp_path):
    """Two load_graph calls with identical args return the SAME
    instance. This is the leverage point — Tier 1 dispatch had
    100+ pickle.load calls of the same engine; now collapses to
    one per (graph_id, cache_root, mtime) key."""
    _load_graph_cached.cache_clear()
    gid = "0123456789ab"
    save_graph(tier0_engine, gid, cache_root=tmp_path)
    e1 = load_graph(gid, cache_root=tmp_path)
    e2 = load_graph(gid, cache_root=tmp_path)
    assert e1 is e2


def test_load_graph_invalidates_when_file_rewritten(
    tier0_engine, tmp_path
):
    """save_graph's atomic os.replace bumps mtime → next load
    is a cache miss → fresh instance. Validates that mutations
    via annotate (load → mutate → save) propagate correctly to
    subsequent readers."""
    _load_graph_cached.cache_clear()
    gid = "0123456789ab"
    save_graph(tier0_engine, gid, cache_root=tmp_path)
    e1 = load_graph(gid, cache_root=tmp_path)

    save_graph(tier0_engine, gid, cache_root=tmp_path)
    # Belt-and-suspenders +1s mtime bump. On nanosecond-resolution
    # filesystems the two save_graph calls already differ. On
    # 1-second-resolution filesystems (HFS+, ext3, some NFS mounts)
    # two saves within the same second collide → cache hit → flake.
    # Explicit bump makes the test deterministic regardless of FS
    # clock.
    engine_path = tmp_path / gid / "engine.pkl"
    new_ns = engine_path.stat().st_mtime_ns + 1_000_000_000
    os.utime(engine_path, ns=(new_ns, new_ns))

    e2 = load_graph(gid, cache_root=tmp_path)
    assert e1 is not e2


def test_load_graph_invalidates_when_mtime_unchanged(
    tier0_engine, tmp_path
):
    """Coarse-FS scenario: two save_graph calls within the same
    second on HFS+ / ext3 / some NFS mounts produce identical
    mtime_ns. Without explicit cache invalidation inside save_graph,
    the second load_graph hits a stale cache entry → returns the
    pre-save engine → silent lost-update inside _ANNOTATE_LOCK.

    Test approach: save twice, then freeze mtime back to its pre-
    second-save value to simulate coarse-FS resolution. The fix
    (save_graph calls cache_clear after os.replace) must guarantee
    e2 is a fresh instance regardless of FS clock behavior."""
    _load_graph_cached.cache_clear()
    gid = "0123456789ab"
    save_graph(tier0_engine, gid, cache_root=tmp_path)
    e1 = load_graph(gid, cache_root=tmp_path)

    engine_path = tmp_path / gid / "engine.pkl"
    frozen_ns = engine_path.stat().st_mtime_ns
    save_graph(tier0_engine, gid, cache_root=tmp_path)
    # Freeze mtime to simulate coarse-resolution FS where both
    # saves landed in the same FS-clock tick.
    os.utime(engine_path, ns=(frozen_ns, frozen_ns))

    e2 = load_graph(gid, cache_root=tmp_path)
    assert e1 is not e2, (
        "save_graph must invalidate the load cache regardless of "
        "FS mtime resolution; otherwise coarse-FS users hit silent "
        "lost-update inside _ANNOTATE_LOCK"
    )


def test_load_graph_distinct_graph_ids_get_distinct_entries(
    tier0_engine, tmp_path
):
    """Different graph_ids cache independently — no key collision."""
    _load_graph_cached.cache_clear()
    gid_a = "0a0a0a0a0a0a"
    gid_b = "0b0b0b0b0b0b"
    save_graph(tier0_engine, gid_a, cache_root=tmp_path)
    save_graph(tier0_engine, gid_b, cache_root=tmp_path)
    e_a = load_graph(gid_a, cache_root=tmp_path)
    e_b = load_graph(gid_b, cache_root=tmp_path)
    e_a_again = load_graph(gid_a, cache_root=tmp_path)
    assert e_a is e_a_again
    assert e_a is not e_b


def test_load_graph_cache_info_reports_hits_and_misses(
    tier0_engine, tmp_path
):
    """`_load_graph_cached.cache_info()` exposes hit/miss counts
    — useful for debugging perf regressions. Pins the contract
    that the cache actually fires (not just appears to)."""
    _load_graph_cached.cache_clear()
    gid = "0c0c0c0c0c0c"
    save_graph(tier0_engine, gid, cache_root=tmp_path)
    load_graph(gid, cache_root=tmp_path)  # miss
    load_graph(gid, cache_root=tmp_path)  # hit
    load_graph(gid, cache_root=tmp_path)  # hit
    info = _load_graph_cached.cache_info()
    assert info.hits == 2
    assert info.misses == 1


def test_load_graph_missing_file_raises_filenotfound_after_3_12(
    tmp_path,
):
    """Missing engine.pkl raises FileNotFoundError. The
    validation/error path surfaces at the outer wrapper, not
    deep in pickle.load."""
    with pytest.raises(FileNotFoundError):
        load_graph("0d0d0d0d0d0d", cache_root=tmp_path)


def test_concurrent_load_graph_returns_same_instance(
    tier0_graph_id_default_cache,
):
    """Thundering-herd armor.

    CPython's lru_cache wrapper doesn't serialize the wrapped
    function call on a miss — without an outer lock, multiple
    concurrent callers each enter pickle.load and end up with
    DIFFERENT instances for the same key (the cache stores
    one winner; the losers' instances are still held by their
    callers). `_LOAD_LOCK` in src/graph/persist.py serializes
    cache access so all concurrent callers receive the SAME
    instance.

    Test approach: force a cold cache, then start N threads
    that all wait on a `threading.Barrier` and fire load_graph
    simultaneously. After all return, assert every thread got
    the same instance (`id()` match)."""
    import threading

    from src.graph.persist import _load_graph_cached, load_graph

    gid = tier0_graph_id_default_cache

    # Force cold cache so all N workers race on a miss.
    _load_graph_cached.cache_clear()

    instances: list[object] = []
    instances_lock = threading.Lock()
    barrier = threading.Barrier(5)

    def loader() -> None:
        barrier.wait()  # release all 5 simultaneously
        engine = load_graph(gid)
        with instances_lock:
            instances.append(engine)

    threads = [
        threading.Thread(target=loader) for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert len(instances) == 5, (
        f"expected 5 load_graph results; got {len(instances)} "
        f"(some threads may have hung)"
    )

    distinct_ids = {id(e) for e in instances}
    assert len(distinct_ids) == 1, (
        f"thundering-herd race: got {len(distinct_ids)} "
        f"distinct QueryEngine instances from 5 concurrent "
        f"load_graph calls on a cold cache. Pre-fix the "
        f"lru_cache doesn't serialize misses; post-fix "
        f"_LOAD_LOCK in src/graph/persist.py serializes them."
    )


def test_autouse_fixture_provides_clean_cache_at_test_entry(
    tier0_graph_id_default_cache,
):
    """Verify the autouse fixture actually delivers an empty
    cache to every test. If the fixture broke (e.g., was
    renamed without the autouse marker), this sentinel test
    would observe inherited cache state from earlier tests in
    the same session."""
    info = _load_graph_cached.cache_info()
    assert info.currsize == 0, (
        f"cache should be empty at test entry; got "
        f"{info.currsize} entries (autouse fixture broken "
        f"or didn't run)"
    )

    # Loading populates the cache.
    load_graph(tier0_graph_id_default_cache)
    info = _load_graph_cached.cache_info()
    assert info.currsize >= 1
