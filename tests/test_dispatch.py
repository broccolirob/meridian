import threading
from unittest.mock import MagicMock

import pytest

from src.agent import DEFAULT_CONCURRENCY_CAP, dispatch_topo


class _FakeAgent:
    """Records every invoke() call and returns synthetic replies.

    Stand-in for the deepagents-built agent so dispatch_topo can be
    exercised without LLM calls (no API key, no $)."""

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
        reply.content = "/fake/path/written.md"
        return {"messages": [reply]}


def test_dispatch_walks_every_node(monkeypatch, tier0_graph_id_default_cache):
    """All documentable Tier 0 nodes get exactly one agent invocation."""
    gid = tier0_graph_id_default_cache
    fake = _FakeAgent()
    monkeypatch.setattr("src.agent.build_agent", lambda *a, **k: fake)

    result = dispatch_topo(gid, "/tmp/fake-vault", concurrency_cap=1)

    assert result["node_count"] == 8
    assert len(result["successes"]) == 8
    assert len(result["failures"]) == 0
    assert len(fake.calls) == 8


def test_dispatch_includes_node_id_in_task(
    monkeypatch, tier0_graph_id_default_cache
):
    """Task message uses the "Document the node `<id>`" verb the
    main prompt steers on. After chunk 3.16's I15 refactor
    (template parameterization), the verb is also coupling armor
    — if the template gets swapped with the FlowTracer template,
    this test catches it."""
    gid = tier0_graph_id_default_cache
    fake = _FakeAgent()
    monkeypatch.setattr("src.agent.build_agent", lambda *a, **k: fake)

    dispatch_topo(gid, "/tmp/fake-vault", concurrency_cap=1)

    assert any("Document the node" in c for c in fake.calls)
    assert any("src.tokens.ERC20:ERC20" in c for c in fake.calls)
    assert any("src.tokens.ERC4626:ERC4626" in c for c in fake.calls)
    # Cross-check: NEVER use the flow-tracer verb.
    assert not any("Trace the entrypoint" in c for c in fake.calls)


def test_dispatch_continues_past_failures(
    monkeypatch, tier0_graph_id_default_cache
):
    """One node failing must not block the rest."""
    gid = tier0_graph_id_default_cache
    fake = _FakeAgent(fail_for={"src.tokens.ERC20:ERC20"})
    monkeypatch.setattr("src.agent.build_agent", lambda *a, **k: fake)

    result = dispatch_topo(gid, "/tmp/fake-vault", concurrency_cap=1)

    assert len(result["failures"]) == 1
    assert result["failures"][0]["node_id"] == "src.tokens.ERC20:ERC20"
    assert "simulated failure" in result["failures"][0]["error"]
    assert len(result["successes"]) == 7


def test_dispatch_respects_concurrency_cap_default():
    """Cap default tracks the documented constant."""
    assert DEFAULT_CONCURRENCY_CAP == 5


def test_dispatch_rejects_zero_or_negative_cap(
    monkeypatch, tier0_graph_id_default_cache
):
    gid = tier0_graph_id_default_cache
    monkeypatch.setattr(
        "src.agent.build_agent", lambda *a, **k: _FakeAgent()
    )
    for bad in (0, -1):
        with pytest.raises(ValueError, match="concurrency_cap must be"):
            dispatch_topo(
                gid,
                "/tmp/fake-vault",
                concurrency_cap=bad,
            )


def test_dispatch_order_field_matches_topo_order(
    monkeypatch, tier0_graph_id_default_cache
):
    """The returned `order` field IS the topo order, for replay/debug."""
    from src.graph.topo import topo_order

    gid = tier0_graph_id_default_cache
    expected = topo_order(gid)  # default cache_root
    fake = _FakeAgent()
    monkeypatch.setattr("src.agent.build_agent", lambda *a, **k: fake)

    result = dispatch_topo(gid, "/tmp/fake-vault", concurrency_cap=1)

    assert result["order"] == expected


