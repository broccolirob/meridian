from src.agent import DEFAULT_MODEL, _build_system_prompt, build_agent

GRAPH_ID = "0d453ae2b905"
VAULT = "/tmp/test-vault"


def test_build_agent_returns_invocable(monkeypatch):
    """Agent constructs without error and exposes the langgraph
    invocation surface (.invoke / .stream).

    `ChatOpenAI` validates `OPENAI_API_KEY` at construction time
    (pydantic v2 validator), so the test sets a dummy key — we never
    call the API here, just verify the agent assembles."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-fake-key-for-construct-only")
    agent = build_agent(GRAPH_ID, VAULT)
    assert hasattr(agent, "invoke")
    assert hasattr(agent, "stream")


def test_build_agent_prompt_contains_topological_order():
    """CHUNKS.md success criterion — the literal phrase must appear."""
    prompt = _build_system_prompt(GRAPH_ID, VAULT)
    assert "topological order" in prompt


def test_build_agent_prompt_carries_graph_id_and_vault():
    """The 9-step plan needs these as constants; verify they're
    interpolated."""
    prompt = _build_system_prompt(GRAPH_ID, VAULT)
    assert GRAPH_ID in prompt
    assert VAULT in prompt


def test_build_agent_is_deterministic():
    """Two builds with identical args produce identical system prompts.
    We can't directly compare CompiledStateGraphs, but the prompt is
    the planning-layer surface that matters."""
    a = _build_system_prompt(GRAPH_ID, VAULT)
    b = _build_system_prompt(GRAPH_ID, VAULT)
    assert a == b


def test_default_model_is_gpt5_mini():
    """Locks the model choice — agents drift when defaults change
    silently."""
    assert DEFAULT_MODEL == "gpt-5-mini"
