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


def _stub_phase4a_clean(script_module, monkeypatch):
    """Stub Phase 4a entry points (run_slither, augment_sarif,
    run_preanalysis) AND complexity_hotspots to no-op-success
    values. Used by tests focused on Phase 4b verification
    logic — they need analyzer side effects mocked AND need
    the F2 'should-have-notes' gate to evaluate to False by
    default (so empty risk-note replies are genuinely OK)."""
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
        script_module, "complexity_hotspots",
        lambda *a, **k: [],
    )


def _make_fake_dispatch_with_staging(
    vault: Path,
    write_names: list[str],
    ok: bool = True,
    *,
    claim_names: list[str] | None = None,
    error: str | None = None,
):
    """Build a fake dispatch_risk_synthesis callable that
    simulates the round-10 staging contract:
    - Creates a per-call staging dir under
      `<vault>/.audit/risk-staging/<run_id>/risks/`.
    - Writes the `write_names` files into staging.
    - Returns the result dict with `staging_root` + reply
      listing the `claim_names` files (defaults to
      `write_names` — honest LLM).

    Pass `claim_names=[]` to simulate a lying LLM that
    writes files but claims none. Pass any subset/superset
    of write_names to simulate other lie patterns. The
    script's verifier should catch these via the
    inventory_mismatch check.
    """
    import json as _json
    import secrets as _secrets

    def _fake_dispatch(*a, **k):
        run_id = _secrets.token_hex(8)
        staging_root = (
            vault / ".audit" / "risk-staging" / run_id
        )
        staging_risks = staging_root / "risks"
        staging_risks.mkdir(parents=True, exist_ok=True)
        for name in write_names:
            (staging_risks / name).write_text(
                f"content for {name}"
            )
        reported_names = (
            claim_names if claim_names is not None else write_names
        )
        reported_paths = [
            str(staging_risks / n) for n in reported_names
        ]
        return {
            "graph_id": "abc",
            "ok": ok,
            "reply": _json.dumps(reported_paths) if ok else "",
            "error": error,
            "staging_root": str(staging_root),
        }

    return _fake_dispatch


