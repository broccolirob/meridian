import time

import pytest

from src.graph.persist import (
    _load_graph_cached,
    load_graph,
    repo_hash,
    save_graph,
)


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


# --- mtime-aware lru_cache (chunk 3.12) ------------------------------


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
    # 10ms guarantees distinct mtime_ns on any modern filesystem.
    time.sleep(0.01)
    save_graph(tier0_engine, gid, cache_root=tmp_path)
    e2 = load_graph(gid, cache_root=tmp_path)
    assert e1 is not e2


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
    """Pre-3.12 behavior preserved: missing engine.pkl raises
    FileNotFoundError. Validation/error path unchanged after the
    cache refactor — failure surfaces at the outer wrapper, not
    deep in pickle.load."""
    with pytest.raises(FileNotFoundError):
        load_graph("0d0d0d0d0d0d", cache_root=tmp_path)
