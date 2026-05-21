"""Tests for scripts/trace_one_flow.py.

Loads the script as a module via importlib (scripts/ is not a
Python package). Establishes the testing pattern for other
script-based harnesses that currently have no test coverage.
"""

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "trace_one_flow.py"
)


@pytest.fixture
def trace_module():
    """Fresh import of trace_one_flow.py for each test."""
    spec = importlib.util.spec_from_file_location(
        "trace_one_flow_under_test", str(SCRIPT_PATH)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_returns_2_when_no_api_key(trace_module, monkeypatch):
    """No OPENAI_API_KEY → exit code 2 (setup error)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["trace_one_flow.py", "contracts.X:X.foo"],
    )
    assert trace_module.main() == 2


def test_returns_2_when_repo_missing(trace_module, monkeypatch):
    """Nonexistent repo path → exit code 2."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "trace_one_flow.py",
            "contracts.X:X.foo",
            "--repo",
            "/does/not/exist",
        ],
    )
    assert trace_module.main() == 2


def test_returns_2_when_entrypoint_not_in_graph(
    trace_module, monkeypatch, tmp_path
):
    """entrypoint_id missing from the parsed graph → exit 2."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "trace_one_flow.py",
            "contracts.DoesNotExist:DoesNotExist.foo",
            "--repo",
            "tests/fixtures/tier1_uniswap_v2",
            "--vault",
            str(tmp_path),
        ],
    )
    assert trace_module.main() == 2


def test_passes_focused_filter_to_dispatch_flows(
    trace_module, monkeypatch, tmp_path
):
    """dispatch_flows is called with an entrypoint_filter that
    narrows to ONLY the requested entrypoint. Verifies the
    script uses the filter parameter instead of mutating
    module globals."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")

    captured: dict = {}

    def fake_dispatch_flows(*args, **kwargs):
        captured.update(kwargs)
        return {
            "graph_id": "abc",
            "entrypoint_count": 1,
            "successes": [
                {"node_id": "fake", "agent_reply": "/fake/path.md"}
            ],
            "failures": [],
            "order": ["fake"],
        }

    monkeypatch.setattr(
        trace_module, "dispatch_flows", fake_dispatch_flows
    )

    target = "contracts.UniswapV2Pair:UniswapV2Pair.swap"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "trace_one_flow.py",
            target,
            "--repo",
            "tests/fixtures/tier1_uniswap_v2",
            "--vault",
            str(tmp_path),
        ],
    )

    rc = trace_module.main()
    assert rc == 0

    # dispatch_flows received an entrypoint_filter that narrows
    # correctly.
    assert "entrypoint_filter" in captured
    filter_fn = captured["entrypoint_filter"]
    fake_surface = [
        {"node_id": target},
        {"node_id": "contracts.UniswapV2Pair:UniswapV2Pair.mint"},
        {
            "node_id": (
                "contracts.interfaces.IUniswapV2Pair:"
                "IUniswapV2Pair.swap"
            )
        },
    ]
    filtered = filter_fn(fake_surface)
    assert len(filtered) == 1
    assert filtered[0]["node_id"] == target

    # skip_leaf_entrypoints stays False — caller wants the
    # requested entrypoint regardless of leaf status.
    assert captured["skip_leaf_entrypoints"] is False


def test_dispatches_node_not_on_attack_surface(
    trace_module, monkeypatch, tmp_path
):
    """The harness must dispatch FlowTracer even when the
    requested node isn't on the attack surface — e.g., when
    iterating on internal helpers like _safeTransfer. The
    warning at scripts/trace_one_flow.py promises "FlowTracer
    will still run"; a filter that only narrowed the
    attack_surface() input would silently produce FLOWS:0
    instead, contradicting the warning."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")

    captured: dict = {}

    def fake_dispatch_flows(*args, **kwargs):
        # Simulate dispatch_flows applying the filter to its
        # real attack_surface() result — input list does NOT
        # contain the internal helper we requested. Filter
        # must still return our target.
        fake_surface = [
            {"node_id": "contracts.UniswapV2Pair:UniswapV2Pair.swap"},
            {"node_id": "contracts.UniswapV2Pair:UniswapV2Pair.mint"},
        ]
        filtered = kwargs["entrypoint_filter"](fake_surface)
        captured["filtered"] = filtered
        return {
            "graph_id": "abc",
            "entrypoint_count": len(filtered),
            "successes": [
                {"node_id": e["node_id"], "agent_reply": "/n.md"}
                for e in filtered
            ],
            "failures": [],
            "order": [e["node_id"] for e in filtered],
        }

    monkeypatch.setattr(
        trace_module, "dispatch_flows", fake_dispatch_flows
    )

    # _safeTransfer is a `private` Solidity function in the
    # Tier 1 fixture — exists in the graph but is NOT on
    # attack_surface().
    target = (
        "contracts.UniswapV2Pair:UniswapV2Pair._safeTransfer"
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "trace_one_flow.py",
            target,
            "--repo",
            "tests/fixtures/tier1_uniswap_v2",
            "--vault",
            str(tmp_path),
        ],
    )

    rc = trace_module.main()
    assert rc == 0, (
        f"non-surface node should still dispatch + succeed; "
        f"got rc={rc}. The filter must return the requested "
        f"entrypoint even when attack_surface() doesn't "
        f"contain it."
    )
    assert captured["filtered"] == [{"node_id": target}], (
        f"filter should produce a single-entry list with the "
        f"requested entrypoint regardless of the attack_surface "
        f"input; got {captured['filtered']!r}"
    )


def test_no_module_global_mutation(
    trace_module, monkeypatch, tmp_path
):
    """After main() returns, src.agent.attack_surface is the
    original function. The script uses the entrypoint_filter
    parameter and never patches module globals."""
    import src.agent

    original = src.agent.attack_surface

    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key")
    monkeypatch.setattr(
        trace_module,
        "dispatch_flows",
        lambda *a, **k: {
            "graph_id": "x",
            "entrypoint_count": 0,
            "successes": [],
            "failures": [],
            "order": [],
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "trace_one_flow.py",
            "contracts.UniswapV2Pair:UniswapV2Pair.swap",
            "--repo",
            "tests/fixtures/tier1_uniswap_v2",
            "--vault",
            str(tmp_path),
        ],
    )

    trace_module.main()
    assert src.agent.attack_surface is original