# --- per-invocation timeout (chunk 3.11) -----------------------------


class _HangingAgent:
    """Stand-in agent whose invoke() blocks on an event forever.

    Use to simulate the chunk 3.5 hang pattern in tests without
    real LLM calls."""

    def __init__(self, block_signal: threading.Event):
        self._lock = threading.Lock()
        self.calls = 0
        self._block = block_signal

    def invoke(self, inputs):
        with self._lock:
            self.calls += 1
        self._block.wait()  # hang until released (or never)
        raise AssertionError("unreachable")


def test_dispatch_per_invoke_timeout_records_failure(
    monkeypatch, tier0_graph_id_default_cache
):
    """A hung LLM call must NOT block the orchestrator. After
    per_invoke_timeout seconds the future is recorded as a
    failure and dispatch_topo returns. Pre-3.11 this would hang
    forever (chunk 3.5 pattern reproducible here)."""
    gid = tier0_graph_id_default_cache
    block = threading.Event()  # never set during test
    hanging = _HangingAgent(block)
    monkeypatch.setattr(
        "src.agent.build_agent", lambda *a, **k: hanging
    )

    try:
        result = dispatch_topo(
            gid,
            "/tmp/fake-vault",
            concurrency_cap=2,
            per_invoke_timeout=0.3,
        )

        # All 8 Tier 0 nodes recorded as timeout failures.
        assert result["node_count"] == 8
        assert len(result["successes"]) == 0
        assert len(result["failures"]) == 8
        for fail in result["failures"]:
            assert "TimeoutError" in fail["error"]
            assert "per_invoke_timeout" in fail["error"]
        # Workers ran (got past the lock).
        assert hanging.calls >= 2
    finally:
        # Release daemon workers belt-and-suspenders.
        block.set()


def test_dispatch_rejects_zero_or_negative_per_invoke_timeout(
    monkeypatch, tier0_graph_id_default_cache
):
    gid = tier0_graph_id_default_cache
    monkeypatch.setattr(
        "src.agent.build_agent", lambda *a, **k: _FakeAgent()
    )
    for bad in (0, -1, -0.5):
        with pytest.raises(ValueError, match="per_invoke_timeout"):
            dispatch_topo(
                gid,
                "/tmp/fake-vault",
                concurrency_cap=1,
                per_invoke_timeout=bad,
            )


# --- node_id allowlist validation (chunk 3.14) ----------------------


def test_invoke_one_validates_node_id():
    """The validator is the single trust-boundary check for
    LLM-facing prompts. Rejects backticks, newlines, semicolons,
    spaces, and overly long inputs."""
    from src.agent import _invoke_one

    fake_agent = object()
    bad_ids = [
        "back`tick",            # backtick — escapes prompt fence
        "new\nline",            # newline — escapes the line
        "tab\there",            # tab
        "space here",           # space
        "semi;colon",           # semicolon
        "unicode break",   # U+2028 LINE SEPARATOR
        "",                     # empty
        "x" * 501,              # too long (501 chars > 500 cap)
    ]
    for bad in bad_ids:
        with pytest.raises(ValueError, match="invalid node_id"):
            _invoke_one(fake_agent, "abc012345678", bad, "/v")


def test_invoke_one_accepts_real_trailmark_ids():
    """Real Tier 0/1 node IDs must pass validation unchanged."""
    from src.agent import _validate_node_id

    real_ids = [
        "src.tokens.ERC4626:ERC4626",
        "src.tokens.ERC4626:ERC4626.deposit",
        "contracts.UniswapV2Pair:UniswapV2Pair",
        "contracts.UniswapV2Pair:UniswapV2Pair.swap",
        "contracts.UniswapV2Pair:UniswapV2Pair._safeTransfer",
        "contracts.interfaces.IUniswapV2Pair:IUniswapV2Pair",
        "contracts.libraries.SafeMath:SafeMath",
        "contracts.libraries.SafeMath",  # module-kind node
    ]
    for nid in real_ids:
        _validate_node_id(nid)  # must not raise


