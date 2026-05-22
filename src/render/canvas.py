"""Obsidian Canvas exporter — emits a `.canvas` JSON file
that opens in Obsidian and visualizes the inheritance
hierarchy across all documentable contracts/libraries/
interfaces.

Canvas format: https://jsoncanvas.org/. Each node is a
`file`-type tile pointing at the vault note; clicking the
tile in Obsidian opens the note. Each edge is an inheritance
relation rendered as an arrow (child's top → parent's bottom,
so the layout reads top-to-bottom as "this extends that").

Layout: layered / topological. Layer = longest inheritance
chain from this node to any root. Layer 0 = roots (no
parents in the graph). Within a layer, nodes sort by name
for deterministic output.

Out of scope for this chunk:
  - `uses` / `implements` / call edges (inheritance only)
  - Styling / colors / group rectangles
  - Header text node ("Contract overview")
  - CLI subcommand (`washable canvas`)
"""

import json
import os
import secrets
import threading
from pathlib import Path
from typing import Annotated, Any

from langchain_core.tools import InjectedToolArg

from src.graph.persist import CACHE_ROOT, load_graph
from src.render.obsidian import _disambiguated_path

# Canvas-node defaults. Sized to fit a typical contract
# tile (file name + a few preview lines in Obsidian's
# default theme).
_NODE_WIDTH = 300
_NODE_HEIGHT = 150
_COL_PITCH = 350
_LAYER_PITCH = 220

# Documentable top-level kinds. Methods/modules/structs/
# enums live inside their parent's note — the canvas
# overview is per-file, not per-symbol.
_NODE_KINDS = ("contract", "library", "interface", "trait")


def _canvas_id() -> str:
    """16-hex id, matching Obsidian's own id format."""
    return secrets.token_hex(8)


def _bare_name(node_id: str) -> str:
    """Tail after the last `:`. Mirrors mermaid.py's helper
    of the same name. `contracts.X:UniswapV2Pair` →
    `UniswapV2Pair`."""
    return node_id.rsplit(":", 1)[-1]


def _resolved_inherits_pairs(
    data: dict[str, Any],
    node_ids: set[str],
) -> list[tuple[str, str]]:
    """Return inheritance edges as `(child_id, parent_id)`
    pairs with both ids resolved to canonical entries in
    `node_ids`.

    Trailmark's Solidity parser frequently emits `inherits`
    edges whose target is prefixed with the SOURCE file's
    module path (e.g. `UniswapV2Pair:UniswapV2Pair →
    UniswapV2Pair:UniswapV2ERC20`, where the real ERC20
    canonical id is `UniswapV2ERC20:UniswapV2ERC20`). When
    the literal target isn't in `node_ids`, fall back to
    bare-name lookup; exactly one match resolves. Zero or
    ambiguous matches drop the edge entirely (the canvas
    can't link a relationship it can't anchor to a tile).

    Mirrors `src/render/mermaid.py::_resolve_phantom`.
    """
    by_bare: dict[str, set[str]] = {}
    for nid in node_ids:
        by_bare.setdefault(_bare_name(nid), set()).add(nid)

    pairs: list[tuple[str, str]] = []
    for e in data.get("edges", []):
        if e.get("kind") != "inherits":
            continue
        child = e.get("source")
        parent_raw = e.get("target")
        if not isinstance(child, str) or not isinstance(parent_raw, str):
            continue
        if child not in node_ids:
            # Edges originate from documentable units in our
            # filter; an unresolved source means the edge
            # spans a kind we excluded (e.g., struct → enum).
            continue
        if parent_raw in node_ids:
            parent = parent_raw
        else:
            candidates = by_bare.get(_bare_name(parent_raw), set())
            if len(candidates) != 1:
                continue
            parent = next(iter(candidates))
        pairs.append((child, parent))
    return pairs


def _compute_layers(
    node_ids: set[str],
    resolved_pairs: list[tuple[str, str]],
) -> dict[str, int]:
    """Assign each node to a layer: 0 = no parents in the
    graph (roots), N = longest inheritance chain of N edges
    from a root.

    Iterative longest-path on a DAG; safe because inheritance
    cycles are a Solidity error Trailmark wouldn't surface.
    `resolved_pairs` is already canonical-id pairs from
    `_resolved_inherits_pairs`.
    """
    parents: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for child, parent in resolved_pairs:
        parents[child].append(parent)

    layer: dict[str, int] = {}

    def _depth(nid: str, visiting: set[str]) -> int:
        if nid in layer:
            return layer[nid]
        if nid in visiting:
            # Cycle guard — invalid Solidity, but don't
            # recurse forever if Trailmark ever produces
            # one. Treat the cycle entry as a root.
            return 0
        visiting = visiting | {nid}
        ps = parents.get(nid, [])
        d = 0 if not ps else 1 + max(
            _depth(p, visiting) for p in ps
        )
        layer[nid] = d
        return d

    for nid in node_ids:
        _depth(nid, set())
    return layer


