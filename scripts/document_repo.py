"""Multi-node driver — invoke dispatch_topo end-to-end.

Mirrors document_one_node.py but loops over the whole graph in
topological order. Use to dogfood the full Phase 2 path.

Usage:
    uv run python scripts/document_repo.py [--repo PATH] [--vault PATH]
                                            [--model NAME] [--concurrency N]
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

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
    complexity_hotspots,
    run_preanalysis,
    trailmark_parse,
)

# Risk-note filename allowlist — matches the three risk
# notes RiskSynthesizer is contracted to produce (see
# `src/subagents.py:_RISK_SYNTHESIZER_PROMPT`). The
# verification gate uses this to reject path claims that
# don't correspond to a real RiskSynthesizer output (Codex
# follow-up F2).
_EXPECTED_RISK_NOTE_NAMES = frozenset({
    "hotspots.md",
    "delegatecall-sites.md",
    "reentrancy-candidates.md",
})

DEFAULT_REPO = "tests/fixtures/tier0_erc4626"
DEFAULT_VAULT = ".meridian/vaults/tier0"


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

    # Unconditional stale-risk-note delete (Codex round-7 F1).
    # Risk notes are derived artifacts produced by Phase 4b;
    # ANY rerun must start with no stale ones from a previous
    # run. Previously the wipe was inside `if phase4_ok:`,
    # which skipped it when analyzers failed — leaving stale
    # files for `write_root_moc` to index into the partial
    # vault.
    pre_risks_dir = vault / "risks"
    if pre_risks_dir.is_dir():
        for stale in pre_risks_dir.glob("*.md"):
            stale.unlink(missing_ok=True)

    # Codex round-9 fix: also wipe `.audit/risk-staging/` —
    # RiskSynthesizer now writes to a per-run staging
    # subdir under there (see `dispatch_risk_synthesis`).
    # Late-running workers from a PREVIOUS process may have
    # left files in old staging dirs. They're not indexed
    # by Obsidian (dotted parent) or by `write_root_moc`,
    # but cleaning them keeps disk usage bounded.
    staging_parent = vault / ".audit" / "risk-staging"
    if staging_parent.is_dir():
        shutil.rmtree(staging_parent, ignore_errors=True)

    # Phase 4a: analyzer pre-pass (slither → SARIF → augment →
    # preanalysis). All-or-nothing: any failure skips Phase 4b
    # (RiskSynthesizer) but allows the rest of the pipeline to
    # continue. Auditor still gets node + flow notes; just no
    # vault/risks/ folder. rc=2 (advisory) signals the
    # degradation. Common cause of skip: slither binary missing.
    sarif_path = vault / ".audit" / "slither.sarif"
    phase4_ok = True
    # Capture augment_sarif's matched-findings count for the
    # Phase 4b consistency check (Codex follow-up F3). If
    # SARIF attached findings but RiskSynthesizer returns an
    # empty path list, that's a misleading-success path —
    # the LLM was prompt-injected or ignored its mandate.
    augment_result: dict[str, Any] | None = None
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
        augment_result = augment_sarif(graph_id, sarif_path)
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
        # Note: vault/risks/*.md was already wiped
        # unconditionally near the top of main() (Codex
        # round-7 F1). No additional pre-dispatch delete
        # needed here.
        risk_result = dispatch_risk_synthesis(
            graph_id, str(vault), model=args.model,
        )
        risks_ok = risk_result["ok"]
        # Codex round-10 fix: the dispatcher no longer
        # promotes. It returns staging metadata; verification
        # runs against the staged inventory, then THIS code
        # promotes only on full pass. Final `vault/risks/`
        # stays untouched until verification has finished.
        # That closes the promotion-to-wipe exposure window
        # where a crash or Obsidian sync could surface
        # rejected attacker content.
        staging_root_str = risk_result.get("staging_root")
        staging_root: Path | None = (
            Path(staging_root_str)
            if isinstance(staging_root_str, str)
            else None
        )
        # The staging inventory is the source of truth.
        staging_risks_dir: Path | None = (
            (staging_root / "risks").resolve()
            if staging_root is not None
            else None
        )
        if risks_ok:
            # Parse the dispatcher's reply (JSON list of
            # paths in STAGING, not vault). Each path must
            # resolve under the staging risks dir, end in
            # `.md`, AND have an allowlisted filename.
            reply = risk_result.get("reply", "")
            try:
                paths = json.loads(reply) if reply else []
                if not isinstance(paths, list):
                    raise ValueError(
                        f"expected JSON list, got {type(paths).__name__}"
                    )

                def _is_valid_staged_path(p: Any) -> bool:
                    if not isinstance(p, str):
                        return False
                    if staging_risks_dir is None:
                        return False
                    try:
                        resolved = Path(p).resolve()
                    except (OSError, RuntimeError):
                        return False
                    if not resolved.is_relative_to(staging_risks_dir):
                        return False
                    if resolved.suffix != ".md":
                        return False
                    # Filename must be in the canonical
                    # allowlist (defense vs. prompt-injected
                    # writes to `staging/risks/<custom>.md`).
                    if resolved.name not in _EXPECTED_RISK_NOTE_NAMES:
                        return False
                    return resolved.is_file()

                invalid = [
                    p for p in paths
                    if not _is_valid_staged_path(p)
                ]
                # An empty path list is OK only when nothing
                # warrants synthesis. "Something to
                # synthesize" means SARIF matched findings
                # OR complexity hotspots are non-empty.
                matched = (
                    augment_result.get("matched_findings", 0)
                    if augment_result is not None
                    else 0
                )
                hotspots_present = False
                if phase4_ok:
                    try:
                        hotspots_present = bool(
                            complexity_hotspots(graph_id, threshold=5)
                        )
                    except Exception as e:
                        print(
                            f"      WARNING: hotspot probe "
                            f"failed (treating as no hotspots): "
                            f"{e}",
                            file=sys.stderr,
                        )
                should_have_notes = matched > 0 or hotspots_present
                # Bidirectional gate (Codex round 4):
                # should_have_notes=True → claimed must EQUAL
                # the expected set. False → claimed must be
                # EMPTY (clean graph + any output is
                # suspicious).
                claimed_names = {Path(p).name for p in paths}

                # Inventory cross-check on STAGING (Codex
                # round 5 + 10): the staging dir's *.md
                # filenames must equal what the dispatcher
                # reported. Catches the dispatcher itself
                # disagreeing with the filesystem (shouldn't
                # happen since the dispatcher reads the dir
                # to build its reply, but defense in depth).
                actual_names: set[str] = set()
                if staging_risks_dir is not None and staging_risks_dir.is_dir():
                    actual_names = {
                        p.name for p in staging_risks_dir.glob("*.md")
                    }

                if should_have_notes:
                    gate_violation = (
                        claimed_names != _EXPECTED_RISK_NOTE_NAMES
                    )
                else:
                    gate_violation = bool(claimed_names)
                inventory_mismatch = actual_names != claimed_names

                if invalid:
                    print(
                        f"      WARNING: dispatcher reported "
                        f"{len(paths)} risk note(s) but "
                        f"{len(invalid)} aren't in the "
                        f"expected allowlist "
                        f"{sorted(_EXPECTED_RISK_NOTE_NAMES)} "
                        f"under staging risks/: {invalid}",
                        file=sys.stderr,
                    )
                    risks_ok = False
                elif inventory_mismatch:
                    print(
                        f"      WARNING: dispatcher's claimed "
                        f"risk-note set {sorted(claimed_names)} "
                        f"does not match actual files in "
                        f"staging: {sorted(actual_names)}. "
                        f"Likely a bug or a race; not "
                        f"promoting to vault/risks/.",
                        file=sys.stderr,
                    )
                    risks_ok = False
                elif gate_violation and should_have_notes:
                    missing = sorted(
                        _EXPECTED_RISK_NOTE_NAMES - claimed_names
                    )
                    extra = sorted(
                        claimed_names - _EXPECTED_RISK_NOTE_NAMES
                    )
                    print(
                        f"      WARNING: RiskSynthesizer "
                        f"produced {sorted(claimed_names)} but "
                        f"all three notes were required "
                        f"(matched_findings={matched}, "
                        f"hotspots_present={hotspots_present})."
                        f" Missing: {missing}. Unexpected: "
                        f"{extra}. Misleading-success path: "
                        f"the LLM ignored its mandate or was "
                        f"prompt-injected. Not promoting to "
                        f"vault/risks/.",
                        file=sys.stderr,
                    )
                    risks_ok = False
                elif gate_violation and not should_have_notes:
                    print(
                        f"      WARNING: RiskSynthesizer "
                        f"produced {sorted(claimed_names)} on "
                        f"a clean graph (matched_findings={matched}, "
                        f"hotspots_present={hotspots_present}). "
                        f"The prompt contract requires `[]` "
                        f"when nothing's worth synthesizing. "
                        f"Likely prompt-injected; the note "
                        f"content is suspect. Not promoting to "
                        f"vault/risks/.",
                        file=sys.stderr,
                    )
                    risks_ok = False
                else:
                    # ALL CHECKS PASSED. Promote allowlisted
                    # staging files → vault/risks/. Defense
                    # in depth: even though we validated the
                    # set above, we still iterate only
                    # allowlisted names so an inventory race
                    # can't sneak in an off-list file.
                    vault_risks = vault / "risks"
                    vault_risks.mkdir(parents=True, exist_ok=True)
                    promoted_count = 0
                    if staging_risks_dir is not None:
                        for name in _EXPECTED_RISK_NOTE_NAMES:
                            src = staging_risks_dir / name
                            if src.is_file():
                                shutil.move(
                                    str(src),
                                    str(vault_risks / name),
                                )
                                promoted_count += 1
                    print(
                        f"      OK ({promoted_count} risk note(s) "
                        f"promoted from staging)"
                    )
            except (ValueError, TypeError) as e:
                # Non-JSON reply / wrong shape from the
                # dispatcher. Should not happen in normal
                # operation (the dispatcher always emits
                # valid JSON), but degrade safely.
                print(
                    f"      WARNING: risk-synthesizer reply "
                    f"not parseable as JSON list: {e}. Not "
                    f"promoting; staging will be cleaned.",
                    file=sys.stderr,
                )
                risks_ok = False
        else:
            print(
                f"      WARNING: risk synthesis failed: "
                f"{risk_result.get('error', 'unknown')}",
                file=sys.stderr,
            )

        # Always clean staging: on success the allowlisted
        # files have been MOVED out; on failure nothing was
        # promoted. Either way, staging is no longer needed.
        if staging_root is not None and staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)

    # Codex round-8 fix (F1) + round-10 update: vault/risks/
    # cleanup on verification failure. With round 10's
    # caller-side promotion, vault/risks/ is only written
    # when ALL gates pass — so on failure, the dir is
    # typically empty already (pre-dispatch wipe + no
    # promotion). This pass remains as defense-in-depth:
    # if some other code path (future bug, external write)
    # populates vault/risks/, we still wipe before
    # write_root_moc indexes anything suspect.
    if not risks_ok:
        risks_dir = vault / "risks"
        if risks_dir.is_dir():
            for suspect in risks_dir.glob("*.md"):
                suspect.unlink(missing_ok=True)
            try:
                risks_dir.rmdir()
            except OSError:
                pass

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
