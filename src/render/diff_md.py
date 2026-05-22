"""Renderer for diff notes — turns the dict from
`diff_graphs` into a `vault/diffs/<before8>-<after8>.md`
note.

Companion to src/render/obsidian.py. Reuses
`write_obsidian_note` (atomic write + vault containment +
`..`-segment rejection + YAML frontmatter) and the
`_flatten_to_one_line` defang helper for interpolated node
names — parsed-source identifiers from non-Solidity
languages can contain characters the auditor's vault would
otherwise render unsafely.

Render-then-write split mirrors `render_node_note` /
`render_and_write_node_note` in obsidian.py: the pure
`render_diff_note` is deterministic and snapshot-friendly;
`render_and_write_diff_note` composes + writes.
"""

from pathlib import Path
from typing import Any

from src.render.obsidian import (
    _flatten_to_one_line,
    write_obsidian_note,
)


def _short(graph_id: str) -> str:
    """8-char prefix of a 12-hex graph_id. Used in both the
    filename (`<before8>-<after8>.md`) and the body header.
    Mirrors git's short-sha convention — for now derives from
    the graph cache rather than a real git commit. CHUNKS.md
    5.2 spec says `<short-sha>.md`; this becomes git-derived
    if a future chunk adds `git worktree`-based parsing."""
    return graph_id[:8]


def _render_summary_table(summary_delta: dict[str, Any]) -> str:
    """Markdown table of {nodes, edges, entrypoints} counts.
    Trailmark's `compute_diff` only emits a metric key when
    the count changed, so iterate the canonical metric set
    and render `_unchanged_` for omitted entries — keeps the
    table shape stable across diff pairs."""
    metrics = ("nodes", "edges", "entrypoints")
    rows = ["| Metric | Before | After | Delta |", "|---|---|---|---|"]
    for m in metrics:
        d = summary_delta.get(m)
        if d is None:
            rows.append(f"| {m} | — | — | _unchanged_ |")
        else:
            sign = "+" if d["delta"] > 0 else ""
            rows.append(
                f"| {m} | {d['before']} | {d['after']} | "
                f"{sign}{d['delta']} |"
            )
    return "## Summary\n\n" + "\n".join(rows) + "\n"


def _render_unit_bullets(units: list[dict[str, Any]]) -> str:
    """Bullet list of node-unit summaries (added/removed
    sections under `## Structural changes`). Entries come
    from `compute_diff`'s `_unit_summary`:
    `{id, name, kind, file, cyclomatic_complexity}`.

    Defang name/kind/file via `_flatten_to_one_line` —
    parsed-source identifiers may contain HTML / markdown
    chars in non-Solidity languages, and an attacker-supplied
    repo eventually feeds these values.

    Empty list renders as `_None._` so the section heading
    above never appears followed by silence."""
    if not units:
        return "_None._\n"
    out: list[str] = []
    for u in units:
        name = _flatten_to_one_line(u.get("name", "?"))
        kind = _flatten_to_one_line(u.get("kind", "?"))
        file = _flatten_to_one_line(u.get("file", "?"))
        cc = u.get("cyclomatic_complexity")
        cc_part = f" — complexity {cc}" if cc is not None else ""
        out.append(f"- `{name}` ({kind}, {file}){cc_part}")
    return "\n".join(out) + "\n"


def _render_entrypoint_bullets(
    eps: list[dict[str, Any]],
) -> str:
    """Bullet list of entrypoint summaries (added/removed
    sections under `## Attack surface changes`). Entries come
    from `compute_diff`'s `_ep_summary` keyed by node id:
    `{id, kind, trust_level, asset_value, description}`.

    NOTE the shape diverges from `_render_unit_bullets`:
    entrypoint entries have NO `name` / `file` (those live on
    the underlying node, which the caller can look up via
    `get_node` if needed). The bullet shows the node id (the
    auditor's only direct handle) plus the entrypoint
    classification triple."""
    if not eps:
        return "_None._\n"
    out: list[str] = []
    for ep in eps:
        nid = _flatten_to_one_line(ep.get("id", "?"))
        kind = _flatten_to_one_line(ep.get("kind", "?"))
        trust = _flatten_to_one_line(ep.get("trust_level", "?"))
        asset = _flatten_to_one_line(ep.get("asset_value", "?"))
        out.append(
            f"- `{nid}` ({kind}, trust={trust}, asset={asset})"
        )
    return "\n".join(out) + "\n"


def _render_modified_nodes(modified: list[dict[str, Any]]) -> str:
    """Bullet list of modified-node entries. Each entry has
    `{id, changes}` where `changes` covers
    `cyclomatic_complexity`, `parameters`, `line_span`. Only
    the keys that actually changed are present in `changes`.

    Forward-compat: if a future Trailmark adds new change
    keys we don't recognize, render the bullet with an
    `unknown change` marker so the auditor knows something
    changed (rather than emitting a degenerate `- id:`
    bullet with nothing after the colon)."""
    if not modified:
        return "_None._\n"
    out: list[str] = []
    for m in modified:
        nid = _flatten_to_one_line(m["id"])
        changes = m["changes"]
        change_strs: list[str] = []
        if "cyclomatic_complexity" in changes:
            cc = changes["cyclomatic_complexity"]
            change_strs.append(f"CC {cc['before']} → {cc['after']}")
        if "parameters" in changes:
            change_strs.append("parameters changed")
        if "line_span" in changes:
            ls = changes["line_span"]
            change_strs.append(
                f"line span {ls['before']} → {ls['after']}"
            )
        if not change_strs:
            change_strs.append("unknown change")
        out.append(f"- `{nid}`: " + ", ".join(change_strs))
    return "\n".join(out) + "\n"


