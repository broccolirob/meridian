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
    # Bypass build_analyzer_env's HOME isolation for THIS
    # integration test only. The Codex follow-up review
    # removed the .solc-select symlink (it leaked real HOME
    # via readlink), so the isolated temp HOME can't reach
    # ~/.solc-select/artifacts/. Real production runs MUST
    # get the isolated env; this monkeypatch is a TEST-ONLY
    # escape hatch for verifying slither's end-to-end
    # behavior against a TRUSTED fixture (Tier 1).
    import os
    monkeypatch.setattr(
        "src.analyzers.slither.build_analyzer_env",
        lambda **_kw: os.environ.copy(),
    )
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


def test_run_slither_rejects_repo_local_binary(monkeypatch, tmp_path):
    """Codex round-12 fix: if PATH has been poisoned with
    an entry inside the attacker repo, `shutil.which()`
    returns an attacker binary. The wrapper must refuse to
    execute it BEFORE subprocess.run runs. Pin via a fake
    `slither` placed inside the repo; `shutil.which` is
    monkeypatched to return that path."""
    attacker_repo = tmp_path / "attacker-repo"
    attacker_repo.mkdir()
    fake_slither = attacker_repo / "slither"
    fake_slither.write_text("#!/bin/sh\ntouch /tmp/PWNED\n")
    fake_slither.chmod(0o755)

    monkeypatch.setattr(
        "src.analyzers.slither.shutil.which",
        lambda _: str(fake_slither),
    )

    # subprocess.run must NEVER be called — the rejection
    # happens before that. Set up a sentinel that would
    # fire if subprocess.run executed.
    def _fail_if_called(*a, **k):
        raise AssertionError(
            "subprocess.run was called — rejection failed"
        )
    monkeypatch.setattr(
        "src.analyzers.slither.subprocess.run",
        _fail_if_called,
    )

    with pytest.raises(ValueError, match="refusing to execute"):
        run_slither(attacker_repo, tmp_path / "out.sarif")

    # Sentinel file should NOT exist (subprocess never ran).
    assert not Path("/tmp/PWNED").exists()


def test_run_slither_argv_uses_dash_dash_separator(
    monkeypatch, tmp_path,
):
    """Codex follow-up review fix (F3): the positional repo
    path must come AFTER a `--` separator so a path starting
    with `-` cannot be parsed as a slither flag. Mirrors the
    semgrep wrapper. Pin the argv shape."""
    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured["argv"] = list(args[0])
        out = tmp_path / "out.sarif"
        out.write_text('{"runs": []}')
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="", stderr="",
        )

    monkeypatch.setattr(
        "src.analyzers.slither.subprocess.run", fake_run
    )
    monkeypatch.setattr(
        "src.analyzers.slither.shutil.which",
        lambda _: "/fake/slither",
    )

    run_slither(tmp_path, tmp_path / "out.sarif")

    argv = captured["argv"]
    # `--` must appear in argv and the target arg must be
    # AFTER it. (Slither's first positional arg is the
    # target; the `--` defends against `-`-prefixed paths.)
    assert "--" in argv, (
        f"argv missing `--` separator: {argv}"
    )
    sep_idx = argv.index("--")
    target_idx = len(argv) - 1
    assert target_idx > sep_idx, (
        f"target must come after `--`; argv={argv}"
    )


