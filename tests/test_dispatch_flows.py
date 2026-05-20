import threading
import time
from unittest.mock import MagicMock

import pytest

from src.agent import DEFAULT_CONCURRENCY_CAP, dispatch_flows


class _FakeAgent:
    """Records every invoke() call and returns synthetic replies.

    Stand-in for the deepagents-built agent so dispatch_flows can
    be exercised without LLM calls (no API key, no $)."""

    def __init__(self, fail_for: set[str] | None = None):
        self.calls: list[str] = []
        self.fail_for = fail_for or set()
        self._lock = threading.Lock()

    def invoke(self, inputs):
        msg = inputs["messages"][0]["content"]
        with self._lock:
            self.calls.append(msg)
        for eid in self.fail_for:
            if f"`{eid}`" in msg:
                raise RuntimeError(f"simulated failure for {eid}")
        reply = MagicMock()
        reply.content = "/fake/path/written.md"
        return {"messages": [reply]}


def test_dispatch_flows_walks_filtered_entrypoints(
    monkeypatch, tier0_graph_id_default_cache
):
    """With default skip_leaf_entrypoints=True, only Tier 0
    entrypoints with at least one outgoing callee are dispatched.
    Probe established this is 12 of 19."""
    gid = tier0_graph_id_default_cache
    fake = _FakeAgent()
    monkeypatch.setattr("src.agent.build_agent", lambda *a, **k: fake)

    result = dispatch_flows(gid, "/tmp/fake-vault", concurrency_cap=1)
    assert result["entrypoint_count"] == 12
    assert len(result["successes"]) == 12
    assert len(result["failures"]) == 0
    assert len(fake.calls) == 12


def test_dispatch_flows_includes_entrypoint_id_in_task(
    monkeypatch, tier0_graph_id_default_cache
):
    """Task message uses the "Trace the entrypoint `<id>`" verb
    match the main prompt steers on."""
    gid = tier0_graph_id_default_cache
    fake = _FakeAgent()
    monkeypatch.setattr("src.agent.build_agent", lambda *a, **k: fake)

    dispatch_flows(gid, "/tmp/fake-vault", concurrency_cap=1)
    assert any("Trace the entrypoint" in c for c in fake.calls)
    assert any(
        "src.tokens.ERC4626:ERC4626.deposit" in c for c in fake.calls
    )
    # Cross-check: NEVER use the node-documenter verb (chunk
    # 3.16 I15 refactor — template-swap regression armor).
    assert not any("Document the node" in c for c in fake.calls)


def test_dispatch_flows_no_filter_includes_leaves(
    monkeypatch, tier0_graph_id_default_cache
):
    """skip_leaf_entrypoints=False dispatches the full attack
    surface — all 19 Tier 0 entrypoints."""
    gid = tier0_graph_id_default_cache
    fake = _FakeAgent()
    monkeypatch.setattr("src.agent.build_agent", lambda *a, **k: fake)

    result = dispatch_flows(
        gid,
        "/tmp/fake-vault",
        concurrency_cap=1,
        skip_leaf_entrypoints=False,
    )
    assert result["entrypoint_count"] == 19


def test_dispatch_flows_continues_past_failures(
    monkeypatch, tier0_graph_id_default_cache
):
    """One entrypoint failing must not block the rest."""
    gid = tier0_graph_id_default_cache
    bad = "src.tokens.ERC4626:ERC4626.deposit"
    fake = _FakeAgent(fail_for={bad})
    monkeypatch.setattr("src.agent.build_agent", lambda *a, **k: fake)

    result = dispatch_flows(gid, "/tmp/fake-vault", concurrency_cap=1)
    assert len(result["failures"]) == 1
    assert result["failures"][0]["node_id"] == bad
    assert "simulated failure" in result["failures"][0]["error"]
    assert len(result["successes"]) == 11


def test_dispatch_flows_concurrency_cap_default():
    """Cap default tracks the documented constant (same as
    dispatch_topo)."""
    assert DEFAULT_CONCURRENCY_CAP == 5


def test_dispatch_flows_rejects_zero_or_negative_cap(
    monkeypatch, tier0_graph_id_default_cache
):
    gid = tier0_graph_id_default_cache
    monkeypatch.setattr(
        "src.agent.build_agent", lambda *a, **k: _FakeAgent()
    )
    for bad in (0, -1):
        with pytest.raises(ValueError, match="concurrency_cap must be"):
            dispatch_flows(
                gid, "/tmp/fake-vault", concurrency_cap=bad
            )


