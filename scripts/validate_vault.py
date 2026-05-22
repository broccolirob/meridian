"""Wikilink validation CLI — scans every .md in a vault for
[[...]] references and reports any that don't resolve.

Wikilink-parsing logic lives in `src/validate.py` (chunk 5.4)
so the `washable validate` subcommand can share the same
implementation. This script remains the standalone entry
point for dev use:

    uv run python scripts/validate_vault.py <vault>
    uv run python scripts/validate_vault.py <vault> --fix

Exit codes:
    0  no broken wikilinks (or --fix mode succeeded)
    1  broken wikilinks found and reported (no --fix)
    2  setup error (e.g., vault not a directory)
"""

import argparse
import sys
from pathlib import Path

from src.validate import find_broken_links, strip_broken_links


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
