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
