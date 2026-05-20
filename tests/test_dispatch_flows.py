import threading
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
