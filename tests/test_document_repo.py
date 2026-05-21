"""Tests for scripts/document_repo.py.

The full-pipeline harness: trailmark_parse → dispatch_topo →
dispatch_flows → write_root_moc. Tests verify arg parsing,
env validation, call ordering, and exit-code paths.

Loads the script via importlib (scripts/ is not a Python
package). Mocks dispatch_* and write_root_moc so the tests
don't hit a real LLM.
"""

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "document_repo.py"
)


@pytest.fixture
def script_module():
    """Fresh import of document_repo.py per test."""
    spec = importlib.util.spec_from_file_location(
        "document_repo_under_test", str(SCRIPT_PATH)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_phase4_success(script_module, monkeypatch):
    """Patch all Phase 4 entry points (run_slither,
    augment_sarif, run_preanalysis, dispatch_risk_synthesis)
    to no-op success. Existing rc=0 / rc=1 tests rely on
    this so slither doesn't actually shell out during the
    test run (CI may not have the binary; even if it does,
    the test isn't about Phase 4 behavior)."""
    monkeypatch.setattr(
        script_module, "run_slither", lambda *a, **k: None
    )
    monkeypatch.setattr(
        script_module, "augment_sarif", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        script_module, "run_preanalysis", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        script_module,
        "dispatch_risk_synthesis",
        lambda *a, **k: {
            "graph_id": "abc",
            "ok": True,
            "reply": "[]",
            "error": None,
        },
    )


def test_returns_4_when_no_api_key(script_module, monkeypatch):
    """Missing OPENAI_API_KEY → exit 4 (setup error).

    Chunk 4.6 review fix: rc=4 is the setup-error class,
    distinct from rc=2 (Phase 4 advisory degradation).
    Lets CI distinguish 'nothing to ship — fix the setup'
    from 'ship the partial vault, risk notes pending'.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["document_repo.py"])
    assert script_module.main() == 4


def test_returns_4_when_repo_missing(script_module, monkeypatch):
    """Nonexistent --repo path → exit 4 (setup error)."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    monkeypatch.setattr(
        sys, "argv",
        [
            "document_repo.py",
            "--repo", "/does/not/exist",
        ],
    )
    assert script_module.main() == 4


