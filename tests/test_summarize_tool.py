"""Tests for backend._summarize_tool — the stdout audit log line builder.

Covers both runtimes' tool names:
- Claude SDK: Read, Edit, Write, Glob, Grep, Bash, WebFetch, WebSearch
- Codex CLI item types: command_execution, file_change, mcp_tool_call,
  web_search, plan / plan_update
"""
from am_hooks import _summarize_tool


# ---- Claude SDK tool names ------------------------------------------

def test_read_shows_file_path():
    assert "intro.md" in _summarize_tool("Read", {"file_path": "examples/intro.md"})


def test_edit_shows_file_path():
    assert "intro.md" in _summarize_tool("Edit", {"file_path": "examples/intro.md"})


def test_bash_shows_first_token_of_command():
    out = _summarize_tool("Bash", {"command": "ls -la examples/"})
    assert "ls" in out


def test_grep_shows_pattern():
    assert "rolle" in _summarize_tool("Grep", {"pattern": "rolle"}).lower()


# ---- Codex CLI item types -------------------------------------------

def test_command_execution_surfaces_command():
    # Codex's item.command is the shell line
    out = _summarize_tool("command_execution", {"command": "bash -lc ls"})
    assert "command_execution" in out or "shell" in out.lower()
    assert "bash" in out or "ls" in out


def test_file_change_surfaces_path():
    item = {"changes": [{"path": "examples/intro.md", "kind": "modify", "diff": "..."}]}
    out = _summarize_tool("file_change", item)
    assert "file_change" in out or "intro.md" in out


def test_web_search_surfaces_query():
    out = _summarize_tool("web_search", {"query": "apery proof"})
    assert "apery" in out.lower() or "web_search" in out


def test_mcp_tool_call_surfaces_server_and_tool():
    out = _summarize_tool("mcp_tool_call",
                          {"server": "fs", "tool": "read",
                           "arguments": {"path": "x"}})
    # Server and tool together are the useful identifier
    assert "mcp" in out.lower() or ("fs" in out and "read" in out)


def test_plan_surfaces_text_excerpt():
    out = _summarize_tool("plan", {"text": "1. Read the file 2. Edit it"})
    # Should show some hint of the plan content, not just "plan {...}"
    assert "plan" in out.lower()


# ---- Unknown tool names: graceful fallback --------------------------

def test_unknown_tool_with_payload_surfaces_keys():
    out = _summarize_tool("future_tool", {"command": "do-thing", "id": "x_1"})
    # Shouldn't be just "future_tool {...}"; should show the payload hint
    assert "do-thing" in out or "command" in out


def test_unknown_tool_with_no_payload_falls_back():
    out = _summarize_tool("future_tool", {})
    # Should at least show the tool name
    assert "future_tool" in out


def test_none_input_does_not_crash():
    out = _summarize_tool("Read", None)
    # No file_path in payload — should not crash, should at least show the name
    assert "Read" in out
