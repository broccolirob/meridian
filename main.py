"""washable CLI entry point.

Wired by `pyproject.toml`'s `[project.scripts]` as
`washable = "main:cli"`. Three subcommands today:

  - `washable parse <repo>`           — cache a parsed graph
  - `washable diff <before> <after>`  — write a diff note
  - `washable validate`               — check vault wikilinks

Plus top-level flags:

  - `--version`     — print the installed version + exit
  - `--vault-path`  — vault root (required for `diff` and
                       `validate`)
  - `--help`        — argparse default; lists subcommands

argparse (stdlib) was chosen over click/typer because the
CLI surface is small and we want to avoid a dependency. The
scaffold extends cleanly: each subcommand registers an
`_cmd_*` function and adds a parser via
`subparsers.add_parser`.
"""

import argparse
import re
import sys
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

from src.render.diff_md import render_and_write_diff_note
from src.render.obsidian import ensure_vault
from src.tools import diff_graphs, trailmark_parse
from src.validate import find_broken_links, strip_broken_links

# Same shape `_validate_graph_id` enforces in
# `src/graph/persist.py`. Used to distinguish a cached
# graph reference from a source directory path.
_GRAPH_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _project_version() -> str:
    """Read the installed package version. Falls back to
    reading `pyproject.toml` at the repo root for dev runs
    where the package isn't installed (rare with chunk 5.2's
    `tool.uv.package = true`, but the fallback is cheap and
    keeps `uv run washable --version` working pre-install)."""
    try:
        return _pkg_version("washable")
    except PackageNotFoundError:
        import tomllib
        pyproject = Path(__file__).resolve().parent / "pyproject.toml"
        try:
            data = tomllib.loads(pyproject.read_text())
            return data["project"]["version"]
        except (FileNotFoundError, KeyError, tomllib.TOMLDecodeError):
            return "0.0.0+unknown"


def _require_vault(
    args: argparse.Namespace,
    cmd: str,
    *,
    must_exist: bool = False,
) -> Path:
    """Resolve the vault from `--vault-path`. Raises
    `SystemExit` with a helpful message if not set. Single
    check site so every subcommand that needs a vault gets
    identical UX.

    `must_exist=True` for read-only subcommands (`validate`)
    that should fail loudly on a typo'd path. `False`
    (default) for write subcommands (`diff`) where
    create-on-demand is intentional."""
    if not args.vault_path:
        raise SystemExit(
            f"error: `washable {cmd}` requires --vault-path "
            f"(set at the top level, e.g. "
            f"`washable --vault-path ./my-vault {cmd} ...`)"
        )
    if must_exist:
        path = Path(args.vault_path)
        if not path.exists():
            raise SystemExit(
                f"error: vault does not exist: {args.vault_path} "
                f"(typo? `washable {cmd}` will not create the "
                f"vault for you)"
            )
        if not path.is_dir():
            raise SystemExit(
                f"error: vault path is not a directory: "
                f"{args.vault_path}"
            )
    return ensure_vault(args.vault_path)


def _resolve_to_graph_id(arg: str) -> str:
    """Accept either a 12-hex graph_id (use cached) or a
    source directory path (parse it, return the new id).

    Symmetric for both `<before>` and `<after>` of `diff` so
    users can mix a cached graph_id with a fresh directory.

    Raises `SystemExit` when `arg` is neither shape;
    argparse converts SystemExit to a user-friendly error
    before the subcommand body runs."""
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
    return trailmark_parse(str(path))


def _cmd_parse(args: argparse.Namespace) -> int:
    """`washable parse <repo>` — parses a source tree into
    the graph cache; prints the 12-hex graph_id to stdout.
    Pipeable into `washable diff`."""
    repo = Path(args.repo)
    if not repo.exists():
        raise SystemExit(f"error: repo path does not exist: {repo}")
    if not repo.is_dir():
        raise SystemExit(f"error: repo path is not a directory: {repo}")
    gid = trailmark_parse(str(repo), language=args.language)
    print(gid)
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    """`washable diff <before> <after>` — vault comes from
    the top-level `--vault-path` flag (chunk 5.4 contract
    change from 5.2's positional vault)."""
    vault = _require_vault(args, "diff")
    before_id = _resolve_to_graph_id(args.before)
    after_id = _resolve_to_graph_id(args.after)
    diff = diff_graphs(before_id, after_id)
    written = render_and_write_diff_note(
        vault, diff,
        before_id=before_id, after_id=after_id,
    )
    print(written)
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    """`washable validate` — checks every wikilink in the
    vault. Optional `--fix` rewrites source notes to strip
    broken links. Wraps `src.validate` helpers; matches the
    standalone `scripts/validate_vault.py` semantics."""
    vault = _require_vault(args, "validate", must_exist=True)
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


def cli(argv: list[str] | None = None) -> int:
    """Build the parser and dispatch. Returns the exit code
    so callers (including tests) can assert on it without
    going through SystemExit. `main()` wraps with
    `sys.exit(cli())` for the console-script entry point."""
    parser = argparse.ArgumentParser(
        prog="washable",
        description=(
            "Code atlas — turns codebases into audit-ready "
            "Obsidian vaults."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"washable {_project_version()}",
    )
    parser.add_argument(
        "--vault-path",
        dest="vault_path",
        default=None,
        help=(
            "Vault root directory (created if missing). "
            "Required for `diff` and `validate` subcommands."
        ),
    )
    subparsers = parser.add_subparsers(
        dest="command", required=True,
    )

    # ---- parse ----------------------------------------------
    parse_p = subparsers.add_parser(
        "parse",
        help="Parse a source repo into the graph cache; print graph_id.",
        description=(
            "Parses the given source tree via trailmark and "
            "caches the resulting graph at "
            "`.washable/graph/<graph_id>/`. Prints the "
            "12-hex graph_id to stdout (pipeable into "
            "`washable diff`)."
        ),
    )
    parse_p.add_argument(
        "repo", help="Source repository root directory.",
    )
    parse_p.add_argument(
        "--language",
        default="auto",
        help=(
            "Source language for trailmark "
            "(default: auto-detect)."
        ),
    )
    parse_p.set_defaults(func=_cmd_parse)

    # ---- diff -----------------------------------------------
    diff_p = subparsers.add_parser(
        "diff",
        help=(
            "Write a markdown diff note between two graph "
            "snapshots."
        ),
        description=(
            "Diff two parsed graphs and write the markdown "
            "result under `<vault>/diffs/`. Each of "
            "`before` and `after` may be either a 12-hex "
            "graph_id (uses the cached graph) or a source "
            "directory path (parsed on the fly)."
        ),
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

    # ---- validate -------------------------------------------
    validate_p = subparsers.add_parser(
        "validate",
        help="Check every wikilink in the vault.",
        description=(
            "Scan every `.md` in the vault for `[[...]]` "
            "references. Reports any that don't resolve to "
            "an existing note. Use `--fix` to rewrite "
            "source notes, stripping broken links "
            "(destructive)."
        ),
    )
    validate_p.add_argument(
        "--fix",
        action="store_true",
        help=(
            "Rewrite source notes to strip broken "
            "wikilinks. Destructive — produces a diff in "
            "the vault."
        ),
    )
    validate_p.set_defaults(func=_cmd_validate)

    args = parser.parse_args(argv)
    return args.func(args)


def main() -> None:
    """Console-script entry point. Wraps `cli()` with
    `sys.exit` so the process exit code reflects the
    subcommand return."""
    sys.exit(cli())


if __name__ == "__main__":
    main()
