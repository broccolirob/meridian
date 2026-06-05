"""Tests for src/analyzers/env.py and its wiring into
run_slither + run_semgrep subprocess invocations.

The auditor threat model:
- scripts/document_repo.py loads .env BEFORE Phase 4 runs.
- OPENAI_API_KEY and similar credentials land in os.environ.
- Without `env=` allowlisting, every analyzer subprocess
  inherits the full parent env.
- Slither invokes crytic-compile, which may execute project
  build tooling on attacker-supplied repos.

These tests pin the contract: the allowlist works AND both
wrappers actually use it (not just imported).
"""

import subprocess
from pathlib import Path

import pytest

from src.analyzers.env import build_analyzer_env


def test_build_analyzer_env_strips_openai_api_key(monkeypatch):
    """The driver bug — OPENAI_API_KEY must not pass through.
    Allowlist approach: anything NOT in the allowlist is
    dropped, so new credential vars added later are dropped
    by default."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-1234")
    monkeypatch.setenv("PATH", "/usr/bin:/usr/local/bin")
    env = build_analyzer_env()
    assert "OPENAI_API_KEY" not in env
    assert env.get("PATH") == "/usr/bin:/usr/local/bin"


def test_build_analyzer_env_strips_common_secrets(monkeypatch):
    """Other credential-shaped env vars also dropped.
    Allowlist > denylist: this isn't an exhaustive list,
    it's a representative sample of what dev shells leak."""
    secrets = {
        "GITHUB_TOKEN": "ghp_xxx",
        "AWS_ACCESS_KEY_ID": "AKIA...",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "ANTHROPIC_API_KEY": "sk-ant-xxx",
        "GOOGLE_APPLICATION_CREDENTIALS": "/path/to/creds.json",
        "NPM_TOKEN": "npm_xxx",
        "DATABASE_URL": "postgres://user:pass@host/db",
        "SLACK_BOT_TOKEN": "xoxb-xxx",
    }
    for k, v in secrets.items():
        monkeypatch.setenv(k, v)
    env = build_analyzer_env()
    for k in secrets:
        assert k not in env, f"{k} leaked into analyzer env"


def test_build_analyzer_env_keeps_required_vars(monkeypatch):
    """PATH, locale, SOLC_VERSION etc. must pass through —
    analyzers refuse to run without these or fall back to
    unsafe defaults. HOME is set (see separate test) but
    its value is REPLACED with an isolated temp dir."""
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("SOLC_VERSION", "0.5.16")
    env = build_analyzer_env()
    assert env["PATH"] == "/usr/bin"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["SOLC_VERSION"] == "0.5.16"
    # HOME is in the dict but with the temp-dir value, NOT
    # the parent's HOME value.
    assert "HOME" in env


def test_build_analyzer_env_replaces_home_with_isolated_temp_dir(
    monkeypatch,
):
    """Codex follow-up review fix (F2): real HOME exposes
    credential files (`~/.ssh`, `~/.aws/credentials`,
    `~/.npmrc`). build_analyzer_env replaces HOME with an
    isolated temp dir so HOME-honoring tools (semgrep cache,
    git config, etc.) cannot reach the auditor's real
    home."""
    monkeypatch.setenv("HOME", "/Users/fake-real-home")
    env = build_analyzer_env()
    assert env["HOME"] != "/Users/fake-real-home", (
        "HOME must be replaced, not passed through verbatim"
    )
    # Temp HOME exists and is a directory.
    temp_home = Path(env["HOME"])
    assert temp_home.is_dir(), (
        f"replacement HOME must exist as a dir: {temp_home}"
    )
    # Temp HOME prefix marks it as meridian-managed
    # (not the real one or some random other path).
    assert "meridian-analyzer-home" in temp_home.name