# --- per-invocation timeout (chunk 3.11) -----------------------------


class _HangingAgent:
    """Same pattern as test_dispatch._HangingAgent — agent
    whose invoke() blocks forever to simulate the chunk 3.5 hang."""

    def __init__(self, block_signal: threading.Event):
        self._lock = threading.Lock()
        self.calls = 0
        self._block = block_signal

    def invoke(self, inputs):
        with self._lock:
            self.calls += 1
        self._block.wait()
        raise AssertionError("unreachable")


def test_dispatch_flows_per_invoke_timeout_records_failure(
    monkeypatch, tier0_graph_id_default_cache
):
    """Same as dispatch_topo's timeout test — hung agent.invoke()
    must be recorded as a timeout failure after per_invoke_timeout
    seconds. Mirrors the chunk 3.5 hang pattern at the flows
    dispatch level."""
    gid = tier0_graph_id_default_cache
    block = threading.Event()
    hanging = _HangingAgent(block)
    monkeypatch.setattr(
        "src.agent.build_agent", lambda *a, **k: hanging
    )

    try:
        result = dispatch_flows(
            gid,
            "/tmp/fake-vault",
            concurrency_cap=2,
            per_invoke_timeout=0.3,
        )
        # Tier 0 leaf-filtered: 12 entrypoints, all timeout.
        assert result["entrypoint_count"] == 12
        assert len(result["successes"]) == 0
        assert len(result["failures"]) == 12
        for fail in result["failures"]:
            assert "TimeoutError" in fail["error"]
            assert "per_invoke_timeout" in fail["error"]
        assert hanging.calls >= 2
    finally:
        block.set()


def test_dispatch_flows_rejects_zero_or_negative_per_invoke_timeout(
    monkeypatch, tier0_graph_id_default_cache
):
    gid = tier0_graph_id_default_cache
    monkeypatch.setattr(
        "src.agent.build_agent", lambda *a, **k: _FakeAgent()
    )
    for bad in (0, -1, -0.5):
        with pytest.raises(ValueError, match="per_invoke_timeout"):
            dispatch_flows(
                gid,
                "/tmp/fake-vault",
                concurrency_cap=1,
                per_invoke_timeout=bad,
            )


def test_invoke_one_flow_validates_entrypoint_id():
    """Same allowlist applies to FlowTracer's entrypoint_id
    (chunk 3.14)."""
    from src.agent import _invoke_one_flow

    fake_agent = object()
    for bad in ("back`tick", "new\nline", "", "space x"):
        with pytest.raises(ValueError, match="invalid node_id"):
            _invoke_one_flow(fake_agent, "abc012345678", bad, "/v")


# --- entrypoint_filter (chunk 3.15) --------------------------------


def test_dispatch_flows_entrypoint_filter_narrows_dispatch(
    monkeypatch, tier0_graph_id_default_cache
):
    """entrypoint_filter scopes the dispatch — only the
    filter-approved entrypoints get dispatched."""
    gid = tier0_graph_id_default_cache
    fake = _FakeAgent()
    monkeypatch.setattr(
        "src.agent.build_agent", lambda *a, **k: fake
    )

    target = "src.tokens.ERC4626:ERC4626.deposit"

    def only_deposit(entrypoints):
        return [e for e in entrypoints if e["node_id"] == target]

    result = dispatch_flows(
        gid,
        "/tmp/fake-vault",
        concurrency_cap=1,
        skip_leaf_entrypoints=False,
        entrypoint_filter=only_deposit,
    )
    assert result["entrypoint_count"] == 1
    assert result["order"] == [target]
    assert len(fake.calls) == 1


