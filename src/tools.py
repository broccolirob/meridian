import json
from pathlib import Path
from typing import Any

from trailmark.models import AnnotationKind
from trailmark.query.api import QueryEngine

from src.graph.persist import CACHE_ROOT, load_graph, repo_hash, save_graph

# Pre-computed for the error-message valid-kinds list. We expose
# `str` in our public signature for LLM-friendliness, but convert to
# the AnnotationKind enum before calling Trailmark — Trailmark's
# annotate() permissively accepts strings on the write path, but its
# read path (annotations_of, nodes_with_annotation, clear_annotations)
# assumes stored kinds are enums and crashes on `.value` access.
# Converting at the boundary keeps both sides happy.
_VALID_ANNOTATION_KINDS: frozenset[str] = frozenset(
    k.value for k in AnnotationKind
)


def _to_annotation_kind(kind: str) -> AnnotationKind:
    try:
        return AnnotationKind(kind)
    except ValueError:
        valid = ", ".join(sorted(_VALID_ANNOTATION_KINDS))
        raise ValueError(
            f"invalid annotation kind {kind!r}: expected one of {valid}"
        ) from None


def trailmark_parse(
    repo_path: str | Path,
    language: str = "auto",
    *,
    cache_root: Path = CACHE_ROOT,
) -> str:
    """Parse `repo_path`, persist the engine, return its graph_id."""
    engine = QueryEngine.from_directory(str(repo_path), language=language)
    rh = repo_hash(repo_path)
    save_graph(engine, rh, cache_root=cache_root)
    return rh


def graph_summary(
    graph_id: str,
    *,
    cache_root: Path = CACHE_ROOT,
) -> dict[str, Any]:
    """Return total_nodes / functions / classes / call_edges /
    dependencies / entrypoints for the cached graph."""
    return load_graph(graph_id, cache_root=cache_root).summary()


def list_nodes(
    graph_id: str,
    kind: str | None = None,
    *,
    cache_root: Path = CACHE_ROOT,
) -> list[dict[str, Any]]:
    """Return all node dicts, optionally filtered by kind
    (e.g. 'contract', 'method', 'library', 'module')."""
    engine = load_graph(graph_id, cache_root=cache_root)
    data = json.loads(engine.to_json())
    nodes = list(data["nodes"].values())
    if kind is not None:
        nodes = [n for n in nodes if n["kind"] == kind]
    return nodes


def get_node(
    graph_id: str,
    node_id: str,
    *,
    cache_root: Path = CACHE_ROOT,
) -> dict[str, Any]:
    """Return one node by id. Raises KeyError if missing."""
    engine = load_graph(graph_id, cache_root=cache_root)
    data = json.loads(engine.to_json())
    return data["nodes"][node_id]


def callers_of(
    graph_id: str,
    node_id: str,
    *,
    cache_root: Path = CACHE_ROOT,
) -> list[dict[str, Any]]:
    """Direct callers of `node_id`. Empty list if node is unknown
    or has no callers in this graph."""
    return load_graph(graph_id, cache_root=cache_root).callers_of(node_id)


def callees_of(
    graph_id: str,
    node_id: str,
    *,
    cache_root: Path = CACHE_ROOT,
) -> list[dict[str, Any]]:
    """Direct callees of `node_id`."""
    return load_graph(graph_id, cache_root=cache_root).callees_of(node_id)


def ancestors_of(
    graph_id: str,
    node_id: str,
    *,
    cache_root: Path = CACHE_ROOT,
) -> list[dict[str, Any]]:
    """Every function that can transitively reach `node_id` (upward
    slice). Useful for 'who could ever invoke this sink'."""
    return load_graph(graph_id, cache_root=cache_root).ancestors_of(node_id)


def reachable_from(
    graph_id: str,
    node_id: str,
    *,
    cache_root: Path = CACHE_ROOT,
) -> list[dict[str, Any]]:
    """Every function transitively reachable from `node_id`
    (downward slice). Useful for blast-radius framing."""
    return load_graph(graph_id, cache_root=cache_root).reachable_from(node_id)


def paths_between(
    graph_id: str,
    src: str,
    dst: str,
    *,
    cache_root: Path = CACHE_ROOT,
) -> list[list[str]]:
    """All simple call paths from `src` to `dst`. Each path is a list
    of node IDs starting with `src` and ending with `dst`."""
    return load_graph(graph_id, cache_root=cache_root).paths_between(src, dst)