def test_calls_phase4_then_topo_then_flows_then_risk_then_moc(
    script_module, monkeypatch, tmp_path
):
    """Script orchestration order pins the full Phase 4 path:
    run_slither → augment_sarif → run_preanalysis (Phase 4a,
    pre-rendering so chunk 4.7's node-note risks section sees
    annotations) → dispatch_topo → dispatch_flows →
    dispatch_risk_synthesis (Phase 4b, AFTER the dispatch
    waves drain — _ANNOTATE_LOCK contention constraint from
    chunk 4.5) → write_root_moc."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")

    call_order: list[str] = []

    monkeypatch.setattr(
        script_module, "run_slither",
        lambda *a, **k: call_order.append("slither") or None,
    )
    monkeypatch.setattr(
        script_module, "augment_sarif",
        lambda *a, **k: call_order.append("augment") or {},
    )
    monkeypatch.setattr(
        script_module, "run_preanalysis",
        lambda *a, **k: call_order.append("preanalysis") or {},
    )

    def fake_topo(*a, **k):
        call_order.append("topo")
        return {
            "graph_id": "abc",
            "node_count": 1,
            "successes": [
                {"node_id": "n", "agent_reply": "/n.md"}
            ],
            "failures": [],
            "order": ["n"],
        }

    def fake_flows(*a, **k):
        call_order.append("flows")
        return {
            "graph_id": "abc",
            "entrypoint_count": 0,
            "successes": [],
            "failures": [],
            "order": [],
        }

    def fake_risk(*a, **k):
        call_order.append("risk")
        return {
            "graph_id": "abc", "ok": True,
            "reply": "[]", "error": None,
        }

    def fake_moc(*a, **k):
        call_order.append("moc")
        return []  # script expects a list (len() is called on it)

    monkeypatch.setattr(script_module, "dispatch_topo", fake_topo)
    monkeypatch.setattr(script_module, "dispatch_flows", fake_flows)
    monkeypatch.setattr(
        script_module, "dispatch_risk_synthesis", fake_risk
    )
    monkeypatch.setattr(script_module, "write_root_moc", fake_moc)

    monkeypatch.setattr(
        sys, "argv",
        [
            "document_repo.py",
            "--repo", "tests/fixtures/tier1_uniswap_v2",
            "--vault", str(tmp_path),
        ],
    )

    rc = script_module.main()
    assert rc == 0
    assert call_order == [
        "slither", "augment", "preanalysis",
        "topo", "flows", "risk", "moc",
    ], f"order mismatch; got {call_order}"


def test_returns_1_when_dispatch_topo_has_failures(
    script_module, monkeypatch, tmp_path
):
    """Exit code 1 distinguishes 'partial success' (some
    nodes failed) from setup error (2) or full success
    (0)."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_phase4_success(script_module, monkeypatch)

    monkeypatch.setattr(
        script_module,
        "dispatch_topo",
        lambda *a, **k: {
            "graph_id": "abc",
            "node_count": 2,
            "successes": [
                {"node_id": "n1", "agent_reply": "/n1.md"}
            ],
            "failures": [
                {"node_id": "n2", "error": "TimeoutError: ..."}
            ],
            "order": ["n1", "n2"],
        },
    )
    monkeypatch.setattr(
        script_module,
        "dispatch_flows",
        lambda *a, **k: {
            "graph_id": "abc",
            "entrypoint_count": 0,
            "successes": [],
            "failures": [],
            "order": [],
        },
    )
    monkeypatch.setattr(
        script_module, "write_root_moc", lambda *a, **k: []
    )
    monkeypatch.setattr(
        sys, "argv",
        [
            "document_repo.py",
            "--repo", "tests/fixtures/tier1_uniswap_v2",
            "--vault", str(tmp_path),
        ],
    )

    assert script_module.main() == 1


def test_returns_1_when_dispatch_flows_has_failures(
    script_module, monkeypatch, tmp_path
):
    """Flow dispatch failures must also propagate to exit
    code 1 — silent flow failures would leave CI green while
    shipping vaults missing FlowTracer notes."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_phase4_success(script_module, monkeypatch)

    # All node dispatches succeed; only flow dispatch fails.
    monkeypatch.setattr(
        script_module,
        "dispatch_topo",
        lambda *a, **k: {
            "graph_id": "abc",
            "node_count": 1,
            "successes": [
                {"node_id": "n1", "agent_reply": "/n1.md"}
            ],
            "failures": [],
            "order": ["n1"],
        },
    )
    monkeypatch.setattr(
        script_module,
        "dispatch_flows",
        lambda *a, **k: {
            "graph_id": "abc",
            "entrypoint_count": 2,
            "successes": [
                {"node_id": "e1", "agent_reply": "/e1.md"}
            ],
            "failures": [
                {"node_id": "e2", "error": "TimeoutError: ..."}
            ],
            "order": ["e1", "e2"],
        },
    )
    monkeypatch.setattr(
        script_module, "write_root_moc", lambda *a, **k: []
    )
    monkeypatch.setattr(
        sys, "argv",
        [
            "document_repo.py",
            "--repo", "tests/fixtures/tier1_uniswap_v2",
            "--vault", str(tmp_path),
        ],
    )

    assert script_module.main() == 1, (
        "flow dispatch failures must produce exit 1 — silent "
        "flow failures would leave CI green while shipping "
        "incomplete vaults"
    )


# ---------------------------------------------------------------
# Phase 4 wiring tests (chunk 4.6)
# ---------------------------------------------------------------

def _stub_topo_flows_moc(script_module, monkeypatch):
    """Patch dispatch_topo, dispatch_flows, write_root_moc to
    no-op success. Used by Phase 4 tests that want to focus
    on phase-4 behavior without re-stubbing every dispatcher."""
    monkeypatch.setattr(
        script_module, "dispatch_topo",
        lambda *a, **k: {
            "graph_id": "abc", "node_count": 0,
            "successes": [], "failures": [], "order": [],
        },
    )
    monkeypatch.setattr(
        script_module, "dispatch_flows",
        lambda *a, **k: {
            "graph_id": "abc", "entrypoint_count": 0,
            "successes": [], "failures": [], "order": [],
        },
    )
    monkeypatch.setattr(
        script_module, "write_root_moc", lambda *a, **k: []
    )


def test_phase4_failure_skips_risk_synthesis_but_continues(
    script_module, monkeypatch, tmp_path,
):
    """run_slither raises (e.g., binary missing) →
    dispatch_risk_synthesis NEVER called, dispatch_topo +
    dispatch_flows + write_root_moc still execute, rc=2
    (advisory degradation)."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)

    def slither_blows_up(*a, **k):
        raise RuntimeError("slither binary not found")

    monkeypatch.setattr(script_module, "run_slither", slither_blows_up)

    risk_called: list[bool] = []
    monkeypatch.setattr(
        script_module, "dispatch_risk_synthesis",
        lambda *a, **k: risk_called.append(True) or {
            "graph_id": "abc", "ok": True,
            "reply": "[]", "error": None,
        },
    )
    monkeypatch.setattr(
        sys, "argv",
        [
            "document_repo.py",
            "--repo", "tests/fixtures/tier1_uniswap_v2",
            "--vault", str(tmp_path),
        ],
    )

    rc = script_module.main()
    assert rc == 2, (
        f"Phase 4 failure should yield rc=2 (advisory); got {rc}"
    )
    assert risk_called == [], (
        "dispatch_risk_synthesis must NOT run when Phase 4a "
        "analyzers failed (RiskSynthesizer depends on findings + "
        "preanalysis subgraphs that never got attached)"
    )


