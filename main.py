"""washable CLI entry point.

Wired by `pyproject.toml`'s `[project.scripts]` as
`washable = "main:cli"`. Today only `washable diff` is
implemented; chunk 5.4 will add `parse`, `validate`,
`--version`, and a `--vault-path` flag to the same
argparse scaffold.

argparse (stdlib) was chosen over click/typer because the
CLI surface is small and we want to avoid a new dependency.
The scaffold extends cleanly: each subcommand registers an
`_cmd_*` function and adds an `argparse.ArgumentParser` via
`subparsers.add_parser`.
"""

import argparse
import re
import sys
from pathlib import Path

from src.render.diff_md import render_and_write_diff_note
from src.render.obsidian import ensure_vault
from src.tools import diff_graphs, trailmark_parse

# Same shape `_validate_graph_id` enforces in
# src/graph/persist.py. Used to distinguish a cached graph
# reference from a source directory path.
_GRAPH_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _resolve_to_graph_id(arg: str) -> str:
    """Accept either a 12-hex graph_id (use cached) or a
    source directory path (parse it, return the new id).

    Symmetric for both `<before>` and `<after>` so users can
    mix-and-match — e.g., a cached graph_id from a previous
    parse + a fresh directory for the current checkout.

    Raises `SystemExit` with a clear message when `arg` is
    neither shape; argparse converts SystemExit to a
    user-friendly error before the subcommand body runs."""
    if _GRAPH_ID_RE.fullmatch(arg):
        return arg
    path = Path(arg)
    if not path.exists():
        raise SystemExit(
            f"`{arg}` is neither a 12-hex graph_id nor an "
            f"existing path"
        )
    if not path.is_dir():
        raise SystemExit(
            f"`{arg}` exists but is not a directory; "
            f"trailmark_parse requires a source root"
        )
    # Language auto-detect lives in `trailmark_parse`
    # (default `language="auto"`). No CLI flag needed for 5.2;
    # 5.4 polish can add `--language` if a user hits
    # multi-language ambiguity.
    return trailmark_parse(str(path))


def _cmd_diff(args: argparse.Namespace) -> int:
    """`washable diff <vault> <before> <after>` — render a
    markdown diff note under `vault/diffs/`."""
    vault = ensure_vault(args.vault)
    before_id = _resolve_to_graph_id(args.before)
    after_id = _resolve_to_graph_id(args.after)
    diff = diff_graphs(before_id, after_id)
    written = render_and_write_diff_note(
        vault, diff,
        before_id=before_id, after_id=after_id,
    )
    print(written)
    return 0


def cli(argv: list[str] | None = None) -> int:
    """Build the parser and dispatch. Returns the exit code
    so callers (including tests) can assert on it without
    going through SystemExit. `main()` wraps this with
    `sys.exit(cli())` for the console-script entry point."""
    parser = argparse.ArgumentParser(
        prog="washable",
        description=(
            "Code atlas — turns codebases into audit-ready "
            "Obsidian vaults."
        ),
    )
    subparsers = parser.add_subparsers(
        dest="command", required=True,
    )

    diff_p = subparsers.add_parser(
        "diff",
        help=(
            "Write a markdown diff note between two graph "
            "snapshots."
        ),
        description=(
            "Diff two parsed graphs and write the markdown "
            "result under `<vault>/diffs/`. Each of `before` "
            "and `after` may be either a 12-hex graph_id "
            "(uses the cached graph) or a source directory "
            "path (parsed on the fly via trailmark_parse)."
        ),
    )
    diff_p.add_argument(
        "vault",
        help="Vault root directory (created if missing).",
    )
    diff_p.add_argument(
        "before",
        help=(
            "Before state: 12-hex graph_id (use cached) or "
            "source directory path (parse it)."
        ),
    )
    diff_p.add_argument(
        "after",
        help="After state: same accepted forms as `before`.",
    )
    diff_p.set_defaults(func=_cmd_diff)

    args = parser.parse_args(argv)
    return args.func(args)


def main() -> None:
    """Console-script entry point. Wraps `cli()` with
    `sys.exit` so the process exit code reflects the
    subcommand return."""
    sys.exit(cli())


if __name__ == "__main__":
    main()
