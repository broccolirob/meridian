"""Tests for src/analyzers/semgrep.py::run_semgrep.

Three binary-independent tests (always run) cover the
argument-validation defenses: empty rules, missing repo,
project_root containment. Four binary-gated tests (skip if
semgrep not on PATH) cover the end-to-end runner behavior
+ the augment_sarif ingestion contract.
"""

import json
import shutil
from pathlib import Path

import pytest

from src.analyzers.semgrep import run_semgrep
from src.tools import augment_sarif, trailmark_parse

PYTHON_SMOL = (
    Path(__file__).resolve().parent / "fixtures" / "python_smol"
)

_SKIP_NO_BINARY = pytest.mark.skipif(
    shutil.which("semgrep") is None,
    reason="semgrep not on PATH — `uv sync` to install",
)


@pytest.fixture
def fresh_python_smol(tmp_path):
    """Copy the fixture into tmp_path. Hermetic — semgrep's
    side effects (cached rule packs, log artifacts) stay
    user-scope, but copying the input keeps any in-repo
    write attempts contained too."""
    repo = tmp_path / "python_smol"
    shutil.copytree(PYTHON_SMOL, repo)
    return repo


def _local_rules(repo: Path) -> str:
    """Path to the fixture's hermetic rule pack. Using the
    local YAML instead of `p/python` means tests don't
    depend on registry network fetch or semgrep version
    drift — `smol-dangerous-exec` and `smol-hardcoded-secret`
    are pinned to match `bad.py`."""
    return str(repo / "rules.yaml")


# ---- binary-gated end-to-end tests ----------------------------

@_SKIP_NO_BINARY
def test_run_semgrep_produces_valid_sarif(
    fresh_python_smol, tmp_path,
):
    """Chunk 4.8 success criterion (part 1): runs on a small
    Python fixture and produces SARIF with at least one
    finding (the `exec(input())` and hardcoded `API_KEY`
    patterns are caught by p/python deterministically)."""
    out = tmp_path / "semgrep.sarif"
    written = run_semgrep(
        fresh_python_smol, out, rules=[_local_rules(fresh_python_smol)],
    )
    assert written == out.resolve()
    assert written.exists()
    assert written.stat().st_size > 0

    data = json.loads(written.read_text())
    assert "sarif" in data.get("$schema", "").lower()
    assert data.get("runs", []), "expected at least one run"
    results = data["runs"][0].get("results", [])
    assert len(results) >= 1, (
        f"expected ≥1 finding in python_smol; got: {results}"
    )


@_SKIP_NO_BINARY
def test_run_semgrep_output_is_ingestable_by_augment_sarif(
    fresh_python_smol, tmp_path,
):
    """Chunk 4.8 success criterion (part 2): SARIF that
    `augment_sarif` ingests. End-to-end: parse the Python
    fixture as a Trailmark graph, run semgrep, augment,
    verify a sarif:* subgraph was created and findings were
    tallied (matched + unmatched)."""
    cache_root = tmp_path / "cache"
    gid = trailmark_parse(
        str(fresh_python_smol),
        language="python",
        cache_root=cache_root,
    )
    out = tmp_path / "semgrep.sarif"
    run_semgrep(fresh_python_smol, out, rules=[_local_rules(fresh_python_smol)])

    result = augment_sarif(gid, out, cache_root=cache_root)
    assert result["subgraphs_created"], (
        f"expected a sarif:* subgraph; got {result}"
    )
    # Pin the actual chunk 4.8 success criterion: findings
    # must MATCH graph nodes, not just be tallied. Asserting
    # `matched + unmatched >= 1` would silently pass even
    # when semgrep emits absolute URIs that augment_sarif
    # can't reconcile (chunk-4.8 review finding 1: this is
    # the exact silent footgun the chunk's wiring was meant
    # to prevent).
    assert result["matched_findings"] >= 1, (
        f"expected ≥1 finding attached to a graph node; got "
        f"{result}. If unmatched > 0 but matched == 0, "
        f"semgrep's SARIF URIs aren't matching Trailmark's "
        f"file-path normalization — likely absolute URIs."
    )


