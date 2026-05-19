"""Wikilink validation — scans every .md in a vault for [[...]]
references and reports any that don't resolve to a real file.

Usage:
    uv run python scripts/validate_vault.py <vault>
    uv run python scripts/validate_vault.py <vault> --fix

Exit codes:
    0  no broken wikilinks (or --fix mode succeeded)
    1  broken wikilinks found and reported (no --fix)
    2  setup error (e.g., vault not a directory)
"""

import argparse
import re
import sys
from pathlib import Path

# Matches [[anything-except-square-brackets]], capturing the inner.
# The inner may contain `|display` — we strip that to get the target.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")


def _target_of(inner: str) -> str:
    """Extract the target from a wikilink inner. `[[a/b|display]]`
    has inner `a/b|display`; the target is `a/b`."""
    return inner.split("|", 1)[0].strip()


def _resolves(vault: Path, target: str) -> bool:
    """True if `target` resolves to an existing .md in `vault`.

    Targets with `/` are vault-relative paths. Bare names use
    Obsidian's vault-wide search (matches any .md with that stem)."""
    if "/" in target:
        return (vault / f"{target}.md").exists()
    return any(vault.rglob(f"{target}.md"))


def find_broken_links(vault: Path) -> list[tuple[Path, str]]:
    """Return a list of (source_note, broken_target) pairs."""
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
    replacement is the display text (or the target itself if no
    display was given).

    Returns the number of wikilinks replaced.
    """
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
                # Use captured display text, or fall back to the
                # target (bare name = the user already wrote it as
                # the display)
                return m.group(1) or _tgt

            text, n = pattern.subn(_repl, text)
            total += n
        src.write_text(text, encoding="utf-8")
    return total


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate every wikilink in an Obsidian vault.",
    )
    parser.add_argument("vault", help="Path to the vault root.")
    parser.add_argument(
        "--fix",
        action="store_true",
        help=(
            "Rewrite source notes to strip broken wikilinks. "
            "Destructive — produces a diff in the vault."
        ),
    )
    args = parser.parse_args()

    vault = Path(args.vault)
    if not vault.is_dir():
        print(f"ERROR: not a directory: {vault}", file=sys.stderr)
        return 2

    broken = find_broken_links(vault)
    note_count = sum(1 for _ in vault.rglob("*.md"))

    if not broken:
        print(f"OK: 0 broken wikilinks across {note_count} notes")
        return 0

    print(f"FOUND {len(broken)} broken wikilink(s):")
    for src, tgt in broken:
        print(f"  {src.relative_to(vault)} -> [[{tgt}]]")

    if not args.fix:
        return 1

    n = strip_broken_links(vault, broken)
    print(f"FIXED: stripped {n} broken wikilink(s) from source notes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
