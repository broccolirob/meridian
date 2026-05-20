import logging
import os
import threading
from pathlib import Path
from typing import Any

import yaml

from src.graph.persist import CACHE_ROOT
from src.render.mermaid import (
    render_call_graph,
    render_inheritance,
    render_sequence,
)
from src.tools import get_node, list_nodes

_log = logging.getLogger(__name__)

VAULT_SUBDIRS: tuple[str, ...] = (
    "_meta",
    "contracts",
    "interfaces",
    "libraries",
    "flows",
    "diagrams",
    "attack-surface",
    "risks",
)


def ensure_vault(vault_path: str | Path) -> Path:
    """Create the canonical washable vault skeleton at `vault_path`.
    Idempotent. Returns the resolved vault Path."""
    vault = Path(vault_path)
    vault.mkdir(parents=True, exist_ok=True)
    for sub in VAULT_SUBDIRS:
        (vault / sub).mkdir(exist_ok=True)
    return vault


def write_obsidian_note(
    vault_path: str | Path,
    rel_path: str,
    frontmatter: dict[str, Any],
    body: str,
) -> Path:
    """Write an Obsidian note at `<vault>/<rel_path>`.

    Frontmatter is serialized as YAML between `---` markers. If
    `frontmatter` is empty, no marker block is emitted. The body is
    written verbatim. Parent directories under the vault are created
    on demand. Returns the absolute path written.

    Atomic-write semantics (chunk 3.13): the file is written to
    `.<name>.tmp.<pid>.<tid>` in the same directory, then
    `os.replace`d into the final path. Vault scanners
    (`write_root_moc`, `validate_vault.py`) and Obsidian's file
    watcher always observe EITHER the previous contents OR the
    complete new contents — never a partial file. Mirrors the
    pattern in `src/graph/persist.py::save_graph`.
    """
    vault = Path(vault_path)
    target = vault / rel_path
    try:
        target.resolve().relative_to(vault.resolve())
    except ValueError as e:
        raise ValueError(
            f"rel_path escapes vault: {rel_path!r}"
        ) from e
    target.parent.mkdir(parents=True, exist_ok=True)

    parts: list[str] = []
    if frontmatter:
        yaml_block = yaml.safe_dump(
            frontmatter, sort_keys=False, default_flow_style=False
        )
        parts.append("---\n")
        parts.append(yaml_block)
        parts.append("---\n\n")
    parts.append(body)
    if not body.endswith("\n"):
        parts.append("\n")
    content = "".join(parts)

    # PID + thread ID in the tmp name so concurrent ThreadPool
    # workers in the same process can't clobber each other's tmp.
    # (save_graph uses PID only because _ANNOTATE_LOCK serializes
    # its writes; write_obsidian_note has no such lock.)
    tmp_path = target.parent / (
        f".{target.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    )
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, target)
    except Exception:
        # Intentionally broad (chunk 3.16 I17 cleanup left this
        # one alone): the catch exists to clean up the tmp file
        # on ANY failure path, then re-raises. Coding bugs
        # (TypeError, AttributeError) still surface to the
        # caller because of the `raise` below — the broad catch
        # is a cleanup hook, not a swallower.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    return target


KIND_TO_FOLDER: dict[str, str] = {
    "contract": "contracts",
    "library": "libraries",
    "interface": "interfaces",
    "trait": "interfaces",
    "class": "contracts",
    "struct": "contracts",
    "enum": "contracts",
    "namespace": "contracts",
    "function": "contracts",
    "module": "_meta",
}


def _disambiguated_path(
    node: dict[str, Any],
    graph_id: str,
    *,
    cache_root: Path = CACHE_ROOT,
) -> str:
    """Vault-relative path stem (no `.md`) for a node note.

    Returns `<folder>/<bare>` when the bare name is unique among
    nodes routing to the same folder, or
    `<folder>/<module>.<bare>` when a collision exists. Per-folder
    (not per-kind) because `KIND_TO_FOLDER` maps multiple kinds to
    the same folder (contract/struct/enum → contracts).

    Used by both `render_and_write_node_note` (the file's path)
    and `resolve_wikilink` (the link target) so they stay in
    sync — wikilinks always point at whatever filename the writer
    picks.
    """
    kind = node["kind"]
    folder = KIND_TO_FOLDER.get(kind, "contracts")
    bare = node["name"]

    same_folder_kinds = {
        k for k, f in KIND_TO_FOLDER.items() if f == folder
    }
    others = [
        n
        for n in list_nodes(graph_id, cache_root=cache_root)
        if n["kind"] in same_folder_kinds
        and n["name"] == bare
        and n["id"] != node["id"]
    ]
    if not others:
        return f"{folder}/{bare}"
    module = node["id"].rsplit(":", 1)[0]
    return f"{folder}/{module}.{bare}"


