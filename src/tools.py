import json
from pathlib import Path
from typing import Any

from trailmark.query.api import QueryEngine

from src.graph.persist import CACHE_ROOT, load_graph, repo_hash, save_graph


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
