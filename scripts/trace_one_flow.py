"""Single-flow harness — invoke FlowTracer against one entrypoint.

Use to dogfood the FlowTracer subagent on individual entrypoints
without paying for full Tier 1 dispatch (~$0.45). Per-call cost is
~$0.02. Iteration-friendly counterpart to scripts/document_one_node.py.

Usage:
    uv run python scripts/trace_one_flow.py ENTRYPOINT_ID [OPTIONS]

Example:
    uv run python scripts/trace_one_flow.py \\
        contracts.UniswapV2Pair:UniswapV2Pair.swap \\
        --repo tests/fixtures/tier1_uniswap_v2 \\
        --vault .washable/vaults/tier1
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from src.agent import (  # noqa: E402
    DEFAULT_CONCURRENCY_CAP,
    DEFAULT_MODEL,
    dispatch_flows,
)
from src.render.obsidian import ensure_vault  # noqa: E402
from src.tools import attack_surface, get_node, trailmark_parse  # noqa: E402

DEFAULT_REPO = "tests/fixtures/tier1_uniswap_v2"
DEFAULT_VAULT = ".washable/vaults/tier1"
DEFAULT_ENTRY = "contracts.UniswapV2Pair:UniswapV2Pair.swap"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Trace one entrypoint via FlowTracer.",
    )
    p.add_argument(
        "entrypoint_id",
        nargs="?",
        default=DEFAULT_ENTRY,
        help=f"Trailmark node id (default: {DEFAULT_ENTRY})",
    )
    p.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"Repo path to parse (default: {DEFAULT_REPO})",
    )
    p.add_argument(
        "--vault",
        default=DEFAULT_VAULT,
        help=f"Vault path to write into (default: {DEFAULT_VAULT})",
    )
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenAI model id (default: {DEFAULT_MODEL})",
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

    try:
        node = get_node(graph_id, args.entrypoint_id)
    except KeyError:
        print(
            f"ERROR: entrypoint not in graph: {args.entrypoint_id}",
            file=sys.stderr,
        )
        return 2

    # Guard rail: warn if the requested entrypoint isn't on the
    # actual attack surface — running FlowTracer on an internal
    # helper would produce a flow note that document_repo.py
    # wouldn't reproduce.
    surface_ids = {e["node_id"] for e in attack_surface(graph_id)}
    if args.entrypoint_id not in surface_ids:
        print(
            f"WARNING: {args.entrypoint_id} is not on the attack "
            f"surface. FlowTracer will still run; the result may "
            f"not match what document_repo.py produces.",
            file=sys.stderr,
        )
    print(f"[2/3] Entrypoint = {node['kind']} {node['name']}")

    vault = ensure_vault(args.vault).resolve()
    print(f"[3/3] Running FlowTracer on {args.model}...")

    # Scope dispatch to ONE entrypoint via the entrypoint_filter
    # parameter (chunk 3.15 replaced the prior monkey-patch of
    # src.agent.attack_surface). No module-global mutation; the
    # filter is a per-call argument.
    def _only_this_entrypoint(
        entrypoints: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            e for e in entrypoints
            if e["node_id"] == args.entrypoint_id
        ]

    result = dispatch_flows(
        graph_id,
        str(vault),
        model=args.model,
        concurrency_cap=DEFAULT_CONCURRENCY_CAP,
        skip_leaf_entrypoints=False,
        entrypoint_filter=_only_this_entrypoint,
    )

    print("---")
    print(f"FLOWS: {result['entrypoint_count']}")
    print(f"OK   : {len(result['successes'])}")
    print(f"FAIL : {len(result['failures'])}")
    for f in result["failures"]:
        print(f"  FAIL {f['node_id']}: {f['error']}")

    if result["successes"]:
        path = result["successes"][0]["agent_reply"]
        print(f"NOTE WRITTEN: {path}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
