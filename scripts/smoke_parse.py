"""End-to-end smoke for Phase 0.

Parses a Solidity directory through the full meridian tool stack
(`trailmark_parse` -> `list_nodes` -> `callers_of`/`callees_of`) and
emits a JSON manifest of every node with caller/callee counts.

Usage:
    uv run python scripts/smoke_parse.py                 # Tier 0 default
    uv run python scripts/smoke_parse.py path/to/repo    # any Solidity tree
"""

import json
import sys
from pathlib import Path

# Make `src/` importable when invoked as `uv run python scripts/...`.
# pytest gets this for free via pyproject.toml's pythonpath setting,
# but plain `python` does not.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.tools import (  # noqa: E402
    callees_of,
    callers_of,
    graph_summary,
    list_nodes,
    trailmark_parse,
)

DEFAULT_REPO = "tests/fixtures/tier0_erc4626"


def build_manifest(repo_path: str) -> dict:
    graph_id = trailmark_parse(repo_path, language="solidity")
    entries = []
    for n in list_nodes(graph_id):
        nid = n["id"]
        entries.append(
            {
                "node_id": nid,
                "kind": n["kind"],
                "name": n["name"],
                "file": n["location"]["file_path"],
                "callers_count": len(callers_of(graph_id, nid)),
                "callees_count": len(callees_of(graph_id, nid)),
            }
        )
    return {
        "repo": repo_path,
        "graph_id": graph_id,
        "summary": graph_summary(graph_id),
        "nodes": entries,
    }


def main() -> int:
    repo = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REPO
    if not Path(repo).is_dir():
        print(f"ERROR: not a directory: {repo}", file=sys.stderr)
        return 2
    manifest = build_manifest(repo)
    json.dump(manifest, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