def render_canvas(
    graph_id: str,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> str:
    """Render a layered inheritance Canvas for every
    documentable unit in the graph. Returns the JSON string
    (UTF-8, indented for diff-readability)."""
    engine = load_graph(graph_id, cache_root=cache_root)
    data = json.loads(engine.to_json())

    # Top-level documentable units only. Method/struct/enum
    # nodes stay inside their parent contract's note.
    nodes = [
        n for n in data.get("nodes", {}).values()
        if n.get("kind") in _NODE_KINDS
    ]
    node_ids = {n["id"] for n in nodes}
    resolved_pairs = _resolved_inherits_pairs(data, node_ids)

    # Layout: layer by longest inheritance chain; within a
    # layer, sort by name (deterministic across runs).
    layers = _compute_layers(node_ids, resolved_pairs)
    by_layer: dict[int, list[dict[str, Any]]] = {}
    for n in nodes:
        by_layer.setdefault(layers[n["id"]], []).append(n)
    for layer_nodes in by_layer.values():
        layer_nodes.sort(key=lambda n: n["name"])

    # Trailmark node_id → Canvas node id. The same mapping
    # routes edges to the right tiles below.
    id_map: dict[str, str] = {n["id"]: _canvas_id() for n in nodes}

    canvas_nodes: list[dict[str, Any]] = []
    for layer_idx, layer_nodes in sorted(by_layer.items()):
        for col, n in enumerate(layer_nodes):
            rel_path = (
                f"{_disambiguated_path(n, graph_id, cache_root=cache_root)}"
                f".md"
            )
            canvas_nodes.append({
                "id": id_map[n["id"]],
                "type": "file",
                "file": rel_path,
                "x": col * _COL_PITCH,
                "y": layer_idx * _LAYER_PITCH,
                "width": _NODE_WIDTH,
                "height": _NODE_HEIGHT,
            })

    canvas_edges: list[dict[str, Any]] = []
    for child, parent in resolved_pairs:
        canvas_edges.append({
            "id": _canvas_id(),
            # Arrow flows from child's TOP → parent's BOTTOM,
            # so the rendered diagram reads top-to-bottom
            # as "this child extends that parent above it".
            "fromNode": id_map[child],
            "fromSide": "top",
            "toNode": id_map[parent],
            "toSide": "bottom",
        })

    return json.dumps(
        {"nodes": canvas_nodes, "edges": canvas_edges},
        indent=2,
        sort_keys=False,
    )


# Cross-thread lock for atomic writes. dispatch_topo today
# only calls writers from a single producer thread, but
# the lock costs nothing and matches the pattern in
# `src/graph/cache.py::_CACHE_WRITE_LOCK`.
_CANVAS_WRITE_LOCK = threading.Lock()


def _write_canvas_atomic(
    vault_path: str | Path,
    rel_path: str,
    content: str,
) -> Path:
    """Atomic write via tmp + os.replace with the same
    containment defenses `write_obsidian_note` enforces:
    no `..` segments, no absolute paths, resolved target
    must stay inside vault.

    Inlined rather than refactored from obsidian.py because
    `write_obsidian_note` is YAML-frontmatter-coupled (always
    emits a `---` block). Canvas JSON has no frontmatter.
    The atomic-write logic is ~15 LOC and worth duplicating
    over a refactor that touches a load-bearing writer."""
    rel_parts = rel_path.replace("\\", "/").split("/")
    if ".." in rel_parts:
        raise ValueError(
            f"rel_path contains traversal segment: {rel_path!r}"
        )
    if rel_path.startswith(("/", "\\")) or (
        len(rel_path) >= 2 and rel_path[1] == ":"
    ):
        raise ValueError(
            f"rel_path must be relative to vault: {rel_path!r}"
        )
    vault = Path(vault_path)
    target = vault / rel_path
    try:
        target.resolve().relative_to(vault.resolve())
    except ValueError as exc:
        raise ValueError(
            f"rel_path escapes vault: {rel_path!r}"
        ) from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.parent / (
        f".{target.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    )
    with _CANVAS_WRITE_LOCK:
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(tmp_path, target)
        except Exception:
            # Intentionally broad: the catch exists to
            # clean up the tmp file on ANY failure path
            # (permission denied on the replace, disk
            # full mid-write, etc.), then re-raises.
            # Mirrors `write_obsidian_note`'s pattern;
            # without this, vault-root orphans accumulate
            # because `_sweep_stale_tmp_files` only
            # covers VAULT_SUBDIRS entries, not the
            # vault root itself.
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise
    return target


def render_and_write_canvas(
    vault_path: str | Path,
    graph_id: str,
    *,
    rel_path: str = "overview.canvas",
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> str:
    """Render the inheritance canvas and atomically write
    it to `<vault>/<rel_path>` (default `overview.canvas`
    at vault root). Returns the absolute path as a string."""
    content = render_canvas(graph_id, cache_root=cache_root)
    written = _write_canvas_atomic(vault_path, rel_path, content)
    return str(written)
