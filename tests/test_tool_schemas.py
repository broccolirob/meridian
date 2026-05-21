"""Regression armor: prevent the LLM-tool-schema parameter
leak from coming back.

Every public tool exposed to the LLM (main agent's `tools=`
list or any subagent's `tools` list) must NOT include
`cache_root` or `vault_path` in its OpenAI function-call
schema. LangChain's default schema generation includes ALL
keyword-only params, so the load-bearing fix is to annotate
those params with `InjectedToolArg` so the schema generator
excludes them.

If this test starts failing, a new tool was added without
the annotation. Copy the annotation pattern from any
existing tool:

    from typing import Annotated
    from langchain_core.tools import InjectedToolArg

    def new_tool(
        graph_id: str,
        *,
        cache_root: Annotated[Path, InjectedToolArg] = CACHE_ROOT,
    ) -> ...:
        ...
"""

import inspect

from langchain_core.tools import StructuredTool

from src.agent import _wrap_subagent_writers
from src.graph.topo import topo_order
from src.subagents import FLOW_TRACER_SUBAGENT, NODE_DOCUMENTER_SUBAGENT
from src.tools import graph_summary

# Params that must NEVER appear in any LLM-facing tool schema.
# Update this list when a new injected param is added.
FORBIDDEN_LLM_PARAMS = frozenset(["cache_root", "vault_path"])

# Main agent tools list mirrors src/agent.py:build_agent.
# Hard-coded so a refactor that drops a tool while keeping the
# test passing doesn't silently shrink coverage.
MAIN_AGENT_TOOLS = [graph_summary, topo_order]

# Subagent tool lists AFTER the closure wrap. The writers
# (render_and_write_*_note) get replaced by closures that pre-
# bind vault_path so the LLM tool schema can't expose it. We
# test the actually-built subagent tools, not the raw module-
# level templates — the templates still reference raw writers
# (which DO leak vault_path on their own), but those never
# reach the LLM in production.
_FAKE_VAULT = "/tmp/fake-vault-for-schema-test"
WRAPPED_NODE_DOCUMENTER = _wrap_subagent_writers(
    NODE_DOCUMENTER_SUBAGENT, _FAKE_VAULT
)
WRAPPED_FLOW_TRACER = _wrap_subagent_writers(
    FLOW_TRACER_SUBAGENT, _FAKE_VAULT
)
SUBAGENT_TOOLS = (
    list(WRAPPED_NODE_DOCUMENTER["tools"])
    + list(WRAPPED_FLOW_TRACER["tools"])
)


def _llm_schema_params(fn) -> set[str]:
    """Return the param names a LangChain-wrapped tool exposes
    to the LLM. Wraps fn via StructuredTool.from_function (the
    same path deepagents uses internally) and reads
    `tool_call_schema`, which respects InjectedToolArg."""
    tool = StructuredTool.from_function(fn)
    # tool_call_schema is typed `type[BaseModel] | dict` in
    # LangChain — in practice always the BaseModel class for
    # tools built from Python functions. Read via getattr so
    # mypy doesn't trip on the union.
    fields: dict = getattr(tool.tool_call_schema, "model_fields", {})
    return set(fields.keys())


def test_no_main_agent_tool_leaks_injected_params():
    """Tools on the main agent's tools=[] list must not expose
    cache_root or vault_path to the LLM. cache_root is hidden
    via `Annotated[Path, InjectedToolArg]`; vault_path doesn't
    appear on these tools at all."""
    for fn in MAIN_AGENT_TOOLS:
        params = _llm_schema_params(fn)
        leaked = params & FORBIDDEN_LLM_PARAMS
        assert not leaked, (
            f"{fn.__name__} leaks {leaked} to LLM schema; "
            f"add `Annotated[Path, InjectedToolArg]` to the "
            f"parameter type."
        )


