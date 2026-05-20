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

import json
import logging
from collections import deque
from pathlib import Path
from typing import Any

from src.graph.persist import CACHE_ROOT, load_graph
from src.render.mermaid_styles import (
    COMPLEXITY_CLASSDEFS,
    FOCUS_CLASSDEF,
    bucket_for_complexity,
)
from src.tools import callees_of, callers_of, get_node

_log = logging.getLogger(__name__)


def _bare_name(node_id: str) -> str:
    """Bare name = the tail after the last `:`. For a contract
    `contracts.X:UniswapV2Pair` → `UniswapV2Pair`; for a method
    `contracts.X:UniswapV2Pair.swap` → `UniswapV2Pair.swap`.
    Qualifying methods this way avoids collisions when two
    contracts each have a method named e.g. `swap`."""
    return node_id.rsplit(":", 1)[-1]


def _containing_class(node_id: str) -> str:
    """Containing-class name. `module:Contract.method` →
    `Contract`. `module:Contract` (top-level) → `Contract`."""
    bare = _bare_name(node_id)
    return bare.rsplit(".", 1)[0]


def _method_or_name(node_id: str) -> str:
    """Sequence-arrow label. `module:Contract.method` → `method`.
    `module:Contract` (top-level, no `.` in the bare name) →
    the bare name (caller's intent must be the whole node)."""
    bare = _bare_name(node_id)
    if "." in bare:
        return bare.rsplit(".", 1)[1]
    return bare


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
        lines.append(f"    {alias[nid]}[{_quoted_label(_bare_name(nid))}]")
    lines.append(f"    class {alias[node_id]} focus;")
    lines.append(f"    {FOCUS_CLASSDEF};")
    for src, dst in sorted(edges):
        lines.append(f"    {alias[src]} --> {alias[dst]}")
    lines.append("```")
    return "\n".join(lines) + "\n"


# Edge styles for Mermaid classDiagram. Standard UML:
#   <|--  solid arrow → contract inheritance (extends)
#   <|..  dashed arrow → interface implementation (realizes)
_INHERIT_EDGE = "<|--"
_IMPLEMENT_EDGE = "<|.."

# Trailmark NodeKind values that mean "this is an interface, draw
# implements style". Everything else (contract, library, trait)
# uses inherits style.
_INTERFACE_KINDS: frozenset[str] = frozenset({"interface"})


def _resolve_phantom(
    target_id: str,
    nodes_by_id: dict[str, dict[str, Any]],
    nodes_by_bare_name: dict[str, set[str]],
) -> dict[str, Any] | None:
    """Map a (possibly phantom) inheritance target to a real node.

    Trailmark's Solidity parser frequently emits `inherits` edges
    where the target's ID is prefixed with the SOURCE file's
    module path (e.g. edge `contracts.Pair:Pair → contracts.Pair:
    UniswapV2ERC20`, where the real ERC20 lives at
    `contracts.UniswapV2ERC20:UniswapV2ERC20`). When the literal
    ID isn't a node, fall back to bare-name lookup; exactly one
    match returns that node. Zero or many matches returns None
    (caller defaults to solid-inherits styling).

    Mirrors src/graph/topo.py::_resolve_target's strategy but
    returns the node dict (not just the ID) so the caller can
    inspect `kind` to pick the right edge style.
    """
    if target_id in nodes_by_id:
        return nodes_by_id[target_id]
    bare = _bare_name(target_id)
    candidates = nodes_by_bare_name.get(bare, set())
    if len(candidates) == 1:
        return nodes_by_id[next(iter(candidates))]
    if len(candidates) > 1:
        _log.warning(
            "inheritance edge to %r: %d nodes match bare name %r "
            "(%s) — ambiguous, defaulting to inherits style.",
            target_id,
            len(candidates),
            bare,
            sorted(candidates),
        )
    return None


