import json
import logging

import pytest

from src.render.mermaid import render_inheritance

PAIR_ID = "contracts.UniswapV2Pair:UniswapV2Pair"
ERC20_ID = "contracts.UniswapV2ERC20:UniswapV2ERC20"


def test_pair_inherits_erc20_is_solid(tier1_graph_id):
    """Success criterion — literal substring from spec."""
    gid, cache_root = tier1_graph_id
    out = render_inheritance(gid, PAIR_ID, cache_root=cache_root)
    assert "UniswapV2ERC20 <|-- UniswapV2Pair" in out


def test_pair_implements_ipair_is_dashed(tier1_graph_id):
    """The interface side of `is IUniswapV2Pair, UniswapV2ERC20`
    must render with the dashed/implements style, not solid."""
    gid, cache_root = tier1_graph_id
    out = render_inheritance(gid, PAIR_ID, cache_root=cache_root)
    assert "IUniswapV2Pair <|.. UniswapV2Pair" in out


def test_output_is_a_fenced_classdiagram_block(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    out = render_inheritance(gid, PAIR_ID, cache_root=cache_root)
    assert out.startswith("```mermaid\n")
    assert out.rstrip().endswith("```")
    assert "classDiagram" in out


def test_erc20_has_pair_as_child(tier1_graph_id):
    """ERC20 is a base of Pair — rendering ERC20 must list Pair as
    a child. This tests the child-side traversal that requires
    phantom-target resolution to work."""
    gid, cache_root = tier1_graph_id
    out = render_inheritance(gid, ERC20_ID, cache_root=cache_root)
    assert "UniswapV2ERC20 <|-- UniswapV2Pair" in out


def test_unknown_node_raises_key_error(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    with pytest.raises(KeyError):
        render_inheritance(gid, "fake:DoesNotExist", cache_root=cache_root)


# --- ambiguous phantom resolution ------------------------------------


def _synthetic_node(node_id: str, kind: str = "contract") -> dict:
    """Minimal Trailmark-shaped node for synthetic-graph tests."""
    return {
        "id": node_id,
        "name": node_id.rsplit(":", 1)[-1].rsplit(".", 1)[-1],
        "kind": kind,
        "location": {
            "file_path": f"/fake/{node_id}.sol",
            "start_line": 1,
            "end_line": 10,
            "start_col": 0,
            "end_col": 0,
        },
        "parameters": [],
        "return_type": None,
        "exception_types": [],
        "cyclomatic_complexity": None,
        "branches": [],
        "docstring": None,
    }


def test_render_inheritance_ambiguous_phantom_warns_and_defaults_to_inherit(
    monkeypatch, caplog
):
    """When a phantom inheritance target's bare name matches
    MULTIPLE real nodes (e.g., two `Foo` contracts in different
    modules), `_resolve_phantom` can't pick one — it logs a
    warning and returns None. `render_inheritance` then defaults
    to the solid `<|--` (inherit) style instead of `<|..`
    (implements).

    Direct test on a synthetic graph; the Tier 0/1 fixtures
    don't have name collisions that would exercise this
    branch, so without this test a real-world repo with
    colliding base names could emit wrong edge styles silently."""
    fake_data = {
        "language": "solidity",
        "root_path": "/fake",
        "summary": {},
        "nodes": {
            # Two real "Foo" contracts in different modules ⇒
            # bare name "Foo" maps to 2 candidates ⇒ ambiguous.
            "module1.Foo:Foo": _synthetic_node("module1.Foo:Foo"),
            "module2.Vendored:Foo": _synthetic_node(
                "module2.Vendored:Foo"
            ),
            # Inheritor with a phantom edge.
            "module3.Inheritor:Inheritor": _synthetic_node(
                "module3.Inheritor:Inheritor"
            ),
        },
        "edges": [
            {
                # Phantom target: not in nodes_by_id; bare name
                # "Foo" matches the 2 real Foo nodes above.
                "source": "module3.Inheritor:Inheritor",
                "target": "module3.Inheritor:Foo",
                "kind": "inherits",
                "confidence": "inferred",
            },
        ],
        "subgraphs": {},
    }

    class _FakeEngine:
        def to_json(self):
            return json.dumps(fake_data)

    monkeypatch.setattr(
        "src.render.mermaid.load_graph",
        lambda graph_id, *, cache_root=None: _FakeEngine(),
    )

    caplog.set_level(logging.WARNING, logger="src.render.mermaid")

    out = render_inheritance(
        "abc012345678", "module3.Inheritor:Inheritor"
    )

    # 1. Warning fired with the diagnostic details we promised.
    warning_messages = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING
    ]
    assert any(
        "ambiguous" in m and "Foo" in m for m in warning_messages
    ), f"expected ambiguous-name warning; got: {warning_messages}"

    # 2. Edge style defaults to inherits (solid <|--). The
    #    resolver couldn't determine the target's kind, so
    #    `target_kind = "contract"` (the fallback), which means
    #    we DON'T switch to the implements style.
    assert "Foo <|-- Inheritor" in out
    assert "Foo <|.. Inheritor" not in out

    # 3. Output is still well-formed Mermaid (the failure mode
    #    is wrong style, not a broken diagram).
    assert out.startswith("```mermaid\n")
    assert "classDiagram" in out
    assert out.rstrip().endswith("```")
