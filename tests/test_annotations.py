import pytest

from src.tools import (
    annotate,
    annotations_of,
    clear_annotations,
    nodes_with_annotation,
    trailmark_parse,
)

TRANSFER = "src.tokens.ERC20:ERC20.transfer"
APPROVE = "src.tokens.ERC20:ERC20.approve"


@pytest.fixture
def fresh_tier0(tier0_dir, tmp_path):
    """Fresh parse + per-test cache. Mutations don't leak between
    tests (unlike the session-scoped tier0_graph_id fixture)."""
    cache_root = tmp_path / "cache"
    gid = trailmark_parse(
        str(tier0_dir), language="solidity", cache_root=cache_root
    )
    return gid, cache_root


def test_annotate_and_read_back(fresh_tier0):
    gid, cache_root = fresh_tier0
    added = annotate(
        gid, TRANSFER, "assumption",
        "caller has approved the transfer amount",
        source="llm", cache_root=cache_root,
    )
    assert added is True
    notes = annotations_of(gid, TRANSFER, cache_root=cache_root)
    assert len(notes) == 1
    assert notes[0]["kind"] == "assumption"
    assert notes[0]["source"] == "llm"
    assert "approved" in notes[0]["description"]


def test_nodes_with_annotation_lists_annotated_node(fresh_tier0):
    gid, cache_root = fresh_tier0
    annotate(
        gid, TRANSFER, "finding", "lacks reentrancy guard",
        source="llm", cache_root=cache_root,
    )
    found = nodes_with_annotation(gid, "finding", cache_root=cache_root)
    ids = {n["id"] for n in found}
    assert TRANSFER in ids


def test_annotation_persists_through_reload(fresh_tier0):
    """Critical: load -> annotate -> save -> load again must see it."""
    gid, cache_root = fresh_tier0
    annotate(
        gid, TRANSFER, "invariant", "balance never goes negative",
        cache_root=cache_root,
    )
    notes = annotations_of(gid, TRANSFER, cache_root=cache_root)
    assert any(n["kind"] == "invariant" for n in notes)


def test_annotations_filtered_by_kind(fresh_tier0):
    gid, cache_root = fresh_tier0
    annotate(gid, TRANSFER, "assumption", "a", cache_root=cache_root)
    annotate(gid, TRANSFER, "finding", "f", cache_root=cache_root)
    all_notes = annotations_of(gid, TRANSFER, cache_root=cache_root)
    findings = annotations_of(
        gid, TRANSFER, kind="finding", cache_root=cache_root
    )
    assert len(all_notes) == 2
    assert len(findings) == 1
    assert findings[0]["kind"] == "finding"


def test_clear_all_annotations_on_node(fresh_tier0):
    gid, cache_root = fresh_tier0
    annotate(gid, TRANSFER, "assumption", "a", cache_root=cache_root)
    annotate(gid, TRANSFER, "finding", "f", cache_root=cache_root)
    clear_annotations(gid, TRANSFER, cache_root=cache_root)
    assert annotations_of(gid, TRANSFER, cache_root=cache_root) == []


def test_clear_annotations_by_kind(fresh_tier0):
    gid, cache_root = fresh_tier0
    annotate(gid, TRANSFER, "assumption", "a", cache_root=cache_root)
    annotate(gid, TRANSFER, "finding", "f", cache_root=cache_root)
    clear_annotations(
        gid, TRANSFER, kind="finding", cache_root=cache_root
    )
    remaining = annotations_of(gid, TRANSFER, cache_root=cache_root)
    kinds = {n["kind"] for n in remaining}
    assert kinds == {"assumption"}


@pytest.mark.parametrize(
    "bad_kind",
    ["asumption", "FINDING", "", "random-string"],
)
def test_invalid_kind_raises(fresh_tier0, bad_kind):
    gid, cache_root = fresh_tier0
    with pytest.raises(ValueError, match="invalid annotation kind"):
        annotate(
            gid, TRANSFER, bad_kind, "x", cache_root=cache_root
        )


def test_annotations_on_separate_nodes_are_independent(fresh_tier0):
    gid, cache_root = fresh_tier0
    annotate(gid, TRANSFER, "assumption", "t1", cache_root=cache_root)
    annotate(gid, APPROVE, "finding", "a1", cache_root=cache_root)
    transfer_notes = annotations_of(
        gid, TRANSFER, cache_root=cache_root
    )
    approve_notes = annotations_of(
        gid, APPROVE, cache_root=cache_root
    )
    assert len(transfer_notes) == 1
    assert len(approve_notes) == 1
    assert transfer_notes[0]["description"] == "t1"
    assert approve_notes[0]["description"] == "a1"
