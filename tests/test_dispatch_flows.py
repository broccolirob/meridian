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
