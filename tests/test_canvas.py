"""Tests for src/render/canvas.py.

Strategy: use the session-scoped tier1_graph_id fixture
(same as test_mermaid_inheritance.py), parse the canvas
JSON, and assert structural invariants. No Obsidian
dependency — round-trip JSON suffices for the success
criterion ("opens in Obsidian and shows inheritance edges").

Tier 1 graph has 11 documentable top-level units
(3 contracts + 5 interfaces + 3 libraries) and 4
inheritance edges:
  - UniswapV2ERC20 → IUniswapV2ERC20
  - UniswapV2Factory → IUniswapV2Factory
  - UniswapV2Pair → IUniswapV2Pair
  - UniswapV2Pair → UniswapV2ERC20    ← CHUNKS.md success edge
"""

import itertools
import json
from pathlib import Path

import pytest

from src.render.canvas import (
    _write_canvas_atomic,
    render_and_write_canvas,
    render_canvas,
)


def test_render_canvas_returns_valid_json(tier1_graph_id):
    """Pure-function contract: top-level shape is
    `{nodes: [...], edges: [...]}` and the result parses
    as JSON."""
    gid, cache_root = tier1_graph_id
    out = render_canvas(gid, cache_root=cache_root)
    parsed = json.loads(out)
    assert "nodes" in parsed and isinstance(parsed["nodes"], list)
    assert "edges" in parsed and isinstance(parsed["edges"], list)


def test_render_canvas_is_deterministic(
    tier1_graph_id, monkeypatch,
):
    """Two calls with monkey-patched ids produce byte-equal
    output. Real `_canvas_id` uses `secrets.token_hex`, so
    we replace it with a deterministic counter; the rest
    of the renderer must be pure."""
    gid, cache_root = tier1_graph_id

    def _counter():
        counter = itertools.count()

        def _next() -> str:
            return f"id{next(counter):04d}"

        return _next

    monkeypatch.setattr(
        "src.render.canvas._canvas_id", _counter(),
    )
    out1 = render_canvas(gid, cache_root=cache_root)
    monkeypatch.setattr(
        "src.render.canvas._canvas_id", _counter(),
    )
    out2 = render_canvas(gid, cache_root=cache_root)
    assert out1 == out2


def test_render_canvas_includes_all_tier1_documentable_units(
    tier1_graph_id,
):
    """Every Tier 1 top-level unit (contract/library/
    interface) appears as a canvas node. Pins the
    `_NODE_KINDS` filter against the real fixture."""
    gid, cache_root = tier1_graph_id
    canvas = json.loads(render_canvas(gid, cache_root=cache_root))
    files = {n["file"] for n in canvas["nodes"]}
    expected = {
        # Contracts
        "contracts/UniswapV2ERC20.md",
        "contracts/UniswapV2Factory.md",
        "contracts/UniswapV2Pair.md",
        # Interfaces (folder maps to `interfaces/` via
        # KIND_TO_FOLDER)
        "interfaces/IERC20.md",
        "interfaces/IUniswapV2Callee.md",
        "interfaces/IUniswapV2ERC20.md",
        "interfaces/IUniswapV2Factory.md",
        "interfaces/IUniswapV2Pair.md",
        # Libraries
        "libraries/Math.md",
        "libraries/SafeMath.md",
        "libraries/UQ112x112.md",
    }
    missing = expected - files
    assert not missing, f"missing canvas nodes: {missing}"


def test_render_canvas_draws_pair_extends_erc20_edge(
    tier1_graph_id,
):
    """CHUNKS.md success criterion: shows all Tier 1
    contracts with inheritance edges visible. UniswapV2Pair
    extends UniswapV2ERC20; assert that arrow is in the
    canvas (child→parent: Pair's top → ERC20's bottom)."""
    gid, cache_root = tier1_graph_id
    canvas = json.loads(render_canvas(gid, cache_root=cache_root))

    # Build canvas-id → file path lookup.
    id_to_file = {n["id"]: n["file"] for n in canvas["nodes"]}
    pair_to_erc20 = [
        e for e in canvas["edges"]
        if id_to_file.get(e["fromNode"]) == "contracts/UniswapV2Pair.md"
        and id_to_file.get(e["toNode"]) == "contracts/UniswapV2ERC20.md"
    ]
    assert pair_to_erc20, (
        "missing Pair→ERC20 inheritance edge in canvas "
        f"{canvas['edges']}"
    )
    # Pin the arrow direction convention: child's top →
    # parent's bottom (reads top-to-bottom as "this extends
    # that above it").
    assert pair_to_erc20[0]["fromSide"] == "top"
    assert pair_to_erc20[0]["toSide"] == "bottom"


def test_render_canvas_places_derived_below_base(
    tier1_graph_id,
):
    """Layered layout puts roots at top (smaller y) and
    derived contracts below (larger y). Pair extends
    ERC20, so Pair's y > ERC20's y."""
    gid, cache_root = tier1_graph_id
    canvas = json.loads(render_canvas(gid, cache_root=cache_root))
    by_file = {n["file"]: n for n in canvas["nodes"]}
    pair = by_file["contracts/UniswapV2Pair.md"]
    erc20 = by_file["contracts/UniswapV2ERC20.md"]
    assert pair["y"] > erc20["y"], (
        f"Pair (derived) should be below ERC20 (base): "
        f"pair.y={pair['y']}, erc20.y={erc20['y']}"
    )


def test_render_canvas_no_position_collisions(tier1_graph_id):
    """Layout invariant: no two nodes share the same
    (x, y) position. Within-layer alphabetical sort + per-
    column pitch guarantees this; pin so a future layout
    refactor that introduces stacking gets caught."""
    gid, cache_root = tier1_graph_id
    canvas = json.loads(render_canvas(gid, cache_root=cache_root))
    positions = [(n["x"], n["y"]) for n in canvas["nodes"]]
    assert len(set(positions)) == len(positions), (
        f"stacked nodes: {positions}"
    )


def test_render_and_write_canvas_writes_file(
    tier1_graph_id, tmp_path,
):
    """End-to-end write: file lands at
    `<vault>/overview.canvas`, parses as JSON, contains
    the Pair→ERC20 edge."""
    gid, cache_root = tier1_graph_id
    vault = tmp_path / "vault"
    vault.mkdir()
    written = render_and_write_canvas(
        vault, gid, cache_root=cache_root,
    )
    path = Path(written)
    assert path.exists()
    assert path.name == "overview.canvas"
    assert path.parent == vault
    canvas = json.loads(path.read_text())
    files = {n["file"] for n in canvas["nodes"]}
    assert "contracts/UniswapV2Pair.md" in files
    assert "contracts/UniswapV2ERC20.md" in files


def test_write_canvas_rejects_traversal_path(tmp_path):
    """Mirrors `write_obsidian_note`'s `..`-segment defense.
    A future caller passing a traversal path must hit the
    inline atomic-write's containment check, not silently
    escape the vault."""
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(ValueError, match="traversal segment"):
        _write_canvas_atomic(vault, "../escape.canvas", "{}")
    # Absolute paths blocked too.
    with pytest.raises(ValueError, match="must be relative"):
        _write_canvas_atomic(vault, "/tmp/escape.canvas", "{}")
    # The phantom file must NOT have been created.
    assert not (tmp_path / "escape.canvas").exists()
