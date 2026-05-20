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


def test_returns_2_when_no_api_key(script_module, monkeypatch):
    """Missing OPENAI_API_KEY → exit 2 (setup error)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["document_repo.py"])
    assert script_module.main() == 2


def test_returns_2_when_repo_missing(script_module, monkeypatch):
    """Nonexistent --repo path → exit 2."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    monkeypatch.setattr(
        sys, "argv",
        [
            "document_repo.py",
            "--repo", "/does/not/exist",
        ],
    )
    assert script_module.main() == 2


def test_calls_dispatch_topo_then_flows_then_moc(
    script_module, monkeypatch, tmp_path
):
    """Script orchestration order: dispatch_topo before
    dispatch_flows, both before write_root_moc. Derived
    notes need bases on disk; the MOC needs both."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")

    call_order: list[str] = []

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

    def fake_moc(*a, **k):
        call_order.append("moc")
        return []  # script expects a list (len() is called on it)

    monkeypatch.setattr(script_module, "dispatch_topo", fake_topo)
    monkeypatch.setattr(script_module, "dispatch_flows", fake_flows)
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
    assert call_order == ["topo", "flows", "moc"], (
        f"expected dispatch_topo → dispatch_flows → "
        f"write_root_moc; got {call_order}"
    )


def test_returns_1_when_dispatch_topo_has_failures(
    script_module, monkeypatch, tmp_path
):
    """Exit code 1 distinguishes 'partial success' (some
    nodes failed) from setup error (2) or full success
    (0)."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")

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