# --- broken on_progress / on_done callback resilience (chunk 3.19) ---


def test_dispatch_continues_when_on_progress_callback_raises(
    monkeypatch, tier0_graph_id_default_cache, caplog
):
    """Chunk 3.19 /review C-NEW-4: a broken on_progress callback
    must NOT abort the dispatch run. Pre-3.19 the callback's
    exception propagated out of `_gather_with_per_invoke_timeout`
    → out of `_run_pool` → out of `dispatch_topo`, abandoning
    in-flight workers and dropping every node's failure record.
    Post-3.19 the gather loop catches on_done exceptions, logs
    them via _log.exception, and continues to drain.

    Production scenario: a notebook user's progress bar writes
    to a closed stderr (Jupyter kernel restart, broken pipe);
    the dispatch must still complete and the user gets the
    full result summary."""
    import logging

    gid = tier0_graph_id_default_cache
    fake = _FakeAgent()
    monkeypatch.setattr("src.agent.build_agent", lambda *a, **k: fake)

    def broken_callback(idx: int, total: int, nid: str) -> None:
        raise RuntimeError(
            f"simulated broken pipe for {nid} (idx={idx}/{total})"
        )

    caplog.set_level(logging.WARNING, logger="src.agent")
    result = dispatch_topo(
        gid,
        "/tmp/fake-vault",
        concurrency_cap=1,
        on_progress=broken_callback,
    )

    # Dispatch completed despite the broken callback — every
    # node got documented (or attempted), and the summary
    # came back.
    assert result["node_count"] == 8
    assert len(result["successes"]) == 8

    # Every on_done exception was logged with a traceback.
    on_done_errors = [
        r
        for r in caplog.records
        if "on_done raised" in r.getMessage()
    ]
    assert len(on_done_errors) == 8, (
        f"expected one on_done log per node "
        f"(got {len(on_done_errors)})"
    )
    # Tracebacks captured (caplog records the exception info
    # for _log.exception calls).
    assert any(r.exc_info for r in on_done_errors), (
        "_log.exception should attach exc_info; without it the "
        "traceback is lost"
    )