def test_isolated_home_contains_no_symlinks_into_real_home(monkeypatch):
    """Codex follow-up review fix (F1): an earlier revision
    symlinked `.solc-select` from real HOME into the
    isolated temp HOME. That leaked the real HOME path via
    readlink() AND let writes to the symlinked target
    propagate back into real `~/.solc-select` (compiler-
    poisoning). The fix removed the symlink entirely.

    Pin the no-symlinks contract: the isolated temp HOME
    contains NO entries that point into real HOME."""
    env = build_analyzer_env()
    temp_home = Path(env["HOME"])
    # Temp HOME exists and is empty (or contains only its
    # own non-symlink content).
    assert temp_home.is_dir()
    for entry in temp_home.iterdir():
        assert not entry.is_symlink(), (
            f"isolated HOME must contain no symlinks (found "
            f"{entry} -> {entry.readlink() if entry.is_symlink() else None})"
        )


def test_build_analyzer_env_strips_pyenv_root_and_virtual_env(
    monkeypatch,
):
    """Codex follow-up review fix (F1): PYENV_ROOT and
    VIRTUAL_ENV typically point under real HOME, which
    enables path-traversal discovery of credential files
    (`$PYENV_ROOT/../.ssh/id_rsa`). Dropped from the
    allowlist."""
    monkeypatch.setenv("PYENV_ROOT", "/Users/fake/.pyenv")
    monkeypatch.setenv("VIRTUAL_ENV", "/Users/fake/proj/.venv")
    env = build_analyzer_env()
    assert "PYENV_ROOT" not in env
    assert "VIRTUAL_ENV" not in env


def test_build_analyzer_env_strips_dyld_paths(monkeypatch):
    """Codex round-3 fix: DYLD_LIBRARY_PATH and
    DYLD_FALLBACK_LIBRARY_PATH typically point under real
    HOME (especially when set by a venv/conda wrapper).
    Dropped from the allowlist."""
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/Users/fake/.venv/lib")
    monkeypatch.setenv(
        "DYLD_FALLBACK_LIBRARY_PATH", "/Users/fake/.local/lib"
    )
    env = build_analyzer_env()
    assert "DYLD_LIBRARY_PATH" not in env
    assert "DYLD_FALLBACK_LIBRARY_PATH" not in env


def test_build_analyzer_env_sanitizes_path_to_drop_real_home_entries(
    monkeypatch,
):
    """Codex round-3 fix: PATH passed verbatim leaks real
    HOME via entries like `/Users/<auditor>/.venv/bin`. A
    subprocess can parse PATH and recover the auditor's
    home path string even when HOME env is isolated.
    Sanitize: drop entries under real HOME."""
    monkeypatch.setenv(
        "HOME", "/Users/fake-auditor",
    )
    monkeypatch.setenv(
        "PATH",
        "/Users/fake-auditor/.venv/bin:"
        "/Users/fake-auditor/.pyenv/shims:"
        "/usr/local/bin:"
        "/usr/bin:"
        "/bin",
    )
    env = build_analyzer_env()
    # User-home PATH entries dropped.
    assert "/Users/fake-auditor/.venv/bin" not in env["PATH"]
    assert "/Users/fake-auditor/.pyenv/shims" not in env["PATH"]
    # System dirs preserved.
    assert "/usr/local/bin" in env["PATH"]
    assert "/usr/bin" in env["PATH"]
    assert "/bin" in env["PATH"]
    # No `/Users/fake-auditor/` substring anywhere in PATH.
    assert "/Users/fake-auditor" not in env["PATH"]


def test_build_analyzer_env_path_fallback_when_all_entries_stripped(
    monkeypatch,
):
    """Defense-in-depth: if EVERY PATH entry is under real
    HOME (pathological dev shell), fall back to known
    system dirs. Without this, env["PATH"] could be ""
    which breaks every analyzer."""
    import os as _os
    monkeypatch.setenv("HOME", "/Users/fake-auditor")
    monkeypatch.setenv(
        "PATH",
        f"/Users/fake-auditor/.venv/bin{_os.pathsep}"
        f"/Users/fake-auditor/.local/bin",
    )
    env = build_analyzer_env()
    # Falls back to system dirs, NOT empty.
    assert env["PATH"]
    assert "/usr/bin" in env["PATH"] or "/bin" in env["PATH"]
    # The dropped entries are gone.
    assert "/Users/fake-auditor" not in env["PATH"]


