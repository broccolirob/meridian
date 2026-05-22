"""Wikilink validation helpers for Obsidian vaults.

Used by:
  - `scripts/validate_vault.py` — the standalone CLI script.
  - `main.py::_cmd_validate` — the `washable validate`
    subcommand (chunk 5.4).

Lives in `src/` rather than `scripts/` so it ships in the
hatchling wheel (chunk 5.2 packaged `["src"]` + `main.py`;
`scripts/` is dev-only). Both consumers share the same
implementation — no duplication.
"""

import re
from pathlib import Path

# Matches `[[anything-except-square-brackets]]`, capturing
# the inner content. The inner may contain `|display` —
# `_target_of` strips that to extract the link target.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")


def _target_of(inner: str) -> str:
    """Extract the target from a wikilink inner. `[[a/b|display]]`
    has inner `a/b|display`; the target is `a/b`."""
    return inner.split("|", 1)[0].strip()


def _resolves(vault: Path, target: str) -> bool:
    """True if `target` resolves to an existing .md in `vault`.

    Targets with `/` are vault-relative paths. Bare names
    use Obsidian's vault-wide search (matches any .md with
    that stem)."""
    if "/" in target:
        return (vault / f"{target}.md").exists()
    return any(vault.rglob(f"{target}.md"))


def find_broken_links(vault: Path) -> list[tuple[Path, str]]:
    """Return `(source_note, broken_target)` pairs for every
    wikilink that doesn't resolve to a real `.md` in the
    vault."""
    broken: list[tuple[Path, str]] = []
    for md in sorted(vault.rglob("*.md")):
        text = md.read_text(encoding="utf-8")
        for match in _WIKILINK_RE.finditer(text):
            target = _target_of(match.group(1))
            if not _resolves(vault, target):
                broken.append((md, target))
    return broken


def strip_broken_links(
    vault: Path, broken: list[tuple[Path, str]]
) -> int:
    """Rewrite source notes to strip broken wikilinks. The
    replacement is the display text (or the target itself
    if no display was given).

    Destructive — produces a diff in the vault. Returns the
    number of wikilinks replaced."""
    by_source: dict[Path, set[str]] = {}
    for src, tgt in broken:
        by_source.setdefault(src, set()).add(tgt)

    total = 0
    for src, targets in by_source.items():
        text = src.read_text(encoding="utf-8")
        for tgt in targets:
            pattern = re.compile(
                r"\[\[" + re.escape(tgt) + r"(?:\|([^\]]+))?\]\]"
            )

            def _repl(m: re.Match[str], _tgt: str = tgt) -> str:
                # Captured display text wins; otherwise fall
                # back to the target (bare-name wikilinks
                # already used the target as display).
                return m.group(1) or _tgt

            text, n = pattern.subn(_repl, text)
            total += n
        src.write_text(text, encoding="utf-8")
    return total