def resolve_wikilink(
    graph_id: str,
    node_id: str,
    *,
    cache_root: Path = CACHE_ROOT,
) -> str:
    """Return an Obsidian wikilink string for `node_id`.

    Top-level kinds (contract, library, interface, etc.) point at
    their own note: ``[[contracts/Pair|Pair]]``.

    Methods point at their parent's note with a qualified display
    label: ``[[contracts/Pair|Pair.swap]]``.

    When two nodes route to the same folder with the same bare
    name (collision case from chunk 3.10), the link target gets
    qualified with the module prefix
    (``[[contracts/contracts.A.Vault|Vault]]``). The display
    label stays bare.

    Raises `KeyError` if `node_id` (or, for methods, its parent) is
    not in the cached graph.
    """
    node = get_node(graph_id, node_id, cache_root=cache_root)
    kind = node["kind"]
    name = node["name"]

    if kind == "method":
        parent_id = node_id.rsplit(".", 1)[0]
        parent = get_node(graph_id, parent_id, cache_root=cache_root)
        method_name = name.rsplit(".", 1)[-1]
        parent_path = _disambiguated_path(
            parent, graph_id, cache_root=cache_root
        )
        return f"[[{parent_path}|{parent['name']}.{method_name}]]"

    path = _disambiguated_path(node, graph_id, cache_root=cache_root)
    return f"[[{path}|{name}]]"


_VISIBILITY_ORDER: tuple[str, ...] = ("external", "public", "internal", "private")


def _bare_name_from_wikilink(wikilink: str) -> str:
    """Extract the display label from `[[path|display]]` (or the path
    portion if no `|` is present)."""
    inner = wikilink.strip("[]")
    if "|" in inner:
        return inner.split("|", 1)[1]
    return inner.rsplit("/", 1)[-1]


def _render_overview(body: str) -> str:
    if body.strip():
        return f"## Overview\n\n{body.rstrip()}\n"
    return "## Overview\n\n_Overview not yet written._\n"


def _render_link_list(items: list[str], empty_msg: str) -> str:
    if not items:
        return f"{empty_msg}\n"
    return "".join(f"- {item}\n" for item in items)


def _render_graph_context(graph_ctx: dict[str, Any]) -> str:
    inheritance_diagram = graph_ctx.get("inheritance_mermaid", "")
    call_graph_diagram = graph_ctx.get("call_graph_mermaid", "")
    inherits = graph_ctx.get("inherits") or []
    implements = graph_ctx.get("implements") or []
    uses = graph_ctx.get("uses") or []
    callers = graph_ctx.get("callers") or []
    callees = graph_ctx.get("callees") or []

    parts = ["## Graph context\n"]
    # Visual structure first (auditors scan diagrams before lists).
    if inheritance_diagram:
        parts.append(
            f"\n### Inheritance diagram\n\n{inheritance_diagram}"
        )
    if call_graph_diagram:
        parts.append(f"\n### Call graph\n\n{call_graph_diagram}")
    parts.append(
        "\n### Inheritance\n\n"
        f"{_render_link_list(inherits, '_No inheritance edges._')}\n"
        "### Implements\n\n"
        f"{_render_link_list(implements, '_Implements nothing._')}\n"
        "### Uses\n\n"
        f"{_render_link_list(uses, '_No library uses._')}\n"
        "### Callers\n\n"
        f"{_render_link_list(callers, '_No callers in this graph._')}\n"
        "### Callees\n\n"
        f"{_render_link_list(callees, '_No callees in this graph._')}"
    )
    return "".join(parts)


def _render_state(node: dict[str, Any]) -> str:
    loc = node["location"]
    return (
        "## State\n\n"
        "_Trailmark does not extract state variables yet — read the "
        f"source at `{loc['file_path']}` lines "
        f"`{loc['start_line']}`-`{loc['end_line']}`._\n"
    )


