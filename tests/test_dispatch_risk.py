"""Tests for src/agent.py::dispatch_risk_synthesis.

Single-invocation dispatcher; mirrors the _FakeAgent pattern
from tests/test_dispatch.py but exercises the no-pool path.
Validates the script-level scheduling contract:
- exactly ONE agent.invoke per call (RiskSynthesizer's
  _ANNOTATE_LOCK contention rules out pool dispatch);
- exceptions from the LLM are CAUGHT and reported via
  {ok: False, error: <str>} so the orchestrator can degrade
  to rc=2 instead of aborting the whole run;
- preconditions (preanalysis subgraphs registered) are
  enforced at runtime, not just by docstring;
- a wedged invoke is cut off by per_invoke_timeout, never
  hanging the script forever.
"""

import time
from unittest.mock import MagicMock

import pytest


class _FakeAgent:
    """Records every invoke() call. Returns a synthetic reply,
    raises a sentinel error, or sleeps. Stand-in for the
    deepagents-built agent."""

    def __init__(
        self,
        *,
        raise_on_invoke: Exception | None = None,
        sleep_seconds: float = 0.0,
    ):
        self.calls: list[str] = []
        self.raise_on_invoke = raise_on_invoke
        self.sleep_seconds = sleep_seconds

    def invoke(self, inputs):
        msg = inputs["messages"][0]["content"]
        self.calls.append(msg)
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)
        if self.raise_on_invoke is not None:
            raise self.raise_on_invoke
        reply = MagicMock()
        reply.content = (
            '["/v/risks/hotspots.md", '
            '"/v/risks/delegatecall-sites.md", '
            '"/v/risks/reentrancy-candidates.md"]'
        )
        return {"messages": [reply]}


class _FakeEngine:
    """Stand-in for Trailmark QueryEngine with a configurable
    subgraph_names() return value."""

    def __init__(self, subgraphs: set[str]):
        self._subgraphs = subgraphs

    def subgraph_names(self):
        return list(self._subgraphs)


def _patch_build_agent(monkeypatch, fake):
    """Replace src.agent.build_agent at the function-local
    binding inside dispatch_risk_synthesis. Skips _validate_*
    side effects from the real build_agent."""
    monkeypatch.setattr(
        "src.agent.build_agent", lambda *a, **k: fake
    )


def _patch_load_graph_with_preanalysis(monkeypatch):
    """Mock load_graph so the precondition check sees a
    graph with the "tainted" subgraph registered (proving
    run_preanalysis has run). Default for tests that aren't
    exercising the precondition path."""
    monkeypatch.setattr(
        "src.agent.load_graph",
        lambda *a, **k: _FakeEngine({
            "tainted", "entrypoints", "high_blast_radius",
        }),
    )


_VALID_GID = "deadbeef0123"


def test_dispatch_risk_synthesis_invokes_agent_once_with_task(
    monkeypatch, tmp_path,
):
    """RiskSynthesizer is single-invocation: agent.invoke is
    called exactly once with a task message that names the
    risk-synthesizer subagent and the graph_id. This pins
    the scheduling constraint (no pool, no parallel calls)."""
    from src.agent import dispatch_risk_synthesis

    _patch_load_graph_with_preanalysis(monkeypatch)
    fake = _FakeAgent()
    _patch_build_agent(monkeypatch, fake)

    dispatch_risk_synthesis(_VALID_GID, str(tmp_path))

    assert len(fake.calls) == 1
    msg = fake.calls[0]
    assert _VALID_GID in msg
    assert "risk-synthesizer" in msg
    # The task message should instruct the agent to USE the
    # subagent via task tool, not to do the work itself.
    assert "task" in msg.lower() or "subagent" in msg.lower()


def test_dispatch_risk_synthesis_returns_ok_on_success(
    monkeypatch, tmp_path,
):
    """Reply text bubbles up; ok=True when the agent returns
    cleanly. Error field is None."""
    from src.agent import dispatch_risk_synthesis

    _patch_load_graph_with_preanalysis(monkeypatch)
    fake = _FakeAgent()
    _patch_build_agent(monkeypatch, fake)

    result = dispatch_risk_synthesis(_VALID_GID, str(tmp_path))

    assert result["graph_id"] == _VALID_GID
    assert result["ok"] is True
    assert "hotspots.md" in result["reply"]
    assert result["error"] is None


