"""Regression armor: writer-side deepcopy prevents concurrent
readers from observing partial mutations.

The bug being armored against: load_graph's lru_cache returns
the same QueryEngine instance to all workers. Without
deepcopy, annotate() mutating that shared instance in-place
would race with concurrent unlocked readers (get_node,
nodes_with_annotation, attack_surface, etc.) iterating the
same engine's internal dicts — RuntimeError: dictionary
changed size during iteration, or observing partial state.

The fix: copy.deepcopy(load_graph(...)) before mutating. The
shared cached instance stays untouched; save_graph below bumps
mtime AND calls cache_clear(), which invalidates the
mtime-keyed cache so subsequent reads pick up the new state.

Test isolation: every test uses `fresh_tier0` (function-
scoped, tmp cache_root). The session-shared
`tier0_graph_id_default_cache` fixture explicitly forbids
annotate/clear_annotations against the default cache root —
mutations would persist in the default cache file across the
session, and the mtime-aware lru_cache invalidates on save,
so subsequent tests would observe the leaked annotations.
"""

import threading

import pytest

from src.graph.persist import load_graph
from src.tools import (
    annotate,
    clear_annotations,
    nodes_with_annotation,
    trailmark_parse,
)


@pytest.fixture
def fresh_tier0(tier0_dir, tmp_path):
    """Fresh parse + per-test cache. Mutations don't leak between
    tests (unlike the session-scoped
    tier0_graph_id_default_cache fixture). Same shape as the
    fixture of the same name in test_annotations.py; duplicated
    here rather than promoted to conftest because only two
    files use it today."""
    cache_root = tmp_path / "cache"
    gid = trailmark_parse(
        str(tier0_dir), language="solidity", cache_root=cache_root
    )
    return gid, cache_root


def test_annotate_does_not_mutate_cached_engine_instance(fresh_tier0):
    """Deterministic: holding a reference to the cached engine
    BEFORE annotate() runs, the reference's internal state
    must be unchanged AFTER. Without the deepcopy guard, the
    cached instance would have the new annotation mutated
    in-place — readers iterating it would race."""
    gid, cache_root = fresh_tier0
    engine = load_graph(gid, cache_root=cache_root)
    target = "src.tokens.ERC4626:ERC4626"
    annotations_before = list(engine.annotations_of(target))

    # Sanity: confirm the cache returned the SAME instance for
    # both calls (test would be vacuous if load_graph returns
    # a fresh instance each time).
    assert engine is load_graph(gid, cache_root=cache_root), (
        "test setup: load_graph should return the same cached "
        "instance on repeated calls — the lru_cache should be "
        "hot."
    )

    # Run a write that would have mutated the shared instance
    # pre-fix.
    annotate(
        gid,
        target,
        "assumption",
        "regression armor: deepcopy test",
        source="test",
        cache_root=cache_root,
    )

    # Same reference, must be unchanged.
    annotations_after = list(engine.annotations_of(target))
    assert annotations_after == annotations_before, (
        "annotate() mutated the cached engine instance — "
        "concurrent readers would observe partial state. "
        "Check that annotate() uses copy.deepcopy before "
        "mutating."
    )

    # Sanity: the SAVED state has the new annotation. Confirm
    # the writer's copy was persisted and a fresh load sees it.
    # (cache_clear forces a fresh load from disk.)
    from src.graph.persist import _load_graph_cached
    _load_graph_cached.cache_clear()
    fresh_engine = load_graph(gid, cache_root=cache_root)
    fresh_annotations = list(fresh_engine.annotations_of(target))
    assert len(fresh_annotations) == len(annotations_before) + 1, (
        "save_graph didn't persist the new annotation — "
        "the deepcopy fix should still save through normally."
    )