def _render_functions(graph_ctx: dict[str, Any]) -> str:
    functions = graph_ctx.get("functions") or []
    if not functions:
        body = "_No functions._\n"
        return "## Functions\n\n" + body

    by_visibility: dict[str, list[dict[str, Any]]] = {
        v: [] for v in _VISIBILITY_ORDER
    }
    for fn in functions:
        vis = fn.get("visibility") or "internal"
        by_visibility.setdefault(vis, []).append(fn)

    out = ["## Functions\n"]
    for vis in _VISIBILITY_ORDER:
        out.append(f"\n### {vis.capitalize()}\n\n")
        bucket = by_visibility.get(vis, [])
        if not bucket:
            out.append("_None._\n")
            continue
        for fn in bucket:
            wikilink = fn.get("wikilink") or fn.get("name", "?")
            signature = fn.get("signature", "")
            cc = fn.get("cyclomatic_complexity")
            callers_n = fn.get("callers_count", 0)
            callees_n = fn.get("callees_count", 0)
            doc = fn.get("docstring")
            sig_part = f" `{signature}`" if signature else ""
            cc_part = f" — complexity {cc}" if cc is not None else ""
            out.append(
                f"- {wikilink}{sig_part}{cc_part} "
                f"(callers: {callers_n}, callees: {callees_n})\n"
            )
            if doc:
                first_line = doc.strip().splitlines()[0]
                out.append(f"  - _{first_line}_\n")
    return "".join(out)


def _render_events_etc(node: dict[str, Any]) -> str:
    loc = node["location"]
    return (
        "## Events / Errors / Modifiers\n\n"
        "_Trailmark does not extract events, errors, or modifiers yet "
        f"— read the source at `{loc['file_path']}` lines "
        f"`{loc['start_line']}`-`{loc['end_line']}`._\n"
    )


def _render_annotations(graph_ctx: dict[str, Any]) -> str:
    annotations = graph_ctx.get("annotations") or []
    if not annotations:
        return "## Annotations\n\n_No annotations yet._\n"
    out = ["## Annotations\n\n"]
    for a in annotations:
        kind = a.get("kind", "note")
        source = a.get("source", "")
        desc = a.get("description", "")
        src_part = f" _(via {source})_" if source else ""
        out.append(f"- **{kind}**: {desc}{src_part}\n")
    return "".join(out)


def _render_risks(graph_ctx: dict[str, Any]) -> str:
    risks = graph_ctx.get("risks") or []
    if not risks:
        return "## Risks\n\n_No risks recorded._\n"
    return "## Risks\n\n" + "".join(f"- {item}\n" for item in risks)


def _build_frontmatter(
    node: dict[str, Any], graph_ctx: dict[str, Any]
) -> dict[str, Any]:
    loc = node["location"]
    start = loc["start_line"]
    end = loc["end_line"]
    fm: dict[str, Any] = {
        "name": node["name"],
        "kind": node["kind"],
        "node_id": node["id"],
        "file": loc["file_path"],
        "lines": f"{start}-{end}",
        # Trailmark ranges are inclusive on both ends. Lines 10-183 is
        # 174 lines (183 - 10 + 1), not 173.
        "loc": end - start + 1,
        "cyclomatic_complexity": node.get("cyclomatic_complexity"),
        "callers_count": len(graph_ctx.get("callers") or []),
        "callees_count": len(graph_ctx.get("callees") or []),
    }
    for key in ("inherits", "implements", "uses"):
        wikilinks = graph_ctx.get(key) or []
        if wikilinks:
            fm[key] = [_bare_name_from_wikilink(w) for w in wikilinks]
    return fm


def render_node_note(
    node: dict[str, Any],
    graph_ctx: dict[str, Any] | None = None,
    body: str = "",
) -> tuple[dict[str, Any], str]:
    """Render a complete Obsidian note for one graph node.

    Returns `(frontmatter, body)` ready to hand to
    `write_obsidian_note(vault, rel_path, frontmatter, body)`.

    `node` is a Trailmark node dict (from `get_node`).
    `graph_ctx` is structured graph data the caller pre-computed;
    missing keys produce placeholder sections.
    `body` is the LLM-written Overview narrative; empty produces a
    placeholder.
    """
    ctx = graph_ctx or {}
    frontmatter = _build_frontmatter(node, ctx)
    sections = [
        _render_overview(body),
        _render_graph_context(ctx),
        _render_state(node),
        _render_functions(ctx),
        _render_events_etc(node),
        _render_annotations(ctx),
        _render_risks(ctx),
    ]
    return frontmatter, "\n".join(sections)


