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
from pathlib import Path
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


class _FakeGraph:
    """Stand-in for Trailmark's internal graph store. Matches
    the `engine._store._graph.annotations` access path used by
    `clear_annotations_by_source`. Empty dict means the
    helper removes nothing and skips save_graph."""

    def __init__(self):
        self.annotations: dict = {}


class _FakeStore:
    """Stand-in for engine._store. Wraps a _FakeGraph."""

    def __init__(self):
        self._graph = _FakeGraph()


class _FakeEngine:
    """Stand-in for Trailmark QueryEngine. Carries a
    configurable subgraph_names() (for the precondition
    check) and a _store._graph.annotations dict (for the
    clear_annotations_by_source path)."""

    def __init__(self, subgraphs: set[str]):
        self._subgraphs = subgraphs
        self._store = _FakeStore()

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
    exercising the precondition path.

    Patches BOTH `src.agent.load_graph` (used by the
    precondition check) and `src.tools.load_graph` (used by
    `clear_annotations_by_source`, called next in the
    dispatcher for re-run idempotency). They're separate
    module-level bindings; patching only one bypasses the
    cache for half the dispatcher."""
    def _fake_engine(*a, **k):
        return _FakeEngine({
            "tainted", "entrypoints", "high_blast_radius",
        })
    monkeypatch.setattr("src.agent.load_graph", _fake_engine)
    monkeypatch.setattr("src.tools.load_graph", _fake_engine)


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
    """ok=True when the agent returns cleanly. Error field
    is None.

    Codex round-9 fix: the reply field now contains the
    JSON list of ACTUALLY PROMOTED files (from staging →
    vault/risks/), NOT the LLM's reported reply. The bare
    `_FakeAgent` doesn't write any files to its bound
    vault, so promotion yields an empty list and reply is
    `"[]"`. Tests that exercise the promotion path use
    `_WritingFakeAgent` (see below)."""
    from src.agent import dispatch_risk_synthesis

    _patch_load_graph_with_preanalysis(monkeypatch)
    fake = _FakeAgent()
    _patch_build_agent(monkeypatch, fake)

    result = dispatch_risk_synthesis(_VALID_GID, str(tmp_path))

    assert result["graph_id"] == _VALID_GID
    assert result["ok"] is True
    # No files were written by the fake agent, so nothing
    # promoted to vault/risks/, so reply is an empty list.
    assert result["reply"] == "[]"
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


class _WritingFakeAgent:
    """Stand-in agent that actually writes files to its
    bound vault on invoke. Used to exercise the staging →
    vault/risks/ promotion path."""

    def __init__(self, vault_path, file_names: list[str]):
        self.vault_path = Path(vault_path)
        self.file_names = file_names
        self.calls: list[str] = []

    def invoke(self, inputs):
        import json
        risks_dir = self.vault_path / "risks"
        risks_dir.mkdir(parents=True, exist_ok=True)
        for name in self.file_names:
            (risks_dir / name).write_text(f"content for {name}")
        msg = inputs["messages"][0]["content"]
        self.calls.append(msg)
        reply = MagicMock()
        # LLM reports the staging paths it wrote to. The
        # dispatcher OVERRIDES this with actual promoted
        # paths regardless of what the LLM says.
        reply.content = json.dumps([
            str(risks_dir / name) for name in self.file_names
        ])
        return {"messages": [reply]}


def test_dispatch_risk_synthesis_binds_agent_to_staging_not_vault(
    monkeypatch, tmp_path,
):
    """Codex round-9 fix: dispatcher binds the agent to
    `<vault>/.audit/risk-staging/<run_id>/` instead of the
    real vault. Late-running workers (post-timeout) can
    only write to staging, never to vault/risks/.

    Pin the binding: build_agent receives the staging
    path, not the vault path."""
    from src.agent import dispatch_risk_synthesis

    _patch_load_graph_with_preanalysis(monkeypatch)
    captured = {}

    def capture_build_agent(graph_id, vault_path, **kwargs):
        captured["vault_path"] = str(vault_path)
        return _FakeAgent()

    monkeypatch.setattr(
        "src.agent.build_agent", capture_build_agent
    )

    vault = tmp_path / "vault"
    vault.mkdir()
    dispatch_risk_synthesis(_VALID_GID, str(vault))

    # Agent received staging path, NOT the vault itself.
    assert ".audit/risk-staging/" in captured["vault_path"]
    assert str(vault.resolve()) in captured["vault_path"]
    # The vault path itself was NOT used as the agent's
    # vault (staging is a SUBdir of vault).
    assert captured["vault_path"] != str(vault.resolve())


def test_dispatch_risk_synthesis_leaves_files_in_staging(
    monkeypatch, tmp_path,
):
    """Codex round-10 fix: dispatcher no longer promotes
    staging → vault/risks/. Files written by the agent
    REMAIN in staging; the caller is responsible for
    verification + promotion. Reply contains staging paths
    + result includes `staging_root` for the caller to
    inspect and (after promotion or rejection) clean up."""
    import json
    from src.agent import dispatch_risk_synthesis

    _patch_load_graph_with_preanalysis(monkeypatch)

    expected_names = [
        "hotspots.md",
        "delegatecall-sites.md",
        "reentrancy-candidates.md",
    ]

    def make_writing_agent(graph_id, vault_path, **kwargs):
        return _WritingFakeAgent(vault_path, expected_names)

    monkeypatch.setattr(
        "src.agent.build_agent", make_writing_agent
    )

    vault = tmp_path / "vault"
    vault.mkdir()
    result = dispatch_risk_synthesis(_VALID_GID, str(vault))

    assert result["ok"] is True
    # Files are in STAGING, NOT in vault/risks/.
    vault_risks = vault / "risks"
    assert not vault_risks.exists() or not list(vault_risks.glob("*.md")), (
        "dispatcher must NOT promote to vault/risks/ "
        "(caller's responsibility)"
    )
    # Staging metadata returned for the caller.
    staging_root = Path(result["staging_root"])
    assert staging_root.is_dir()
    staging_risks = staging_root / "risks"
    for name in expected_names:
        assert (staging_risks / name).is_file(), (
            f"expected staged {name} in {staging_risks}"
        )
    # Reply is JSON list of STAGING paths.
    staged_paths = json.loads(result["reply"])
    assert len(staged_paths) == 3
    for p in staged_paths:
        resolved = Path(p).resolve()
        assert resolved.is_relative_to(staging_risks.resolve())


def test_dispatch_risk_synthesis_isolates_late_writes_on_failure(
    monkeypatch, tmp_path,
):
    """Codex round-9 fix: on agent.invoke failure, NOTHING
    is promoted. The vault stays clean. A late-running
    worker (simulated here by post-dispatch writes via the
    captured staging path) can only reach staging, never
    vault/risks/."""
    from src.agent import dispatch_risk_synthesis

    _patch_load_graph_with_preanalysis(monkeypatch)
    captured_staging = {}

    def capture_then_fail(graph_id, vault_path, **kwargs):
        captured_staging["path"] = Path(vault_path)
        return _FakeAgent(
            raise_on_invoke=RuntimeError("simulated"),
        )

    monkeypatch.setattr(
        "src.agent.build_agent", capture_then_fail
    )

    vault = tmp_path / "vault"
    vault.mkdir()
    result = dispatch_risk_synthesis(_VALID_GID, str(vault))

    assert result["ok"] is False
    # vault/risks/ is empty (no promotion on failure).
    vault_risks = vault / "risks"
    if vault_risks.exists():
        assert list(vault_risks.glob("*.md")) == [], (
            "late write reached vault/risks/ on failure — "
            "staging isolation broken"
        )
    # Simulate a late worker writing AFTER dispatch returned.
    # In reality, the bound closure writes to staging via
    # render_and_write_risk_note. Here we simulate by
    # recreating staging and writing directly. The vault
    # remains clean.
    staging_risks = captured_staging["path"] / "risks"
    staging_risks.mkdir(parents=True, exist_ok=True)
    (staging_risks / "hotspots.md").write_text(
        "late attacker-controlled content"
    )
    # vault/risks/ STILL empty after the late write.
    if vault_risks.exists():
        assert list(vault_risks.glob("*.md")) == [], (
            "late write reached vault/risks/ — staging "
            "isolation broken"
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