def test_phase4_creates_audit_dir(
    script_module, monkeypatch, tmp_path,
):
    """vault/.audit/ is created before run_slither writes its
    SARIF output (chunk 4.6 wiring: parent.mkdir(parents=True,
    exist_ok=True) before the run_slither call)."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)
    _stub_phase4_success(script_module, monkeypatch)

    monkeypatch.setattr(
        sys, "argv",
        [
            "document_repo.py",
            "--repo", "tests/fixtures/tier1_uniswap_v2",
            "--vault", str(tmp_path),
        ],
    )

    script_module.main()

    # ensure_vault().resolve() is the vault root; .audit/
    # sits inside it. tmp_path is the vault arg, but
    # ensure_vault may resolve symlinks — use the resolved path.
    audit_dir = tmp_path.resolve() / ".audit"
    assert audit_dir.is_dir(), (
        f"Phase 4a must create {audit_dir} before run_slither "
        f"(SARIF parent dir). Listing: "
        f"{list(tmp_path.iterdir()) if tmp_path.exists() else 'no vault'}"
    )


def test_phase4_rc2_when_risk_synthesis_fails(
    script_module, monkeypatch, tmp_path,
):
    """Analyzers succeed but dispatch_risk_synthesis returns
    ok=False → rc=2 (advisory). Distinguishes risk-synth
    failure from hard topo/flow failures (rc=1)."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)

    monkeypatch.setattr(
        script_module, "run_slither", lambda *a, **k: None
    )
    monkeypatch.setattr(
        script_module, "augment_sarif", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        script_module, "run_preanalysis", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        script_module, "dispatch_risk_synthesis",
        lambda *a, **k: {
            "graph_id": "abc", "ok": False,
            "reply": "", "error": "LLM timeout",
        },
    )

    monkeypatch.setattr(
        sys, "argv",
        [
            "document_repo.py",
            "--repo", "tests/fixtures/tier1_uniswap_v2",
            "--vault", str(tmp_path),
        ],
    )

    assert script_module.main() == 2