def test_dispatch_risk_synthesis_returns_error_on_raise(
    monkeypatch, tmp_path,
):
    """If agent.invoke raises, dispatcher returns
    {ok: False, error: <str>} — does NOT propagate the
    exception. This is the contract the orchestrator script
    relies on for graceful rc=2 degradation."""
    from src.agent import dispatch_risk_synthesis

    _patch_load_graph_with_preanalysis(monkeypatch)
    fake = _FakeAgent(
        raise_on_invoke=RuntimeError("simulated LLM blowup"),
    )
    _patch_build_agent(monkeypatch, fake)

    result = dispatch_risk_synthesis(_VALID_GID, str(tmp_path))

    assert result["graph_id"] == _VALID_GID
    assert result["ok"] is False
    assert result["reply"] == ""
    assert "simulated LLM blowup" in result["error"]


def test_dispatch_risk_synthesis_validates_graph_id(tmp_path):
    """Bad graph_id raises ValueError BEFORE the agent is
    built — same trust-boundary validation as dispatch_topo
    and dispatch_flows. No try/except around _validate_graph_id;
    callers expect bad input to fail loudly."""
    from src.agent import dispatch_risk_synthesis

    with pytest.raises(ValueError, match="invalid graph_id"):
        dispatch_risk_synthesis("not-hex!", str(tmp_path))


def test_dispatch_risk_synthesis_precondition_fails_without_preanalysis(
    monkeypatch, tmp_path,
):
    """Chunk 4.6 review fix (Finding 3): runtime guard against
    out-of-order callers. If run_preanalysis hasn't been
    called for this graph, RiskSynthesizer would silently
    produce empty/wrong risk notes (empty subgraphs). Refuse
    to invoke; surface a clear precondition error."""
    from src.agent import dispatch_risk_synthesis

    # Engine without "tainted" — preanalysis hasn't run.
    monkeypatch.setattr(
        "src.agent.load_graph",
        lambda *a, **k: _FakeEngine(set()),
    )
    build_called: list[bool] = []
    monkeypatch.setattr(
        "src.agent.build_agent",
        lambda *a, **k: build_called.append(True),
    )

    result = dispatch_risk_synthesis(_VALID_GID, str(tmp_path))

    assert result["ok"] is False
    assert "precondition_failed" in result["error"]
    assert "tainted" in result["error"]
    assert "run_preanalysis" in result["error"]
    # build_agent must NOT have been called — refuse cheaply
    # before any LLM cost.
    assert build_called == []


def test_dispatch_risk_synthesis_per_invoke_timeout_cuts_off_hang(
    monkeypatch, tmp_path,
):
    """Chunk 4.6 review fix (Finding 1): orchestrator-level
    timeout via 1-future ThreadPoolExecutor. If agent.invoke
    hangs (langgraph/deepagents deadlock, no HTTP in flight),
    the wrapper bails after per_invoke_timeout. This pins
    the no-hang contract — without it, the os._exit hatch in
    document_repo.py would never fire."""
    from src.agent import dispatch_risk_synthesis

    _patch_load_graph_with_preanalysis(monkeypatch)
    # Sleep longer than the timeout; dispatcher should bail.
    fake = _FakeAgent(sleep_seconds=5.0)
    _patch_build_agent(monkeypatch, fake)

    t0 = time.monotonic()
    result = dispatch_risk_synthesis(
        _VALID_GID, str(tmp_path), per_invoke_timeout=0.5,
    )
    elapsed = time.monotonic() - t0

    assert result["ok"] is False
    assert "per_invoke_timeout" in result["error"]
    assert "deadlock" in result["error"]
    # Must have cut off well before the 5s sleep finishes.
    # Generous bound: 0.5s timeout + ~0.5s pool teardown.
    assert elapsed < 2.0, (
        f"dispatcher took {elapsed:.2f}s; timeout failed to "
        f"cut off the wedged invoke"
    )


def test_dispatch_risk_synthesis_rejects_nonpositive_timeout(
    monkeypatch, tmp_path,
):
    """per_invoke_timeout=0 (or negative) is meaningless. Fail
    loud rather than passing it down to future.result() where
    behavior is undefined."""
    from src.agent import dispatch_risk_synthesis

    with pytest.raises(ValueError, match="per_invoke_timeout"):
        dispatch_risk_synthesis(
            _VALID_GID, str(tmp_path), per_invoke_timeout=0,
        )
