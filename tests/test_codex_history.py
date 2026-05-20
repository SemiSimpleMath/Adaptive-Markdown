"""Tests for codex_runtime conversation-history accumulation and cap.

History grows on each successful turn and is replayed into subsequent
prompts so the per-turn `codex exec` subprocess sees prior context. The
cap prevents unbounded growth: at ~5 KB per turn pair and ~21 KB skill,
40+ turns without a cap means >200 KB prompts.

Cap policy: keep the last N pairs (one user + one assistant = one pair),
default 10. When dropping, the prompt should mark that earlier turns
were truncated so the model knows context was elided rather than absent.
"""
from pathlib import Path

from agent_runtime.codex_runtime import CodexRuntime, HISTORY_MAX_PAIRS


def _r_with_skill_disabled():
    r = CodexRuntime(Path("."))
    # Make prompt testing easier by short-circuiting the skill content,
    # which is otherwise ~21 KB and dominates the rendered prompt.
    r._skill_text = None
    return r


def test_default_history_cap_is_reasonable():
    # Sanity check: cap should be present, not ridiculous.
    assert HISTORY_MAX_PAIRS >= 5
    assert HISTORY_MAX_PAIRS <= 50


def test_empty_history_still_carries_time_block():
    # Even with no skill and no history, the time block is always present
    # so the agent has a clock to read. Bare-text return is no longer
    # possible — the user request is wrapped beneath the time line.
    r = _r_with_skill_disabled()
    r._history = []
    out = r._build_prompt("hello")
    assert out.startswith("Current time:"), out[:80]
    assert "hello" in out
    assert out.endswith("hello")


def test_under_cap_history_is_included_in_full():
    r = _r_with_skill_disabled()
    r._history = [
        {"role": "user", "text": "first"},
        {"role": "assistant", "text": "reply 1"},
        {"role": "user", "text": "second"},
        {"role": "assistant", "text": "reply 2"},
    ]
    out = r._build_prompt("third")
    assert "first" in out
    assert "reply 1" in out
    assert "second" in out
    assert "reply 2" in out
    assert "third" in out
    # No truncation marker when under cap
    assert "earlier conversation truncated" not in out.lower()


def test_over_cap_history_drops_oldest_with_marker():
    r = _r_with_skill_disabled()
    # Build N+5 pairs so we go meaningfully over the cap. Pad indices so
    # `user-001` doesn't substring-match against `user-010`.
    pairs = HISTORY_MAX_PAIRS + 5
    r._history = []
    for i in range(pairs):
        r._history.append({"role": "user", "text": f"user-{i:03d}"})
        r._history.append({"role": "assistant", "text": f"reply-{i:03d}"})
    out = r._build_prompt("latest")
    # Most recent N pairs are present
    for i in range(pairs - HISTORY_MAX_PAIRS, pairs):
        assert f"user-{i:03d}" in out, f"recent turn user-{i:03d} should be present"
        assert f"reply-{i:03d}" in out, f"recent turn reply-{i:03d} should be present"
    # Oldest pairs are gone
    for i in range(0, pairs - HISTORY_MAX_PAIRS):
        assert f"user-{i:03d}" not in out, f"old turn user-{i:03d} should be truncated"
    # The marker is present so the model knows context was elided
    assert "earlier conversation truncated" in out.lower()


def test_at_exact_cap_no_marker():
    r = _r_with_skill_disabled()
    r._history = []
    for i in range(HISTORY_MAX_PAIRS):
        r._history.append({"role": "user", "text": f"user-{i:03d}"})
        r._history.append({"role": "assistant", "text": f"reply-{i:03d}"})
    out = r._build_prompt("latest")
    # All pairs present
    for i in range(HISTORY_MAX_PAIRS):
        assert f"user-{i:03d}" in out
        assert f"reply-{i:03d}" in out
    # No marker
    assert "earlier conversation truncated" not in out.lower()


def test_unbalanced_history_does_not_crash():
    # Defensive: if history ends with a user message without an assistant
    # reply (an edge case that shouldn't happen but could), build_prompt
    # should still produce something sane.
    r = _r_with_skill_disabled()
    r._history = [{"role": "user", "text": "orphan"}]
    out = r._build_prompt("next")
    assert "orphan" in out
    assert out.endswith("next")