def test_gather_logs_and_continues_when_on_done_raises():
    """Direct unit test of `_gather_with_per_invoke_timeout`:
    if on_done raises for every future, the gather loop
    must drain all futures, log each failure, and return —
    not raise, not hang.

    Pre-3.19 the function aborted with an uncaught exception
    after the first on_done failure (in the completion
    branch's recovery path). Post-3.19 every future is
    drained from pending in `finally` even if on_done blew up."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    from src.agent import _gather_with_per_invoke_timeout

    pool = ThreadPoolExecutor(max_workers=3)
    try:

        def _quick(name: str) -> tuple[str, dict]:
            return ("ok", {"node_id": name, "agent_reply": f"/{name}.md"})

        f1 = pool.submit(_quick, "n1")
        f2 = pool.submit(_quick, "n2")
        f3 = pool.submit(_quick, "n3")
        futures_map = {f1: "n1", f2: "n2", f3: "n3"}

        called: list[str] = []

        def broken_on_done(nid: str, status: str, info) -> None:
            called.append(nid)
            raise RuntimeError(f"simulated callback failure: {nid}")

        # Mock start_times — these test futures complete fast
        # enough that no timeout check fires; an empty dict is
        # fine (queued-future code path will skip per-invoke
        # check for items not in start_times).
        start_times: dict[str, float] = {}

        start = time.monotonic()
        # Must NOT raise. The gather function swallows on_done
        # exceptions and continues.
        _gather_with_per_invoke_timeout(
            futures_map,
            start_times=start_times,
            per_invoke_timeout=10.0,
            on_done=broken_on_done,
        )
        elapsed = time.monotonic() - start

        # Loop terminated in bounded time (didn't hang).
        assert elapsed < 5.0, (
            f"gather took {elapsed:.1f}s — possible hang or "
            f"infinite retry loop"
        )
        # All three futures were processed exactly once each.
        assert sorted(called) == ["n1", "n2", "n3"], (
            f"expected each future's on_done called once; got "
            f"{called}"
        )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# --- worker-time per-invoke deadline (chunk 3.20 / C-NEW-5) ---


def test_per_invoke_timeout_does_not_count_queue_wait():
    """Chunk 3.20 / C-NEW-5: tail items in a saturated pool
    must NOT consume their per_invoke_timeout while queued.
    Pre-3.20 the deadline was stamped at submission, so any
    item queued for > per_invoke_timeout seconds before its
    worker started would be marked TimeoutError despite
    never having run.

    Test setup: 30 items × 80ms work × cap=5 ×
    per_invoke_timeout=0.4s. The tail (items 21-30) finish
    queue+work past their 0.4s submission deadline. Pre-3.20
    they fail with TimeoutError; post-3.20 they all succeed
    because their workers' per-invoke clocks start when
    execution actually begins.

    Production scenario this armor protects: Tier-2 codebase
    with 80 entrypoints × cap=5 × 60s avg runtime, where
    items 50+ would spuriously time out under submission-
    time accounting."""
    import time
    from typing import Any

    from src.agent import _run_pool

    items = [f"item_{i:02d}" for i in range(30)]
    results: list[tuple[str, str]] = []

    def slow_invoke(item_id: str) -> dict[str, Any]:
        time.sleep(0.08)  # 80ms of "work"
        return {"node_id": item_id, "agent_reply": "ok"}

    def on_done(item_id: str, status: str, info: Any) -> None:
        results.append((item_id, status))

    _run_pool(
        items,
        slow_invoke,
        concurrency_cap=5,
        per_invoke_timeout=0.4,
        on_done=on_done,
        log_kind="dispatch",
    )

    statuses = {item: status for item, status in results}
    succeeded = sum(1 for s in statuses.values() if s == "ok")
    failed = sum(1 for s in statuses.values() if s == "fail")

    assert succeeded == 30, (
        f"expected all 30 items to succeed under post-3.20 "
        f"worker-time deadline accounting; got "
        f"{succeeded} succeed, {failed} fail. Pre-3.20 the "
        f"queue-wait of items 21-30 would consume their "
        f"per_invoke_timeout before workers picked them up."
    )


def test_pool_deadlock_detector_marks_queued_when_workers_hang():
    """Chunk 3.20 / C-NEW-5: when running workers hang AND
    queued items can't make progress, the gather loop's
    deadlock detector marks them as failed after
    2× per_invoke_timeout of no progress. Without the
    detector, dispatch would loop forever (queued items
    have no per-invoke deadline; running items are hung).

    This test exercises `_run_pool` DIRECTLY with synthetic
    hanging items so the per-invoke vs deadlock split is
    deterministic (cap=2 + 8 items = exactly 2 per-invoke +
    6 deadlock). The end-to-end chunk 3.11 hang test
    (`test_dispatch_per_invoke_timeout_records_failure`)
    goes through dispatch_topo's level loop, where each
    level creates a fresh pool — the per-level split varies
    with tier 0's topology and isn't a good unit-level
    contract."""
    import threading
    import time
    from typing import Any

    from src.agent import _run_pool

    block = threading.Event()  # never set during the test
    items = [f"item_{i}" for i in range(8)]
    results: list[tuple[str, str, Any]] = []

    def hanging_invoke(item_id: str) -> dict[str, Any]:
        # Hang indefinitely. The safety timeout (5s) makes the
        # test fail-fast if the deadlock detector doesn't fire.
        block.wait(timeout=5.0)
        return {"node_id": item_id, "agent_reply": "/never.md"}

    def on_done(item_id: str, status: str, info: Any) -> None:
        results.append((item_id, status, info))

    try:
        start = time.monotonic()
        _run_pool(
            items,
            hanging_invoke,
            concurrency_cap=2,
            per_invoke_timeout=0.2,
            on_done=on_done,
            log_kind="test",
        )
        elapsed = time.monotonic() - start

        # Total runtime: 0.2s per-invoke + 0.4s deadlock + a bit
        # of poll-interval slack. Should be well under 2s.
        assert elapsed < 2.0, (
            f"_run_pool took {elapsed:.2f}s — possibly looped"
        )

        # All 8 items dispatched to on_done.
        assert len(results) == 8

        # The split: 2 running workers hit per-invoke timeout
        # at t=0.2; 6 queued items hit deadlock detection at
        # t≈0.6.
        invoke_msgs = [
            info for _, _, info in results
            if "invocation exceeded" in info
        ]
        deadlock_msgs = [
            info for _, _, info in results
            if "pool made no progress" in info
        ]
        assert len(invoke_msgs) == 2, (
            f"expected 2 per-invoke timeouts (the cap=2 running "
            f"workers); got {len(invoke_msgs)}: {invoke_msgs}"
        )
        assert len(deadlock_msgs) == 6, (
            f"expected 6 deadlock-detected failures (the queued "
            f"items); got {len(deadlock_msgs)}: {deadlock_msgs}"
        )

        # All messages contain the contract substrings the
        # chunk 3.11 hang test asserts.
        for _, _, info in results:
            assert "TimeoutError" in info
            assert "per_invoke_timeout" in info
    finally:
        block.set()


# --- graph_id + vault_path prompt-boundary validation (chunk 3.22 / C-NEW-7) ---


def test_invoke_one_validates_graph_id():
    """Chunk 3.22 / C-NEW-7: _invoke_one validates graph_id
    at the LLM trust boundary. graph_id is interpolated into
    the task template; injection chars (backticks, newlines)
    would forge LLM instructions. Reuses _validate_graph_id
    from persist.py (strict 12-hex allowlist)."""
    from src.agent import _invoke_one

    fake_agent = object()
    bad_ids = [
        "BACKTICK`HERE",       # backtick in graph_id
        "new\nline123",        # newline
        "tab\there0001",       # tab
        "space here00",        # space
        "TOOLONG_NHEX",        # non-hex (still 12 chars)
        "abcdef",              # too short
        "abcdef0123456",       # too long (13 hex)
        "ABCDEF012345",        # uppercase (regex is lowercase-only)
        "",                    # empty
    ]
    for bad in bad_ids:
        with pytest.raises(ValueError, match="invalid graph_id"):
            _invoke_one(fake_agent, bad, "src.X:X", "/v")


def test_invoke_one_accepts_valid_graph_id():
    """Sanity: 12-hex graph_ids produced by repo_hash pass.
    Protects against a future regex tightening that
    accidentally rejects legitimate IDs."""
    from src.graph.persist import _validate_graph_id, repo_hash

    real = repo_hash("/some/path")
    assert len(real) == 12
    _validate_graph_id(real)  # must not raise


def test_build_agent_validates_graph_id(monkeypatch):
    """build_agent rejects malformed graph_id before
    constructing the system prompt that bakes it in."""
    from src.agent import build_agent

    # Stub ChatOpenAI so we don't hit the network (build_agent
    # constructs one even if validation succeeds).
    monkeypatch.setattr(
        "src.agent.ChatOpenAI",
        lambda *a, **k: object(),
    )

    for bad in ["BAD`TICK", "new\nline", "uppercase", ""]:
        with pytest.raises(ValueError, match="invalid graph_id"):
            build_agent(bad, "/abs/vault")


def test_build_agent_validates_vault_path(monkeypatch):
    """build_agent rejects vault paths that could inject into
    the system prompt OR aren't absolute."""
    from src.agent import build_agent

    monkeypatch.setattr(
        "src.agent.ChatOpenAI",
        lambda *a, **k: object(),
    )
    # Also stub create_deep_agent so it doesn't try to wire up
    # real LLM machinery during the sanity-pass at the end.
    monkeypatch.setattr(
        "src.agent.create_deep_agent",
        lambda *a, **k: object(),
    )

    # Control chars: newline, carriage return, tab, NUL,
    # unicode line/paragraph separators.
    bad_chars = [
        "/abs/vault\nINJECT",
        "/abs/vault\rINJECT",
        "/abs/vault\tinject",
        "/abs/vault\x00inject",
        "/abs/vault inject",
        "/abs/vault inject",
    ]
    for bad in bad_chars:
        with pytest.raises(
            ValueError, match="contains control chars"
        ):
            build_agent("abc012345678", bad)

    # Non-absolute paths.
    for rel in ["relative/vault", "./vault", "vault"]:
        with pytest.raises(ValueError, match="must be absolute"):
            build_agent("abc012345678", rel)

    # Empty.
    with pytest.raises(ValueError, match="empty"):
        build_agent("abc012345678", "")

    # Sanity: a clean absolute path with spaces, parens, and
    # a backtick passes (backticks are legal in POSIX and
    # don't break the system prompt's `vault_path = {value}`
    # syntax — no fence to escape).
    build_agent(
        "abc012345678",
        "/path/with spaces/and (parens)/and`backtick",
    )


