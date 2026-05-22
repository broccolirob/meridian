"""Tests for src/graph/cache.py + dispatch_topo file-hash
incremental cache integration.

Unit tests cover the pure cache helpers. Integration tests
exercise the full dispatch_topo loop with a mocked agent
(no LLM calls), running it twice to verify nodes from
unchanged files are skipped on the second run.
"""

import hashlib
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

from src.agent import dispatch_topo
from src.graph.cache import (
    _cache_path,
    compute_file_hash,
    compute_file_hashes,
    load_file_hash_cache,
    save_file_hash_cache,
)


# ---- unit tests on cache helpers -------------------------------


def test_compute_file_hash_returns_sha256_hex(tmp_path):
    """Hash is deterministic and matches stdlib sha256 hex."""
    f = tmp_path / "x.txt"
    f.write_bytes(b"hello world")
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert compute_file_hash(f) == expected
    # Deterministic across two calls.
    assert compute_file_hash(f) == compute_file_hash(f)


def test_compute_file_hashes_dedups(tmp_path):
    """Repeated paths in the input collapse to one entry."""
    a = tmp_path / "a.txt"
    a.write_bytes(b"a")
    b = tmp_path / "b.txt"
    b.write_bytes(b"b")
    result = compute_file_hashes([str(a), str(a), str(b)])
    assert set(result.keys()) == {str(a), str(b)}
    assert result[str(a)] == hashlib.sha256(b"a").hexdigest()
    assert result[str(b)] == hashlib.sha256(b"b").hexdigest()


def test_compute_file_hashes_skips_missing_files(tmp_path):
    """Missing files are silently omitted from the result —
    no FileNotFoundError propagates. Caller treats absence
    as a cache miss."""
    real = tmp_path / "real.txt"
    real.write_bytes(b"r")
    missing = tmp_path / "missing.txt"  # doesn't exist
    result = compute_file_hashes([str(real), str(missing)])
    assert str(real) in result
    assert str(missing) not in result


def test_load_file_hash_cache_returns_empty_when_missing(tmp_path):
    """Fresh vault, no .washable/cache/ → empty dict, no
    exception."""
    vault = tmp_path / "vault"
    vault.mkdir()
    assert load_file_hash_cache(vault) == {}


def test_load_file_hash_cache_returns_empty_when_corrupt(tmp_path):
    """Corrupt JSON in the cache file degrades to empty — a
    bad cache should never abort a dispatch."""
    vault = tmp_path / "vault"
    cache_file = _cache_path(vault)
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text("not json at all {")
    assert load_file_hash_cache(vault) == {}


def test_load_file_hash_cache_filters_wrong_shape_entries(tmp_path):
    """Defense against an auditor hand-editing the cache
    with mixed types — keep only str→str entries.

    JSON serializes integer keys as strings, so the only
    practical wrong-shape variant on disk is `key:
    non-string-value`. The filter drops those."""
    vault = tmp_path / "vault"
    cache_file = _cache_path(vault)
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text(json.dumps({
        "real_file.sol": "abc123",
        "bad_value_int": 42,       # not a str — drop
        "bad_value_list": ["abc"], # not a str — drop
        "bad_value_null": None,    # not a str — drop
    }))
    loaded = load_file_hash_cache(vault)
    assert loaded == {"real_file.sol": "abc123"}


def test_save_then_load_round_trip(tmp_path):
    """Writing and reading back yields the same dict."""
    vault = tmp_path / "vault"
    payload = {
        "/abs/path/A.sol": "abc",
        "/abs/path/B.sol": "def",
    }
    save_file_hash_cache(vault, payload)
    assert load_file_hash_cache(vault) == payload


def test_save_file_hash_cache_leaves_no_tmp_files(tmp_path):
    """Atomic write via tmp+rename should not litter the
    cache directory with leftover `.files.json.tmp.*` files
    after a clean write."""
    vault = tmp_path / "vault"
    save_file_hash_cache(vault, {"k": "v"})
    cache_dir = _cache_path(vault).parent
    tmp_litter = list(cache_dir.glob(".files.json.tmp.*"))
    assert tmp_litter == [], (
        f"tmp files left behind: {tmp_litter}"
    )


