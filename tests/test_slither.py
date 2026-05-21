"""Tests for src/analyzers/slither.py.

End-to-end test runs slither on Tier 1 fixtures — requires
slither-analyzer (in [project].dependencies via `uv sync`) AND
solc 0.5.16 (manual setup: `solc-select install 0.5.16 &&
solc-select use 0.5.16`). Skips gracefully if either is missing
so local devs without the full setup get green tests instead of
failures; CI must have both installed.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.analyzers.slither import run_slither

TIER1_CONTRACTS = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "tier1_uniswap_v2"
    / "contracts"
)


def _solc_516_available() -> bool:
    """True if solc-select has 0.5.16 installed."""
    if shutil.which("solc-select") is None:
        return False
    try:
        proc = subprocess.run(
            ["solc-select", "versions"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        return "0.5.16" in proc.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


@pytest.mark.skipif(
    shutil.which("slither") is None,
    reason="slither not on PATH — `uv sync` to install",
)
@pytest.mark.skipif(
    not _solc_516_available(),
    reason=(
        "solc 0.5.16 not installed — "
        "`solc-select install 0.5.16 && solc-select use 0.5.16`"
    ),
)
def test_run_slither_on_tier1_produces_sarif_with_findings(
    monkeypatch, tmp_path
):
    """Chunk 4.1 success criterion: running slither on Tier 1
    produces a valid SARIF file with at least one finding.

    Copies the Tier 1 fixture to tmp_path before running slither
    because run_slither uses cwd=repo to produce repo-relative
    SARIF URIs (needed by chunk 4.2's augment_sarif matcher).
    With cwd=repo, slither writes crytic-export/ into the repo;
    copying to tmp avoids polluting the fixture.

    Uses SOLC_VERSION env var to scope the solc selection to
    THIS test's subprocess only — `solc-select use` would
    mutate ~/.solc-select/global-version persistently, silently
    changing the dev's solc setup across the test run."""
    monkeypatch.setenv("SOLC_VERSION", "0.5.16")
    repo = tmp_path / "tier1"
    shutil.copytree(TIER1_CONTRACTS.parent, repo)

    out = tmp_path / "tier1.sarif"
    written = run_slither(repo / "contracts", out, timeout=120.0)

    assert written == out.resolve()
    assert written.exists()

    data = json.loads(written.read_text())
    schema = data.get("$schema", "")
    assert "sarif" in schema.lower(), (
        f"expected SARIF schema in $schema; got {schema!r}"
    )
    runs = data.get("runs", [])
    assert runs, "SARIF should have at least one run"
    results = runs[0].get("results", [])
    assert len(results) >= 1, (
        f"Tier 1 (Uniswap V2) should produce >= 1 slither "
        f"finding; got {len(results)}"
    )


def test_run_slither_raises_when_repo_missing(tmp_path):
    """Nonexistent repo path → FileNotFoundError before any
    subprocess call."""
    with pytest.raises(FileNotFoundError, match="does not exist"):
        run_slither(
            tmp_path / "does-not-exist", tmp_path / "out.sarif"
        )


def test_run_slither_raises_when_slither_missing(monkeypatch, tmp_path):
    """If slither isn't on PATH, raise FileNotFoundError with a
    helpful install message."""
    monkeypatch.setattr(
        "src.analyzers.slither.shutil.which", lambda _: None
    )
    with pytest.raises(FileNotFoundError, match="slither not found"):
        run_slither(tmp_path, tmp_path / "out.sarif")


def test_run_slither_unlinks_stale_sarif_before_run(
    monkeypatch, tmp_path
):
    """Defense against stale-SARIF false-success: if a prior
    run left a SARIF file at out_sarif AND the current
    invocation fails before writing, the function must NOT
    return the stale path as if successful. Verified by
    pre-creating a stale file, monkey-patching subprocess.run
    to not write anything, and asserting CalledProcessError."""
    out = tmp_path / "stale.sarif"
    out.write_text('{"runs":[{"results":[{"stale":true}]}]}')
    assert out.exists() and out.stat().st_size > 0

    # Fake subprocess that doesn't write any file (simulates
    # slither crashing before SARIF output, e.g., solc compile
    # error). Returns exit code 1, no stdout/stderr.
    def fake_run(*args, **kwargs):
        # Verify the stale file was unlinked before this ran.
        assert not Path(kwargs.get("args", args[0])[3]).exists(), (
            "stale SARIF should have been unlinked BEFORE "
            "subprocess.run; otherwise existence check is "
            "compromised"
        )
        return subprocess.CompletedProcess(
            args=args[0], returncode=1, stdout="", stderr="fail"
        )

    monkeypatch.setattr(
        "src.analyzers.slither.subprocess.run", fake_run
    )
    monkeypatch.setattr(
        "src.analyzers.slither.shutil.which",
        lambda _: "/fake/slither",
    )

    with pytest.raises(subprocess.CalledProcessError):
        run_slither(tmp_path, out)


def test_run_slither_rejects_zero_byte_sarif(monkeypatch, tmp_path):
    """Defense against partial-write false-success: a 0-byte
    SARIF (e.g., slither killed mid-write by timeout, OOM, or
    SIGKILL) must raise CalledProcessError. Without the size
    check, downstream `augment_sarif` would crash on
    JSONDecodeError far from the root cause."""
    out = tmp_path / "partial.sarif"

    def fake_run(*args, **kwargs):
        # Create a 0-byte file (simulates "slither opened the
        # file for writing, then was killed before any content
        # was flushed").
        out.touch()
        return subprocess.CompletedProcess(
            args=args[0], returncode=-9, stdout="", stderr="killed"
        )

    monkeypatch.setattr(
        "src.analyzers.slither.subprocess.run", fake_run
    )
    monkeypatch.setattr(
        "src.analyzers.slither.shutil.which",
        lambda _: "/fake/slither",
    )

    with pytest.raises(subprocess.CalledProcessError):
        run_slither(tmp_path, out)


def test_run_slither_propagates_timeout(monkeypatch, tmp_path):
    """subprocess.TimeoutExpired must bubble to the caller so
    the operator sees the real cause. Wrapping or swallowing
    timeouts would hide wedged-slither scenarios behind
    confusing 'no SARIF written' messages."""
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=args[0], timeout=kwargs.get("timeout", 0)
        )

    monkeypatch.setattr(
        "src.analyzers.slither.subprocess.run", fake_run
    )
    monkeypatch.setattr(
        "src.analyzers.slither.shutil.which",
        lambda _: "/fake/slither",
    )

    with pytest.raises(subprocess.TimeoutExpired):
        run_slither(tmp_path, tmp_path / "out.sarif", timeout=0.001)
