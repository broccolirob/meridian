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
    gid = tier0_graph_id_default_cache
    fake = _FakeAgent()
    monkeypatch.setattr("src.agent.build_agent", lambda *a, **k: fake)

    dispatch_topo(gid, "/tmp/fake-vault", concurrency_cap=1)

    assert any("src.tokens.ERC20:ERC20" in c for c in fake.calls)
    assert any("src.tokens.ERC4626:ERC4626" in c for c in fake.calls)


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
