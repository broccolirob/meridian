import pytest

from src.render.mermaid import render_call_graph

# Tier 1 fixture: the swap function is the canonical entrypoint
# the chunk's success criterion is written against.
SWAP_ID = "contracts.UniswapV2Pair:UniswapV2Pair.swap"


def test_swap_diagram_contains_required_nodes(tier1_graph_id):
    """Chunk 3.1 success criterion: swap + _update + _safeTransfer
    all present in the call graph at default depth."""
    gid, cache_root = tier1_graph_id
    out = render_call_graph(gid, SWAP_ID, cache_root=cache_root)
    assert "swap" in out
    assert "_update" in out
    assert "_safeTransfer" in out


def test_output_is_a_fenced_mermaid_block(tier1_graph_id):
    """Consumers embed the return value directly in Markdown."""
    gid, cache_root = tier1_graph_id
    out = render_call_graph(gid, SWAP_ID, cache_root=cache_root)
    assert out.startswith("```mermaid\n")
    assert out.rstrip().endswith("```")
    assert "graph TD" in out


def test_depth_zero_returns_root_only(tier1_graph_id):
    """depth=0 = just the focus node, no callers or callees."""
    gid, cache_root = tier1_graph_id
    out = render_call_graph(gid, SWAP_ID, depth=0, cache_root=cache_root)
    assert "-->" not in out
    assert "swap" in out


def test_negative_depth_raises(tier1_graph_id):
    gid, cache_root = tier1_graph_id
    with pytest.raises(ValueError, match="depth must be >= 0"):
        render_call_graph(gid, SWAP_ID, depth=-1, cache_root=cache_root)


def test_output_is_byte_stable(tier1_graph_id):
    """Sorted-alias assignment + sorted-edge output must produce
    identical bytes across runs — required for snapshot tests in
    later chunks (3.5 goldens)."""
    gid, cache_root = tier1_graph_id
    a = render_call_graph(gid, SWAP_ID, cache_root=cache_root)
    b = render_call_graph(gid, SWAP_ID, cache_root=cache_root)
    assert a == b


# --- cycle handling (chunk 3.16, /review I4) --------------------------


def _method_node(node_id: str) -> dict:
    """Minimal Trailmark-shaped method node for synthetic graphs."""
    return {
        "id": node_id,
        "name": node_id.rsplit(":", 1)[-1],
        "kind": "method",
        "location": {
            "file_path": f"/fake/{node_id}.sol",
            "start_line": 1,
            "end_line": 10,
            "start_col": 0,
            "end_col": 0,
        },
        "parameters": [],
        "return_type": None,
        "exception_types": [],
        "cyclomatic_complexity": None,
        "branches": [],
        "docstring": None,
    }


def test_render_call_graph_terminates_on_mutual_recursion(monkeypatch):
    """Mutual recursion A.foo ↔ B.bar must NOT infinite-loop the
    BFS. The `visited` set + `edges` set (chunk 3.1) are what
    make this safe; pre-3.16 Tier 0/1 fixtures were DAGs so the
    cycle-handling claim in the docstring was unverified.

    Invariants checked:
      1. Termination — test returns at all (no infinite loop).
      2. Each node appears EXACTLY once in the output (visited
         dict enforces this).
      3. Each edge appears EXACTLY once (edges set enforces this)."""
    A_FOO = "module:A.foo"
    B_BAR = "module:B.bar"

    nodes = {
        A_FOO: _method_node(A_FOO),
        B_BAR: _method_node(B_BAR),
    }
    # Mutual recursion: A.foo calls B.bar AND B.bar calls A.foo.
    # callers_map[X] = nodes that CALL X. callees_map[X] = nodes
    # that X CALLS. Cycle: both directions present.
    callers_map = {
        A_FOO: [nodes[B_BAR]],
        B_BAR: [nodes[A_FOO]],
    }
    callees_map = {
        A_FOO: [nodes[B_BAR]],
        B_BAR: [nodes[A_FOO]],
    }

    monkeypatch.setattr(
        "src.render.mermaid.get_node",
        lambda gid, nid, *, cache_root=None: nodes[nid],
    )
    monkeypatch.setattr(
        "src.render.mermaid.callers_of",
        lambda gid, nid, *, cache_root=None: callers_map.get(nid, []),
    )
    monkeypatch.setattr(
        "src.render.mermaid.callees_of",
        lambda gid, nid, *, cache_root=None: callees_map.get(nid, []),
    )

    # If the visited guard regresses, this would either
    # infinite-loop (and pytest would hang) or OOM. Reaching the
    # assertions below is itself the termination proof.
    out = render_call_graph(
        "abc012345678", A_FOO, depth=5
    )

    # Each node line appears EXACTLY once. The visited dict
    # prevents duplicate `nN[...]` declarations.
    node_lines = [
        ln for ln in out.splitlines()
        if ln.lstrip().startswith(("n0[", "n1["))
    ]
    assert len(node_lines) == 2, (
        f"expected 2 node lines (one per real node), got "
        f"{len(node_lines)}: {node_lines}"
    )
    assert sum("A.foo" in ln for ln in node_lines) == 1
    assert sum("B.bar" in ln for ln in node_lines) == 1

    # Each edge appears EXACTLY once. The edges set prevents
    # `n0 --> n1` showing twice even though both BFS directions
    # discover it.
    edge_lines = [
        ln for ln in out.splitlines() if " --> " in ln
    ]
    assert len(edge_lines) == 2, (
        f"expected 2 edges (A→B and B→A), got "
        f"{len(edge_lines)}: {edge_lines}"
    )
    assert sum("n0 --> n1" in ln for ln in edge_lines) == 1
    assert sum("n1 --> n0" in ln for ln in edge_lines) == 1


