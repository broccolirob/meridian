"""Map-of-content (MOC) generation.

Writes a root README plus per-folder READMEs so the Obsidian vault
opens to a real navigation page instead of a directory listing.
"""

from pathlib import Path
from typing import Any

from src.graph.persist import CACHE_ROOT
from src.render.obsidian import VAULT_SUBDIRS, write_obsidian_note
from src.tools import graph_summary

# Display label per folder, in the order they appear on the root MOC.
SECTION_DISPLAY: dict[str, str] = {
    "contracts": "Contracts",
    "libraries": "Libraries",
    "interfaces": "Interfaces",
    "_meta": "Modules",
    "flows": "Flows",
    "diagrams": "Diagrams",
    "attack-surface": "Attack Surface",
    "risks": "Risks",
}


def _list_notes(folder: Path) -> list[str]:
    """Return sorted note stems (basename without .md) in `folder`,
    excluding the folder's own README.md (case-insensitive)."""
    if not folder.is_dir():
        return []
    return sorted(
        p.stem
        for p in folder.glob("*.md")
        if p.stem.lower() != "readme"
    )


def _render_folder_moc_body(folder_name: str, notes: list[str]) -> str:
    display = SECTION_DISPLAY.get(folder_name, folder_name.capitalize())
    plural = "s" if len(notes) != 1 else ""
    lines = [
        f"# {display}",
        "",
        f"{len(notes)} note{plural}.",
        "",
    ]
    for note in notes:
        lines.append(f"- [[{folder_name}/{note}|{note}]]")
    lines.append("")
    return "\n".join(lines)


def _render_root_moc_body(
    summary: dict[str, Any], populated: dict[str, list[str]]
) -> str:
    total_notes = sum(len(ns) for ns in populated.values())
    lines = [
        "# washable vault",
        "",
        (
            f"Generated documentation for a parsed codebase. "
            f"{total_notes} notes across {len(populated)} populated "
            f"sections."
        ),
        "",
        "## Sections",
        "",
    ]
    for folder in SECTION_DISPLAY:
        if folder not in populated:
            continue
        display = SECTION_DISPLAY[folder]
        count = len(populated[folder])
        plural = "s" if count != 1 else ""
        lines.append(
            f"- [[{folder}/README|{display}]] "
            f"({count} note{plural})"
        )
    lines.append("")
    lines.append("## Source graph")
    lines.append("")
    lines.append(f"- Total graph nodes: {summary.get('total_nodes', 0)}")
    lines.append(f"- Functions/methods: {summary.get('functions', 0)}")
    lines.append(f"- Call edges:        {summary.get('call_edges', 0)}")
    lines.append(f"- Entrypoints:       {summary.get('entrypoints', 0)}")
    deps = summary.get("dependencies", [])
    if deps:
        lines.append(f"- Dependencies:      {', '.join(deps)}")
    lines.append("")
    return "\n".join(lines)


def write_root_moc(
    vault_path: str | Path,
    graph_id: str,
    *,
    cache_root: Path = CACHE_ROOT,
) -> list[Path]:
    """Write the root README.md + a README.md inside each populated
    section folder. Returns the list of paths written (root first).

    Skips empty folders — the MOC only links to populated ones.
    """
    vault = Path(vault_path)
    summary = graph_summary(graph_id, cache_root=cache_root)

    populated: dict[str, list[str]] = {}
    for folder_name in VAULT_SUBDIRS:
        notes = _list_notes(vault / folder_name)
        if notes:
            populated[folder_name] = notes

    written: list[Path] = []

    root_fm = {
        "type": "moc",
        "graph_id": graph_id,
        "generated_by": "washable",
    }
    root_path = write_obsidian_note(
        vault,
        "README.md",
        root_fm,
        _render_root_moc_body(summary, populated),
    )
    written.append(root_path)

    for folder_name, notes in populated.items():
        fm = {"type": "moc", "section": folder_name}
        body = _render_folder_moc_body(folder_name, notes)
        path = write_obsidian_note(
            vault, f"{folder_name}/README.md", fm, body
        )
        written.append(path)

    return written
