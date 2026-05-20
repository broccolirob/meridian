import copy
import itertools
import json
import threading
from pathlib import Path
from typing import Annotated, Any

from langchain_core.tools import InjectedToolArg
from trailmark.models import AnnotationKind
from trailmark.query.api import QueryEngine

from src.graph.persist import (
    CACHE_ROOT,
    load_graph,
    load_parse_root,
    repo_hash,
    save_graph,
    save_parse_root,
)

# Process-level lock for graph-write paths in src/tools.py —
# scope covers every save_graph caller in this module
# (annotate, clear_annotations, AND trailmark_parse).
#
# Without this:
#   - Two threads in dispatch_topo's ThreadPoolExecutor can both
#     load engine v1, both annotate, and the second save_graph
#     overwrites the first's annotation (lost update).
#   - A concurrent trailmark_parse and annotate (theoretical
#     today; no caller does this) could race on save_graph —
#     parse's save and annotate's save would clobber each other.
#     trailmark_parse's save is wrapped with this lock as
#     defense-in-depth.
#
# Atomic writes in save_graph fixed the partial-read race but
# not lost-update. Cross-process locking (fcntl.flock) is
# parked — washable runs in one Python process; multi-process
# is a future concern.
_ANNOTATE_LOCK = threading.Lock()

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
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> str:
    """Parse `repo_path`, persist the engine, return its graph_id.

    The save_graph call is serialized under `_ANNOTATE_LOCK`
    so a future flow that runs parse concurrently with annotate
    can't lose updates. Also writes `parse_root.txt` alongside
    engine.pkl so `read_node_source` can enforce that file
    paths it reads are within the parsed directory — defends
    against symlinked exfiltration via adversarial repos.
    """
    engine = QueryEngine.from_directory(str(repo_path), language=language)
    rh = repo_hash(repo_path)
    with _ANNOTATE_LOCK:
        save_graph(engine, rh, cache_root=cache_root)
        save_parse_root(repo_path, rh, cache_root=cache_root)
    return rh


