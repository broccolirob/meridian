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

    Hardening invariants:
    - Pre-existing `out_sarif` is deleted before invocation, so
      a returned path always reflects THIS run (no stale-data
      false-success when slither fails before writing).
    - Empty (0-byte) SARIF files raise CalledProcessError. This
      catches kill-mid-write scenarios (timeout, OOM, SIGKILL)
      where the file is created but never populated; downstream
      `augment_sarif` would otherwise crash on a JSONDecodeError
      far from the root cause.
    - subprocess.run uses `cwd=out.parent` so slither's
      incidental artifacts (`crytic-export/`, `crytic-compile.config.json`)
      land next to the SARIF, NOT in the auditor's working tree.

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

    out = Path(out_sarif).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    # Defense against stale-SARIF false-success: if a prior run
    # left a file here AND this slither invocation fails before
    # writing, out.exists() would return True and the function
    # would silently hand back stale data. Unlinking first
    # guarantees the returned path always reflects THIS run.
    out.unlink(missing_ok=True)

    proc = subprocess.run(
        [slither, str(repo), "--sarif", str(out)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        # Slither writes crytic-export/ and crytic-compile.config.json
        # relative to cwd. Landing them next to the SARIF (rather
        # than the auditor's cwd) keeps the auditor's working tree
        # clean. Build-config detection (foundry.toml, etc.) walks
        # up from `repo`, not cwd, so this is safe.
        cwd=str(out.parent),
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