def annotate(
    graph_id: str,
    node_id: str,
    kind: str,
    description: str,
    *,
    source: str = "manual",
    cache_root: Path = CACHE_ROOT,
) -> bool:
    """Add an annotation to `node_id`. Persists to the cache.

    `kind` must be one of Trailmark's AnnotationKind values
    (e.g. 'assumption', 'invariant', 'finding'). `source` defaults
    to 'manual'; agents should pass 'llm' or a more specific tag.

    Returns True if added, False if the node rejected it (e.g.,
    duplicate). Persists changes via save_graph."""
    kind_enum = _to_annotation_kind(kind)
    engine = load_graph(graph_id, cache_root=cache_root)
    result = engine.annotate(node_id, kind_enum, description, source=source)
    save_graph(engine, graph_id, cache_root=cache_root)
    return result


def annotations_of(
    graph_id: str,
    node_id: str,
    kind: str | None = None,
    *,
    cache_root: Path = CACHE_ROOT,
) -> list[dict[str, Any]]:
    """All annotations on `node_id`, optionally filtered by kind.
    Each dict has 'kind', 'description', 'source' keys."""
    kind_enum = _to_annotation_kind(kind) if kind is not None else None
    engine = load_graph(graph_id, cache_root=cache_root)
    return engine.annotations_of(node_id, kind=kind_enum)


def nodes_with_annotation(
    graph_id: str,
    kind: str,
    *,
    cache_root: Path = CACHE_ROOT,
) -> list[dict[str, Any]]:
    """Every node carrying an annotation of `kind`. Returns full node
    dicts (same shape as `list_nodes` output), not bare IDs."""
    kind_enum = _to_annotation_kind(kind)
    engine = load_graph(graph_id, cache_root=cache_root)
    return engine.nodes_with_annotation(kind_enum)


def clear_annotations(
    graph_id: str,
    node_id: str,
    kind: str | None = None,
    *,
    cache_root: Path = CACHE_ROOT,
) -> bool:
    """Remove annotations from `node_id`. With `kind`, only that kind;
    without, all annotations on the node. Persists via save_graph."""
    kind_enum = _to_annotation_kind(kind) if kind is not None else None
    engine = load_graph(graph_id, cache_root=cache_root)
    result = engine.clear_annotations(node_id, kind=kind_enum)
    save_graph(engine, graph_id, cache_root=cache_root)
    return result


def read_file_range(
    path: str | Path,
    start_line: int,
    end_line: int,
) -> str:
    """Read lines `[start_line, end_line]` (1-indexed, inclusive)
    from `path`. Out-of-range bounds clamp to the file's actual
    length; reversed ranges return an empty string.

    Raises `FileNotFoundError` if the file doesn't exist and
    `ValueError` if `start_line` or `end_line` is < 1.

    Note: This primitive accepts ARBITRARY paths and is therefore
    **not on any subagent's tool list**. Adversarial Solidity comments
    in target repos could prompt-inject the agent into reading
    sensitive local files. Agents go through `read_node_source()`
    instead, which derives the path from trusted Trailmark node
    metadata.
    """
    if start_line < 1 or end_line < 1:
        raise ValueError(
            f"line numbers must be >= 1 "
            f"(got start={start_line}, end={end_line})"
        )
    if end_line < start_line:
        return ""
    file_path = Path(path)
    with open(file_path, encoding="utf-8") as f:
        lines = f.readlines()
    return "".join(lines[start_line - 1 : end_line])


def read_node_source(
    graph_id: str,
    node_id: str,
    *,
    cache_root: Path = CACHE_ROOT,
) -> str:
    """Return the source code for `node_id` — its full parsed line
    range, read from the file path Trailmark recorded.

    This is the agent-safe wrapper around `read_file_range`: the
    agent never names a path, so it can't be prompt-injected into
    reading `/etc/passwd` or similar via adversarial source comments.
    The path comes from the parsed graph, which is trusted by
    construction (we ran `trailmark_parse` over a known directory).
    """
    node = get_node(graph_id, node_id, cache_root=cache_root)
    loc = node["location"]
    return read_file_range(
        loc["file_path"], loc["start_line"], loc["end_line"]
    )