def test_build_analyzer_env_path_drops_relative_entries(monkeypatch):
    """Codex round-6 fix: relative PATH entries (`.`,
    `bin`, `./foo`, empty) resolve against the analyzer
    subprocess's cwd, which is the attacker-supplied repo
    on a hostile audit. An attacker shipping `./solc` or
    `bin/solc` would hijack downstream tool invocations.
    Drop all non-absolute PATH entries."""
    monkeypatch.setenv("HOME", "/Users/fake-auditor")
    monkeypatch.setenv(
        "PATH",
        ".:bin:./foo:/usr/bin:/bin",
    )
    env = build_analyzer_env()
    # Relative entries all gone.
    assert "." not in env["PATH"].split(":")
    assert "bin" not in env["PATH"].split(":")
    assert "./foo" not in env["PATH"].split(":")
    # Empty entries also gone.
    assert "" not in env["PATH"].split(":")
    # Absolute system dirs preserved.
    assert "/usr/bin" in env["PATH"]
    assert "/bin" in env["PATH"]


def test_build_analyzer_env_path_drops_analyzer_cwd_entries(monkeypatch):
    """Codex round-6 fix: even an ABSOLUTE PATH entry that
    points INTO the attacker-supplied repo is dangerous
    (e.g., `/tmp/audit-target/bin` where the audit target
    is the attacker's repo). Pass `analyzer_cwd` to
    `build_analyzer_env` so those are dropped too."""
    monkeypatch.setenv("HOME", "/Users/fake-auditor")
    monkeypatch.setenv(
        "PATH",
        "/tmp/attacker-repo/bin:"
        "/tmp/attacker-repo:"
        "/tmp/something-else/bin:"
        "/usr/bin",
    )
    env = build_analyzer_env(
        analyzer_cwd="/tmp/attacker-repo",
    )
    # Entries under analyzer cwd dropped.
    assert "/tmp/attacker-repo/bin" not in env["PATH"]
    assert "/tmp/attacker-repo" not in env["PATH"].split(":")
    # Sibling absolute path that's NOT under analyzer cwd
    # survives.
    assert "/tmp/something-else/bin" in env["PATH"]
    assert "/usr/bin" in env["PATH"]


def test_reject_repo_local_binary_blocks_path_poisoning(tmp_path):
    """Codex round-12 fix: PATH poisoning RCE defense.
    `shutil.which()` runs in the parent process and can
    return a binary path INSIDE the attacker repo if PATH
    has been poisoned. `reject_repo_local_binary` refuses
    such binaries before subprocess.run executes them."""
    from src.analyzers.env import reject_repo_local_binary

    # Fake binary inside the "attacker repo".
    attacker_repo = tmp_path / "attacker-repo"
    attacker_repo.mkdir()
    fake_binary = attacker_repo / "slither"
    fake_binary.write_text("#!/bin/sh\necho pwned\n")
    fake_binary.chmod(0o755)

    with pytest.raises(ValueError, match="refusing to execute"):
        reject_repo_local_binary(
            str(fake_binary),
            analyzer_cwd=str(attacker_repo),
        )


def test_reject_repo_local_binary_handles_macos_tmp_symlink(tmp_path):
    """Codex round-12 fix: symlink-aware containment.
    On macOS `/tmp` symlinks to `/private/tmp`. If the
    binary path uses `/tmp/...` and analyzer_cwd uses
    `/private/tmp/...` (or vice versa), naive string
    comparison would miss the containment."""
    from src.analyzers.env import reject_repo_local_binary

    # Use tmp_path to create a symlink scenario.
    real_dir = tmp_path / "real-repo"
    real_dir.mkdir()
    fake_binary = real_dir / "slither"
    fake_binary.write_text("#!/bin/sh\necho pwned\n")
    fake_binary.chmod(0o755)

    sym_dir = tmp_path / "symlinked-repo"
    sym_dir.symlink_to(real_dir)

    # Binary referenced through the symlink path. cwd
    # referenced through the real path. Resolve normalizes
    # both; rejection must fire.
    with pytest.raises(ValueError, match="refusing to execute"):
        reject_repo_local_binary(
            str(sym_dir / "slither"),
            analyzer_cwd=str(real_dir),
        )