def test_phase4_rc2_when_risk_synth_claims_missing_files(
    script_module, monkeypatch, tmp_path,
):
    """Cross-cutting review fix (I8): dispatch_risk_synthesis
    returns ok=True with a JSON list of claimed paths. If any
    claimed path doesn't exist on disk, the script must
    treat this as rc=2 advisory degradation rather than
    silently shipping an incomplete vault. Failure mode: LLM
    returns ok-looking output without actually calling
    render_and_write_risk_note."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)

    monkeypatch.setattr(
        script_module, "run_slither", lambda *a, **k: None
    )
    monkeypatch.setattr(
        script_module, "augment_sarif", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        script_module, "run_preanalysis", lambda *a, **k: {}
    )
    # LLM claims it wrote /tmp/nonexistent.md — file doesn't exist.
    monkeypatch.setattr(
        script_module, "dispatch_risk_synthesis",
        lambda *a, **k: {
            "graph_id": "abc", "ok": True,
            "reply": '["/tmp/definitely-does-not-exist-' \
                     'washable-test.md"]',
            "error": None,
        },
    )

    monkeypatch.setattr(
        sys, "argv",
        [
            "document_repo.py",
            "--repo", "tests/fixtures/tier1_uniswap_v2",
            "--vault", str(tmp_path),
        ],
    )

    assert script_module.main() == 2, (
        "LLM claiming nonexistent paths must yield rc=2 "
        "(advisory), not silently pass as rc=0"
    )


def test_phase4_rc2_when_risk_synth_reply_unparseable(
    script_module, monkeypatch, tmp_path,
):
    """Cross-cutting review fix (I8): malformed reply (not
    JSON, wrong shape) also degrades to rc=2. The vault may
    still have risk notes — but the auditing layer can't
    confirm what the LLM claims to have done."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)
    monkeypatch.setattr(
        script_module, "run_slither", lambda *a, **k: None
    )
    monkeypatch.setattr(
        script_module, "augment_sarif", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        script_module, "run_preanalysis", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        script_module, "dispatch_risk_synthesis",
        lambda *a, **k: {
            "graph_id": "abc", "ok": True,
            "reply": "I wrote some risk notes (no JSON)",
            "error": None,
        },
    )
    monkeypatch.setattr(
        sys, "argv",
        [
            "document_repo.py",
            "--repo", "tests/fixtures/tier1_uniswap_v2",
            "--vault", str(tmp_path),
        ],
    )

    assert script_module.main() == 2


def test_phase4_rc1_overrides_rc2_when_topo_fails(
    script_module, monkeypatch, tmp_path,
):
    """Phase 4 degraded AND topo failures → rc=1 (hard
    failure dominates over advisory). Bug-bait: if the rc
    branching order flips, an analyzer warning could mask a
    real topo failure."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")

    # Hard topo failure.
    monkeypatch.setattr(
        script_module, "dispatch_topo",
        lambda *a, **k: {
            "graph_id": "abc", "node_count": 1,
            "successes": [],
            "failures": [
                {"node_id": "n1", "error": "TimeoutError"},
            ],
            "order": ["n1"],
        },
    )
    monkeypatch.setattr(
        script_module, "dispatch_flows",
        lambda *a, **k: {
            "graph_id": "abc", "entrypoint_count": 0,
            "successes": [], "failures": [], "order": [],
        },
    )
    monkeypatch.setattr(
        script_module, "write_root_moc", lambda *a, **k: []
    )

    # Phase 4a fails too.
    def slither_blows_up(*a, **k):
        raise RuntimeError("slither binary not found")

    monkeypatch.setattr(script_module, "run_slither", slither_blows_up)
    monkeypatch.setattr(
        script_module, "dispatch_risk_synthesis",
        lambda *a, **k: {
            "graph_id": "abc", "ok": True,
            "reply": "[]", "error": None,
        },
    )

    monkeypatch.setattr(
        sys, "argv",
        [
            "document_repo.py",
            "--repo", "tests/fixtures/tier1_uniswap_v2",
            "--vault", str(tmp_path),
        ],
    )

    assert script_module.main() == 1, (
        "hard topo failure must override Phase 4 advisory rc=2"
    )
