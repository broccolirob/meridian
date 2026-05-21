"""Slither static-analysis runner.

Thin subprocess wrapper around the `slither` CLI. Outputs a
SARIF report at the requested path; downstream `augment_sarif`
(chunk 4.2) ingests it.

The caller is responsible for ensuring a compatible solc is on
PATH. Use `solc-select` to switch — slither will fail with
`InvalidCompilation` if the system solc doesn't match the
repo's pragma.
"""

import shutil
import subprocess
from pathlib import Path

_DEFAULT_TIMEOUT_S = 300.0


def run_slither(
    repo_path: str | Path,
    out_sarif: str | Path,
    *,
    project_root: str | Path | None = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> Path:
    """Run `slither <repo> --sarif <out>` and return the SARIF
    file path on success.

    Returns the absolute path of the written SARIF (matches the
    Phase 4.2 augment_sarif input shape — see PLAN.md tools
    layer).

    Raises:
        FileNotFoundError: slither not on PATH, or repo_path
            doesn't exist.
        subprocess.TimeoutExpired: slither exceeded `timeout`.
        subprocess.CalledProcessError: slither failed AND no
            SARIF file (or a zero-byte one) was written
            (genuine analyzer error, e.g. solc compile failure
            or kill-mid-write).

    Note: slither exits non-zero (1 or 255 depending on version)
    when findings are found — this is NOT an error. As long as
    the SARIF file exists with non-zero size, the run is treated
    as successful.

    `project_root` controls the subprocess cwd, which controls
    how slither emits SARIF location URIs. Defaults to
    `repo_path` itself. When the caller analyzes a subdirectory
    of a larger project (e.g. `repo_path=foo/contracts`,
    `project_root=foo`), set this explicitly so URIs come out
    as `contracts/X.sol` rather than bare `X.sol` — that's the
    shape `augment_sarif` (chunk 4.2) needs to match findings
    against graph nodes parsed from `project_root`.

    Hardening invariants:
    - Pre-existing `out_sarif` is deleted before invocation, so
      a returned path always reflects THIS run (no stale-data
      false-success when slither fails before writing).
    - Empty (0-byte) SARIF files raise CalledProcessError. This
      catches kill-mid-write scenarios (timeout, OOM, SIGKILL)
      where the file is created but never populated; downstream
      `augment_sarif` would otherwise crash on a JSONDecodeError
      far from the root cause.
    - subprocess.run uses `cwd=project_root` (default
      `repo_path`) so slither's SARIF URIs are
      project-relative. Trailmark's `augment_sarif` matches by
      file+line and resolves URIs against the parsed graph's
      paths — running slither outside the project produces
      `../../../...` URIs that match nothing.
    - `project_root`, when explicitly passed, MUST contain
      `repo_path` (validated via Path.is_relative_to).
      Mismatched roots are a silent footgun: slither runs OK,
      but augment_sarif returns 0 matched because URIs compute
      against the wrong base.

    Side effect (worth knowing for production callers):
    Slither's incidental artifacts (`crytic-export/`,
    `crytic-compile.config.json`) land inside `cwd` — i.e.,
    inside the auditor's input repo by default. This matches
    stock slither behavior and is gitignored at washable's
    root, but for auditors running against external repos
    (git clones, submodules, read-only mounts) it leaves
    untracked files in the audit target. For hermetic runs,
    copy the target to a tmp dir first and pass the copy as
    `repo_path` (this is what `tests/test_augment_sarif.py`'s
    `fresh_tier1_with_sarif` fixture does).

    Security: subprocess uses list-form argv (no shell=True), so
    the auditor-supplied `repo_path` cannot inject shell
    metacharacters. Slither's own surface (parsing adversarial
    Solidity) is outside our threat model.
    """
    slither = shutil.which("slither")
    if slither is None:
        raise FileNotFoundError(
            "slither not found on PATH — install slither-analyzer "
            "(`uv sync` should pull it via [project].dependencies, "
            "or `brew install slither-analyzer` on macOS)"
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
        # Containment check: SARIF URIs are computed relative to
        # `cwd`. If `repo` isn't inside `cwd`, the URIs come out
        # as `../...` paths that Trailmark's augment_sarif can't
        # resolve against the graph (silent 0-matched footgun).
        if not repo.is_relative_to(cwd):
            raise ValueError(
                f"repo_path ({repo}) must be inside project_root "
                f"({cwd}) so SARIF URIs resolve against the graph"
            )
    else:
        cwd = repo

    out = Path(out_sarif).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    # Defense against stale-SARIF false-success: if a prior run
    # left a file here AND this slither invocation fails before
    # writing, out.exists() would return True and the function
    # would silently hand back stale data. Unlinking first
    # guarantees the returned path always reflects THIS run.
    out.unlink(missing_ok=True)

    # Pass the target as a path RELATIVE to `cwd`. Slither
    # happens to emit relative SARIF URIs even when given an
    # absolute target in current versions — but that's an
    # internal slither behavior, not a documented contract.
    # The Phase-4 semgrep wrapper enforces relativization
    # explicitly because semgrep DOES emit absolute URIs when
    # given an absolute target, and Trailmark's file+line
    # matcher silently fails on macOS where `/var` symlinks
    # to `/private/var`. Mirroring that defense here keeps
    # both analyzers behaving consistently and protects
    # against a future slither release switching to
    # absolute artifactLocation URIs (which would be more
    # SARIF-compliant but break the chain).
    try:
        target_arg = str(repo.relative_to(cwd))
    except ValueError:
        # Containment check above should prevent this — but
        # belt-and-suspenders for any future refactor.
        target_arg = str(repo)

    proc = subprocess.run(
        [slither, target_arg, "--sarif", str(out)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        # cwd controls SARIF URI shape. Defaults to repo_path;
        # caller overrides via `project_root` when analyzing a
        # subdir of a larger project so URIs are project-relative
        # (chunk 4.2's augment_sarif needs that to match graph
        # nodes). Running slither outside the project produces
        # `../../../...` URIs that Trailmark can't match.
        # Crytic-export artifacts land at `cwd`; this matches
        # stock slither behavior and is gitignored.
        cwd=str(cwd),
    )

    # File-existence + non-zero size is the success signal. A
    # 0-byte file (kill-mid-write, disk-full, etc.) is a partial
    # write that downstream parsers would choke on; surface it
    # here as an analyzer error instead.
    if not out.exists() or out.stat().st_size == 0:
        raise subprocess.CalledProcessError(
            proc.returncode,
            proc.args,
            output=proc.stdout,
            stderr=proc.stderr,
        )

    return out
