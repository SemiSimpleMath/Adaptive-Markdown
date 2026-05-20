"""Tests for codex_runtime._jsonl_to_event against the documented Codex CLI
`exec --json` event schema.

Schema reference (per OpenAI Codex docs):
- Top-level event types: thread.started, turn.started, turn.completed,
  turn.failed, item.started, item.updated, item.completed, error.
- item payload is at `data.item` with `type` (snake_case), `id`, plus
  type-specific fields.
- item.type values: command_execution, agent_message, reasoning,
  file_change, mcp_tool_call, web_search, plan / plan_update.
- turn.completed has `usage: {input_tokens, cached_input_tokens,
  output_tokens, reasoning_output_tokens}`.
- Example: {"type":"item.started","item":{"id":"item_1",
  "type":"command_execution","command":"bash -lc ls","status":"in_progress"}}
"""
import json
from pathlib import Path

from agent_runtime.codex_runtime import CodexRuntime


def _r():
    """Throwaway CodexRuntime with no finalize fn — fine for testing the
    pure _jsonl_to_event helper which doesn't touch instance state."""
    return CodexRuntime(Path("."))


def _ev(d):
    """Pack a dict as the JSONL line the parser receives."""
    return json.dumps(d)


# ---- Bookkeeping events are filtered out -----------------------------

def test_thread_started_is_dropped():
    assert _r()._jsonl_to_event(_ev({"type": "thread.started", "thread_id": "t_1"})) is None


def test_turn_started_is_dropped():
    assert _r()._jsonl_to_event(_ev({"type": "turn.started"})) is None


def test_turn_failed_is_dropped():
    # Currently None; could become a visible error in future.
    result = _r()._jsonl_to_event(_ev({"type": "turn.failed"}))
    assert result is None or result.get("type") == "error"


# ---- turn.completed surfaces usage as turn_done ----------------------

def test_turn_completed_emits_turn_done_with_usage():
    usage = {
        "input_tokens": 1000,
        "cached_input_tokens": 800,
        "output_tokens": 250,
        "reasoning_output_tokens": 100,
    }
    result = _r()._jsonl_to_event(_ev({"type": "turn.completed", "usage": usage}))
    assert result is not None
    assert result["type"] == "turn_done"
    assert result.get("usage") == usage


def test_turn_completed_without_usage_still_emits_turn_done():
    result = _r()._jsonl_to_event(_ev({"type": "turn.completed"}))
    assert result is not None
    assert result["type"] == "turn_done"


# ---- error events ----------------------------------------------------

def test_error_event_emits_error():
    result = _r()._jsonl_to_event(_ev({"type": "error", "message": "boom"}))
    assert result is not None
    assert result["type"] == "error"
    assert "boom" in result["text"]


# ---- item.started: tool_use events for actual operations -------------

def test_item_started_command_execution_emits_tool_use():
    payload = {
        "type": "item.started",
        "item": {"id": "item_1", "type": "command_execution",
                 "command": "bash -lc ls", "status": "in_progress"},
    }
    result = _r()._jsonl_to_event(_ev(payload))
    assert result is not None
    assert result["type"] == "tool_use"
    assert result["name"] == "command_execution"
    # The item itself should be passed as input so the summarizer can mine it
    assert result["input"].get("command") == "bash -lc ls"
    assert result["input"].get("id") == "item_1"


def test_item_started_file_change_emits_tool_use():
    payload = {
        "type": "item.started",
        "item": {"id": "item_2", "type": "file_change",
                 "changes": [{"path": "foo.md", "kind": "modify", "diff": "..."}],
                 "status": "in_progress"},
    }
    result = _r()._jsonl_to_event(_ev(payload))
    assert result is not None
    assert result["type"] == "tool_use"
    assert result["name"] == "file_change"


def test_item_started_mcp_tool_call_emits_tool_use():
    payload = {
        "type": "item.started",
        "item": {"id": "item_3", "type": "mcp_tool_call",
                 "server": "fs", "tool": "read", "arguments": {"path": "x"}},
    }
    result = _r()._jsonl_to_event(_ev(payload))
    assert result is not None
    assert result["name"] == "mcp_tool_call"


def test_item_started_web_search_emits_tool_use():
    payload = {
        "type": "item.started",
        "item": {"id": "item_4", "type": "web_search", "query": "rolle theorem"},
    }
    result = _r()._jsonl_to_event(_ev(payload))
    assert result is not None
    assert result["name"] == "web_search"


def test_item_started_plan_emits_tool_use():
    for t in ("plan", "plan_update"):
        payload = {"type": "item.started",
                   "item": {"id": "p_1", "type": t, "text": "draft plan..."}}
        result = _r()._jsonl_to_event(_ev(payload))
        assert result is not None, f"plan-like type {t} should surface"
        assert result["name"] == t


# ---- item.started: reasoning is filtered (noise) ---------------------

def test_item_started_reasoning_is_dropped():
    payload = {
        "type": "item.started",
        "item": {"id": "r_1", "type": "reasoning",
                 "summary": "thinking...", "content": "..."},
    }
    assert _r()._jsonl_to_event(_ev(payload)) is None


# ---- item.completed: agent_message becomes assistant text ------------

def test_item_completed_agent_message_emits_text():
    payload = {
        "type": "item.completed",
        "item": {"id": "item_3", "type": "agent_message",
                 "text": "Repo has docs, sdk, examples."},
    }
    result = _r()._jsonl_to_event(_ev(payload))
    assert result is not None
    assert result.get("role") == "assistant"
    assert result["type"] == "text"
    assert "docs, sdk, examples" in result["text"]


def test_item_completed_command_execution_is_dropped():
    # The item.started already emitted the tool_use; completion is a no-op
    # for the activity strip (status/exit info could be logged later).
    payload = {
        "type": "item.completed",
        "item": {"id": "item_1", "type": "command_execution",
                 "command": "ls", "status": "completed", "exit_code": 0},
    }
    assert _r()._jsonl_to_event(_ev(payload)) is None


# ---- item.updated: silent for now ------------------------------------

def test_item_updated_is_dropped():
    # Streaming deltas not yet surfaced; should not crash.
    payload = {"type": "item.updated", "item": {"id": "item_3",
                                                 "type": "agent_message",
                                                 "text": "partial..."}}
    result = _r()._jsonl_to_event(_ev(payload))
    assert result is None or result.get("type") in ("text", "tool_use")


# ---- malformed input -------------------------------------------------

def test_invalid_json_is_dropped():
    assert _r()._jsonl_to_event("not json at all") is None


def test_empty_type_is_dropped():
    assert _r()._jsonl_to_event(_ev({"foo": "bar"})) is None


def test_item_started_without_item_does_not_crash():
    # Defensive: malformed Codex output shouldn't blow up the adapter
    payload = {"type": "item.started"}
    # Should return either None or a tool_use with a sensible fallback
    result = _r()._jsonl_to_event(_ev(payload))
    if result is not None:
        assert result.get("type") in ("tool_use", "text")


# ---- unrecognized item types still surface ---------------------------

def test_unknown_item_type_still_surfaces_as_tool_use():
    # Future-proofing: if Codex adds a new item.type, we should still
    # surface it rather than silently drop. The audit log can show
    # generic name + payload keys.
    payload = {
        "type": "item.started",
        "item": {"id": "x_1", "type": "future_thing",
                 "some_field": "value"},
    }
    result = _r()._jsonl_to_event(_ev(payload))
    assert result is not None
    assert result["name"] == "future_thing"