def test_save_file_hash_cache_concurrent_writes_serialize(tmp_path):
    """Module-level lock serializes concurrent writes. The
    final state is one of the inputs (last writer wins);
    pin that the file is never torn (always valid JSON
    parseable as a dict)."""
    vault = tmp_path / "vault"
    errors: list[Exception] = []

    def writer(value):
        try:
            for _ in range(20):
                save_file_hash_cache(vault, {"k": value})
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=writer, args=(f"v{i}",))
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], f"writer errors: {errors}"
    # Final cache is intact, parseable, and contains "k".
    loaded = load_file_hash_cache(vault)
    assert "k" in loaded
    assert loaded["k"].startswith("v")


# ---- integration tests on dispatch_topo ----------------------


class _CountingFakeAgent:
    """Stand-in agent that records every invoke and lets
    tests inject failures by node id."""

    def __init__(self, fail_for: set[str] | None = None):
        self.calls: list[str] = []
        self.fail_for = fail_for or set()
        self._lock = threading.Lock()

    def invoke(self, inputs):
        msg = inputs["messages"][0]["content"]
        with self._lock:
            self.calls.append(msg)
        for nid in self.fail_for:
            if f"`{nid}`" in msg:
                raise RuntimeError(f"simulated failure for {nid}")
        reply = MagicMock()
        reply.content = "/fake/path.md"
        return {"messages": [reply]}


def test_dispatch_topo_skips_unchanged_nodes_on_second_run(
    monkeypatch, tier0_graph_id_default_cache, tmp_path,
):
    """CHUNKS.md success criterion: two consecutive runs on
    unchanged Tier 0 — first dispatches all subagents,
    second dispatches zero."""
    gid = tier0_graph_id_default_cache
    vault = tmp_path / "vault"
    fake = _CountingFakeAgent()
    monkeypatch.setattr("src.agent.build_agent", lambda *a, **k: fake)

    # First run: cache empty, all nodes dispatch.
    r1 = dispatch_topo(gid, str(vault), concurrency_cap=1)
    assert len(r1["successes"]) == 8
    assert len(r1["failures"]) == 0
    assert len(r1["skipped"]) == 0
    invokes_run1 = len(fake.calls)
    assert invokes_run1 == 8

    # Cache file was written.
    cache_path = _cache_path(vault)
    assert cache_path.exists()
    cached = load_file_hash_cache(vault)
    assert len(cached) > 0

    # Second run: no source changes → all 8 nodes skipped.
    r2 = dispatch_topo(gid, str(vault), concurrency_cap=1)
    assert len(r2["successes"]) == 0
    assert len(r2["failures"]) == 0
    assert len(r2["skipped"]) == 8
    # Agent was NOT invoked again (count unchanged from run 1).
    assert len(fake.calls) == invokes_run1