def graph_summary(
    graph_id: str,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> dict[str, Any]:
    """Return total_nodes / functions / classes / call_edges /
    dependencies / entrypoints for the cached graph."""
    return load_graph(graph_id, cache_root=cache_root).summary()


def list_nodes(
    graph_id: str,
    kind: str | None = None,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
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
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> dict[str, Any]:
    """Return one node by id. Raises KeyError if missing."""
    engine = load_graph(graph_id, cache_root=cache_root)
    data = json.loads(engine.to_json())
    return data["nodes"][node_id]


def callers_of(
    graph_id: str,
    node_id: str,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> list[dict[str, Any]]:
    """Direct callers of `node_id`. Empty list if node is unknown
    or has no callers in this graph."""
    return load_graph(graph_id, cache_root=cache_root).callers_of(node_id)


def callees_of(
    graph_id: str,
    node_id: str,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> list[dict[str, Any]]:
    """Direct callees of `node_id`."""
    return load_graph(graph_id, cache_root=cache_root).callees_of(node_id)


def ancestors_of(
    graph_id: str,
    node_id: str,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> list[dict[str, Any]]:
    """Every function that can transitively reach `node_id` (upward
    slice). Useful for 'who could ever invoke this sink'."""
    return load_graph(graph_id, cache_root=cache_root).ancestors_of(node_id)


def reachable_from(
    graph_id: str,
    node_id: str,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> list[dict[str, Any]]:
    """Every function transitively reachable from `node_id`
    (downward slice). Useful for blast-radius framing."""
    return load_graph(graph_id, cache_root=cache_root).reachable_from(node_id)


def paths_between(
    graph_id: str,
    src: str,
    dst: str,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
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
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> bool:
    """Add an annotation to `node_id`. Persists to the cache.

    `kind` must be one of Trailmark's AnnotationKind values
    (e.g. 'assumption', 'invariant', 'finding'). `source` defaults
    to 'manual'; agents should pass 'llm' or a more specific tag.

    Returns True if added, False if the node rejected it (e.g.,
    duplicate). Persists changes via save_graph."""
    kind_enum = _to_annotation_kind(kind)
    with _ANNOTATE_LOCK:
        # Deep-copy the cached instance before mutating. The
        # mtime-aware lru_cache makes every worker share the
        # SAME QueryEngine reference; concurrent unlocked
        # readers iterate engine._store._graph.annotations (and
        # other internal dicts) while this write runs. Mutating
        # that shared reference in-place would race with reader
        # iteration (RuntimeError: dictionary changed size
        # during iteration). save_graph below bumps file mtime
        # via atomic os.replace, which invalidates the mtime-
        # keyed lru_cache so subsequent readers re-load from
        # disk and see the new state.
        engine = copy.deepcopy(
            load_graph(graph_id, cache_root=cache_root)
        )
        result = engine.annotate(
            node_id, kind_enum, description, source=source
        )
        save_graph(engine, graph_id, cache_root=cache_root)
    return result


def annotations_of(
    graph_id: str,
    node_id: str,
    kind: str | None = None,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
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
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
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
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> bool:
    """Remove annotations from `node_id`. With `kind`, only that kind;
    without, all annotations on the node. Persists via save_graph."""
    kind_enum = _to_annotation_kind(kind) if kind is not None else None
    with _ANNOTATE_LOCK:
        # Deep-copy before mutating (see `annotate` for the
        # reader-race rationale).
        engine = copy.deepcopy(
            load_graph(graph_id, cache_root=cache_root)
        )
        result = engine.clear_annotations(node_id, kind=kind_enum)
        save_graph(engine, graph_id, cache_root=cache_root)
    return result


# Cap on the requested line range for read_file_range. Defends
# against attacker-supplied repos where a node's Trailmark-parsed
# line range claims something absurd like start_line=1,
# end_line=20_000_000. The cap is generous — 10× the largest
# realistic single-contract size (Compound's MoneyMarket ~1500
# lines, MakerDAO MCD ~2000) — so legitimate Solidity passes
# through unchanged, but a multi-GB-file DoS is rejected before
# any I/O happens.
MAX_SOURCE_LINES = 10_000


def read_file_range(
    path: str | Path,
    start_line: int,
    end_line: int,
) -> str:
    """Read lines `[start_line, end_line]` (1-indexed, inclusive)
    from `path`. Out-of-range bounds clamp to the file's actual
    length; reversed ranges return an empty string.

    Bounds memory: requests for more than `MAX_SOURCE_LINES` lines
    are rejected with `ValueError` BEFORE any I/O, and accepted
    requests stream through `itertools.islice` so the in-memory
    set is at most `end_line - start_line + 1` lines regardless of
    file size. This protects the orchestrator against attacker
    repos containing multi-GB files — `f.readlines()` would have
    loaded the whole file before slicing.

    Raises `FileNotFoundError` if the file doesn't exist and
    `ValueError` if `start_line` or `end_line` is < 1, or if the
    requested range exceeds `MAX_SOURCE_LINES`.

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
    requested = end_line - start_line + 1
    if requested > MAX_SOURCE_LINES:
        raise ValueError(
            f"requested range too large: {requested} lines exceeds "
            f"MAX_SOURCE_LINES={MAX_SOURCE_LINES}. Likely an adversarial "
            f"repo or a Trailmark misparse — refusing to read."
        )
    file_path = Path(path)
    with open(file_path, encoding="utf-8") as f:
        # islice(f, start, stop) is 0-indexed exclusive-stop; we
        # want 1-indexed inclusive. Stops at min(end_line, EOF), so
        # an oversized end_line that survived the cap above still
        # only reads up to EOF.
        selected = list(itertools.islice(f, start_line - 1, end_line))
    return "".join(selected)


def read_node_source(
    graph_id: str,
    node_id: str,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> str:
    """Return the source code for `node_id` — its full parsed line
    range, read from the file path Trailmark recorded.

    Agent-safe wrapper around `read_file_range`. The agent never
    names a path, so it can't be prompt-injected into reading
    `/etc/passwd` via tool arguments.

    Also validates that the recorded `file_path` is INSIDE the
    original `parse_root`. Defends against symlink-exfiltration:
    Trailmark's source walker follows file-level symlinks, so
    an adversarial repo can plant `evil.sol -> /etc/passwd` and
    the parsed graph will record `/etc/passwd` as a node's
    file_path. This check rejects any path resolving outside
    parse_root.

    Backward-compat: legacy caches without `parse_root.txt`
    fall back to trust-the-path behavior (`load_parse_root`
    returns None) rather than rejecting wholesale.
    """
    parse_root = load_parse_root(graph_id, cache_root=cache_root)
    node = get_node(graph_id, node_id, cache_root=cache_root)
    loc = node["location"]
    file_path = Path(loc["file_path"]).resolve()
    if parse_root is not None:
        try:
            file_path.relative_to(parse_root.resolve())
        except ValueError:
            raise ValueError(
                f"read_node_source rejected: file_path "
                f"{file_path} escapes parse_root {parse_root}. "
                f"Likely a symlinked file in the parsed repo "
                f"pointing outside the parse tree — possible "
                f"exfiltration attempt."
            ) from None
    return read_file_range(
        file_path, loc["start_line"], loc["end_line"]
    )


def attack_surface(
    graph_id: str,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> list[dict[str, Any]]:
    """Return entrypoint specs — external/public functions
    Trailmark identifies as untrusted-input surfaces.

    Each item dict has keys:
        node_id, trust_level, kind, asset_value, description.

    NOT raw node dicts — unlike `callers_of`/`get_node`, these
    are entrypoint METADATA. Use `get_node(graph_id,
    item['node_id'])` for full node detail. The `description`
    field encodes Trailmark's visibility heuristic
    (e.g., "Solidity external/public function").
    """
    return load_graph(graph_id, cache_root=cache_root).attack_surface()


def entrypoint_paths_to(
    graph_id: str,
    node_id: str,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> list[list[str]]:
    """All simple call paths from any attack-surface entrypoint
    to `node_id`. Each path is a list of node IDs starting at an
    entrypoint and ending at `node_id`.

    RESERVED for chunk 4.5 (RiskSynthesizer). No subagent's tool
    allowlist exposes this today — `NODE_DOCUMENTER_SUBAGENT`
    documents one node, `FLOW_TRACER_SUBAGENT` is dispatched per
    entrypoint by the main agent (it doesn't need to enumerate
    paths from OTHER entrypoints). Phase 4's RiskSynthesizer
    will use this for "show me every entrypoint that reaches
    this dangerous sink" narratives in `risks/*.md`. Tested in
    `tests/test_tools_surface.py` so the wrapper contract stays
    pinned until 4.5 lands.

    Returns `[]` if `node_id` is itself an entrypoint — no
    OTHER entrypoint reaches it via call edges (entrypoint-to-
    entrypoint isn't typically what callers want here).
    """
    return load_graph(
        graph_id, cache_root=cache_root
    ).entrypoint_paths_to(node_id)


def complexity_hotspots(
    graph_id: str,
    threshold: int = 10,
    *,
    cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
) -> list[dict[str, Any]]:
    """Methods with `cyclomatic_complexity >= threshold`.

    RESERVED for chunk 4.5 (RiskSynthesizer). Same status as
    `entrypoint_paths_to` above — no subagent's tool allowlist
    has it today. Phase 4's RiskSynthesizer will use this to
    populate `risks/hotspots.md` alongside
    `render_complexity_heatmap` (the visualization side, see
    `src/render/mermaid.py`). Tested in
    `tests/test_tools_surface.py` so the wrapper contract stays
    pinned until 4.5 lands.

    Returns full Trailmark node dicts (same shape as
    `get_node`). Default threshold=10 matches Trailmark and
    ToB's diagramming-code skill. Tier 1 tops at CC=6 so the
    default returns `[]`; tests use threshold=4 to surface
    meaningful content.

    Raises:
        ValueError: if `threshold` is negative. Cyclomatic
            complexity is non-negative, so a negative threshold
            would match every method — almost certainly a
            caller-side bug. Mirrors the same validation in
            `render_complexity_heatmap`.
    """
    if threshold < 0:
        raise ValueError(f"threshold must be >= 0 (got {threshold})")
    return load_graph(graph_id, cache_root=cache_root).complexity_hotspots(
        threshold=threshold
    )