# --- dispatch_topo level-gating + on_progress armor (chunk 3.23) ---


def test_dispatch_topo_completes_level_before_next_starts(
    monkeypatch, tier0_graph_id_default_cache
):
    """Chunk 3.23 / I-NEW-1: dispatch_topo's
    `for level in levels: _run_pool(...)` structure
    guarantees that level N+1 starts only AFTER level N
    completes. This keeps wikilink targets on disk before
    derived contracts are documented (chunk 3.5 design).

    Test approach: record the order in which on_progress
    fires for each item, then assert items partition
    cleanly by level index. A regression that collapsed
    the level loop into a single flat pool would
    interleave items from different levels in the
    completion order.

    cap=5 + a small invoke sleep makes the test
    meaningful: multiple items in a level run
    concurrently, so the only thing keeping them grouped
    by level is `_run_pool`'s synchronous return between
    levels."""
    import time

    from src.graph.topo import topo_levels

    gid = tier0_graph_id_default_cache
    levels = topo_levels(gid)
    level_of: dict[str, int] = {
        nid: i for i, level in enumerate(levels) for nid in level
    }

    fake = _FakeAgent()
    original_invoke = fake.invoke

    def slow_invoke(inputs):
        time.sleep(0.05)
        return original_invoke(inputs)

    fake.invoke = slow_invoke  # type: ignore[method-assign]
    monkeypatch.setattr(
        "src.agent.build_agent", lambda *a, **k: fake
    )

    completion_order: list[str] = []

    def track(idx: int, total: int, nid: str) -> None:
        completion_order.append(nid)

    dispatch_topo(
        gid,
        "/tmp/fake-vault",
        concurrency_cap=5,
        on_progress=track,
    )

    assert len(completion_order) == sum(len(L) for L in levels)

    levels_seen = [level_of[nid] for nid in completion_order]

    # Invariant: levels_seen is monotonically
    # non-decreasing. A flattened level loop would
    # produce descents (e.g., level 1 item firing after
    # a level 2 item).
    for i in range(1, len(levels_seen)):
        assert levels_seen[i] >= levels_seen[i - 1], (
            f"level-gating broken: completion order "
            f"{completion_order} has level descent at "
            f"position {i} ({completion_order[i]} in "
            f"level {levels_seen[i]} fired after "
            f"{completion_order[i - 1]} in level "
            f"{levels_seen[i - 1]})"
        )