def test_dispatch_topo_redispatches_after_file_touch(
    monkeypatch, tier0_graph_id_default_cache, tmp_path,
):
    """Modifying a source file invalidates its cache entry;
    nodes whose owning file changed re-dispatch."""
    from src.tools import list_nodes

    gid = tier0_graph_id_default_cache
    vault = tmp_path / "vault"
    fake = _CountingFakeAgent()
    monkeypatch.setattr("src.agent.build_agent", lambda *a, **k: fake)

    # Prime the cache.
    dispatch_topo(gid, str(vault), concurrency_cap=1)
    invokes_after_run1 = len(fake.calls)

    # Find a source file owned by at least one node and
    # touch it (append a Solidity comment so the parser
    # doesn't reject the file).
    nodes = list_nodes(gid)
    files = {n["location"]["file_path"] for n in nodes}
    target = sorted(files)[0]
    Path(target).write_text(
        Path(target).read_text() + "\n// chunk 5.3 cache test\n"
    )
    try:
        # Second run: only nodes from `target` re-dispatch.
        nodes_in_target = {
            n["id"] for n in nodes
            if n["location"]["file_path"] == target
        }
        r = dispatch_topo(gid, str(vault), concurrency_cap=1)
        # Successes correspond to nodes in the touched file
        # that are in topo order. Filter expected by topo
        # membership.
        from src.graph.topo import topo_order
        order = set(topo_order(gid))
        expected_redispatched = nodes_in_target & order
        actual_redispatched = {
            s["node_id"] for s in r["successes"]
        }
        assert actual_redispatched == expected_redispatched, (
            f"expected re-dispatch of {expected_redispatched}, "
            f"got {actual_redispatched}"
        )
        # All other nodes were skipped.
        assert (
            len(r["skipped"])
            == len(order) - len(expected_redispatched)
        )
        # Agent was invoked exactly once per re-dispatched node.
        assert (
            len(fake.calls) - invokes_after_run1
            == len(expected_redispatched)
        )
    finally:
        # Restore the file so other tests aren't affected
        # (tier0_dir is session-scoped).
        Path(target).write_text(
            Path(target).read_text().replace(
                "\n// chunk 5.3 cache test\n", ""
            )
        )


def test_dispatch_topo_does_not_cache_failed_file(
    monkeypatch, tier0_graph_id_default_cache, tmp_path,
):
    """If a node fails, its owning file's hash is NOT
    recorded — next run re-dispatches all its nodes."""
    from src.tools import get_node, list_nodes

    gid = tier0_graph_id_default_cache
    vault = tmp_path / "vault"
    # Pick a failing node, then look up its owning file.
    failing_id = "src.tokens.ERC20:ERC20"
    failing_file = get_node(gid, failing_id)["location"]["file_path"]

    fake = _CountingFakeAgent(fail_for={failing_id})
    monkeypatch.setattr("src.agent.build_agent", lambda *a, **k: fake)

    r1 = dispatch_topo(gid, str(vault), concurrency_cap=1)
    assert len(r1["failures"]) == 1

    # The failed file is NOT in the cache.
    cached = load_file_hash_cache(vault)
    assert failing_file not in cached, (
        f"failed file should not be cached; cache: {cached}"
    )

    # Second run: no longer fails (clean fake), the failing
    # node's file re-dispatches.
    fake2 = _CountingFakeAgent()
    monkeypatch.setattr(
        "src.agent.build_agent", lambda *a, **k: fake2,
    )
    r2 = dispatch_topo(gid, str(vault), concurrency_cap=1)
    redispatched = {s["node_id"] for s in r2["successes"]}
    # Every node from the failing file must be in the
    # redispatched set.
    nodes_in_failing_file = {
        n["id"] for n in list_nodes(gid)
        if n["location"]["file_path"] == failing_file
    }
    from src.graph.topo import topo_order
    order = set(topo_order(gid))
    expected_redispatched = nodes_in_failing_file & order
    assert expected_redispatched <= redispatched, (
        f"expected {expected_redispatched} to all re-dispatch; "
        f"got {redispatched}"
    )


def test_dispatch_topo_prunes_cache_entries_for_missing_files(
    monkeypatch, tier0_graph_id_default_cache, tmp_path,
):
    """Cache entries whose file is no longer referenced by
    any node in the current graph get pruned at write time
    (prevents indefinite growth across renamed-file
    workflows)."""
    gid = tier0_graph_id_default_cache
    vault = tmp_path / "vault"
    # Seed the cache with a stale entry that no node owns.
    cache_path = _cache_path(vault)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({
        "/nonexistent/legacy.sol": "deadbeef",
    }))

    fake = _CountingFakeAgent()
    monkeypatch.setattr("src.agent.build_agent", lambda *a, **k: fake)

    dispatch_topo(gid, str(vault), concurrency_cap=1)

    cached_after = load_file_hash_cache(vault)
    assert "/nonexistent/legacy.sol" not in cached_after, (
        f"stale entry survived dispatch: {cached_after}"
    )
    # Real file entries ARE present.
    assert len(cached_after) > 0
