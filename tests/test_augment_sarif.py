"""Tests for src/tools.py::augment_sarif.

End-to-end test runs slither on a tmp copy of the Tier 1
fixture (avoids polluting the fixture with crytic-export/),
then verifies the augmentation attaches findings to graph
nodes. Skips when slither/solc are missing.
"""

import shutil
import subprocess as sp
from pathlib import Path

import pytest

from src.analyzers.slither import run_slither
from src.graph.persist import load_graph
from src.tools import (
    augment_sarif,
    nodes_with_annotation,
    trailmark_parse,
)

TIER1_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "tier1_uniswap_v2"
)


def _solc_516_available() -> bool:
    """Shared with test_slither.py — duplicated rather than
    promoted to conftest because only two files use this today
    (CLAUDE.md: 'three similar lines is better than premature
    abstraction')."""
    if shutil.which("solc-select") is None:
        return False
    try:
        proc = sp.run(
            ["solc-select", "versions"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        return "0.5.16" in proc.stdout
    except (sp.TimeoutExpired, FileNotFoundError):
        return False


@pytest.fixture
def fresh_tier1_with_sarif(tmp_path, monkeypatch):
    """Copy Tier 1 to tmp + parse + run slither in one shot.
    Returns (graph_id, cache_root, sarif_path). Hermetic: no
    pollution of fixture, no shared state across tests.

    SOLC_VERSION scopes solc selection to slither's subprocess
    only (no mutation of ~/.solc-select/global-version).

    Bypasses build_analyzer_env's HOME isolation so slither's
    subprocess can reach ~/.solc-select/artifacts/ — see the
    same note on test_run_slither_on_tier1_... in
    tests/test_slither.py for the rationale. Production runs
    MUST get the isolated env; this is a TEST-ONLY escape
    hatch."""
    monkeypatch.setenv("SOLC_VERSION", "0.5.16")
    import os
    monkeypatch.setattr(
        "src.analyzers.slither.build_analyzer_env",
        lambda **_kw: os.environ.copy(),
    )
    repo = tmp_path / "tier1"
    shutil.copytree(TIER1_FIXTURE, repo)
    cache_root = tmp_path / "cache"
    gid = trailmark_parse(
        str(repo), language="solidity", cache_root=cache_root
    )
    sarif = tmp_path / "tier1.sarif"
    # Analysis target is `repo/contracts` (where the .sol files
    # live — slither can't auto-discover them from the bare repo
    # root). project_root=repo overrides cwd so SARIF URIs come
    # out as `contracts/UniswapV2Pair.sol` — matches the parsed
    # graph's file paths (which use the same prefix because
    # trailmark_parse was called with str(repo)).
    run_slither(
        repo / "contracts",
        sarif,
        project_root=repo,
        timeout=120.0,
    )
    return gid, cache_root, sarif


@pytest.mark.skipif(
    shutil.which("slither") is None,
    reason="slither not on PATH",
)
@pytest.mark.skipif(
    not _solc_516_available(),
    reason="solc 0.5.16 not installed",
)
def test_augment_sarif_attaches_findings_to_nodes(
    fresh_tier1_with_sarif,
):
    """Chunk 4.2 success criterion: after augmentation,
    nodes_with_annotation returns at least one finding-tagged
    node. Empirically Tier 1 produces 82 matched findings on
    ~31 unique nodes."""
    gid, cache_root, sarif = fresh_tier1_with_sarif
    result = augment_sarif(gid, sarif, cache_root=cache_root)

    assert result["matched_findings"] >= 1, (
        f"slither found 82 issues on Tier 1; trailmark should "
        f"match at least one; got {result}"
    )
    assert "subgraphs_created" in result
    assert "unmatched_findings" in result

    findings_nodes = nodes_with_annotation(
        gid, "finding", cache_root=cache_root
    )
    assert len(findings_nodes) >= 1, (
        "nodes_with_annotation('finding') must return >= 1 "
        "node after augmentation — the success criterion of "
        "chunk 4.2"
    )


@pytest.mark.skipif(
    shutil.which("slither") is None,
    reason="slither not on PATH",
)
@pytest.mark.skipif(
    not _solc_516_available(),
    reason="solc 0.5.16 not installed",
)
def test_augment_sarif_persists_across_reload(
    fresh_tier1_with_sarif,
):
    """Augmentation must be saved to disk — reloading the
    engine should still see the findings. Validates the
    save_graph step inside the wrapper actually persists,
    not just mutates the in-memory deepcopy."""
    from src.graph.persist import _load_graph_cached

    gid, cache_root, sarif = fresh_tier1_with_sarif
    augment_sarif(gid, sarif, cache_root=cache_root)

    # Force a fresh load from disk (cache_clear evicts the
    # mutated engine; next load_graph deserializes from the
    # saved pickle).
    _load_graph_cached.cache_clear()
    engine = load_graph(gid, cache_root=cache_root)
    assert len(engine.findings()) >= 1, (
        "augmentation should be persisted via save_graph — a "
        "fresh load_graph must still see the findings"
    )


def test_augment_sarif_raises_when_sarif_missing(tmp_path):
    """Missing sarif_path → FileNotFoundError with the failing
    path in the message. Raises BEFORE acquiring _ANNOTATE_LOCK
    or touching the engine."""
    cache_root = tmp_path / "cache"
    gid = trailmark_parse(
        str(TIER1_FIXTURE),
        language="solidity",
        cache_root=cache_root,
    )
    missing = tmp_path / "does-not-exist.sarif"
    with pytest.raises(FileNotFoundError, match="does not exist"):
        augment_sarif(gid, missing, cache_root=cache_root)


def test_augment_sarif_rejects_oversized_sarif(tmp_path):
    """Cross-cutting review fix (I5): SARIF is attacker-facing
    input from analyzers running on attacker-supplied code.
    A malicious slither plugin or semgrep rule pack can emit
    arbitrarily large SARIF; Trailmark's json.load() has no
    streaming or cap. Wrapper caps at 50MB. Test pins this
    contract with a 51MB sparse file (cheap to create — file
    system reports the truncated size without storing
    physical bytes)."""
    cache_root = tmp_path / "cache"
    gid = trailmark_parse(
        str(TIER1_FIXTURE),
        language="solidity",
        cache_root=cache_root,
    )
    oversized = tmp_path / "huge.sarif"
    with oversized.open("wb") as f:
        # 51 MB sparse file. File-system reports the full
        # apparent size via stat().st_size without storing
        # 51 MB of zeros.
        f.truncate(51 * 1024 * 1024)
    with pytest.raises(ValueError, match="SARIF too large"):
        augment_sarif(gid, oversized, cache_root=cache_root)


def test_augment_sarif_raises_on_malformed_graph_id(tmp_path):
    """Standard graph_id validation (via load_graph) surfaces
    before any augmentation work."""
    sarif = tmp_path / "fake.sarif"
    sarif.write_text("{}")
    with pytest.raises(ValueError, match="invalid graph_id"):
        augment_sarif("not-hex!", sarif, cache_root=tmp_path)


@pytest.mark.skipif(
    shutil.which("slither") is None,
    reason="slither not on PATH",
)
@pytest.mark.skipif(
    not _solc_516_available(),
    reason="solc 0.5.16 not installed",
)
def test_augment_sarif_is_idempotent(fresh_tier1_with_sarif):
    """Calling augment_sarif twice on the same SARIF must NOT
    double-count findings. Trailmark's augment_from_sarif calls
    clear_augmented('sarif') first, so the second call clears
    the first's annotations and re-adds them — net result is
    the same end state.

    Pins the contract so a future Trailmark change that
    silently dropped the clear-first behavior would fail here
    instead of producing duplicate findings in production."""
    gid, cache_root, sarif = fresh_tier1_with_sarif

    first = augment_sarif(gid, sarif, cache_root=cache_root)
    second = augment_sarif(gid, sarif, cache_root=cache_root)

    assert second["matched_findings"] == first["matched_findings"], (
        f"second call should match same count as first; got "
        f"first={first['matched_findings']}, "
        f"second={second['matched_findings']} — likely a "
        f"regression in Trailmark's clear-then-add idempotency"
    )

    nodes = nodes_with_annotation(
        gid, "finding", cache_root=cache_root
    )
    # The exact node count is a Trailmark internal — what
    # matters is it's the same after a second augment.
    assert len(nodes) > 0


def test_augment_sarif_leaves_engine_clean_on_malformed_sarif(
    tmp_path,
):
    """Malformed SARIF (invalid JSON) must raise without
    leaving the on-disk engine in a partial-write state. The
    deepcopy + lock pattern guarantees this: the mutation
    happens on a copy; save_graph only runs if augment_sarif
    returns successfully.

    Verifies by checking the engine.pkl mtime is unchanged
    across the failing call."""
    repo = tmp_path / "tier1"
    shutil.copytree(TIER1_FIXTURE, repo)
    cache_root = tmp_path / "cache"
    gid = trailmark_parse(
        str(repo), language="solidity", cache_root=cache_root
    )

    bad_sarif = tmp_path / "malformed.sarif"
    bad_sarif.write_text("this is not valid json {{{")

    engine_pkl = cache_root / gid / "engine.pkl"
    mtime_before = engine_pkl.stat().st_mtime_ns

    with pytest.raises(Exception):
        # Trailmark raises some flavor of JSONDecodeError or
        # ValueError on malformed SARIF. We don't pin the
        # exception TYPE (that's Trailmark's contract); we pin
        # that the engine pickle is unchanged.
        augment_sarif(gid, bad_sarif, cache_root=cache_root)

    mtime_after = engine_pkl.stat().st_mtime_ns
    assert mtime_after == mtime_before, (
        "engine.pkl mtime should be unchanged after failed "
        "augment — deepcopy + lock means failures leave the "
        "on-disk state pristine"
    )