def test_reject_repo_local_binary_allows_system_binary(tmp_path):
    """The defense must NOT fire for a legitimate binary
    OUTSIDE the analyzer cwd."""
    from src.analyzers.env import reject_repo_local_binary

    # System-style binary at a fixed system path.
    # /usr/bin/true exists on every macOS/Linux box and is
    # outside any tmp_path-based analyzer cwd.
    reject_repo_local_binary(
        "/usr/bin/true",
        analyzer_cwd=str(tmp_path),
    )
    # No exception = pass.


def test_build_analyzer_env_path_handles_macos_tmp_symlink(
    monkeypatch, tmp_path,
):
    """Codex round-7 fix (F2): macOS `/tmp` is a symlink to
    `/private/tmp`. Raw string comparison of PATH entries
    bypasses containment: `analyzer_cwd="/private/tmp/X"`
    + PATH entry `/tmp/X/bin` survives because the strings
    don't share a prefix. Fix: resolve(strict=False) both
    sides before comparison so symlinks collapse.

    On a real macOS box this is reproducible directly; on
    test infrastructure we synthesize the same shape via a
    tmpdir symlink."""
    # Create the symlink target + a sibling pointing at it.
    real_dir = tmp_path / "real-attacker-repo"
    real_dir.mkdir()
    symlink_dir = tmp_path / "symlinked"
    symlink_dir.symlink_to(real_dir)

    monkeypatch.setenv("HOME", "/Users/fake-auditor")
    # PATH entry uses the SYMLINK spelling.
    monkeypatch.setenv(
        "PATH",
        f"{symlink_dir / 'bin'}:/usr/bin:/bin",
    )
    # analyzer_cwd uses the REAL (resolved) spelling.
    env = build_analyzer_env(
        analyzer_cwd=str(real_dir),
    )
    # The symlinked PATH entry must be dropped because its
    # resolved form is under analyzer_cwd's resolved form.
    assert str(symlink_dir / "bin") not in env["PATH"]
    assert str(real_dir / "bin") not in env["PATH"]
    # System dirs preserved.
    assert "/usr/bin" in env["PATH"]
    assert "/bin" in env["PATH"]


def test_build_analyzer_env_path_combined_relative_and_repo_attack(
    monkeypatch,
):
    """Pin Codex's exact reproduction: `.:bin:<repo>/bin:/usr/bin`
    must collapse to system dirs only."""
    monkeypatch.setenv("HOME", "/Users/fake-auditor")
    monkeypatch.setenv(
        "PATH",
        ".:bin:/tmp/attacker-repo/bin:/usr/bin:/bin",
    )
    env = build_analyzer_env(
        analyzer_cwd="/tmp/attacker-repo",
    )
    # No relative entries.
    assert "." not in env["PATH"].split(":")
    assert "bin" not in env["PATH"].split(":")
    # No attacker-repo entries.
    assert "/tmp/attacker-repo" not in env["PATH"]
    # System dirs survive.
    assert "/usr/bin" in env["PATH"]
    assert "/bin" in env["PATH"]


