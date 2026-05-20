"""Tests for scripts/document_one_node.py.

Loads the script as a module via importlib (scripts/ is not a
Python package). Covers arg parsing, env validation, exit-code
paths, and the task-message wiring to the agent.

Note: unlike `scripts/trace_one_flow.py` (which uses
`dispatch_flows`), this script builds and invokes the LLM agent
inline. Tests patch `create_deep_agent` + `ChatOpenAI` to avoid
LLM calls.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "document_one_node.py"
)


@pytest.fixture
def script_module():
    """Fresh import of document_one_node.py per test."""
    spec = importlib.util.spec_from_file_location(
        "document_one_node_under_test", str(SCRIPT_PATH)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_returns_2_when_no_api_key(script_module, monkeypatch):
    """Missing OPENAI_API_KEY → exit code 2 (setup error)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        sys, "argv",
        ["document_one_node.py", "contracts.X:X"],
    )
    assert script_module.main() == 2


def test_returns_2_when_repo_missing(script_module, monkeypatch):
    """Nonexistent repo path → exit code 2."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    monkeypatch.setattr(
        sys, "argv",
        [
            "document_one_node.py",
            "contracts.X:X.foo",
            "--repo", "/does/not/exist",
        ],
    )
    assert script_module.main() == 2


def test_returns_2_when_node_not_in_graph(
    script_module, monkeypatch, tmp_path
):
    """node_id missing from the parsed graph → exit 2."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    monkeypatch.setattr(
        sys, "argv",
        [
            "document_one_node.py",
            "contracts.DoesNotExist:DoesNotExist",
            "--repo", "tests/fixtures/tier1_uniswap_v2",
            "--vault", str(tmp_path),
        ],
    )
    assert script_module.main() == 2


def test_passes_node_id_to_agent_invoke(
    script_module, monkeypatch, tmp_path
):
    """The script constructs a task message containing the
    target node_id and dispatches it to the agent. Verifies
    the wiring without hitting a real LLM."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")

    # Capture the invoke call.
    captured: dict = {}

    class FakeAgent:
        def invoke(self, inputs):
            captured["task_msg"] = inputs["messages"][0]["content"]
            reply = MagicMock()
            reply.content = "/fake/written/path.md"
            return {"messages": [reply]}

    monkeypatch.setattr(
        script_module, "ChatOpenAI", lambda *a, **k: object()
    )
    monkeypatch.setattr(
        script_module,
        "create_deep_agent",
        lambda *a, **k: FakeAgent(),
    )

    target = "contracts.UniswapV2Pair:UniswapV2Pair"
    monkeypatch.setattr(
        sys, "argv",
        [
            "document_one_node.py",
            target,
            "--repo", "tests/fixtures/tier1_uniswap_v2",
            "--vault", str(tmp_path),
        ],
    )

    # main() returns 1 because the fake agent didn't actually
    # write a file at vault/contracts/UniswapV2Pair.md. That's
    # fine — we're testing the task-message wiring, not the
    # post-write file check.
    rc = script_module.main()
    assert rc in (0, 1), f"unexpected exit code: {rc}"

    assert "task_msg" in captured, "agent.invoke not called"
    assert target in captured["task_msg"], (
        f"task message missing target node_id: "
        f"{captured['task_msg']!r}"
    )


def test_method_kind_node_redirects_to_parent_path(
    script_module, monkeypatch, tmp_path
):
    """When a method node_id is passed, the script invokes
    the agent (which internally redirects to the parent
    contract per NodeDocumenter prompt rule #3) AND THEN
    checks for the PARENT contract's file path — not the
    method's. Verifies the redirect logic on the path-check
    side."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")

    class FakeAgent:
        def invoke(self, inputs):
            reply = MagicMock()
            reply.content = "/fake/written/path.md"
            return {"messages": [reply]}

    monkeypatch.setattr(
        script_module, "ChatOpenAI", lambda *a, **k: object()
    )
    monkeypatch.setattr(
        script_module,
        "create_deep_agent",
        lambda *a, **k: FakeAgent(),
    )

    method_id = "contracts.UniswapV2Pair:UniswapV2Pair.swap"
    monkeypatch.setattr(
        sys, "argv",
        [
            "document_one_node.py",
            method_id,
            "--repo", "tests/fixtures/tier1_uniswap_v2",
            "--vault", str(tmp_path),
        ],
    )

    # Pre-create the PARENT's note path so the script
    # detects "file exists" and returns 0. This pins the
    # redirect logic — if the script were checking the
    # METHOD's path instead of the parent's, it would
    # look for vault/contracts/swap.md (which doesn't
    # exist) and return 1.
    parent_path = tmp_path / "contracts" / "UniswapV2Pair.md"
    parent_path.parent.mkdir(parents=True, exist_ok=True)
    parent_path.write_text("# UniswapV2Pair (test stub)\n")

    rc = script_module.main()
    assert rc == 0, (
        f"expected exit 0 (method redirected to parent + "
        f"file exists); got {rc}. The script may have "
        f"checked the method's path instead of the parent's."
    )