def render_inheritance(
    graph_id: str,
    node_id: str,
    *,
    cache_root: Path = CACHE_ROOT,
) -> str:
    """Render a 1-hop Mermaid classDiagram for `node_id`'s
    inheritance neighborhood.

    Shows:
    - Direct PARENTS (contracts/interfaces this contract inherits
      from), with `<|--` for contracts and `<|..` for interfaces.
    - Direct CHILDREN (contracts that inherit from this one), same
      styling.

    Edge direction in Mermaid classDiagram syntax: `Base <|--
    Derived` reads "Derived extends Base". We follow that
    convention.

    Solidity's `is` keyword covers both inheritance and interface
    implementation — we disambiguate by checking the target
    node's kind. Phantom targets (Trailmark's `inferred`
    cross-file IDs) get resolved by bare-name lookup; unresolved
    targets default to inherits style.

    Returns a fenced Mermaid block. The diagram always declares
    at least a `class <Name>` for the focus, even when there are
    no inheritance edges — so the consumer (chunk 3.5) can embed
    without checking for empty output.

    Raises:
        KeyError: if `node_id` isn't in the cached graph.
    """
    engine = load_graph(graph_id, cache_root=cache_root)
    data = json.loads(engine.to_json())
    nodes_by_id: dict[str, dict[str, Any]] = data["nodes"]
    if node_id not in nodes_by_id:
        raise KeyError(node_id)

    nodes_by_bare_name: dict[str, set[str]] = {}
    for nid in nodes_by_id:
        nodes_by_bare_name.setdefault(_bare_name(nid), set()).add(nid)

    focus_bare = _bare_name(node_id)
    focus_kind = nodes_by_id[node_id]["kind"]
    focus_edge_style = (
        _IMPLEMENT_EDGE
        if focus_kind in _INTERFACE_KINDS
        else _INHERIT_EDGE
    )

    parents: list[tuple[str, str]] = []
    children: list[tuple[str, str]] = []

    for e in data["edges"]:
        if e["kind"] != "inherits":
            continue
        src, tgt = e["source"], e["target"]
        if src == node_id:
            target_node = _resolve_phantom(
                tgt, nodes_by_id, nodes_by_bare_name
            )
            target_kind = (
                target_node["kind"] if target_node else "contract"
            )
            style = (
                _IMPLEMENT_EDGE
                if target_kind in _INTERFACE_KINDS
                else _INHERIT_EDGE
            )
            parents.append((_bare_name(tgt), style))
        else:
            if _bare_name(tgt) != focus_bare:
                continue
            # Guard against bare-name collisions: confirm the
            # edge's target really resolves to US, not some other
            # node that happens to share our bare name.
            target_node = _resolve_phantom(
                tgt, nodes_by_id, nodes_by_bare_name
            )
            if target_node is not None and target_node["id"] != node_id:
                continue
            children.append((_bare_name(src), focus_edge_style))

    class_names: set[str] = {focus_bare}
    class_names.update(name for name, _ in parents)
    class_names.update(name for name, _ in children)

    lines = ["```mermaid", "classDiagram"]
    for name in sorted(class_names):
        lines.append(f"    class {name}")
    for parent_name, style in sorted(parents):
        lines.append(f"    {parent_name} {style} {focus_bare}")
    for child_name, style in sorted(children):
        lines.append(f"    {focus_bare} {style} {child_name}")
    lines.append("```")
    return "\n".join(lines) + "\n"


def render_sequence(
    graph_id: str,
    path: list[str],
    *,
    cache_root: Path = CACHE_ROOT,
) -> str:
    """Render a Mermaid `sequenceDiagram` of a single call chain.

    `path` is a list of node IDs in call order: `path[0]` is the
    initial caller, `path[i]` calls `path[i+1]`. Each consecutive
    pair produces one arrow. The participants are the unique
    containing-class names, declared in first-appearance order so
    the visual reads left-to-right as the call narrative.

    Self-calls (consecutive nodes with the same containing class)
    render as Mermaid self-loops (`A->>A: method`).

    Returns a fenced Mermaid block. A single-node path emits just
    the participant declaration; no arrows.

    The renderer is path-agnostic: it does NOT verify consecutive
    nodes are connected by a `calls` edge. That contract belongs
    to the caller (typically `paths_between`). This separation
    keeps the renderer reusable for hypothesized paths during
    investigation.

    Raises:
        ValueError: if `path` is empty.
        KeyError: if any node ID in `path` isn't in the cached
            graph (validated as one load_graph round-trip, so
            the cost is constant in path length).
    """
    if not path:
        raise ValueError("path must be non-empty")

    engine = load_graph(graph_id, cache_root=cache_root)
    data = json.loads(engine.to_json())
    nodes_by_id: dict[str, dict[str, Any]] = data["nodes"]
    for nid in path:
        if nid not in nodes_by_id:
            raise KeyError(nid)

    # Ordered-unique set: dict preserves insertion order (Python
    # 3.7+), so iterating keys gives first-appearance ordering.
    participants: dict[str, None] = {}
    for nid in path:
        participants[_containing_class(nid)] = None

    lines = ["```mermaid", "sequenceDiagram"]
    for name in participants:
        lines.append(f"    participant {name}")
    for i in range(len(path) - 1):
        src_part = _containing_class(path[i])
        dst_part = _containing_class(path[i + 1])
        dst_label = _method_or_name(path[i + 1])
        lines.append(f"    {src_part}->>{dst_part}: {dst_label}")
    lines.append("```")
    return "\n".join(lines) + "\n"