def _stub_phase4_success(script_module, monkeypatch):
    """Patch all Phase 4 entry points (run_slither,
    augment_sarif, run_preanalysis, dispatch_risk_synthesis,
    complexity_hotspots) to no-op success. Existing rc=0 /
    rc=1 tests rely on this so slither doesn't actually shell
    out during the test run (CI may not have the binary;
    even if it does, the test isn't about Phase 4 behavior).

    complexity_hotspots defaults to [] (no hotspots) so an
    empty risk-note reply is genuinely OK. Tests exercising
    the "hotspots-present requires notes" path should
    override this stub explicitly.

    Codex round-10 contract: dispatcher returns
    `staging_root` even when no files were written. The
    script's cleanup expects to find it (may rmtree)."""
    _stub_phase4a_clean(script_module, monkeypatch)
    monkeypatch.setattr(
        script_module,
        "dispatch_risk_synthesis",
        lambda *a, **k: {
            "graph_id": "abc",
            "ok": True,
            "reply": "[]",
            "error": None,
            "staging_root": None,
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
    # complexity_hotspots stubbed to [] so the F2 gate's
    # "should have notes?" check evaluates to False (no
    # findings + no hotspots → empty reply is OK).
    monkeypatch.setattr(
        script_module, "complexity_hotspots",
        lambda *a, **k: [],
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

    _stub_phase4a_clean(script_module, monkeypatch)
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

    _stub_phase4a_clean(script_module, monkeypatch)
    # LLM claims it wrote /tmp/nonexistent.md — file doesn't exist.
    monkeypatch.setattr(
        script_module, "dispatch_risk_synthesis",
        lambda *a, **k: {
            "graph_id": "abc", "ok": True,
            "reply": '["/tmp/definitely-does-not-exist-' \
                     'meridian-test.md"]',
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


def test_phase4_rc2_when_risk_synth_claims_outside_vault_path(
    script_module, monkeypatch, tmp_path,
):
    """Codex review fix (F6): the LLM can claim ANY existing
    file path and the bare Path(p).exists() check passes
    silently with no vault risk notes. Tighter contract:
    paths MUST resolve under `vault/risks/`, end in `.md`,
    AND exist. Pin via a reply that names `/etc/hosts`
    (exists on every macOS/Linux) — must yield rc=2."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)
    _stub_phase4a_clean(script_module, monkeypatch)
    # /etc/hosts exists on macOS/Linux. Bare exists() would pass.
    # The tightened check must reject it (not under vault/risks/).
    monkeypatch.setattr(
        script_module, "dispatch_risk_synthesis",
        lambda *a, **k: {
            "graph_id": "abc", "ok": True,
            "reply": '["/etc/hosts"]',
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
        "LLM claiming /etc/hosts must yield rc=2 (advisory). "
        "Without vault/risks/ containment check, /etc/hosts "
        "would pass bare Path.exists() and rc=0 would ship "
        "an empty risks/ folder as 'success'."
    )


def test_phase4_deletes_stale_risk_notes_before_dispatch(
    script_module, monkeypatch, tmp_path,
):
    """Codex follow-up review fix (F2): a prompt-injected LLM
    could claim a pre-existing `hotspots.md` from a prior run
    and pass the path-exists check. Defense: delete stale
    risk notes BEFORE dispatch_risk_synthesis. Then ANY path
    the LLM claims must be a NEW file written this run."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)
    _stub_phase4a_clean(script_module, monkeypatch)

    # Pre-create a stale risk note that would otherwise pass
    # the allowlist+suffix check.
    risks_dir = tmp_path / "risks"
    risks_dir.mkdir(parents=True)
    stale = risks_dir / "hotspots.md"
    stale.write_text("STALE content from previous run")

    observed_stale_exists_at_dispatch: list[bool] = []

    def fake_dispatch(*a, **k):
        # Check stale file presence at the time of dispatch.
        observed_stale_exists_at_dispatch.append(stale.exists())
        # Claim the (now-deleted) file as written.
        return {
            "graph_id": "abc", "ok": True,
            "reply": f'["{stale}"]',
            "error": None,
        }

    monkeypatch.setattr(
        script_module, "dispatch_risk_synthesis", fake_dispatch,
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
    # Confirm the stale file was deleted BEFORE dispatch.
    assert observed_stale_exists_at_dispatch == [False], (
        f"stale risk note still existed at dispatch time: "
        f"{observed_stale_exists_at_dispatch}"
    )
    # Since the LLM claims a path that doesn't exist post-
    # delete, verification fails. rc=2 (advisory).
    assert rc == 2


def test_phase4_rc2_when_risk_synth_claims_non_allowlist_name(
    script_module, monkeypatch, tmp_path,
):
    """Codex follow-up review fix (F2): even if a claimed
    path is under vault/risks/ AND ends in .md AND exists,
    the FILENAME must be in the canonical RiskSynthesizer
    output set {hotspots, delegatecall-sites,
    reentrancy-candidates}. A prompt-injected LLM could
    otherwise write `vault/risks/innocuous.md` and pass
    every other gate."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)
    _stub_phase4a_clean(script_module, monkeypatch)

    # Pre-create the non-allowlisted file so it would pass
    # the existence check.
    risks_dir = tmp_path / "risks"
    risks_dir.mkdir(parents=True)
    bogus = risks_dir / "innocuous-name.md"
    bogus.write_text("looks real but wrong name")

    monkeypatch.setattr(
        script_module, "dispatch_risk_synthesis",
        lambda *a, **k: {
            "graph_id": "abc", "ok": True,
            "reply": f'["{bogus}"]',
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

    # Need to re-create AFTER the pre-dispatch delete fires.
    # The pre-delete glob targets *.md so our bogus would
    # also be wiped — recreate it via the fake dispatch hook.
    def fake_dispatch_recreating(*a, **k):
        risks_dir.mkdir(parents=True, exist_ok=True)
        bogus.write_text("looks real but wrong name")
        return {
            "graph_id": "abc", "ok": True,
            "reply": f'["{bogus}"]',
            "error": None,
        }
    monkeypatch.setattr(
        script_module, "dispatch_risk_synthesis",
        fake_dispatch_recreating,
    )

    assert script_module.main() == 2, (
        "non-allowlisted filename must fail the F2 gate"
    )


def test_phase4_rc2_when_hotspots_present_but_empty_reply(
    script_module, monkeypatch, tmp_path,
):
    """Codex follow-up review fix (F2): even if SARIF matched
    no findings, complexity_hotspots may still produce
    nodes worth synthesizing into hotspots.md. An empty
    risk-note reply when hotspots exist must yield rc=2."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)
    monkeypatch.setattr(
        script_module, "run_slither", lambda *a, **k: None
    )
    # Zero SARIF findings.
    monkeypatch.setattr(
        script_module, "augment_sarif",
        lambda *a, **k: {
            "matched_findings": 0,
            "unmatched_findings": 0,
            "subgraphs_created": [],
        },
    )
    monkeypatch.setattr(
        script_module, "run_preanalysis", lambda *a, **k: {}
    )
    # But hotspots ARE present — represents a Tier-0-style
    # codebase where slither didn't fire any rules but there
    # are high-CC methods worth flagging.
    monkeypatch.setattr(
        script_module, "complexity_hotspots",
        lambda *a, **k: [
            {"id": "src:foo.bar", "cyclomatic_complexity": 12},
        ],
    )
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

    assert script_module.main() == 2, (
        "hotspots present + empty reply must yield rc=2 "
        "(previously this slipped through because the gate "
        "only checked matched_findings)"
    )


def test_phase4_rc2_when_risk_synth_returns_partial_subset(
    script_module, monkeypatch, tmp_path,
):
    """Codex round-3 fix (F2): previously a non-empty subset
    of allowlisted names passed (e.g., LLM claims only
    `hotspots.md`, omits the other two). That's a
    misleading-success path: the auditor assumes the
    delegatecall and reentrancy categories were checked,
    but they were silently skipped. Tighten: when
    should_have_notes is True, the claimed names must
    EQUAL the full expected set."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)
    monkeypatch.setattr(
        script_module, "run_slither", lambda *a, **k: None
    )
    # findings present → should_have_notes=True → all three required.
    monkeypatch.setattr(
        script_module, "augment_sarif",
        lambda *a, **k: {
            "matched_findings": 42,
            "unmatched_findings": 0,
            "subgraphs_created": ["sarif:Slither"],
        },
    )
    monkeypatch.setattr(
        script_module, "run_preanalysis", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        script_module, "complexity_hotspots",
        lambda *a, **k: [],
    )

    # Create only one of three risk notes — the LLM's reply
    # claims it wrote that one but silently skipped the
    # other two.
    risks_dir = tmp_path / "risks"

    def fake_dispatch_partial(*a, **k):
        risks_dir.mkdir(parents=True, exist_ok=True)
        # Re-create after pre-dispatch wipe.
        (risks_dir / "hotspots.md").write_text("partial reply")
        return {
            "graph_id": "abc", "ok": True,
            "reply": f'["{risks_dir / "hotspots.md"}"]',
            "error": None,
        }

    monkeypatch.setattr(
        script_module, "dispatch_risk_synthesis",
        fake_dispatch_partial,
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
        "partial reply (1 of 3 expected notes) must yield "
        "rc=2 (advisory). The auditor needs to see all "
        "three categories were checked, even if some are "
        "empty."
    )


def test_phase4_rc0_when_all_three_risk_notes_written(
    script_module, monkeypatch, tmp_path,
):
    """Counterpart to F2: the SUCCESS path requires all
    three notes when should_have_notes is True. Pin the
    rc=0 path."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)
    monkeypatch.setattr(
        script_module, "run_slither", lambda *a, **k: None
    )
    monkeypatch.setattr(
        script_module, "augment_sarif",
        lambda *a, **k: {
            "matched_findings": 42,
            "unmatched_findings": 0,
            "subgraphs_created": ["sarif:Slither"],
        },
    )
    monkeypatch.setattr(
        script_module, "run_preanalysis", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        script_module, "complexity_hotspots",
        lambda *a, **k: [],
    )

    # Round-10 contract: fake writes to staging, script
    # verifies + promotes on its own.
    monkeypatch.setattr(
        script_module, "dispatch_risk_synthesis",
        _make_fake_dispatch_with_staging(
            tmp_path,
            [
                "hotspots.md",
                "delegatecall-sites.md",
                "reentrancy-candidates.md",
            ],
        ),
    )
    monkeypatch.setattr(
        sys, "argv",
        [
            "document_repo.py",
            "--repo", "tests/fixtures/tier1_uniswap_v2",
            "--vault", str(tmp_path),
        ],
    )

    assert script_module.main() == 0, (
        "all three risk notes present + valid paths must yield rc=0"
    )


def test_phase4_rc2_when_findings_attached_but_empty_risk_note_list(
    script_module, monkeypatch, tmp_path,
):
    """Codex follow-up review fix (F3): if augment_sarif
    attached findings to the graph, RiskSynthesizer MUST
    produce at least one risk note. An empty reply ([]) when
    matched_findings >= 1 is a misleading-success path —
    the LLM was prompt-injected or ignored its mandate.
    Must yield rc=2."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)
    monkeypatch.setattr(
        script_module, "run_slither", lambda *a, **k: None
    )
    # augment_sarif reports matched findings — the LLM
    # SHOULD have produced curated risk notes.
    monkeypatch.setattr(
        script_module, "augment_sarif",
        lambda *a, **k: {
            "matched_findings": 42,
            "unmatched_findings": 0,
            "subgraphs_created": ["sarif:Slither"],
        },
    )
    monkeypatch.setattr(
        script_module, "run_preanalysis", lambda *a, **k: {}
    )
    # But RiskSynthesizer returns an empty list — claims
    # it wrote zero risk notes despite 42 findings.
    monkeypatch.setattr(
        script_module, "dispatch_risk_synthesis",
        lambda *a, **k: {
            "graph_id": "abc", "ok": True,
            "reply": "[]",
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
        "empty risk-note list with findings attached must "
        "yield rc=2 (advisory). Without this check, a prompt-"
        "injected LLM could mask all findings as 'OK (0 risk "
        "note(s) written)' with rc=0."
    )


def test_phase4_clears_stale_risk_notes_even_when_analyzers_fail(
    script_module, monkeypatch, tmp_path,
):
    """Codex round-7 fix (F1): the stale-risk-note delete
    previously lived INSIDE the `if phase4_ok:` block, so
    an analyzer failure (slither missing, augment crash)
    would skip the wipe. write_root_moc then indexes stale
    `risks/*.md` from a prior run into the partial vault.
    Move the wipe to run UNCONDITIONALLY, right after
    ensure_vault."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)

    # Pre-create a stale risk note (simulates a prior run's
    # output left in the vault).
    risks_dir = tmp_path / "risks"
    risks_dir.mkdir(parents=True)
    stale = risks_dir / "hotspots.md"
    stale.write_text("STALE content from a previous run")

    # Analyzers fail — phase4_ok=False, Phase 4b skipped.
    def slither_blows_up(*a, **k):
        raise RuntimeError("slither binary not found")

    monkeypatch.setattr(
        script_module, "run_slither", slither_blows_up
    )
    # complexity_hotspots stubbed for completeness; won't
    # be reached because Phase 4b is skipped.
    monkeypatch.setattr(
        script_module, "complexity_hotspots",
        lambda *a, **k: [],
    )
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

    rc = script_module.main()
    # rc=2 because Phase 4a degraded (no surprise).
    assert rc == 2
    # The stale file must be GONE — wipe ran even though
    # Phase 4b was skipped.
    assert not stale.exists(), (
        f"stale risk note survived analyzer failure: "
        f"{stale} still has content "
        f"{stale.read_text() if stale.exists() else 'N/A'}"
    )


def test_phase4_rc2_when_llm_writes_notes_but_returns_empty_reply(
    script_module, monkeypatch, tmp_path,
):
    """Codex round-5 fix: previously the verifier checked
    only the LLM's claimed JSON reply, never the actual
    filesystem state of vault/risks/. A prompt-injected
    RiskSynthesizer could write all three notes to disk
    (with attacker content), return `[]`, and the verifier
    would accept rc=0 because claimed_names=set() matches
    the clean-graph expectation. But the files remain on
    disk and write_root_moc surfaces them.

    Pin the contract: actual filesystem inventory MUST
    match claimed names."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)
    monkeypatch.setattr(
        script_module, "run_slither", lambda *a, **k: None
    )
    monkeypatch.setattr(
        script_module, "augment_sarif",
        lambda *a, **k: {
            "matched_findings": 0,
            "unmatched_findings": 0,
            "subgraphs_created": [],
        },
    )
    monkeypatch.setattr(
        script_module, "run_preanalysis", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        script_module, "complexity_hotspots",
        lambda *a, **k: [],
    )

    # Round-10 contract: LLM writes 3 to STAGING + lies in
    # reply (claims 0). Verifier catches via
    # inventory_mismatch on staging. Files never reach
    # vault/risks/ (no promotion on failure).
    monkeypatch.setattr(
        script_module, "dispatch_risk_synthesis",
        _make_fake_dispatch_with_staging(
            tmp_path,
            write_names=[
                "hotspots.md",
                "delegatecall-sites.md",
                "reentrancy-candidates.md",
            ],
            claim_names=[],  # lying LLM
        ),
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
        "LLM writing notes to disk while claiming `[]` must "
        "yield rc=2. The auditor needs visibility into "
        "stealth-written attacker content."
    )


def test_phase4_suspect_notes_never_reach_vault(
    script_module, monkeypatch, tmp_path,
):
    """Codex round-10 update: with the staging architecture,
    suspect LLM writes never reach vault/risks/ in the
    first place — the verifier runs against staging, and
    promotion only happens after ALL gates pass. Previous
    round-8 fix (post-failure vault wipe) is now a
    defense-in-depth backup; the primary defense is
    staging + don't-promote-on-fail.

    Pin the new contract: clean-graph LLM writes to staging
    + lies in reply → inventory mismatch caught → no
    promotion → vault/risks/ stays empty. Suspect content
    never reaches the final vault."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)
    monkeypatch.setattr(
        script_module, "run_slither", lambda *a, **k: None
    )
    monkeypatch.setattr(
        script_module, "augment_sarif",
        lambda *a, **k: {
            "matched_findings": 0,
            "unmatched_findings": 0,
            "subgraphs_created": [],
        },
    )
    monkeypatch.setattr(
        script_module, "run_preanalysis", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        script_module, "complexity_hotspots",
        lambda *a, **k: [],
    )

    # Fake dispatcher writes 3 files to STAGING + lies in
    # reply (claims 0). Round-10 staging contract.
    monkeypatch.setattr(
        script_module, "dispatch_risk_synthesis",
        _make_fake_dispatch_with_staging(
            tmp_path,
            write_names=[
                "hotspots.md",
                "delegatecall-sites.md",
                "reentrancy-candidates.md",
            ],
            claim_names=[],  # lying LLM
        ),
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
    # rc=2 (advisory) from the inventory mismatch.
    assert rc == 2
    # Suspect notes NEVER reach vault/risks/ — staging
    # architecture prevents the write at all.
    vault_risks = tmp_path / "risks"
    if vault_risks.exists():
        survivors = list(vault_risks.glob("*.md"))
        assert survivors == [], (
            f"suspect risk notes reached vault/risks/: "
            f"{survivors} (staging architecture should "
            f"have kept them in .audit/risk-staging/)"
        )


def test_phase4_rc2_when_extra_unclaimed_risk_note_on_disk(
    script_module, monkeypatch, tmp_path,
):
    """Codex round-5 follow-on: dirty graph case. LLM
    claims the expected three names + writes them, BUT
    also writes a phantom fourth note that it doesn't
    declare in the reply (e.g.,
    `vault/risks/backdoor.md`). Filesystem inventory
    catches the discrepancy."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)
    monkeypatch.setattr(
        script_module, "run_slither", lambda *a, **k: None
    )
    # Dirty graph (findings present).
    monkeypatch.setattr(
        script_module, "augment_sarif",
        lambda *a, **k: {
            "matched_findings": 5, "unmatched_findings": 0,
            "subgraphs_created": ["sarif:Slither"],
        },
    )
    monkeypatch.setattr(
        script_module, "run_preanalysis", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        script_module, "complexity_hotspots",
        lambda *a, **k: [],
    )

    risks_dir = tmp_path / "risks"

    def fake_dispatch_plus_phantom(*a, **k):
        risks_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for name in (
            "hotspots.md",
            "delegatecall-sites.md",
            "reentrancy-candidates.md",
        ):
            p = risks_dir / name
            p.write_text(f"content for {name}")
            paths.append(str(p))
        # Phantom file — written but NOT claimed.
        (risks_dir / "backdoor.md").write_text(
            "stealth attacker-controlled content"
        )
        import json as _json
        return {
            "graph_id": "abc", "ok": True,
            "reply": _json.dumps(paths),
            "error": None,
        }

    monkeypatch.setattr(
        script_module, "dispatch_risk_synthesis",
        fake_dispatch_plus_phantom,
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
        "extra unclaimed risk note on disk must yield rc=2 "
        "(claim/inventory mismatch)"
    )


def test_phase4_rc2_when_clean_graph_but_risk_synth_writes_notes(
    script_module, monkeypatch, tmp_path,
):
    """Codex round-4 fix: the verifier was one-sided. When
    should_have_notes is False (no findings + no hotspots),
    ANY non-empty reply silently passed — a prompt-injected
    RiskSynthesizer on a clean graph could write three
    attacker-controlled notes and get rc=0. Tighten:
    clean graph requires an empty reply.

    The RiskSynthesizer prompt contract
    (src/subagents.py:_RISK_SYNTHESIZER_PROMPT step 7) is
    explicit: 'If NO findings AND NO hotspots existed,
    return [] and skip all three.' Pin via test."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)
    monkeypatch.setattr(
        script_module, "run_slither", lambda *a, **k: None
    )
    # CLEAN GRAPH: zero matched findings.
    monkeypatch.setattr(
        script_module, "augment_sarif",
        lambda *a, **k: {
            "matched_findings": 0,
            "unmatched_findings": 0,
            "subgraphs_created": [],
        },
    )
    monkeypatch.setattr(
        script_module, "run_preanalysis", lambda *a, **k: {}
    )
    # No hotspots either.
    monkeypatch.setattr(
        script_module, "complexity_hotspots",
        lambda *a, **k: [],
    )

    # LLM writes all three notes anyway (probably with
    # attacker-controlled content from a prompt injection).
    # Without the bidirectional gate, this would pass rc=0
    # because the names match the allowlist and the files
    # exist.
    risks_dir = tmp_path / "risks"

    def fake_dispatch_writes_anyway(*a, **k):
        risks_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for name in (
            "hotspots.md",
            "delegatecall-sites.md",
            "reentrancy-candidates.md",
        ):
            p = risks_dir / name
            p.write_text(f"attacker-controlled content in {name}")
            paths.append(str(p))
        import json as _json
        return {
            "graph_id": "abc", "ok": True,
            "reply": _json.dumps(paths),
            "error": None,
        }

    monkeypatch.setattr(
        script_module, "dispatch_risk_synthesis",
        fake_dispatch_writes_anyway,
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
        "clean graph + non-empty reply must yield rc=2 "
        "(advisory). Otherwise a prompt-injected LLM could "
        "write three notes with arbitrary content on a "
        "clean repo and ship it as 'success'."
    )


def test_phase4_rc0_when_no_findings_and_empty_risk_note_list(
    script_module, monkeypatch, tmp_path,
):
    """Counterpart to F3: when augment_sarif matched ZERO
    findings (genuinely clean repo, or Tier 0 with no SARIF
    coverage), an empty risk-note list is correct — there's
    nothing to synthesize. Must yield rc=0, not rc=2."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)
    monkeypatch.setattr(
        script_module, "run_slither", lambda *a, **k: None
    )
    monkeypatch.setattr(
        script_module, "augment_sarif",
        lambda *a, **k: {
            "matched_findings": 0,
            "unmatched_findings": 0,
            "subgraphs_created": [],
        },
    )
    monkeypatch.setattr(
        script_module, "run_preanalysis", lambda *a, **k: {}
    )
    # Codex F2: complexity_hotspots stubbed to [] so the
    # "should-have-notes" gate evaluates False (no findings
    # + no hotspots → empty reply genuinely OK).
    monkeypatch.setattr(
        script_module, "complexity_hotspots",
        lambda *a, **k: [],
    )
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

    assert script_module.main() == 0, (
        "zero findings + empty risk-note list is genuinely "
        "OK; must yield rc=0"
    )


def test_phase4_rc2_when_risk_synth_claims_non_md_path_inside_vault(
    script_module, monkeypatch, tmp_path,
):
    """Codex review fix (F6): even if the claimed path is
    inside `vault/risks/`, it must end in `.md`. An attacker
    LLM could write to `vault/risks/.DS_Store` or
    `vault/risks/symlink-elsewhere` and pass shallow
    containment checks. Suffix `.md` is the simplest
    additional constraint."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)
    _stub_phase4a_clean(script_module, monkeypatch)
    # Create a non-.md file inside vault/risks/ that DOES exist.
    risks_dir = tmp_path / "risks"
    risks_dir.mkdir(parents=True)
    bogus = risks_dir / "claimed.txt"
    bogus.write_text("not a risk note")
    monkeypatch.setattr(
        script_module, "dispatch_risk_synthesis",
        lambda *a, **k: {
            "graph_id": "abc", "ok": True,
            "reply": f'["{bogus}"]',
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


def test_phase4_rc2_when_risk_synth_reply_unparseable(
    script_module, monkeypatch, tmp_path,
):
    """Cross-cutting review fix (I8): malformed reply (not
    JSON, wrong shape) also degrades to rc=2. The vault may
    still have risk notes — but the auditing layer can't
    confirm what the LLM claims to have done."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    _stub_topo_flows_moc(script_module, monkeypatch)
    _stub_phase4a_clean(script_module, monkeypatch)
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