@_SKIP_NO_BINARY
def test_run_semgrep_unlinks_stale_sarif(
    fresh_python_smol, tmp_path,
):
    """Pre-unlink defense: if a prior run left a stale SARIF,
    run_semgrep replaces it rather than appending or returning
    the old file as if it were this run's output."""
    out = tmp_path / "semgrep.sarif"
    out.write_text('{"stale": true}')
    run_semgrep(fresh_python_smol, out, rules=[_local_rules(fresh_python_smol)])
    data = json.loads(out.read_text())
    assert "stale" not in data
    assert "sarif" in data.get("$schema", "").lower()


@_SKIP_NO_BINARY
def test_run_semgrep_supports_multiple_rule_configs(
    fresh_python_smol, tmp_path,
):
    """`rules` is list[str] specifically so callers can stack
    rule packs. Pin the contract that BOTH `--config` args
    are honored — not just the first.

    Uses two SPLIT fixture files (rules-exec.yaml +
    rules-secret.yaml) so each `--config` contributes a
    DISTINCT rule ID. Asserting both IDs appear in the
    results proves the wrapper's argv-extension loop emits
    both `--config` flags and semgrep honors them. A single
    combined file passed twice would not catch a silent
    --config override (chunk-4.8 review finding 2)."""
    out = tmp_path / "semgrep.sarif"
    rules_exec = str(fresh_python_smol / "rules-exec.yaml")
    rules_secret = str(fresh_python_smol / "rules-secret.yaml")
    run_semgrep(
        fresh_python_smol, out,
        rules=[rules_exec, rules_secret],
    )
    data = json.loads(out.read_text())
    assert data.get("runs", []), "expected at least one run"
    results = data["runs"][0].get("results", [])
    # Semgrep emits ruleId as the bare rule ID for local
    # YAML files (no dot-prefix). Both must appear, proving
    # neither --config was silently overridden.
    rule_ids = {r.get("ruleId", "") for r in results}
    assert "smol-dangerous-exec" in rule_ids, (
        f"--config rules-exec.yaml not honored; rule_ids={rule_ids}"
    )
    assert "smol-hardcoded-secret" in rule_ids, (
        f"--config rules-secret.yaml not honored; rule_ids={rule_ids}"
    )


# ---- binary-independent defense tests -------------------------

def test_run_semgrep_rejects_empty_rules(tmp_path):
    """Empty rules list raises ValueError BEFORE invoking
    semgrep. Semgrep with no `--config` prints help and
    exits 0, which would silently produce a zero-finding
    SARIF — failing loud at the wrapper boundary is the
    correct posture.

    Chunk-4.8 review finding 3: also reject empty/whitespace
    strings AND non-string entries inside the list. Semgrep
    with `--config ""` silently produces a zero-finding
    SARIF (config-resolution fails, scan exits 0). Same
    failure mode the empty-list check guards against."""
    out = tmp_path / "semgrep.sarif"
    bad_inputs = (
        [],            # empty list
        [""],          # empty string
        ["   "],       # whitespace only
        ["p/python", ""],   # mixed: one valid, one empty
        [None],        # type: ignore[list-item]
    )
    for bad in bad_inputs:
        with pytest.raises(ValueError, match="rules must be"):
            run_semgrep(tmp_path, out, rules=bad)  # type: ignore[arg-type]


def test_run_semgrep_rejects_missing_repo(tmp_path):
    """Mirrors run_slither: FileNotFoundError on
    nonexistent repo_path BEFORE invoking semgrep. The
    semgrep binary itself would give a less helpful error
    message; surfacing it at the wrapper is cleaner."""
    out = tmp_path / "semgrep.sarif"
    with pytest.raises(FileNotFoundError, match="repo path"):
        run_semgrep(
            tmp_path / "nonexistent",
            out,
            rules=["p/python"],   # never invoked; rule is irrelevant
        )


def test_run_semgrep_rejects_project_root_outside_repo(tmp_path):
    """Containment defense: passing a `project_root` that
    doesn't contain `repo_path` raises ValueError before
    subprocess.run. Mismatched roots produce `../...` SARIF
    URIs that augment_sarif silently can't match against the
    graph — fail loud here instead. Same defense as
    run_slither."""
    repo = tmp_path / "inside" / "repo"
    repo.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    out = tmp_path / "semgrep.sarif"
    with pytest.raises(ValueError, match="must be inside"):
        run_semgrep(
            repo, out,
            rules=["p/python"],   # never invoked; rule is irrelevant
            project_root=outside,
        )
