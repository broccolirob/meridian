from src.subagents import NODE_DOCUMENTER_SUBAGENT


def test_subagent_has_required_keys():
    required = {"name", "description", "system_prompt", "tools"}
    assert required <= set(NODE_DOCUMENTER_SUBAGENT.keys())
    assert NODE_DOCUMENTER_SUBAGENT["name"] == "node-documenter"
    assert len(NODE_DOCUMENTER_SUBAGENT["description"]) > 50
    assert len(NODE_DOCUMENTER_SUBAGENT["system_prompt"]) > 500


def test_subagent_tools_are_callable():
    tools = NODE_DOCUMENTER_SUBAGENT["tools"]
    assert len(tools) >= 8
    for tool in tools:
        assert callable(tool), f"not callable: {tool!r}"


def test_subagent_tools_cover_required_capabilities():
    tool_names = {t.__name__ for t in NODE_DOCUMENTER_SUBAGENT["tools"]}
    # graph queries
    assert "get_node" in tool_names
    assert "callers_of" in tool_names
    assert "callees_of" in tool_names
    # source reading
    assert "read_file_range" in tool_names
    # render layer
    assert "render_node_note" in tool_names
    assert "resolve_wikilink" in tool_names
    # write side effects
    assert "write_obsidian_note" in tool_names
    assert "annotate" in tool_names
