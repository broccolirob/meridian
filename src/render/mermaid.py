"""Mermaid renderers for call graphs, inheritance trees, and
sequence diagrams.

Chunk 3.1: call graphs only. 3.2 adds inheritance, 3.3 adds
sequence, 3.4 incorporates styling patterns from Trail of Bits'
diagramming-code skill.

Each renderer returns a complete fenced Mermaid block ready to
embed in Markdown:

    ```mermaid
    graph TD
        n0[UniswapV2Pair.swap]
        ...
    ```

Why fenced: the consumers (chunk 3.5 onward) drop these into
Markdown body text. Returning bare Mermaid forces every caller to
wrap it identically; centralizing the fence here removes one
opportunity for inconsistent embedding.
"""

from collections import deque
from pathlib import Path
from typing import Any

from src.graph.persist import CACHE_ROOT
from src.tools import callees_of, callers_of, get_node


def _label(node_id: str) -> str:
    """Display label for a node. Derived from the node_id tail:
    `contracts.Pair:Pair.swap` → `Pair.swap`, `contracts.Pair:Pair`
    → `Pair`. Qualifying methods this way avoids collisions when
    two contracts each have a method named e.g. `swap`."""
    return node_id.rsplit(":", 1)[-1]


def _quoted_label(label: str) -> str:
    """Wrap a label in Mermaid double quotes if it contains chars
    Mermaid would otherwise mis-parse (spaces, parens, etc.).
    Solidity identifiers don't, but we're defensive for languages
    Trailmark may add later."""
    needs_quote = any(c in label for c in ' ()<>"|[]{}')
    if needs_quote:
        safe = label.replace('"', "&quot;")
        return f'"{safe}"'
    return label


def render_call_graph(
    graph_id: str,
    node_id: str,
    depth: int = 2,
    *,
    cache_root: Path = CACHE_ROOT,
) -> str:
    """Render a depth-limited call graph centered on `node_id`.

    Returns a fenced Mermaid block (```` ```mermaid\\ngraph TD\\n…
    \\n``` ````) with callers upstream of `node_id` and callees
    downstream, each direction limited to `depth` BFS hops.

    `depth=0` returns just the root node. `depth=1` shows direct
    neighbors only. `depth=2` (default) shows 2-hop neighbors.

    Cycles are handled by a visited set — each (src, dst) edge is
    emitted at most once, and each node appears at most once.

    Mermaid IDs are opaque aliases (`n0`, `n1`, …) assigned in
    deterministic order (sorted by Trailmark ID) so output is
    byte-stable across runs — snapshot-test friendly.

    Raises:
        ValueError: if `depth` is negative.
        KeyError: if `node_id` isn't in the cached graph.
    """
    if depth < 0:
        raise ValueError(f"depth must be >= 0 (got {depth})")

    root = get_node(graph_id, node_id, cache_root=cache_root)

    visited: dict[str, dict[str, Any]] = {node_id: root}
    edges: set[tuple[str, str]] = set()

    # Upstream BFS: edges point FROM caller TO callee, so a caller
    # of cur_id contributes edge (caller_id, cur_id).
    frontier: deque[tuple[str, int]] = deque([(node_id, 0)])
    while frontier:
        cur_id, cur_depth = frontier.popleft()
        if cur_depth >= depth:
            continue
        for caller in callers_of(graph_id, cur_id, cache_root=cache_root):
            cid = caller["id"]
            edges.add((cid, cur_id))
            if cid not in visited:
                visited[cid] = caller
                frontier.append((cid, cur_depth + 1))

    # Downstream BFS: callee of cur_id contributes edge
    # (cur_id, callee_id).
    frontier = deque([(node_id, 0)])
    while frontier:
        cur_id, cur_depth = frontier.popleft()
        if cur_depth >= depth:
            continue
        for callee in callees_of(graph_id, cur_id, cache_root=cache_root):
            cid = callee["id"]
            edges.add((cur_id, cid))
            if cid not in visited:
                visited[cid] = callee
                frontier.append((cid, cur_depth + 1))

    sorted_ids = sorted(visited)
    alias = {nid: f"n{i}" for i, nid in enumerate(sorted_ids)}

    lines = ["```mermaid", "graph TD"]
    for nid in sorted_ids:
        lines.append(f"    {alias[nid]}[{_quoted_label(_label(nid))}]")
    lines.append(f"    class {alias[node_id]} focus;")
    lines.append("    classDef focus stroke:#f66,stroke-width:3px;")
    for src, dst in sorted(edges):
        lines.append(f"    {alias[src]} --> {alias[dst]}")
    lines.append("```")
    return "\n".join(lines) + "\n"
