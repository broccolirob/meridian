"""Topological ordering of documentable nodes.

Returns nodes (kinds: contract, library, interface, module) in
dependency order — bases before derived — so the dispatch loop can
document parents before children.
"""

import json
import logging
from pathlib import Path
from typing import Annotated

from langchain_core.tools import InjectedToolArg

from src.graph.persist import CACHE_ROOT, load_graph

_log = logging.getLogger(__name__)

# Documentable kinds — methods are documented inside their parent's
# note and don't need their own topo position.
DOCUMENTABLE_KINDS: frozenset[str] = frozenset(
    {"contract", "library", "interface", "module"}
)

# Hard ordering edges — derived must come AFTER base.
# `uses` and `imports` are listed in CHUNKS.md but Trailmark's
# Solidity parser doesn't emit them today. We keep them in the
# accept-list so non-Solidity codebases use them when available.
# `contains` IS in here: module notes wikilink to the contracts they
# contain ([[contracts/UniswapV2Pair|...]]), so contracts must be
# documented before their containing module note for those links to
# resolve at write time. Contract→method `contains` edges are
# automatically dropped because methods aren't in DOCUMENTABLE_KINDS.
HARD_EDGE_KINDS: frozenset[str] = frozenset(
    {"inherits", "implements", "uses", "imports", "contains"}
)


def _build_dep_graph(
    graph_id: str,
    cache_root: Path,
) -> tuple[dict[str, dict], dict[str, set[str]], dict[str, set[str]]]:
    """Internal: load the parsed graph and build the documentable
    dep graph. Returns (nodes, deps, dependents)."""
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

    return nodes, deps, dependents


def topo_levels(
    graph_id: str,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> list[list[str]]:
    """Return documentable node IDs grouped into dependency levels.

    Level N contains nodes whose dependencies are all in levels
    0..N-1. Nodes within a single level have no dependencies among
    themselves — the dispatch loop can process them in parallel
    while waiting for the level to finish before starting the next.

    Within each level, IDs are sorted lexicographically for
    deterministic output.

    Raises `ValueError` on cycle.
    """
    nodes, deps, dependents = _build_dep_graph(graph_id, cache_root)
    in_degree = {nid: len(deps[nid]) for nid in nodes}

    levels: list[list[str]] = []
    remaining = set(in_degree)
    while remaining:
        current = sorted(nid for nid in remaining if in_degree[nid] == 0)
        if not current:
            raise ValueError(
                f"cycle in documentable-node dependency graph; "
                f"unresolved: {sorted(remaining)}"
            )
        levels.append(current)
        for nid in current:
            remaining.discard(nid)
            for dep in dependents[nid]:
                if dep in remaining:
                    in_degree[dep] -= 1
    return levels


def topo_order(
    graph_id: str,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> list[str]:
    """Return documentable node IDs in dependency order — bases
    first, derived contracts last.

    Computed by flattening `topo_levels`: within a level, nodes are
    sorted lexicographically; across levels, level 0 comes first.

    `calls` is a soft edge (not used for ordering). Methods are NOT
    in the output — they're documented inside their parent's note.

    Phantom inheritance targets (Trailmark's `inferred`-confidence
    cross-file references like `<file>:<BareName>` that don't match
    any real node) get resolved by bare-name lookup against the
    documentable set. Ambiguous matches log a warning and drop the
    edge (no dependency constraint between those nodes).

    Raises `ValueError` if a cycle is detected.
    """
    return [
        nid
        for level in topo_levels(graph_id, cache_root=cache_root)
        for nid in level
    ]


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

    Ambiguous matches log a warning — a real codebase with name
    collisions (e.g., a vendored `ERC20` alongside a custom one)
    will silently drop dependency edges otherwise, producing wrong
    ordering with no signal.
    """
    if tgt in nodes:
        return tgt
    bare = tgt.rsplit(":", 1)[-1]
    candidates = by_name.get(bare, set())
    if len(candidates) == 1:
        return next(iter(candidates))
    if len(candidates) > 1:
        _log.warning(
            "dropping topo edge: target %r matches %d nodes by bare "
            "name %r (%s) — ambiguous, can't pick one. Result: no "
            "dependency constraint between these nodes.",
            tgt,
            len(candidates),
            bare,
            sorted(candidates),
        )
    return None
