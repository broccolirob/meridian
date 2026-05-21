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

from src.agent import _wrap_subagent_writers  # noqa: E402
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
- Pass graph_id, node_id, and overview_hint verbatim from the user
  message. The vault path is bound by the harness at subagent
  construction time and is NOT an LLM-callable parameter.
- The subagent MUST call render_and_write_node_note() to persist
  the note to disk. If its reply does not include an absolute file
  path, dispatch it AGAIN with explicit instructions to actually
  call render_and_write_node_note.
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
    # Wrap the subagent's writer tools so vault_path is bound
    # by the harness (a trusted Python value), NOT exposed as
    # an LLM-callable arg. Without this wrapping, a prompt-
    # injected agent could supply vault_path=/etc/passwd via
    # the tool schema and write outside the intended vault.
    # Mirrors the production wrapping done by build_agent in
    # src/agent.py — kept separate here so the harness can use
    # its own single-node _MAIN_AGENT_PROMPT.
    agent = create_deep_agent(
        model=model,
        subagents=[
            _wrap_subagent_writers(NODE_DOCUMENTER_SUBAGENT, str(vault))
        ],
        system_prompt=_MAIN_AGENT_PROMPT,
    )

    task_msg = (
        f"Document the node `{args.node_id}` in graph `{graph_id}`. "
        f"Dispatch the `node-documenter` subagent via the `task` "
        f"tool — do not generate note content yourself. "
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

    # Methods don't get their own note — the subagent redirects to the
    # parent contract/library. Mirror that here so checking
    # `ERC4626:ERC4626.deposit` looks for `contracts/ERC4626.md`,
    # not `contracts/deposit.md`.
    expected_node = node
    if node["kind"] == "method":
        parent_id = args.node_id.rsplit(".", 1)[0]
        try:
            expected_node = get_node(graph_id, parent_id)
            print(
                f"      method redirected to parent "
                f"{expected_node['kind']} {expected_node['name']}"
            )
        except KeyError:
            # Parent missing — fall back to the method's own routing
            # (will likely 404 below, which is the right signal)
            pass

    folder = KIND_TO_FOLDER.get(expected_node["kind"], "contracts")
    expected_path = vault / folder / f"{expected_node['name']}.md"
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
    # os._exit bypasses Python's normal shutdown (which waits
    # for non-daemon ThreadPoolExecutor workers). A wedged LLM
    # call leaves a worker stuck in invoke(); without this,
    # the process would hang after the agent reply prints.
    # Flush stdio first so output isn't truncated.
    _rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_rc)
