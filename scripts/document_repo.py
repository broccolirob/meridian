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
    dispatch_flows,
    dispatch_risk_synthesis,
    dispatch_topo,
)
from src.analyzers.slither import run_slither  # noqa: E402
from src.render.moc import write_root_moc  # noqa: E402
from src.render.obsidian import ensure_vault  # noqa: E402
from src.tools import (  # noqa: E402
    augment_sarif,
    run_preanalysis,
    trailmark_parse,
)

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

    # rc=4 for setup errors keeps rc=2 free for the Phase 4
    # advisory degradation path (analyzers skipped or
    # RiskSynthesizer failed). CI consumers can then
    # distinguish "nothing to ship — fix the setup" from
    # "ship the partial vault, risk notes pending."
    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        return 4
    if not Path(args.repo).is_dir():
        print(f"ERROR: not a directory: {args.repo}", file=sys.stderr)
        return 4

    print(f"Parsing {args.repo}...")
    graph_id = trailmark_parse(args.repo, language="solidity")
    print(f"      graph_id = {graph_id}")

    vault = ensure_vault(args.vault).resolve()
    print(f"Vault = {vault}")

    # Phase 4a: analyzer pre-pass (slither → SARIF → augment →
    # preanalysis). All-or-nothing: any failure skips Phase 4b
    # (RiskSynthesizer) but allows the rest of the pipeline to
    # continue. Auditor still gets node + flow notes; just no
    # vault/risks/ folder. rc=2 (advisory) signals the
    # degradation. Common cause of skip: slither binary missing.
    sarif_path = vault / ".audit" / "slither.sarif"
    phase4_ok = True
    print("---")
    print(f"Running Phase 4 analyzers (SARIF -> {sarif_path})...")
    try:
        # Defend against a regular file shadowing the .audit
        # directory (e.g., from a confused prior run). mkdir
        # would raise the cryptic FileExistsError [Errno 17];
        # a concrete RuntimeError makes the cleanup obvious.
        if (
            sarif_path.parent.exists()
            and not sarif_path.parent.is_dir()
        ):
            raise RuntimeError(
                f"{sarif_path.parent} exists but is not a "
                f"directory; remove the file and rerun"
            )
        sarif_path.parent.mkdir(parents=True, exist_ok=True)
        run_slither(args.repo, sarif_path)
        augment_sarif(graph_id, sarif_path)
        run_preanalysis(graph_id)
        print("      OK")
    except Exception as e:
        print(
            f"      WARNING: Phase 4 analyzers failed: {e}",
            file=sys.stderr,
        )
        print(
            "      Skipping risk synthesis; continuing with node "
            "+ flow notes.",
            file=sys.stderr,
        )
        phase4_ok = False

    print("---")
    print(
        f"Dispatching node notes (cap={args.concurrency}, "
        f"model={args.model})..."
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

    # Phase 3 flow dispatch — enumerate attack-surface entrypoints
    # and dispatch FlowTracer per entrypoint. Leaf entrypoints
    # (no outgoing callees) are filtered out by default.
    print("---")
    print(
        f"Tracing flows (cap={args.concurrency}, "
        f"model={args.model})..."
    )

    def _flow_progress(i: int, n: int, eid: str) -> None:
        print(f"  [{i}/{n}] {eid}")

    flow_result = dispatch_flows(
        graph_id,
        str(vault),
        model=args.model,
        concurrency_cap=args.concurrency,
        on_progress=_flow_progress,
    )
    print(f"FLOWS: {flow_result['entrypoint_count']}")
    print(f"OK   : {len(flow_result['successes'])}")
    print(f"FAIL : {len(flow_result['failures'])}")
    for f in flow_result["failures"]:
        print(f"  FAIL {f['node_id']}: {f['error']}")

    # Phase 4b: RiskSynthesizer dispatch. Single-threaded;
    # MUST run after NodeDocumenter + FlowTracer waves drain
    # (RiskSynthesizer issues 15-45 annotate calls per
    # invocation that would serialize on _ANNOTATE_LOCK with
    # concurrent workers — see RISK_SYNTHESIZER_SUBAGENT
    # description). Skip when Phase 4a was degraded.
    risks_ok = True
    if phase4_ok:
        print("---")
        print(f"Dispatching risk synthesis (model={args.model})...")
        risk_result = dispatch_risk_synthesis(
            graph_id, str(vault), model=args.model,
        )
        risks_ok = risk_result["ok"]
        if risks_ok:
            print("      OK")
        else:
            print(
                f"      WARNING: risk synthesis failed: "
                f"{risk_result.get('error', 'unknown')}",
                file=sys.stderr,
            )

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

    # rc semantics:
    #   0 = all phases clean
    #   1 = hard failures in dispatch_topo or dispatch_flows
    #       (silent topo/flow failures would leave CI green
    #       while shipping incomplete vaults)
    #   2 = advisory: Phase 4 degraded (analyzers skipped or
    #       RiskSynthesizer failed) but core pipeline succeeded.
    #       Vault has node + flow notes; just no risks/.
    #   4 = setup error: missing api key or bad --repo path
    #       (returned early above; no vault to ship).
    if result["failures"] or flow_result["failures"]:
        return 1
    if not phase4_ok or not risks_ok:
        return 2
    return 0


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
    # os._exit bypasses Python's normal shutdown (which waits
    # for non-daemon ThreadPoolExecutor workers). A wedged LLM
    # call leaves a worker stuck in invoke(); without this,
    # the process would hang after the summary prints. Flush
    # stdio first so the summary isn't truncated.
    _rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_rc)