def test_dispatch_topo_on_progress_callback_contract(
    monkeypatch, tier0_graph_id_default_cache
):
    """Chunk 3.23 / I-NEW-2: pin the on_progress callback
    contract.

    Three sub-properties:
    1. Signature: callback invoked with
       `(idx: int, total: int, item_id: str)`.
    2. `idx` monotonically increases 1..N as items
       complete (chunk 3.16 I16 cross-level accumulation
       invariant).
    3. `total` is the same `node_count` for every call.

    No existing test covers any of these. A regression
    that, e.g., reset progress_idx per level, or swapped
    argument order, would silently break notebook
    progress bars without any current test failing."""
    gid = tier0_graph_id_default_cache
    fake = _FakeAgent()
    monkeypatch.setattr(
        "src.agent.build_agent", lambda *a, **k: fake
    )

    calls: list[tuple[int, int, str]] = []

    def track(idx: int, total: int, nid: str) -> None:
        calls.append((idx, total, nid))

    result = dispatch_topo(
        gid,
        "/tmp/fake-vault",
        concurrency_cap=5,
        on_progress=track,
    )

    # Sub-property 1: signature shape.
    for idx, total, nid in calls:
        assert isinstance(idx, int)
        assert isinstance(total, int)
        assert isinstance(nid, str)

    # Sub-property 2: idx monotonically 1..N.
    indices = [c[0] for c in calls]
    assert indices == list(range(1, len(indices) + 1)), (
        f"progress_idx not monotonic 1..N (chunk 3.16 I16 "
        f"cross-level accumulation broken): {indices}"
    )

    # Sub-property 3: total constant, equals node_count.
    totals = {c[1] for c in calls}
    assert totals == {result["node_count"]}, (
        f"total varied across calls (expected single value "
        f"matching node_count={result['node_count']}): "
        f"{totals}"
    )

    # Sanity: every dispatched node fired exactly one
    # callback.
    nids = [c[2] for c in calls]
    assert sorted(nids) == sorted(result["order"])