def test_build_analyzer_env_rewrites_tmpdir_to_isolated_home(
    monkeypatch,
):
    """Codex round-3 fix: macOS's default TMPDIR
    `/var/folders/.../T/` is a user-scope hashed path. A
    subprocess seeing it can correlate the run to a specific
    auditor. Rewrite TMPDIR/TMP/TEMP to the isolated temp
    HOME (which we control)."""
    monkeypatch.setenv(
        "TMPDIR", "/var/folders/abc/leaky-user-hash/T/"
    )
    monkeypatch.setenv("TMP", "/var/folders/abc/leaky-user-hash/T/")
    monkeypatch.setenv("TEMP", "/var/folders/abc/leaky-user-hash/T/")
    env = build_analyzer_env()
    # All three temp vars point at the isolated HOME.
    assert env["TMPDIR"] == env["HOME"]
    assert env["TMP"] == env["HOME"]
    assert env["TEMP"] == env["HOME"]
    # No trace of the leaky parent TMPDIR.
    assert "leaky-user-hash" not in env["TMPDIR"]


def test_build_analyzer_env_strips_xdg_paths(monkeypatch):
    """Codex follow-up review fix (F2): XDG_* vars typically
    point at absolute paths under real HOME (`~/.cache`,
    `~/.config`, `~/.local/share`). Passing them through
    would defeat the HOME isolation. Drop them entirely;
    tools that need XDG paths fall back to `$HOME/.cache`
    etc., which resolves under the isolated temp HOME."""
    monkeypatch.setenv("XDG_CACHE_HOME", "/Users/fake/.cache")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/Users/fake/.config")
    monkeypatch.setenv("XDG_DATA_HOME", "/Users/fake/.local/share")
    env = build_analyzer_env()
    assert "XDG_CACHE_HOME" not in env
    assert "XDG_CONFIG_HOME" not in env
    assert "XDG_DATA_HOME" not in env


def test_build_analyzer_env_returns_fresh_dict(monkeypatch):
    """Caller can mutate without affecting future calls."""
    monkeypatch.setenv("PATH", "/usr/bin")
    env1 = build_analyzer_env()
    env2 = build_analyzer_env()
    assert env1 is not env2
    env1["INJECTED"] = "bad"
    assert "INJECTED" not in env2


def test_run_slither_passes_allowlisted_env_to_subprocess(
    monkeypatch, tmp_path,
):
    """End-to-end: run_slither must invoke subprocess with
    env=build_analyzer_env() — pin via fake subprocess that
    captures the env kwarg and asserts OPENAI_API_KEY is
    missing."""
    from src.analyzers import slither as slither_mod

    monkeypatch.setenv("OPENAI_API_KEY", "sk-this-must-not-leak")
    monkeypatch.setenv("PATH", "/usr/bin")

    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        # Pretend slither succeeded — write a non-empty
        # SARIF so the wrapper's size check passes.
        # argv = ['/fake/slither', '<target>', '--sarif', '<out>']
        argv = args[0]
        out_path = argv[argv.index("--sarif") + 1]
        Path(out_path).write_text('{"runs": []}')
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(slither_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        slither_mod.shutil, "which", lambda _: "/fake/slither"
    )

    slither_mod.run_slither(tmp_path, tmp_path / "out.sarif")

    env = captured["env"]
    assert env is not None, (
        "subprocess.run must be called with env= (not inheriting "
        "the parent env). Got env=None — wrapper bypasses the "
        "allowlist."
    )
    assert "OPENAI_API_KEY" not in env, (
        f"OPENAI_API_KEY leaked into slither subprocess env: "
        f"{sorted(env.keys())}"
    )
    assert "PATH" in env


def test_run_semgrep_passes_allowlisted_env_to_subprocess(
    monkeypatch, tmp_path,
):
    """Same contract as the slither test — semgrep also
    must run with the allowlisted env."""
    from src.analyzers import semgrep as semgrep_mod

    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-leak")
    monkeypatch.setenv("PATH", "/usr/bin")

    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        out_idx = args[0].index("--output") + 1
        Path(args[0][out_idx]).write_text('{"runs": []}')
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(semgrep_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        semgrep_mod.shutil, "which", lambda _: "/fake/semgrep"
    )

    semgrep_mod.run_semgrep(
        tmp_path, tmp_path / "out.sarif", rules=["p/python"],
    )

    env = captured["env"]
    assert env is not None
    assert "OPENAI_API_KEY" not in env
    assert "PATH" in env