def _render_edges(edges: list[dict[str, str]]) -> str:
    """Bullet list of edge entries. Each entry has
    `{source, target, kind}`."""
    if not edges:
        return "_None._\n"
    out: list[str] = []
    for e in edges:
        src = _flatten_to_one_line(e["source"])
        dst = _flatten_to_one_line(e["target"])
        kind = _flatten_to_one_line(e["kind"])
        out.append(f"- `{src}` → `{dst}` ({kind})")
    return "\n".join(out) + "\n"


def _render_entrypoints_modified(
    mods: list[dict[str, Any]],
) -> str:
    """Entrypoint modifications carry before/after trust +
    asset shifts — the keystone attack-surface signal. The
    schema is `{id, before: ep_summary, after: ep_summary}`."""
    if not mods:
        return "_None._\n"
    out: list[str] = []
    for m in mods:
        nid = _flatten_to_one_line(m["id"])
        b = m.get("before", {})
        a = m.get("after", {})
        out.append(
            f"- `{nid}`: trust {b.get('trust_level')} → "
            f"{a.get('trust_level')}, asset "
            f"{b.get('asset_value')} → {a.get('asset_value')}"
        )
    return "\n".join(out) + "\n"


def render_diff_note(
    diff: dict[str, Any],
    *,
    before_label: str,
    after_label: str,
) -> tuple[dict[str, Any], str]:
    """Return `(frontmatter, body)` for a diff note.

    Pure function — no I/O. Deterministic given the inputs,
    which makes snapshot-style tests easy.

    `diff` is the dict returned by `src.tools.diff_graphs`
    (Trailmark's `compute_diff` shape). `before_label` and
    `after_label` are human-meaningful identifiers that show
    in the H1 + frontmatter — typically the 8-char graph_id
    short forms, but a future git-aware caller can pass
    short SHAs instead."""
    nodes = diff["nodes"]
    edges = diff["edges"]
    eps = diff["entrypoints"]
    frontmatter: dict[str, Any] = {
        "type": "diff",
        "before": before_label,
        "after": after_label,
        "nodes_added": len(nodes["added"]),
        "nodes_removed": len(nodes["removed"]),
        "nodes_modified": len(nodes["modified"]),
        "edges_added": len(edges["added"]),
        "edges_removed": len(edges["removed"]),
        "entrypoints_added": len(eps["added"]),
        "entrypoints_removed": len(eps["removed"]),
        "entrypoints_modified": len(eps["modified"]),
    }
    body_parts = [
        f"# Diff: {_flatten_to_one_line(before_label)} → "
        f"{_flatten_to_one_line(after_label)}\n\n",
        _render_summary_table(diff["summary_delta"]),
        "\n## Attack surface changes\n\n",
        "### Added entrypoints\n\n",
        _render_entrypoint_bullets(eps["added"]),
        "\n### Removed entrypoints\n\n",
        _render_entrypoint_bullets(eps["removed"]),
        "\n### Modified entrypoints (trust / asset shifts)\n\n",
        _render_entrypoints_modified(eps["modified"]),
        "\n## Structural changes\n\n",
        "### Added nodes\n\n",
        _render_unit_bullets(nodes["added"]),
        "\n### Removed nodes\n\n",
        _render_unit_bullets(nodes["removed"]),
        "\n### Modified nodes\n\n",
        _render_modified_nodes(nodes["modified"]),
        "\n## Edge changes\n\n",
        "### Added edges\n\n",
        _render_edges(edges["added"]),
        "\n### Removed edges\n\n",
        _render_edges(edges["removed"]),
    ]
    return frontmatter, "".join(body_parts)


def render_and_write_diff_note(
    vault_path: str | Path,
    diff: dict[str, Any],
    *,
    before_id: str,
    after_id: str,
    before_label: str | None = None,
    after_label: str | None = None,
) -> str:
    """Render + atomically write
    `vault/diffs/<before8>-<after8>.md`. Returns the
    absolute path written as a string (CLI-friendly).

    Default labels use the 8-char short forms of each
    graph_id. A future git-aware caller can pass real short
    SHAs as `before_label` / `after_label` while keeping the
    graph_id-derived filename stable."""
    before_label = before_label or _short(before_id)
    after_label = after_label or _short(after_id)
    frontmatter, body = render_diff_note(
        diff,
        before_label=before_label,
        after_label=after_label,
    )
    rel_path = f"diffs/{_short(before_id)}-{_short(after_id)}.md"
    written = write_obsidian_note(
        vault_path, rel_path, frontmatter, body,
    )
    return str(written)