def render_complexity_heatmap(
    graph_id: str,
    *,
    threshold: int = 5,
    cache_root: Path = CACHE_ROOT,
) -> str:
    """Render a Mermaid flowchart colored by cyclomatic
    complexity buckets.

    Only methods with `cyclomatic_complexity >= threshold` are
    included. Each is tagged `:::low`/`:::medium`/`:::high` based
    on its CC value (boundaries in `mermaid_styles`). Call edges
    between included methods are drawn.

    Default threshold=5 matches ToB's diagramming-code skill —
    surfaces only methods worth attention. Tests use threshold=4
    to demonstrate multi-bucket output on Tier 1, which tops out
    at CC=6.

    Returns a fenced Mermaid block. If no methods meet the
    threshold, emits a flowchart with a single explanatory note
    rather than empty output.

    Raises:
        ValueError: if `threshold` is negative.
    """
    if threshold < 0:
        raise ValueError(f"threshold must be >= 0 (got {threshold})")

    engine = load_graph(graph_id, cache_root=cache_root)
    data = json.loads(engine.to_json())
    nodes_by_id: dict[str, dict[str, Any]] = data["nodes"]

    # None-CC methods are excluded because threshold semantics
    # mean "at least this much complexity" — None can't satisfy
    # that. (The bucket function treats None as low for other
    # call sites where exclusion isn't desired.)
    included: dict[str, dict[str, Any]] = {}
    for nid, n in nodes_by_id.items():
        if n["kind"] != "method":
            continue
        cc = n.get("cyclomatic_complexity")
        if cc is None or cc < threshold:
            continue
        included[nid] = n

    lines = ["```mermaid", "flowchart TB"]
    if not included:
        lines.append(
            f'    empty["No methods with CC >= {threshold}"]'
        )
        lines.append("```")
        return "\n".join(lines) + "\n"

    sorted_ids = sorted(included)
    alias = {nid: f"n{i}" for i, nid in enumerate(sorted_ids)}

    for nid in sorted_ids:
        n = included[nid]
        cc = n["cyclomatic_complexity"]
        bucket = bucket_for_complexity(cc)
        bare = _bare_name(nid)
        label = _quoted_label(f"{bare}, CC={cc}")
        lines.append(f"    {alias[nid]}[{label}]:::{bucket}")

    edges: set[tuple[str, str]] = set()
    for e in data["edges"]:
        if e["kind"] != "calls":
            continue
        if e["source"] in included and e["target"] in included:
            edges.add((e["source"], e["target"]))
    for src, dst in sorted(edges):
        lines.append(f"    {alias[src]} --> {alias[dst]}")

    for classdef in COMPLEXITY_CLASSDEFS:
        lines.append(f"    {classdef}")

    lines.append("```")
    return "\n".join(lines) + "\n"


def render_containment(
    graph_id: str,
    node_id: str,
    *,
    cache_root: Path = CACHE_ROOT,
) -> str:
    """Render a Mermaid classDiagram showing `node_id`'s direct
    methods as a class body.

    `node_id` must be a contract/library/interface/module node.
    Methods are discovered via `contains` edges where the source
    is the focus node. Members are listed alphabetically by name,
    each as `+<name>()` (visibility is `+` uniformly because
    Trailmark's Solidity parser doesn't yet populate the
    visibility field).

    Methods with a return type get the type name appended:
    `+swap() bool`. Trailmark's `return_type` field is a dict
    (`{"name": str, "module": str | None, "generic_args": list}`)
    when present and None otherwise — we extract `name`.

    Returns a fenced Mermaid block. A focus with no methods emits
    a `class <Name>` block with no members.

    Raises:
        KeyError: if `node_id` isn't in the cached graph.
    """
    engine = load_graph(graph_id, cache_root=cache_root)
    data = json.loads(engine.to_json())
    nodes_by_id: dict[str, dict[str, Any]] = data["nodes"]
    if node_id not in nodes_by_id:
        raise KeyError(node_id)

    focus_bare = _bare_name(node_id)
    member_ids = [
        e["target"]
        for e in data["edges"]
        if e["kind"] == "contains" and e["source"] == node_id
    ]
    # `contains` edges also describe module → contract; restrict
    # to method members so the class body doesn't list contracts.
    methods = [
        nodes_by_id[mid]
        for mid in member_ids
        if mid in nodes_by_id and nodes_by_id[mid]["kind"] == "method"
    ]
    methods.sort(key=lambda m: m["name"])

    lines = [
        "```mermaid",
        "classDiagram",
        f"    class {focus_bare} {{",
    ]
    for m in methods:
        rt = m.get("return_type")
        rt_name = rt.get("name") if isinstance(rt, dict) else rt
        rt_part = f" {rt_name}" if rt_name else ""
        lines.append(f"        +{m['name']}(){rt_part}")
    lines.append("    }")
    lines.append("```")
    return "\n".join(lines) + "\n"