def _pick_primary_method(
    graph_id: str,
    container_node_id: str,
    *,
    cache_root: Path = CACHE_ROOT,
) -> str | None:
    """Highest-CC method belonging to `container_node_id`, tiebreak
    by name ascending. Returns None if the container has no methods.

    "Belonging to" = method ID starts with `container.` (Trailmark's
    `module:Contract.method` format). Could be re-implemented via
    `contains` edges if the ID format ever changes — for now
    ID-prefix is the simplest version-tolerant approach.
    """
    methods = [
        n
        for n in list_nodes(graph_id, kind="method", cache_root=cache_root)
        if n["id"].startswith(container_node_id + ".")
    ]
    if not methods:
        return None
    methods.sort(
        key=lambda m: (-(m.get("cyclomatic_complexity") or 0), m["name"])
    )
    return methods[0]["id"]


def render_and_write_node_note(
    vault_path: str | Path,
    graph_id: str,
    node: dict[str, Any],
    graph_ctx: dict[str, Any] | None = None,
    body: str = "",
    *,
    cache_root: Path = CACHE_ROOT,
) -> str:
    """Render canonical note + write to vault in one atomic call.

    Picks rel_path automatically from `node["kind"]` via
    `KIND_TO_FOLDER`. Computes Mermaid diagrams deterministically
    (inheritance always; call graph if a primary method exists)
    and embeds them in the "Graph context" section. Returns the
    absolute file path as a string (LLM-friendly — tool returns are
    easier to handle when they're primitives, not Path objects).

    Use this from agents instead of calling `render_node_note` +
    `write_obsidian_note` separately. It guarantees every note uses
    the canonical 7-section template AND ships with diagrams.

    `graph_id` is the 12-hex repo identifier (the same one the
    agent already receives in its task message). Diagrams compute
    from it without agent involvement — the LLM never sees or
    forwards them.

    Diagram computation failures are logged and skipped rather than
    raised: the note is the primary artifact, diagrams are
    enrichment. A note still ships if (e.g.) the graph cache is
    missing or the node was constructed for a routing test.

    Raises `ValueError` if `node["kind"] == "method"` — methods are
    documented inside their parent's note, not as standalone files.
    """
    kind = node["kind"]
    if kind == "method":
        raise ValueError(
            f"method nodes are documented inside their parent's "
            f"note, not as standalone files "
            f"(got node_id={node['id']!r})"
        )

    ctx = dict(graph_ctx) if graph_ctx else {}

    # Diagrams are enrichment — never block note writing on
    # EXPECTED failures (bad graph_id, test-synthesized node,
    # malformed Trailmark output). Chunk 3.16 I17 narrowed the
    # catch from `Exception` to the three concrete graph-lookup
    # exceptions: KeyError (missing node), FileNotFoundError
    # (missing graph cache), ValueError (input rejection by a
    # renderer). Coding bugs (TypeError, AttributeError) NOW
    # propagate up to the dispatcher's failure recorder so they
    # surface in the run summary instead of silently producing
    # diagram-less notes.
    try:
        ctx["inheritance_mermaid"] = render_inheritance(
            graph_id, node["id"], cache_root=cache_root
        )
        primary = _pick_primary_method(
            graph_id, node["id"], cache_root=cache_root
        )
        if primary is not None:
            ctx["call_graph_mermaid"] = render_call_graph(
                graph_id, primary, cache_root=cache_root
            )
    except (KeyError, FileNotFoundError, ValueError) as e:
        _log.warning(
            "diagram computation failed for %s: %s — proceeding "
            "without diagrams",
            node["id"],
            e,
        )

    # Filename disambiguation (chunk 3.10) needs the graph to
    # detect bare-name collisions. Same graceful fallback as the
    # diagram block above — if the graph isn't loadable (bad gid,
    # test fixture), use the bare path. Pre-3.10 behavior.
    # Chunk 3.16 I17: narrowed catch (was `except Exception`) to
    # the three graph-lookup failure modes for the same reason —
    # programming bugs propagate so they're noticed.
    try:
        rel_path = (
            f"{_disambiguated_path(node, graph_id, cache_root=cache_root)}"
            f".md"
        )
    except (KeyError, FileNotFoundError, ValueError) as e:
        _log.warning(
            "filename disambiguation failed for %s: %s — using "
            "bare path",
            node["id"],
            e,
        )
        folder = KIND_TO_FOLDER.get(kind, "contracts")
        rel_path = f"{folder}/{node['name']}.md"
    frontmatter, body_text = render_node_note(node, ctx, body)
    written = write_obsidian_note(
        vault_path, rel_path, frontmatter, body_text
    )
    return str(written)


