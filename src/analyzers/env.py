"""Subprocess environment isolation for analyzer runs.

## Why this exists

`scripts/document_repo.py` calls `load_dotenv(".env")` BEFORE
Phase 4 runs, which puts `OPENAI_API_KEY` (and any other
developer-local credentials) into `os.environ`.
`subprocess.run(..., env=None)` inherits the full parent
environment, so without filtering, every analyzer subprocess
(slither + crytic-compile, semgrep) sees the auditor's API
keys.

Slither in particular invokes crytic-compile, which may run
project build tooling (hardhat, foundry, truffle, npm) when
the repo ships those configs. An attacker-supplied Solidity
repo can include build hooks that read env vars and
exfiltrate them. The threat model documented in
`src/analyzers/slither.py` treats slither's parser as
out-of-scope, but the SUBPROCESS ENVIRONMENT (including HOME
and the credential files it implies) is a separate boundary
that wasn't hardened.

## What `build_analyzer_env()` does

1. **Allowlist env vars** — only PATH, locale, toolchain
   discovery vars pass through. `OPENAI_API_KEY`, cloud
   creds, GitHub/npm tokens are dropped (allowlist > denylist
   means new credential vars are dropped by default).

2. **Replace `HOME` with an isolated temp dir.** Many
   analyzers honor `HOME` for config / cache discovery
   (`~/.semgrep/`, `~/.cache/`). Passing real HOME means a
   subprocess can read credential files like `~/.ssh/`,
   `~/.aws/credentials`, `~/.npmrc`, cloud-provider configs.
   The temp HOME starts empty — NO symlinks into real HOME
   (an earlier revision symlinked `.solc-select`; that was
   reverted because `readlink()` on the symlink leaks the
   real HOME path back to a subprocess AND writes to the
   symlink propagate back into the auditor's real
   solc-select state, enabling compiler-poisoning).

3. **Drop real-HOME-bearing env vars.** `PYENV_ROOT`,
   `VIRTUAL_ENV`, and `DYLD_LIBRARY_PATH` /
   `DYLD_FALLBACK_LIBRARY_PATH` typically point under real
   HOME and enable path-traversal-style discovery of
   credential files (`$PYENV_ROOT/../.ssh/...`). They're
   dropped from the allowlist.

4. **Sanitize `PATH`** to drop entries under real HOME or
   under CWD. Without this, a subprocess can `readlink` /
   parse PATH and recover the auditor's real HOME path
   string from typical entries like `/Users/<auditor>/.venv/bin`
   or `/Users/<auditor>/.pyenv/shims`. The sanitized PATH
   contains only system dirs (`/usr/bin`, `/opt/homebrew/bin`,
   etc.). Slither/semgrep binaries themselves are invoked
   by absolute path (via `shutil.which` before subprocess),
   so the sanitized PATH doesn't break those invocations —
   it only affects downstream tools (`solc`, `npm`, etc.)
   that the analyzer might invoke.

5. **Rewrite `TMPDIR` / `TMP` / `TEMP`** to point at the
   isolated temp HOME. The default macOS TMPDIR is a
   user-scope hashed path (`/var/folders/.../T/`) — passing
   it through leaks per-user information that helps an
   attacker correlate the subprocess to a specific auditor.
   Pointing at our isolated temp HOME means subprocess
   temp files land where we control them.

## solc-select compatibility

`run_slither` invokes `solc` via PATH lookup. If the
operator uses solc-select (which installs a wrapper at
`~/.solc-select/bin/solc`), that wrapper reads SOLC_VERSION
and dispatches to a versioned binary under
`~/.solc-select/artifacts/`. With HOME isolated, the wrapper
can't find its artifacts dir.

**Operator options:**
- Install a system solc (homebrew `solc`, `apt install solc`)
  that's on PATH outside `~/.solc-select/`. Slither finds
  it directly without solc-select.
- Set PATH to include a version-pinned solc binary directly
  (e.g., copy `~/.solc-select/artifacts/solc-0.5.16/solc-0.5.16`
  to `/usr/local/bin/solc`).
- Accept that solc-select-managed compilers aren't reachable
  from inside analyzer subprocesses; tests / Tier 1 runs
  may need a separate compiler setup.

## Residual risk (out of scope for this layer)

This is PARTIAL isolation. A subprocess can bypass `HOME`
by calling `getpwuid(getuid())` to look up the real user's
home from the system password database. Tools that do this
(less common, but `git` does for `~/.gitconfig` lookup in
some paths) will still see the real home.

**Real defense in depth requires OS-level sandboxing**
(bubblewrap on Linux, sandbox-exec on macOS, Docker container,
or running washable inside a VM). Auditors running washable
against high-risk repos SHOULD do so in a container with the
target repo mounted read-only, a dedicated writable artifact
dir, and no network access. That's a deployment-time
hardening, not something this Python module can enforce.

This module's job: make the easy attacks (env-variable
exfiltration, `~/.ssh` reads via HOME-honoring tools) hard.
Defense in depth past that point is the operator's
responsibility.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path
from typing import Final

# Variables that analyzers legitimately need. Allowlist
# rather than denylist so a new env-leaking secret added to
# the dev workflow doesn't accidentally pass through.
#
# Notable EXCLUSIONS (vs. earlier revision):
# - XDG_CACHE_HOME / XDG_CONFIG_HOME / XDG_DATA_HOME — if
#   the parent sets these to absolute paths under real HOME
#   (the OS default), they'd point at credential dirs. Drop
#   them; tools that need XDG paths fall back to `~/.cache`
#   etc., which now resolves under our isolated temp HOME.
_ALLOWLIST: Final[frozenset[str]] = frozenset({
    # Toolchain discovery. The VALUE is sanitized below
    # (drop entries under real HOME / CWD) — see
    # `_sanitize_path`.
    "PATH",
    # Process state — many tools refuse to run without these
    # or fall back to root-y defaults. NOTE: HOME is in the
    # allowlist for completeness but its VALUE is replaced
    # below with a temp dir (see `build_analyzer_env`).
    # TMPDIR / TMP / TEMP values are also REWRITTEN to point
    # at the isolated temp HOME (the macOS default TMPDIR
    # `/var/folders/.../T/` is user-scope hashed and leaks
    # per-user information).
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "TMP",
    "TEMP",
    # Locale (semgrep emits UTF-8; without LANG it can
    # mojibake on some systems).
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    # solc-select reads SOLC_VERSION to scope the active
    # compiler version to this subprocess (chunk 4.2 test
    # fixture pattern).
    "SOLC_VERSION",
    # Pyenv VERSION is safe (just a version string like
    # "3.12.9"); ROOT, VIRTUAL_ENV, and DYLD_* typically
    # point under real HOME, which enables credential-file
    # discovery via path traversal from the subprocess.
    # Dropped from the allowlist.
    "PYENV_VERSION",
    # Color/TTY hints (cosmetic, but some tools error
    # without these in non-interactive contexts).
    "TERM",
    "NO_COLOR",
    "FORCE_COLOR",
})


# Trusted system bin directories. Used as the fallback PATH
# if `_sanitize_path` strips everything. Covers macOS
# (homebrew + system), Linux (/usr/local, /usr, sbin), and
# common base utilities. Ordered most-to-least-likely.
_FALLBACK_PATH: Final[str] = (
    "/opt/homebrew/bin"
    ":/opt/homebrew/sbin"
    ":/usr/local/bin"
    ":/usr/local/sbin"
    ":/usr/bin"
    ":/usr/sbin"
    ":/bin"
    ":/sbin"
)


def _resolve_safe(p: str) -> str:
    """Resolve `p` to its canonical absolute path via
    `Path.resolve(strict=False)` — follows symlinks AND
    normalizes `..`/`.` segments without requiring the
    path to exist. Defensive fallback returns `p` unchanged
    on OSError (shouldn't happen with strict=False but
    handles exotic FS errors). Used by `_sanitize_path` so
    macOS's `/tmp` → `/private/tmp` symlink can't bypass
    the containment check."""
    try:
        return str(Path(p).resolve(strict=False))
    except OSError:
        return p


def reject_repo_local_binary(
    binary_path: str,
    *,
    analyzer_cwd: str,
) -> None:
    """Reject an analyzer binary that resolves inside the
    attacker-supplied repo.

    `shutil.which("slither")` runs in the PARENT process and
    uses the PARENT's PATH. If that PATH has been poisoned
    with an entry pointing into the analyzer cwd (or the
    auditor cd'd into the attacker repo with `.` on PATH),
    `which` returns an attacker-supplied binary. The wrapper
    then passes that absolute path to `subprocess.run`,
    which executes the attacker binary — RCE on the auditor
    machine, with full parent env access (which is why we
    sanitize the subprocess env too, but env sanitization
    happens AFTER binary resolution).

    The PATH sanitization in `build_analyzer_env` only
    affects the SUBPROCESS PATH, not the parent's
    binary-resolution step. This helper closes the gap:
    after the parent resolves a binary via `shutil.which`,
    refuse to execute it if its resolved absolute path is
    inside the analyzer cwd. Symlinks in either path are
    followed via `_resolve_safe` (macOS `/tmp` →
    `/private/tmp` parity).

    Raises:
        ValueError: the binary is repo-local. Loud,
            specific message naming the offending paths so
            operators can investigate the PATH poisoning.
    """
    resolved_bin = _resolve_safe(binary_path)
    resolved_cwd = _resolve_safe(analyzer_cwd)
    if (
        resolved_bin == resolved_cwd
        or resolved_bin.startswith(resolved_cwd + os.sep)
    ):
        raise ValueError(
            f"refusing to execute analyzer binary "
            f"{binary_path!r} — it resolves to "
            f"{resolved_bin} which is inside the analyzer "
            f"cwd {resolved_cwd}. Likely PATH poisoning "
            f"(auditor PATH includes a repo-local entry, "
            f"or `.` is on PATH and cwd is the attacker "
            f"repo). The wrapper does not execute this "
            f"binary; investigate before retrying."
        )


def _sanitize_path(
    path_value: str,
    *,
    real_home: str,
    analyzer_cwd: str | None = None,
) -> str:
    """Drop unsafe PATH entries before passing PATH to an
    analyzer subprocess.

    Three classes of entries are dropped:

    1. **Relative entries** (`.`, `bin`, `./foo`, empty).
       Analyzer subprocesses run with `cwd=<repo>` (the
       attacker-supplied repo on a hostile audit). Relative
       PATH entries resolve against that cwd → an attacker
       can ship `./solc` or `bin/solc` in their repo and
       hijack downstream tool invocations. Only absolute
       paths survive.

    2. **Entries under real HOME** (`/Users/<auditor>/...`).
       Leak the auditor's home path string and let a
       subprocess discover credential dirs via path
       traversal.

    3. **Entries under analyzer cwd** (the
       attacker-supplied repo or its project_root). Even
       absolute paths that point INTO the repo (e.g.,
       `/tmp/audit-target/bin`) are attacker-controlled
       and must be dropped.

    All comparisons run on RESOLVED paths (symlinks
    followed, `..`/`.` normalized) so macOS-style symlink
    spellings can't bypass containment. Concretely:
    `/tmp` → `/private/tmp` on macOS, so an entry
    `/tmp/attacker-repo/bin` under analyzer_cwd
    `/private/tmp/attacker-repo` IS caught.

    Falls back to `_FALLBACK_PATH` (curated absolute system
    bin dirs) if every entry was unsafe.
    """
    import os as _os
    sep = _os.pathsep
    entries = path_value.split(sep)

    # Resolve the comparison targets once. The entries get
    # resolved per-iteration inside `_is_safe`.
    real_home_r = _resolve_safe(real_home)
    parent_cwd_r = _resolve_safe(_os.getcwd())
    analyzer_cwd_r = (
        _resolve_safe(analyzer_cwd)
        if analyzer_cwd is not None
        else None
    )

    def _is_safe(e: str) -> bool:
        if not e:
            return False
        # Must be absolute. Drops `.`, `./bin`, `bin`,
        # `../foo`, and any other relative entry.
        if not _os.path.isabs(e):
            return False
        # Resolve symlinks + normalize before containment
        # checks. Codex round-7 fix: `/tmp/X` vs.
        # `/private/tmp/X` are identical after resolve().
        e_r = _resolve_safe(e)
        # Drop entries under real HOME.
        if e_r == real_home_r or e_r.startswith(real_home_r + _os.sep):
            return False
        # Drop entries under the parent process's cwd
        # (usually the repo root containing washable).
        if e_r == parent_cwd_r or e_r.startswith(parent_cwd_r + _os.sep):
            return False
        # Drop entries under the analyzer subprocess's cwd
        # (the attacker-supplied repo).
        if analyzer_cwd_r is not None and (
            e_r == analyzer_cwd_r
            or e_r.startswith(analyzer_cwd_r + _os.sep)
        ):
            return False
        return True

    safe = [e for e in entries if _is_safe(e)]
    return sep.join(safe) if safe else _FALLBACK_PATH

# Process-scoped temp HOME, lazily created on first call to
# `build_analyzer_env`. Reused across calls so we don't spam
# /tmp with empty dirs. Cleaned up at process exit.
_analyzer_temp_home: Path | None = None


def _get_analyzer_temp_home() -> Path:
    """Lazily create (or return existing) isolated temp HOME
    for analyzer subprocesses. The directory is EMPTY — no
    symlinks into real HOME (which would leak the real path
    via readlink + enable writes to propagate back to real
    home content, e.g. compiler-poisoning of solc-select
    artifacts).

    Cleanup is registered at module load via atexit; the
    process can re-use the same temp HOME across all analyzer
    invocations in its lifetime.

    See module docstring's "solc-select compatibility"
    section for the operator-side trade-off this change
    imposes.
    """
    global _analyzer_temp_home
    if (
        _analyzer_temp_home is not None
        and _analyzer_temp_home.exists()
    ):
        return _analyzer_temp_home

    temp = Path(tempfile.mkdtemp(prefix="washable-analyzer-home-"))
    _analyzer_temp_home = temp
    return temp


def _cleanup_analyzer_temp_home() -> None:
    """atexit hook: remove the temp HOME if we created one."""
    global _analyzer_temp_home
    if _analyzer_temp_home is not None and _analyzer_temp_home.exists():
        shutil.rmtree(_analyzer_temp_home, ignore_errors=True)
    _analyzer_temp_home = None


atexit.register(_cleanup_analyzer_temp_home)


def build_analyzer_env(
    *,
    analyzer_cwd: str | None = None,
) -> dict[str, str]:
    """Return a minimal env dict for analyzer subprocesses.

    `analyzer_cwd`, when provided, is the cwd the analyzer
    subprocess will run with (the attacker-supplied repo or
    its project_root). PATH entries under this cwd are
    dropped so attacker-supplied `<repo>/bin/solc` can't
    shadow the system solc. The wrappers
    (`run_slither` / `run_semgrep`) pass it.

    Keeps only variables in the allowlist above. Strips
    everything else, including:
    - OPENAI_API_KEY and other LLM provider credentials
    - GITHUB_TOKEN, AWS_*, GOOGLE_*, npm_token, etc.
    - SSH_*, shell-injection vectors like PROMPT_COMMAND
    - Anything custom an auditor's dev shell adds

    `HOME` is allowlisted but its value is REPLACED with an
    isolated temp dir (process-scoped, atexit-cleaned).
    Tools that read `~/...` paths will read from the temp
    dir. Credential files (`.ssh`, `.aws`, `.npmrc`, etc.)
    are NOT accessible via this HOME.

    See module docstring for the residual-risk caveat:
    a subprocess that calls `getpwuid(getuid())` directly
    can still find real HOME via the OS password database.
    Full isolation requires OS-level sandboxing.

    The returned dict is safe to pass as `env=` to
    `subprocess.run`. subprocess does NOT inherit the parent
    environment when `env=` is supplied.

    Returns a fresh dict each call so the caller can mutate
    without affecting future calls.
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if name in _ALLOWLIST
    }

    # Capture the REAL home before we overwrite HOME — used
    # to filter PATH entries below. `Path.home()` reads
    # `os.environ["HOME"]` (we haven't mutated os.environ
    # yet, only `env`).
    real_home = str(Path.home())

    temp_home = str(_get_analyzer_temp_home())

    # Replace HOME with the isolated temp dir. Reads of
    # `~/.ssh`, `~/.aws/credentials`, etc. fail (no such
    # path under temp HOME).
    env["HOME"] = temp_home

    # Rewrite TMPDIR / TMP / TEMP to point at the isolated
    # temp HOME. The macOS default `/var/folders/.../T/` is
    # a user-scope hashed path that leaks per-user info; a
    # subprocess parsing TMPDIR could correlate the run to a
    # specific auditor.
    for tmp_key in ("TMPDIR", "TMP", "TEMP"):
        if tmp_key in env:
            env[tmp_key] = temp_home

    # Sanitize PATH: drop entries under real HOME or CWD.
    # Without this, a subprocess can parse PATH and recover
    # the auditor's real HOME from typical entries like
    # `/Users/<auditor>/.venv/bin`. The wrappers invoke
    # slither/semgrep by ABSOLUTE path (resolved via
    # `shutil.which` in the parent process), so a sanitized
    # PATH doesn't break those invocations — it only
    # affects downstream tools (`solc`, `npm`) the analyzer
    # might fork.
    if "PATH" in env:
        env["PATH"] = _sanitize_path(
            env["PATH"],
            real_home=real_home,
            analyzer_cwd=analyzer_cwd,
        )

    return env
