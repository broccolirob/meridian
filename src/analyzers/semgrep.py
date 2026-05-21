"""Semgrep static-analysis runner — SARIF emitter for any
Trailmark-supported language.

Companion to src/analyzers/slither.py. Slither owns Solidity;
semgrep handles everything else (Python, Go, JS, Rust, ...)
via pluggable rule packs. Same hardening contract as slither
(pre-unlink, list-form argv, cwd-controls-SARIF-URI, size>0
check, project_root containment) — semgrep just speaks a
different CLI.
"""

import shutil
import subprocess
from pathlib import Path

from src.analyzers.env import (
    build_analyzer_env,
    reject_repo_local_binary,
)

_DEFAULT_TIMEOUT_S = 300.0


def run_semgrep(
    repo_path: str | Path,
    out_sarif: str | Path,
    rules: list[str],
    *,
    project_root: str | Path | None = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> Path:
    """Run `semgrep scan --sarif --output <out> --config <r>...
    <repo>` and return the SARIF file path on success.

    `rules` is a list of `--config` arguments — each item is
    either a semgrep registry ID (`"p/python"`, `"p/secrets"`)
    or a local YAML rule file path. Stacked configs apply every
    rule from every config; semgrep dedupes by rule ID +
    location internally. Empty list raises ValueError —
    semgrep with no `--config` prints help and exits 0, which
    would produce a zero-finding SARIF and look like a clean
    audit. Fail loud at the wrapper boundary instead.

    Returns the absolute path of the written SARIF (matches
    Phase 4.2 augment_sarif input shape).

    Raises:
        ValueError: empty `rules` list, OR project_root is
            outside repo_path (containment defense).
        FileNotFoundError: semgrep not on PATH, OR repo_path
            doesn't exist, OR project_root doesn't exist.
        subprocess.TimeoutExpired: semgrep exceeded `timeout`.
            Partial SARIF (if any) is best-effort unlinked.
        subprocess.CalledProcessError: semgrep finished but no
            SARIF (or a zero-byte one) was written — same
            kill-mid-write defense as slither.

    Note: semgrep exits non-zero (1) when findings are found —
    this is NOT an error. File existence + non-zero size is
    the success signal, matching `run_slither`'s convention.

    `project_root` controls subprocess cwd, which controls
    SARIF URI shape. Same constraint as slither: when
    analyzing a subdir of a larger project, set this to the
    project root so URIs come out as `pkg/foo.py` rather than
    bare `foo.py` — that's the shape `augment_sarif` needs to
    match against graph nodes.

    Hardening invariants (identical to run_slither):
    - Pre-existing `out_sarif` is unlinked before invocation
      so a stale file from a crashed prior run can't be
      mistaken for THIS run's output.
    - Zero-byte SARIF after run raises CalledProcessError.
    - List-form subprocess argv; no shell=True. Auditor-
      supplied `repo_path` cannot inject shell metacharacters.
    - `project_root` (when passed) MUST contain `repo_path`;
      mismatched roots produce `../...` SARIF URIs that
      silently match nothing.

    Side effect: semgrep writes its trace log to
    `~/.semgrep/last.log` and may cache rule packs in
    `~/.semgrep/`. The wrapper doesn't manage these — they're
    user-scope, not repo-scope, so they don't pollute the
    auditor's input tree the way slither's `crytic-export/`
    does.

    Security: subprocess uses list-form argv (no shell=True).
    Semgrep's own surface (parsing adversarial source code,
    fetching rule packs from the registry) is outside our
    threat model.
    """
    # Reject empty list AND empty/whitespace-only items.
    # Semgrep silently produces a zero-finding SARIF for
    # `--config ""` (config-resolution fails, scan exits 0).
    # That looks like a clean audit but isn't — fail loud
    # at the wrapper boundary instead.
    if not rules or any(
        not isinstance(r, str) or not r.strip() for r in rules
    ):
        raise ValueError(
            "rules must be a non-empty list of non-empty "
            "semgrep --config arguments (e.g., ['p/python']); "
            "empty strings and None entries are rejected to "
            "prevent silent zero-finding SARIF output"
        )

    semgrep = shutil.which("semgrep")
    if semgrep is None:
        raise FileNotFoundError(
            "semgrep not found on PATH — install semgrep "
            "(`uv sync` should pull it via "
            "[project].dependencies, or `brew install "
            "semgrep` on macOS)"
        )

    repo = Path(repo_path).resolve()
    if not repo.exists():
        raise FileNotFoundError(f"repo path does not exist: {repo}")
    if project_root is not None:
        cwd = Path(project_root).resolve()
        if not cwd.exists():
            raise FileNotFoundError(
                f"project_root does not exist: {cwd}"
            )
        # Containment check: SARIF URIs are computed relative
        # to `cwd`. If `repo` isn't inside `cwd`, semgrep
        # emits `../...` URIs that Trailmark's augment_sarif
        # can't resolve against the graph (silent 0-matched
        # footgun). Same defense as run_slither.
        if not repo.is_relative_to(cwd):
            raise ValueError(
                f"repo_path ({repo}) must be inside "
                f"project_root ({cwd}) so SARIF URIs "
                f"resolve against the graph"
            )
    else:
        cwd = repo

    # Codex round-12 fix: refuse to execute a `semgrep`
    # binary that resolves inside the analyzer cwd
    # (PATH poisoning RCE defense — same rationale as
    # run_slither).
    reject_repo_local_binary(semgrep, analyzer_cwd=str(cwd))

    out = Path(out_sarif).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    # Defense against stale-SARIF false-success: if a prior
    # run left a file here AND this semgrep invocation fails
    # before writing, out.exists() would return True and the
    # function would silently hand back stale data. Unlinking
    # first guarantees the returned path always reflects
    # THIS run.
    out.unlink(missing_ok=True)

    argv: list[str] = [
        semgrep, "scan",
        "--sarif", "--output", str(out),
        # Codex round-19 fix: a malicious repo can suppress
        # findings via three default-on semgrep mechanisms:
        #
        #   1. `.semgrepignore` in the repo — Codex repro:
        #      adding `.semgrepignore` with `bad.py` made the
        #      wrapper return 0 results on python_smol.
        #   2. `.gitignore` (semgrep honors git-ignored files
        #      by default) — same suppression vector via a
        #      stock `.gitignore` entry.
        #   3. `nosem` comments inline in source — attacker
        #      adds `# nosemgrep` to mark the vulnerable line
        #      as exempt.
        #
        # Disable all three so the auditor sees the real
        # findings against attacker-supplied code. Also turn
        # off the telemetry/version-check pings — auditor
        # privacy AND less network surface in a hermetic run.
        "--x-ignore-semgrepignore-files",
        "--no-git-ignore",
        "--disable-nosem",
        "--metrics", "off",
        "--disable-version-check",
    ]
    for cfg in rules:
        argv.extend(["--config", cfg])
    # Pass the target as a path RELATIVE to `cwd`. Semgrep
    # emits SARIF `artifactLocation.uri` values matching the
    # absoluteness of the target it's given: pass `.`, get
    # `bad.py`; pass `/abs/path`, get `/abs/path/bad.py`.
    # Trailmark's augment_sarif then can't reconcile absolute
    # URIs against graph nodes (especially on macOS where
    # /var symlinks to /private/var — Trailmark's root_path
    # is /var/... while resolve() gives /private/var/...).
    # The default cwd==repo case becomes target=".", which
    # produces clean repo-relative URIs that augment_sarif
    # matches correctly.
    try:
        target_arg = str(repo.relative_to(cwd))
    except ValueError:
        # Containment check above should prevent this — but
        # belt-and-suspenders for any future refactor.
        target_arg = str(repo)
    # `--` separates flags from positional targets so a
    # future repo path that happens to start with `-` can't
    # be re-parsed as a flag (defense-in-depth; current
    # resolved paths can't start with `-` but the rule is
    # cheap).
    argv.extend(["--", target_arg])

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=str(cwd),
            # Allowlisted environment — semgrep can also run
            # user-supplied scripts via certain rules and
            # registry packs, plus we don't want
            # OPENAI_API_KEY leaking through the auditor's
            # session env. Same defense as run_slither.
            # `analyzer_cwd` so PATH entries under the
            # attacker-supplied repo are dropped (codex
            # round-6 fix).
            env=build_analyzer_env(analyzer_cwd=str(cwd)),
        )
    except subprocess.TimeoutExpired:
        # Best-effort cleanup of partial SARIF so a retry
        # starts from a clean slate (the unlink at the top
        # only protects against PRIOR-run staleness; a
        # mid-write timeout here can leave its own partial).
        out.unlink(missing_ok=True)
        raise

    # File-existence + non-zero size is the success signal.
    # A 0-byte file (kill-mid-write, disk-full, etc.) is a
    # partial write that downstream parsers would choke on;
    # surface it here as an analyzer error instead.
    if not out.exists() or out.stat().st_size == 0:
        raise subprocess.CalledProcessError(
            proc.returncode,
            proc.args,
            output=proc.stdout,
            stderr=proc.stderr,
        )

    return out