# --- gather race-recovery (chunk 3.27 / I-NEW-9) ---


def test_gather_handles_race_between_completion_and_deadline(
    monkeypatch,
):
    """Chunk 3.27 / I-NEW-9: a future can complete between
    `concurrent.futures.wait()` returning and the timeout
    check. Pre-3.27 the timeout branch fired on
    `now - start_times[nid] > per_invoke_timeout` without
    re-checking `future.done()`, mis-marking the completed
    future as TimeoutError despite its side effects being
    on disk. Post-3.27 the timeout branch checks done()
    first and processes as a completion if the race fired.

    The race is microseconds wide in production; this test
    monkey-patches `concurrent.futures.wait` to return an
    empty done-set even though the future is done, making
    the timing deterministic."""
    import time as _time
    from concurrent.futures import Future

    import src.agent as agent_module
    from src.agent import _gather_with_per_invoke_timeout

    # Simulate the race: wait() returns empty done-set even
    # though pending contains a future that completed.
    def fake_wait(pending, *, timeout=None, return_when=None):
        return (set(), pending)

    monkeypatch.setattr(
        agent_module.concurrent.futures,
        "wait",
        fake_wait,
    )

    # Already-done future — simulates the race where the
    # worker completed between wait() and the deadline
    # check.
    completed_future: Future = Future()
    completed_future.set_result(
        ("ok", {"node_id": "n1", "agent_reply": "/n1.md"})
    )
    assert completed_future.done()

    futures_map = {completed_future: "n1"}
    # Stale start_time — deadline is well past.
    start_times = {"n1": _time.monotonic() - 100.0}

    results: list[tuple[str, str, object]] = []

    def on_done(nid: str, status: str, info: object) -> None:
        results.append((nid, status, info))

    _gather_with_per_invoke_timeout(
        futures_map,
        start_times=start_times,
        per_invoke_timeout=0.01,
        on_done=on_done,
    )

    assert len(results) == 1, (
        f"expected exactly one on_done call; got "
        f"{len(results)}: {results}"
    )
    assert results[0][1] == "ok", (
        f"expected status='ok' from race-recovery; got "
        f"{results[0][1]!r}. Pre-3.27 the deadline check "
        f"fires before done() check, mis-marking the "
        f"completed future as TimeoutError despite its "
        f"side effects being on disk."
    )
