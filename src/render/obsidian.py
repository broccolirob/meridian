from pathlib import Path
from typing import Any

import yaml

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