def render_and_write_flow_note(
    vault_path: str | Path,
    graph_id: str,
    entrypoint_node: dict[str, Any],
    paths: list[list[str]],
    overview: str = "",
    observations: list[str] | None = None,
    *,
    cache_root: Path = CACHE_ROOT,
) -> str:
    """Render a flow note + write to `vault/flows/<name>.md` in
    one atomic call.

    `entrypoint_node` is a Trailmark node dict (from `get_node`).
    `paths` is a list of caller-chosen call chains; each chain is
    `[entrypoint_id, ..., sink_id]`. One Mermaid sequence diagram
    is rendered per path.

    Body layout:
        ## Overview         ← overview parameter (LLM prose)
        ## Paths
          ### Path N — entry → sink
          ```mermaid sequenceDiagram ... ```
        ## Observations    ← bullet list (skipped when empty)

    Empty `paths` produces a placeholder Paths section — the note
    still ships. Per-path diagram failures (bad node IDs, etc.)
    log and emit an inline placeholder for that path.

    Returns the absolute file path as a string (LLM-friendly).
    """
    bare = entrypoint_node["name"]
    # Always qualify with the containing class so two entrypoints
    # with the same method name (e.g. UniswapV2Pair.swap vs
    # IUniswapV2Pair.swap, which both appear on Tier 1's attack
    # surface) land in distinct files. The bare method name alone
    # isn't auditor-meaningful for a flow note anyway.
    tail = entrypoint_node["id"].rsplit(":", 1)[-1]
    parent = tail.rsplit(".", 1)[0] if "." in tail else None
    if parent and parent != bare:
        rel_path = f"flows/{parent}.{bare}.md"
    else:
        rel_path = f"flows/{bare}.md"

    frontmatter: dict[str, Any] = {
        "type": "flow",
        "name": bare,
        "entrypoint": entrypoint_node["id"],
        "path_count": len(paths),
    }

    parts: list[str] = [_render_overview(overview), "\n"]
    if paths:
        parts.append("## Paths\n\n")
        for i, path in enumerate(paths, 1):
            sink_bare = path[-1].rsplit(":", 1)[-1].rsplit(".", 1)[-1]
            parts.append(f"### Path {i} — {bare} → {sink_bare}\n\n")
            try:
                parts.append(
                    render_sequence(graph_id, path, cache_root=cache_root)
                )
            except (KeyError, FileNotFoundError, ValueError) as e:
                # Chunk 3.16 I17: narrowed from `except Exception`.
                # KeyError (bad node ID in path), FileNotFoundError
                # (missing graph cache), ValueError (render_sequence
                # input validation) are the expected per-path
                # failures — keep emitting inline placeholders for
                # those so a partial flow note still ships
                # (chunk 3.16 I8 design). Coding bugs propagate.
                _log.warning(
                    "sequence render failed for path %d (%s): %s",
                    i,
                    path,
                    e,
                )
                parts.append(
                    f"_Sequence diagram unavailable: {e}_\n\n"
                )
            # Hop wikilinks (chunk 3.9): per-method navigation
            # below the sequence diagram. Unresolvable hops fall
            # back to a backticked bare name.
            parts.append("\n**Hops:**\n\n")
            for j, hop_id in enumerate(path, 1):
                try:
                    link = resolve_wikilink(
                        graph_id, hop_id, cache_root=cache_root
                    )
                    parts.append(f"{j}. {link}\n")
                except KeyError:
                    hop_bare = hop_id.rsplit(":", 1)[-1]
                    parts.append(
                        f"{j}. `{hop_bare}` (no contract note)\n"
                    )
            parts.append("\n")
    else:
        parts.append(
            "## Paths\n\n"
            "_No multi-hop paths from this entrypoint in the graph._\n\n"
        )

    if observations:
        parts.append("## Observations\n\n")
        for obs in observations:
            parts.append(f"- {obs}\n")

    body = "".join(parts)
    written = write_obsidian_note(
        vault_path, rel_path, frontmatter, body
    )
    return str(written)
