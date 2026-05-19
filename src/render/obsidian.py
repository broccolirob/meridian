from pathlib import Path
from typing import Any

import yaml

from src.graph.persist import CACHE_ROOT
from src.tools import get_node

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
    on demand. Returns the absolute path written."""
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

    target.write_text("".join(parts), encoding="utf-8")
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

    Raises `KeyError` if `node_id` (or, for methods, its parent) is
    not in the cached graph.
    """
    node = get_node(graph_id, node_id, cache_root=cache_root)
    kind = node["kind"]
    name = node["name"]

    if kind == "method":
        parent_id = node_id.rsplit(".", 1)[0]
        parent = get_node(graph_id, parent_id, cache_root=cache_root)
        folder = KIND_TO_FOLDER.get(parent["kind"], "contracts")
        method_name = name.rsplit(".", 1)[-1]
        return (
            f"[[{folder}/{parent['name']}|{parent['name']}.{method_name}]]"
        )

    folder = KIND_TO_FOLDER.get(kind, "contracts")
    return f"[[{folder}/{name}|{name}]]"


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
    inherits = graph_ctx.get("inherits") or []
    implements = graph_ctx.get("implements") or []
    uses = graph_ctx.get("uses") or []
    callers = graph_ctx.get("callers") or []
    callees = graph_ctx.get("callees") or []
    return (
        "## Graph context\n\n"
        "### Inheritance\n\n"
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
        "loc": end - start,
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