def test_clear_annotations_does_not_mutate_cached_engine_instance(
    fresh_tier0,
):
    """Same contract for clear_annotations — the other writer
    on the _ANNOTATE_LOCK path."""
    gid, cache_root = fresh_tier0
    target = "src.tokens.ERC4626:ERC4626"
    annotate(
        gid,
        target,
        "assumption",
        "sentinel for clear test",
        source="test",
        cache_root=cache_root,
    )

    # Force a fresh read AFTER the annotate so the cache holds
    # the post-annotate state. We need this engine to be the
    # one clear_annotations will see, so its internal dict has
    # the sentinel.
    from src.graph.persist import _load_graph_cached
    _load_graph_cached.cache_clear()
    engine = load_graph(gid, cache_root=cache_root)
    annotations_before = list(engine.annotations_of(target))
    assert len(annotations_before) >= 1, (
        "test setup: annotation should have been added"
    )

    clear_annotations(
        gid, target, kind="assumption", cache_root=cache_root
    )

    annotations_after = list(engine.annotations_of(target))
    assert annotations_after == annotations_before, (
        "clear_annotations() mutated the cached engine instance "
        "— same C-NEW-3 race as annotate()."
    )


def test_concurrent_readers_and_writers_no_iteration_race(fresh_tier0):
    """Stress test: K reader threads iterate the annotations
    dict while 1 writer thread mutates. Without the writer's
    deepcopy, this would eventually raise RuntimeError:
    dictionary changed size during iteration. With the
    deepcopy, readers iterate an unchanging dict — no error.

    Bounded by WRITER ITERATIONS, not wall-clock: the writer
    completes exactly WRITER_ITERATIONS annotate+clear
    cycles, then signals readers to stop. Threads are
    NON-daemon with a deterministic join + is_alive() check
    — a wall-clock + daemon-thread pattern would let a
    straggler outlive the test and race with subsequent
    setup/teardown (or with pytest's tmp_path cleanup).
    """
    gid, cache_root = fresh_tier0
    target = "src.tokens.ERC4626:ERC4626"

    WRITER_ITERATIONS = 50  # 1 annotate + 1 clear per iter
    READER_COUNT = 4
    # Sanity ceiling: not a normal-case bound. If hit, the
    # assertion below fails loudly rather than the test
    # silently leaking a thread past completion.
    JOIN_TIMEOUT_S = 30.0

    results: dict = {"errors": [], "reads": 0, "writes": 0}
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                # nodes_with_annotation iterates
                # engine._store._graph.annotations.
                _ = nodes_with_annotation(
                    gid, "assumption", cache_root=cache_root
                )
                results["reads"] += 1
            except RuntimeError as e:
                if "dictionary changed size" in str(e):
                    results["errors"].append(str(e))
                else:
                    raise

    def writer() -> None:
        for i in range(WRITER_ITERATIONS):
            annotate(
                gid,
                target,
                "assumption",
                f"concurrent stress test note {i}",
                source="test",
                cache_root=cache_root,
            )
            clear_annotations(
                gid, target, kind="assumption", cache_root=cache_root
            )
            results["writes"] += 1
        # Writer drives test duration; signal readers when done.
        stop.set()

    # Non-daemon threads + deterministic join. daemon=True
    # would let a mid-save_graph straggler survive a join
    # timeout and race with subsequent tests (or with
    # pytest's tmp_path teardown).
    readers = [
        threading.Thread(target=reader) for _ in range(READER_COUNT)
    ]
    writer_thread = threading.Thread(target=writer)
    for t in readers:
        t.start()
    writer_thread.start()

    writer_thread.join(timeout=JOIN_TIMEOUT_S)
    assert not writer_thread.is_alive(), (
        f"writer didn't complete {WRITER_ITERATIONS} "
        f"iterations within {JOIN_TIMEOUT_S}s — likely "
        f"deadlock or extreme contention"
    )
    for t in readers:
        t.join(timeout=JOIN_TIMEOUT_S)
        assert not t.is_alive(), (
            f"reader didn't observe stop within "
            f"{JOIN_TIMEOUT_S}s — likely stop signal "
            f"didn't propagate"
        )

    assert not results["errors"], (
        f"saw {len(results['errors'])} iteration-race errors "
        f"in {results['reads']} reads + {results['writes']} "
        f"writes:\n  {results['errors'][0]}"
    )
    # Deterministic: writer must complete every iteration.
    assert results["writes"] == WRITER_ITERATIONS, (
        f"writer should have completed exactly "
        f"{WRITER_ITERATIONS} iterations; got "
        f"{results['writes']}"
    )
    # Readers should observe many iterations during the
    # writer's WRITER_ITERATIONS cycles. Conservative bound
    # — actual count typically 100s.
    assert results["reads"] > 10, (
        f"reader threads didn't iterate enough to be "
        f"meaningful (only {results['reads']} reads)"
    )
