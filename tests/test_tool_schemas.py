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
    actual LLM invocations broke."""
    from unittest.mock import patch

    node_tool = next(
        t
        for t in WRAPPED_NODE_DOCUMENTER["tools"]
        if getattr(t, "__name__", "") == "render_and_write_node_note"
    )
    wrapped = StructuredTool.from_function(node_tool)
    # Args the LLM can see + supply (no vault_path, no
    # cache_root).
    visible_args = set(wrapped.tool_call_schema.model_fields.keys())
    assert "vault_path" not in visible_args
    assert "cache_root" not in visible_args

    # Invoke via the StructuredTool path the framework uses.
    # Patch the real writer to avoid touching the filesystem
    # — we only care that the wrapper passes vault_path.
    with patch(
        "src.agent.render_and_write_node_note",
        return_value="/fake/path.md",
    ) as fake:
        result = wrapped.invoke({
            "graph_id": "abcdef012345",
            "node": {"id": "x", "kind": "contract", "name": "X"},
            "graph_ctx": {},
            "body": "test",
        })

    assert result == "/fake/path.md"
    # The closure forwarded the orchestrator's vault_path as
    # the first positional arg.
    call_args = fake.call_args
    assert call_args.args[0] == _FAKE_VAULT


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
