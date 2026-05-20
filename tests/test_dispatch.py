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

        start = time.monotonic()
        # Must NOT raise. The gather function swallows on_done
        # exceptions and continues.
        _gather_with_per_invoke_timeout(
            futures_map,
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