def test_run_slither_passes_trusted_empty_config_file(
    monkeypatch, tmp_path,
):
    """Codex round-19 fix: pin the argv shape — slither must
    be invoked with `--config-file <trusted>` where the path
    points OUTSIDE the repo. Without this, slither defaults
    to reading `slither.config.json` from cwd (= the repo),
    which lets a malicious repo control output destinations
    (`{"json": "/abs/exfil.json"}`) and detector options."""
    captured: dict = {}

    def fake_run(*args, **kwargs):
        argv = list(args[0])
        captured["argv"] = argv
        # Capture the config-file contents at subprocess time
        # so the test can assert it's `{}` (no inherited
        # detector overrides).
        cfg_idx = argv.index("--config-file")
        captured["config_path"] = argv[cfg_idx + 1]
        captured["config_contents"] = Path(
            argv[cfg_idx + 1]
        ).read_text()
        out_idx = argv.index("--sarif")
        Path(argv[out_idx + 1]).write_text('{"runs": []}')
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="", stderr="",
        )

    monkeypatch.setattr(
        "src.analyzers.slither.subprocess.run", fake_run
    )
    monkeypatch.setattr(
        "src.analyzers.slither.shutil.which",
        lambda _: "/fake/slither",
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    run_slither(repo, tmp_path / "out.sarif")

    argv = captured["argv"]
    # --config-file must appear and point at a file OUTSIDE
    # the repo (so a malicious repo-local slither.config.json
    # is not consulted).
    assert "--config-file" in argv, (
        f"missing --config-file: {argv}"
    )
    cfg_path = Path(captured["config_path"])
    assert not cfg_path.is_relative_to(repo.resolve()), (
        f"trusted config must live outside the repo: {cfg_path}"
    )
    # Contents must be empty JSON (no inherited settings).
    assert captured["config_contents"].strip() == "{}", (
        f"trusted config must be empty: "
        f"{captured['config_contents']!r}"
    )
    # And the trusted file is cleaned up after the run
    # (no /tmp litter).
    assert not cfg_path.exists(), (
        f"trusted config should have been unlinked: {cfg_path}"
    )


def test_run_slither_ignores_repo_local_config_file(
    monkeypatch, tmp_path,
):
    """Codex round-19 repro: end-to-end pin that a malicious
    `slither.config.json` in the repo does NOT take effect.
    Uses a fake subprocess.run that asserts the trusted
    `--config-file <outside-tmp>` is passed AND that argv
    does NOT contain the repo-local path."""
    repo = tmp_path / "attacker-repo"
    repo.mkdir()
    # Attacker drops a malicious config trying to write an
    # extra JSON report at /tmp/exfil.json.
    exfil_path = "/tmp/washable-slither-exfil-test.json"
    (repo / "slither.config.json").write_text(
        f'{{"json": "{exfil_path}"}}'
    )

    def fake_run(*args, **kwargs):
        argv = list(args[0])
        # The trusted config must be passed; the repo-local
        # one must NOT be referenced anywhere in argv.
        assert "--config-file" in argv, argv
        cfg_path = argv[argv.index("--config-file") + 1]
        assert not Path(cfg_path).is_relative_to(repo.resolve()), (
            f"argv refers to a repo-local config: {cfg_path}"
        )
        # Confirm slither was NOT pointed at the attacker's
        # config (would have to be either via --config-file or
        # via cwd default lookup; cwd is the repo so the
        # presence of the trusted --config-file is what
        # OVERRIDES the cwd default).
        out_idx = argv.index("--sarif")
        Path(argv[out_idx + 1]).write_text('{"runs": []}')
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="", stderr="",
        )

    monkeypatch.setattr(
        "src.analyzers.slither.subprocess.run", fake_run
    )
    monkeypatch.setattr(
        "src.analyzers.slither.shutil.which",
        lambda _: "/fake/slither",
    )

    run_slither(repo, tmp_path / "out.sarif")

    # The exfil file must NOT have been created — the trusted
    # config has no `json` key, so slither shouldn't write
    # anywhere beyond --sarif <out>.
    assert not Path(exfil_path).exists(), (
        f"exfil file got written: {exfil_path}"
    )


def test_run_slither_unlinks_partial_sarif_on_timeout(
    monkeypatch, tmp_path,
):
    """Mirrors run_semgrep's timeout-cleanup behavior. If
    slither times out mid-write, the partial SARIF is
    unlinked so a retry starts clean and downstream
    `augment_sarif` can't ingest stale partial JSON."""
    out = tmp_path / "partial.sarif"

    def fake_run(*args, **kwargs):
        # Simulate slither writing a partial SARIF then
        # hitting the timeout.
        out.write_text("partial")
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
        run_slither(tmp_path, out, timeout=0.001)

    assert not out.exists(), (
        f"partial SARIF must be unlinked after timeout; "
        f"contents: {out.read_text() if out.exists() else 'unlinked'}"
    )
