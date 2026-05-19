"""Multi-node driver — invoke dispatch_topo end-to-end.

Mirrors document_one_node.py but loops over the whole graph in
topological order. Use to dogfood the full Phase 2 path.

Usage:
    uv run python scripts/document_repo.py [--repo PATH] [--vault PATH]
                                            [--model NAME] [--concurrency N]
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env")

from src.agent import (  # noqa: E402
    DEFAULT_CONCURRENCY_CAP,
    DEFAULT_MODEL,
    dispatch_topo,
)
from src.render.moc import write_root_moc  # noqa: E402
from src.render.obsidian import ensure_vault  # noqa: E402
from src.tools import trailmark_parse  # noqa: E402

DEFAULT_REPO = "tests/fixtures/tier0_erc4626"
DEFAULT_VAULT = ".washable/vaults/tier0"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Document an entire repo via dispatch_topo.",
    )
    p.add_argument("--repo", default=DEFAULT_REPO)
    p.add_argument("--vault", default=DEFAULT_VAULT)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY_CAP,
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        return 2
    if not Path(args.repo).is_dir():
        print(f"ERROR: not a directory: {args.repo}", file=sys.stderr)
        return 2

    print(f"[1/3] Parsing {args.repo}...")
    graph_id = trailmark_parse(args.repo, language="solidity")
    print(f"      graph_id = {graph_id}")

    vault = ensure_vault(args.vault).resolve()
    print(f"[2/3] Vault = {vault}")

    print(
        f"[3/3] Dispatching (cap={args.concurrency}, model={args.model})..."
    )

    def _progress(i: int, n: int, nid: str) -> None:
        print(f"  [{i}/{n}] {nid}")

    result = dispatch_topo(
        graph_id,
        str(vault),
        model=args.model,
        concurrency_cap=args.concurrency,
        on_progress=_progress,
    )

    print("---")
    print(f"NODES: {result['node_count']}")
    print(f"OK   : {len(result['successes'])}")
    print(f"FAIL : {len(result['failures'])}")
    for f in result["failures"]:
        print(f"  FAIL {f['node_id']}: {f['error']}")

    # Always try to write MOCs — even partial-success vaults benefit
    # from a navigable landing page. write_root_moc scans the
    # filesystem, so it only lists notes that actually shipped.
    print("---")
    print("Writing MOCs (root + populated folder READMEs)...")
    moc_paths = write_root_moc(str(vault), graph_id)
    print(f"      wrote {len(moc_paths)} MOC files")

    # Advisory wikilink check (chunk 2.6). Never fails the run —
    # broken links are a quality signal, not a dispatch failure.
    # Users can `scripts/validate_vault.py --fix <vault>` to strip.
    print("---")
    print("Validating wikilinks (advisory)...")
    _broken = _find_broken_wikilinks(vault)
    if _broken:
        print(
            f"  WARNING: {len(_broken)} broken wikilink(s). "
            f"Run `scripts/validate_vault.py {vault}` to inspect, "
            f"or add --fix to strip."
        )
    else:
        print("  OK: no broken wikilinks")

    return 0 if not result["failures"] else 1


def _find_broken_wikilinks(vault: Path) -> list[tuple[Path, str]]:
    """Inline thin wrapper around validate_vault.find_broken_links.
    Import is lazy because validate_vault lives in scripts/ alongside
    this file — adding it to the top-level import block would force
    every other importer of scripts/document_repo.py to depend on it."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "validate_vault",
        Path(__file__).resolve().parent / "validate_vault.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.find_broken_links(vault)


if __name__ == "__main__":
    raise SystemExit(main())
