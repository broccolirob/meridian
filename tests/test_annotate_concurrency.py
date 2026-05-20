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
mtime, which invalidates the mtime-keyed cache so subsequent
reads pick up the new state.
"""

import threading
import time

from src.graph.persist import load_graph
from src.tools import annotate, clear_annotations, nodes_with_annotation


def test_annotate_does_not_mutate_cached_engine_instance(
    tier0_graph_id_default_cache,
):
    """Deterministic: holding a reference to the cached engine
    BEFORE annotate() runs, the reference's internal state
    must be unchanged AFTER. Without the deepcopy guard, the
    cached instance would have the new annotation mutated
    in-place — readers iterating it would race."""
    gid = tier0_graph_id_default_cache
    # Pre-populate cache + snapshot annotations.
    engine = load_graph(gid)
    target = "src.tokens.ERC4626:ERC4626"
    annotations_before = list(engine.annotations_of(target))

    # Sanity: confirm the cache returned the SAME instance for
    # both calls (test would be vacuous if load_graph returns
    # a fresh instance each time).
    assert engine is load_graph(gid), (
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
    fresh_engine = load_graph(gid)
    fresh_annotations = list(fresh_engine.annotations_of(target))
    assert len(fresh_annotations) == len(annotations_before) + 1, (
        "save_graph didn't persist the new annotation — "
        "the deepcopy fix should still save through normally."
    )

    # Teardown: clear the annotation so other tests aren't
    # polluted. (Uses the same copy-on-write path.)
    clear_annotations(gid, target, kind="assumption")


def test_clear_annotations_does_not_mutate_cached_engine_instance(
    tier0_graph_id_default_cache,
):
    """Same contract for clear_annotations — the other writer
    on the _ANNOTATE_LOCK path."""
    gid = tier0_graph_id_default_cache
    target = "src.tokens.ERC4626:ERC4626"
    annotate(
        gid,
        target,
        "assumption",
        "sentinel for clear test",
        source="test",
    )

    # Force a fresh read AFTER the annotate so the cache holds
    # the post-annotate state. We need this engine to be the
    # one clear_annotations will see, so its internal dict has
    # the sentinel.
    from src.graph.persist import _load_graph_cached
    _load_graph_cached.cache_clear()
    engine = load_graph(gid)
    annotations_before = list(engine.annotations_of(target))
    assert len(annotations_before) >= 1, (
        "test setup: annotation should have been added"
    )

    clear_annotations(gid, target, kind="assumption")

    annotations_after = list(engine.annotations_of(target))
    assert annotations_after == annotations_before, (
        "clear_annotations() mutated the cached engine instance "
        "— same C-NEW-3 race as annotate()."
    )


def test_concurrent_readers_and_writers_no_iteration_race(
    tier0_graph_id_default_cache,
):
    """Stress test: K reader threads iterate the annotations
    dict while 1 writer thread mutates. Without the writer's
    deepcopy, this would eventually raise RuntimeError:
    dictionary changed size during iteration. With the
    deepcopy, readers iterate an unchanging dict — no error.

    Bounded duration (1.5s) so the test is fast in CI. The
    deterministic tests above are the primary armor; this is
    belt-and-suspenders for the actual concurrent failure
    mode."""
    gid = tier0_graph_id_default_cache
    target = "src.tokens.ERC4626:ERC4626"

    results: dict = {"errors": [], "reads": 0, "writes": 0}
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                # nodes_with_annotation iterates
                # engine._store._graph.annotations.
                _ = nodes_with_annotation(gid, "assumption")
                results["reads"] += 1
            except RuntimeError as e:
                if "dictionary changed size" in str(e):
                    results["errors"].append(str(e))
                else:
                    raise

    def writer() -> None:
        i = 0
        while not stop.is_set():
            annotate(
                gid,
                target,
                "assumption",
                f"concurrent stress test note {i}",
                source="test",
            )
            clear_annotations(gid, target, kind="assumption")
            i += 1
            results["writes"] += 1

    readers = [
        threading.Thread(target=reader, daemon=True) for _ in range(4)
    ]
    writer_thread = threading.Thread(target=writer, daemon=True)
    for t in readers:
        t.start()
    writer_thread.start()

    time.sleep(1.5)
    stop.set()

    for t in readers + [writer_thread]:
        t.join(timeout=2.0)

    assert not results["errors"], (
        f"saw {len(results['errors'])} iteration-race errors "
        f"in {results['reads']} reads + {results['writes']} "
        f"writes:\n  {results['errors'][0]}"
    )
    # Sanity that the test actually exercised both paths.
    assert results["reads"] > 10, (
        f"reader threads didn't iterate enough to be meaningful "
        f"(only {results['reads']} reads)"
    )
    assert results["writes"] > 0, "writer thread didn't run"