def test_dispatch_flows_entrypoint_filter_applies_before_leaf_cut(
    monkeypatch, tier0_graph_id_default_cache
):
    """Filter narrows first, then skip_leaf_entrypoints further
    reduces. The two filters compose."""
    from src.tools import attack_surface, callees_of

    gid = tier0_graph_id_default_cache
    fake = _FakeAgent()
    monkeypatch.setattr(
        "src.agent.build_agent", lambda *a, **k: fake
    )

    def only_erc4626(entrypoints):
        return [
            e for e in entrypoints
            if "ERC4626:ERC4626" in e["node_id"]
        ]

    # Compute expected count directly: ERC4626 entrypoints that
    # also have callees (the same composition the dispatch
    # performs). Test verifies the result matches; the exact
    # number depends on Solmate's ERC4626 surface and isn't worth
    # hard-coding.
    surface = attack_surface(gid)
    expected = sum(
        1
        for e in surface
        if "ERC4626:ERC4626" in e["node_id"]
        and callees_of(gid, e["node_id"])
    )
    assert expected > 0, "fixture invariant: ERC4626 has non-leaf entrypoints"

    result = dispatch_flows(
        gid,
        "/tmp/fake-vault",
        concurrency_cap=1,
        skip_leaf_entrypoints=True,
        entrypoint_filter=only_erc4626,
    )
    assert result["entrypoint_count"] == expected
    for nid in result["order"]:
        assert "ERC4626:ERC4626" in nid

    # Sanity: with the filter off but leaf-cut on, more
    # entrypoints survive (the filter is doing real work).
    no_filter = dispatch_flows(
        gid,
        "/tmp/fake-vault",
        concurrency_cap=1,
        skip_leaf_entrypoints=True,
    )
    assert no_filter["entrypoint_count"] > result["entrypoint_count"]


def test_dispatch_flows_empty_filter_dispatches_nothing(
    monkeypatch, tier0_graph_id_default_cache
):
    """A filter returning [] short-circuits the dispatch — no
    LLM calls, empty result. Sanity check before a costly run."""
    gid = tier0_graph_id_default_cache
    fake = _FakeAgent()
    monkeypatch.setattr(
        "src.agent.build_agent", lambda *a, **k: fake
    )

    result = dispatch_flows(
        gid,
        "/tmp/fake-vault",
        concurrency_cap=1,
        entrypoint_filter=lambda _eps: [],
    )
    assert result["entrypoint_count"] == 0
    assert result["successes"] == []
    assert result["failures"] == []
    assert len(fake.calls) == 0


# --- concurrency at cap=5 (chunk 3.16) ----------------------------


def test_dispatch_flows_uses_concurrency_at_cap_5(
    monkeypatch, tier0_graph_id_default_cache
):
    """Verify dispatch_flows actually runs workers in parallel at
    cap=5. The other tests use cap=1, leaving the race-safety
    claim ("langgraph's CompiledStateGraph keeps no mutable
    instance state across .invoke() calls") structurally
    unverified and `_FakeAgent`'s threading.Lock as dead code.

    Strategy: each invoke() sleeps briefly so workers must
    overlap; assert all 12 entrypoints dispatched once, multiple
    distinct thread IDs ran (proves multi-threading), and total
    wall-time is well below serial."""
    gid = tier0_graph_id_default_cache

    class _ConcurrentFakeAgent:
        def __init__(self):
            self._lock = threading.Lock()
            self.calls: list[str] = []
            self.thread_ids: set[int] = set()

        def invoke(self, inputs):
            # 100ms is long enough that workers MUST overlap.
            # Serial 12 × 0.1 = 1.2s; cap=5 parallel ≈ 0.3s.
            time.sleep(0.1)
            msg = inputs["messages"][0]["content"]
            tid = threading.get_ident()
            with self._lock:
                self.calls.append(msg)
                self.thread_ids.add(tid)
            reply = MagicMock()
            reply.content = "/fake/path.md"
            return {"messages": [reply]}

    fake = _ConcurrentFakeAgent()
    monkeypatch.setattr(
        "src.agent.build_agent", lambda *a, **k: fake
    )

    t0 = time.monotonic()
    result = dispatch_flows(
        gid, "/tmp/fake-vault", concurrency_cap=5
    )
    elapsed = time.monotonic() - t0

    # All 12 entrypoints dispatched — none lost to races on the
    # shared `calls` list (the threading.Lock now actually does
    # work).
    assert result["entrypoint_count"] == 12
    assert len(result["successes"]) == 12
    assert len(result["failures"]) == 0
    assert len(fake.calls) == 12

    # Multi-threading actually happened. Lower bound 2 (parallel
    # at all); upper bound 5 (pool cap). Exact count varies with
    # OS scheduling — assertion is on "did the pool actually
    # parallelize", not "exactly 5 workers".
    assert 2 <= len(fake.thread_ids) <= 5, (
        f"expected 2-5 distinct threads, got {len(fake.thread_ids)}"
    )

    # Wall time well below serial. Serial = 12 × 0.1 = 1.2s;
    # parallel cap=5 should be ~0.3s. Threshold 0.8s leaves
    # plenty of slack for CI/test-machine variance.
    assert elapsed < 0.8, (
        f"dispatch_flows took {elapsed:.2f}s at cap=5; serial "
        f"would be ~1.2s. Parallelism not engaged?"
    )
