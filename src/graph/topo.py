"""Topological ordering for chunk 2.1.

Returns documentable nodes (kinds: contract, library, interface,
module) in dependency order — bases before derived — so the dispatch
loop in chunk 2.3 can document parents before children.
"""

import heapq
import json
from pathlib import Path

from src.graph.persist import CACHE_ROOT, load_graph

# Documentable kinds — methods are documented inside their parent's
# note (chunk 1.3 design) and don't need their own topo position.
DOCUMENTABLE_KINDS: frozenset[str] = frozenset(
    {"contract", "library", "interface", "module"}
)

# Hard ordering edges — derived must come AFTER base.
# `uses` and `imports` are listed in CHUNKS.md but Trailmark's
# Solidity parser doesn't emit them today. We keep them in the
# accept-list so non-Solidity codebases will use them when available.
# `contains` is intentionally NOT here: module-contains-contract is
# structural, not a documentation dependency.
HARD_EDGE_KINDS: frozenset[str] = frozenset(
    {"inherits", "implements", "uses", "imports"}
)


def topo_order(
    graph_id: str,
    *,
    cache_root: Path = CACHE_ROOT,
) -> list[str]:
    """Return documentable node IDs in dependency order.

    Bases come first, derived contracts last. `calls` is soft (not
    used for ordering). `contains` is structural (not used). Methods
    are NOT in the output — they're documented inside their parent.

    Phantom inheritance targets (Trailmark's `inferred`-confidence
    cross-file references like `<file>:<BareName>` that don't match
    any real node) get resolved by bare-name lookup against the
    documentable set. Ambiguous matches (two contracts with the same
    simple name) drop the edge with no constraint between them.

    Deterministic: at each step the lexicographically smallest
    available node is emitted next.

    Raises `ValueError` if a cycle is detected among the resolved
    hard edges.
    """
    engine = load_graph(graph_id, cache_root=cache_root)
    data = json.loads(engine.to_json())

    nodes: dict[str, dict] = {
        nid: n
        for nid, n in data["nodes"].items()
        if n["kind"] in DOCUMENTABLE_KINDS
    }

    by_name: dict[str, set[str]] = {}
    for nid, n in nodes.items():
        by_name.setdefault(n["name"], set()).add(nid)

    deps: dict[str, set[str]] = {nid: set() for nid in nodes}
    for e in data["edges"]:
        if e["kind"] not in HARD_EDGE_KINDS:
            continue
        src = e["source"]
        tgt = e["target"]
        if src not in nodes:
            continue
        resolved = _resolve_target(tgt, nodes, by_name)
        if resolved is None or resolved == src:
            continue
        deps[src].add(resolved)

    dependents: dict[str, set[str]] = {nid: set() for nid in nodes}
    for src, ds in deps.items():
        for d in ds:
            dependents[d].add(src)

    in_degree = {nid: len(deps[nid]) for nid in nodes}
    heap = [nid for nid, deg in in_degree.items() if deg == 0]
    heapq.heapify(heap)

    order: list[str] = []
    while heap:
        nid = heapq.heappop(heap)
        order.append(nid)
        for dep in sorted(dependents[nid]):
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                heapq.heappush(heap, dep)

    if len(order) != len(nodes):
        cycle_nodes = sorted(set(nodes) - set(order))
        raise ValueError(
            f"cycle in documentable-node dependency graph; "
            f"unresolved: {cycle_nodes}"
        )
    return order


def _resolve_target(
    tgt: str,
    nodes: dict[str, dict],
    by_name: dict[str, set[str]],
) -> str | None:
    """Return the actual node ID for an edge target, or None if
    unresolvable / ambiguous.

    1. If `tgt` is itself a documentable node, return it.
    2. Otherwise extract the bare name (last segment after the final
       `:`) and try to match a unique documentable node by name.
    3. If zero matches or ambiguous (>1 match), return None.
    """
    if tgt in nodes:
        return tgt
    bare = tgt.rsplit(":", 1)[-1]
    candidates = by_name.get(bare, set())
    if len(candidates) == 1:
        return next(iter(candidates))
    return None