def test_no_subagent_tool_leaks_injected_params():
    """Same contract for every subagent tool AFTER the closure
    wrap. Writers are vault_path-pre-bound; everything else
    uses InjectedToolArg on cache_root."""
    for fn in SUBAGENT_TOOLS:
        params = _llm_schema_params(fn)
        leaked = params & FORBIDDEN_LLM_PARAMS
        assert not leaked, (
            f"{fn.__name__} leaks {leaked} to LLM schema. "
            f"For writers: ensure _wrap_subagent_writers "
            f"replaces the raw function with a closure. For "
            f"other tools: add `Annotated[Path, "
            f"InjectedToolArg]` to the parameter type."
        )


def test_wrapped_writers_actually_invokable_without_vault_path():
    """The naive InjectedToolArg-only approach would HIDE
    vault_path from the schema but cause the function call to
    fail (Pydantic validation: 'vault_path: Field required').
    The closure wrap must let the LLM call the wrapped writer
    with only its visible args.

    This is the load-bearing armor — without it, a future
    refactor could replace the closure with InjectedToolArg-
    only and the schema tests would still pass while the
    actual LLM invocations broke.

    Round-17 update: the wrapper no longer accepts `node`. The
    LLM-visible surface is `graph_id`, `graph_ctx`, `body`.
    The node id comes from the `_EXPECTED_NODE_ID` contextvar
    set by `_invoke_one`. Pin both: the LLM can invoke without
    `node`, AND the wrapper forwards the contextvar's id."""
    from unittest.mock import patch

    from src.agent import _EXPECTED_NODE_ID

    node_tool = next(
        t
        for t in WRAPPED_NODE_DOCUMENTER["tools"]
        if getattr(t, "__name__", "") == "render_and_write_node_note"
    )
    wrapped = StructuredTool.from_function(node_tool)
    # Args the LLM can see + supply (no vault_path, no
    # cache_root, no `node` — round-17 dropped it from the
    # LLM-facing surface).
    visible_args = set(wrapped.tool_call_schema.model_fields.keys())
    assert "vault_path" not in visible_args
    assert "cache_root" not in visible_args
    assert "node" not in visible_args, (
        f"`node` leaked back into the LLM schema: {visible_args}"
    )

    # Invoke via the StructuredTool path the framework uses.
    # Patch the real writer to avoid touching the filesystem
    # — we only care that the wrapper passes vault_path AND
    # synthesizes the node carrier from the contextvar.
    token = _EXPECTED_NODE_ID.set("expected.module:ExpectedNode")
    try:
        with patch(
            "src.agent.render_and_write_node_note",
            return_value="/fake/path.md",
        ) as fake:
            result = wrapped.invoke({
                "graph_id": "abcdef012345",
                "graph_ctx": {},
                "body": "test",
            })
    finally:
        _EXPECTED_NODE_ID.reset(token)

    assert result == "/fake/path.md"
    # The closure forwarded the orchestrator's vault_path as
    # the first positional arg AND the contextvar's id as the
    # third (the node-carrier dict).
    call_args = fake.call_args
    assert call_args.args[0] == _FAKE_VAULT
    assert call_args.args[2] == {"id": "expected.module:ExpectedNode"}