def test_render_call_graph_terminates_on_self_loop(monkeypatch):
    """Direct self-loop (X calls X) — the degenerate cycle. The
    visited guard must keep `X` from re-entering the frontier,
    and the edges set must dedupe (X, X) to one line."""
    SELF = "module:X.recurse"
    nodes = {SELF: _method_node(SELF)}
    callers_map = {SELF: [nodes[SELF]]}  # X calls X
    callees_map = {SELF: [nodes[SELF]]}

    monkeypatch.setattr(
        "src.render.mermaid.get_node",
        lambda gid, nid, *, cache_root=None: nodes[nid],
    )
    monkeypatch.setattr(
        "src.render.mermaid.callers_of",
        lambda gid, nid, *, cache_root=None: callers_map.get(nid, []),
    )
    monkeypatch.setattr(
        "src.render.mermaid.callees_of",
        lambda gid, nid, *, cache_root=None: callees_map.get(nid, []),
    )

    out = render_call_graph("abc012345678", SELF, depth=5)

    # Exactly one node line (self), exactly one edge (self-loop).
    node_lines = [
        ln for ln in out.splitlines()
        if ln.lstrip().startswith("n0[")
    ]
    edge_lines = [
        ln for ln in out.splitlines() if " --> " in ln
    ]
    assert len(node_lines) == 1
    assert "X.recurse" in node_lines[0]
    assert len(edge_lines) == 1
    assert "n0 --> n0" in edge_lines[0]


# --- _quoted_label direct unit tests (chunk 3.16, /review I11) ------
#
# The function is module-private and gets covered indirectly through
# `render_call_graph` + `render_complexity_heatmap`. /review I11
# noted that the `"`-escape branch is dead in production because
# `_validate_node_id` excludes `"` at the trust boundary and
# `_bare_name`-derived labels (the only inputs `_quoted_label`
# sees today) inherit that restriction. The escape stays in
# place — removing it would silently produce broken Mermaid
# (`""foo""` instead of `"&quot;foo&quot;"`) if Trailmark ever
# surfaces `"`-bearing IDs (e.g., a future language). These
# direct tests pin every reachable AND defensive branch so the
# escape is no longer "dead and untested".


def test_quoted_label_passes_clean_solidity_id_unchanged():
    """Plain Solidity identifiers (the production input shape)
    pass through without quoting — keeps Mermaid output dense."""
    from src.render.mermaid import _quoted_label

    assert _quoted_label("UniswapV2Pair.swap") == "UniswapV2Pair.swap"
    assert _quoted_label("ERC4626.deposit") == "ERC4626.deposit"
    assert _quoted_label("_safeTransfer") == "_safeTransfer"


def test_quoted_label_quotes_labels_with_special_chars():
    """Each char in ' ()<>"|[]{}' triggers quoting individually.
    Loops the inventory so a future addition to the escape list
    fails this test until both sides agree."""
    from src.render.mermaid import _quoted_label

    # Each special char in isolation, embedded in a clean prefix.
    for special in [" ", "(", ")", "<", ">", "|", "[", "]", "{", "}"]:
        label = f"foo{special}bar"
        result = _quoted_label(label)
        assert result.startswith('"') and result.endswith('"'), (
            f"label with {special!r} should be quoted; got {result!r}"
        )


def test_quoted_label_escapes_embedded_double_quote():
    """The defensive `"` escape (dead in production today,
    armored for future Trailmark expansion). Without escaping,
    `_quoted_label('he said "hi"')` would emit
    `"he said "hi""` — a parse error in Mermaid because the
    inner quotes close the outer quotes. The `&quot;` entity
    is Mermaid's HTML-entity escape syntax for embedded
    quotes."""
    from src.render.mermaid import _quoted_label

    result = _quoted_label('he said "hi"')
    assert result == '"he said &quot;hi&quot;"'
    # No literal " inside the quoted body (would break Mermaid).
    assert result.count('"') == 2, (
        f"only the outer wrappers should be literal `\"`; got {result!r}"
    )


def test_quoted_label_handles_complexity_heatmap_label_shape():
    """The other live caller (mermaid.py:451) passes
    `f"{bare}, CC={cc}"` — a comma and space trigger quoting.
    Pins the heatmap label shape so a label-format refactor
    that drops the comma doesn't accidentally unquote."""
    from src.render.mermaid import _quoted_label

    assert _quoted_label("swap, CC=4") == '"swap, CC=4"'
    assert _quoted_label("foo, CC=0") == '"foo, CC=0"'


def test_quoted_label_returns_clean_identifier_unquoted():
    """Boundary: identifier-only chars (the `[A-Za-z0-9_:.]+`
    surface from `_validate_node_id`) never trigger quoting.
    Pins that `_quoted_label` won't quote things `_bare_name`
    extracts from valid Trailmark IDs — keeps the production
    Mermaid output free of unnecessary quotes."""
    from src.render.mermaid import _quoted_label

    for label in [
        "abc",
        "a:b",
        "a.b",
        "a_b",
        "module.Class.method",
        "X:Y.z",
        "Foo123",
    ]:
        assert _quoted_label(label) == label, (
            f"clean label {label!r} should not be quoted"
        )
