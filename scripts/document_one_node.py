"""Single-node harness — invoke NodeDocumenter against one graph node.

This is the first real LLM call in washable. Use to dry-run the agent
on small nodes (ERC4626 contract is the default test target) and
iterate on the subagent prompt + render template.

Usage:
    uv run python scripts/document_one_node.py NODE_ID [OPTIONS]

Example:
    uv run python scripts/document_one_node.py src.tokens.ERC4626:ERC4626
"""

import argparse
import os
import sys
from pathlib import Path

# Make `src/` importable when invoked as `uv run python scripts/...`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env BEFORE importing langchain_openai so OPENAI_API_KEY is set
# at module-import time for any code that reads it eagerly.
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from deepagents import create_deep_agent  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

from src.render.obsidian import KIND_TO_FOLDER, ensure_vault  # noqa: E402
from src.subagents import NODE_DOCUMENTER_SUBAGENT  # noqa: E402
from src.tools import (  # noqa: E402
    clear_annotations,
    get_node,
    trailmark_parse,
)

DEFAULT_REPO = "tests/fixtures/tier0_erc4626"
DEFAULT_VAULT = ".washable/vaults/tier0"
DEFAULT_MODEL = "gpt-5-mini"
DEFAULT_NODE = "src.tokens.ERC4626:ERC4626"

_MAIN_AGENT_PROMPT = """\
You are the washable orchestrator. Your ONLY job: dispatch the task
to the `node-documenter` subagent via the `task` tool.

Rules:
- Pass graph_id, node_id, vault_path (absolute), and overview_hint
  verbatim from the user message.
- The subagent MUST call write_obsidian_note() to persist the note
  to disk. If its reply does not include an absolute file path that
  begins with vault_path, dispatch it AGAIN with explicit instructions
  to actually call write_obsidian_note.
- Do not generate note content yourself. Do not paraphrase the
  subagent's reply. Return only the absolute file path it produced.
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Document one graph node via NodeDocumenter.",
    )
    p.add_argument(
        "node_id",
        nargs="?",
        default=DEFAULT_NODE,
        help=f"Trailmark node id (default: {DEFAULT_NODE})",
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
    p.add_argument(
        "--overview",
        default="",
        help="Optional overview hint passed to the subagent",
    )
    p.add_argument(
        "--keep-annotations",
        action="store_true",
        help="Don't clear prior annotations on the target node",
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

    print(f"[1/4] Parsing {args.repo}...")
    graph_id = trailmark_parse(args.repo, language="solidity")
    print(f"      graph_id = {graph_id}")

    try:
        node = get_node(graph_id, args.node_id)
    except KeyError:
        print(
            f"ERROR: node not in graph: {args.node_id}",
            file=sys.stderr,
        )
        return 2
    print(f"[2/4] Node = {node['kind']} {node['name']}")
    if not args.keep_annotations:
        clear_annotations(graph_id, args.node_id)

    vault = ensure_vault(args.vault).resolve()
    print(f"[3/4] Vault = {vault}")

    print(f"[4/4] Running NodeDocumenter on {args.model}...")
    model = ChatOpenAI(model=args.model)
    agent = create_deep_agent(
        model=model,
        subagents=[NODE_DOCUMENTER_SUBAGENT],
        system_prompt=_MAIN_AGENT_PROMPT,
    )

    task_msg = (
        f"Document the node `{args.node_id}` in graph `{graph_id}`. "
        f"vault_path (absolute) = {vault}. "
        f"The subagent must call write_obsidian_note() with this "
        f"vault_path as its first argument. "
        f"overview_hint = {args.overview or '(none — write your own)'}"
    )
    result = agent.invoke(
        {"messages": [{"role": "user", "content": task_msg}]}
    )

    last_msg = result["messages"][-1].content
    print("---")
    print("Agent final reply:")
    print(last_msg)
    print("---")

    folder = KIND_TO_FOLDER.get(node["kind"], "contracts")
    expected_path = vault / folder / f"{node['name']}.md"
    if expected_path.exists():
        print(f"NOTE WRITTEN: {expected_path}")
        print(f"  size: {expected_path.stat().st_size} bytes")
        return 0
    print(
        f"WARNING: expected note not found at {expected_path}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