def test_wrapper_cannot_be_redirected_by_llm_smuggled_node():
    """Codex round-17 cross-node defense end-to-end: even if
    an LLM tries to smuggle a `node` argument (or any
    arbitrary key in `graph_ctx`), the Pydantic-generated
    schema rejects the unknown field at validation time AND
    the wrapper would still use the contextvar id.

    Pin both layers: (1) the schema rejects unknown fields,
    so the LLM literally can't call the tool with a `node`
    arg; (2) even if `graph_ctx` contains an "id" key, the
    wrapper forwards the contextvar id, not the smuggled
    one. The first layer is the primary defense; the second
    is belt-and-suspenders for any future schema change."""
    from unittest.mock import patch

    from src.agent import _EXPECTED_NODE_ID

    node_tool = next(
        t
        for t in WRAPPED_NODE_DOCUMENTER["tools"]
        if getattr(t, "__name__", "") == "render_and_write_node_note"
    )
    wrapped = StructuredTool.from_function(node_tool)

    token = _EXPECTED_NODE_ID.set("expected:Node")
    try:
        with patch(
            "src.agent.render_and_write_node_note",
            return_value="/fake/path.md",
        ) as fake:
            # The LLM smuggles a "node" key inside graph_ctx
            # (since `node` itself is not on the schema). The
            # wrapper passes graph_ctx through; downstream
            # `render_and_write_node_note` ignores any "node"
            # key in graph_ctx (it consults the `node` POSITIONAL
            # arg only). The wrapper builds that positional arg
            # from the contextvar.
            result = wrapped.invoke({
                "graph_id": "abcdef012345",
                "graph_ctx": {
                    "node": {"id": "ATTACKER:Smuggled"},
                    "callers": [],
                },
                "body": "test",
            })
    finally:
        _EXPECTED_NODE_ID.reset(token)

    assert result == "/fake/path.md"
    # Positional node-carrier dict — must be the expected id,
    # NOT "ATTACKER:Smuggled".
    call_args = fake.call_args
    assert call_args.args[2] == {"id": "expected:Node"}, (
        f"cross-node smuggle succeeded: {call_args.args[2]}"
    )


def test_wrapped_writers_reject_invocation_outside_dispatch_context():
    """Round-17 contract: calling the wrapper without setting
    `_EXPECTED_NODE_ID` / `_EXPECTED_ENTRYPOINT_ID` raises
    RuntimeError. The wrapper has no sane default for the
    node id — every legitimate caller goes through `_invoke_one`,
    which sets the contextvar. Pin this so a future change
    that swaps RuntimeError for a silent fallback (e.g.,
    "use a sentinel id") gets caught."""
    import pytest

    node_tool = next(
        t
        for t in WRAPPED_NODE_DOCUMENTER["tools"]
        if getattr(t, "__name__", "") == "render_and_write_node_note"
    )
    wrapped_node = StructuredTool.from_function(node_tool)
    with pytest.raises(
        RuntimeError, match="outside a dispatch context"
    ):
        wrapped_node.invoke({
            "graph_id": "abcdef012345",
            "graph_ctx": {},
            "body": "test",
        })

    flow_tool = next(
        t
        for t in WRAPPED_FLOW_TRACER["tools"]
        if getattr(t, "__name__", "") == "render_and_write_flow_note"
    )
    wrapped_flow = StructuredTool.from_function(flow_tool)
    with pytest.raises(
        RuntimeError, match="outside a dispatch context"
    ):
        wrapped_flow.invoke({
            "graph_id": "abcdef012345",
            "paths": [],
            "overview": "ov",
            "observations": [],
        })


def test_python_callers_can_still_pass_cache_root():
    """InjectedToolArg only hides the param from the LLM; the
    function's Python signature is unchanged. Tests, scripts,
    and dispatch_topo continue to pass cache_root= as a kwarg.

    Pinning the contract here so a future refactor that
    accidentally widens the fix (e.g., making cache_root
    positional-only) breaks this test before the test suite
    breaks elsewhere."""
    sig = inspect.signature(graph_summary)
    assert "cache_root" in sig.parameters
    assert (
        sig.parameters["cache_root"].kind
        == inspect.Parameter.KEYWORD_ONLY
    )


def test_python_callers_can_still_pass_vault_path():
    """Same contract for vault_path on the writers."""
    from src.render.obsidian import render_and_write_node_note

    sig = inspect.signature(render_and_write_node_note)
    assert "vault_path" in sig.parameters
    # vault_path is positional today; the fix doesn't change
    # that — it only hides the param from the LLM schema.
    assert (
        sig.parameters["vault_path"].kind
        == inspect.Parameter.POSITIONAL_OR_KEYWORD
    )
