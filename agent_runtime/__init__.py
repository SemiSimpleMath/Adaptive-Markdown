"""Agent runtime registry."""
from __future__ import annotations

import os
from importlib import import_module
from pathlib import Path
from typing import Any, Callable

from .base import AgentRuntime


def default_provider() -> str:
    """Resolve the active provider from env at CALL time, not import time.

    Reading AGENT_PROVIDER at import time made the import order
    `set env var -> import agent_runtime` load-bearing — callers had
    to remember to set `os.environ['AGENT_PROVIDER']` BEFORE any
    transitive import of this module, or the constant captured the
    pre-set value (usually 'claude') and ignored later changes.
    Making it a function removes the coupling: any code path that
    needs to know the current default just calls this."""
    return os.environ.get("AGENT_PROVIDER", "claude").lower()


_RUNTIMES = {
    "claude": ("agent_runtime.claude_runtime", "ClaudeRuntime"),
    "codex": ("agent_runtime.codex_runtime", "CodexRuntime"),
}


def create_runtime(
    provider: str | None,
    root: Path,
    pre_edit_hook: Callable[..., Any],
    post_edit_hook: Callable[..., Any],
    finalize_md_edit_fn: Callable[..., Any] | None = None,
    pre_bash_hook: Callable[..., Any] | None = None,
) -> AgentRuntime:
    """Create a provider-specific runtime at the single provider boundary.

    `finalize_md_edit_fn(doc_path, before_text, snap_id, author)` is the
    shared substrate hook used by providers that don't have per-edit hooks
    (e.g. Codex CLI). Claude's runtime ignores it — its hook system handles
    the substrate per Edit/Write tool call.

    `pre_bash_hook` is the PreToolUse hook on `Bash`. Used by ClaudeRuntime
    on Windows to wrap commands through the subprocess sandbox (since the
    CLI's built-in sandbox is macOS/Linux/WSL2-only). Codex ignores it.
    """
    selected = (provider or default_provider()).lower()
    try:
        module_name, class_name = _RUNTIMES[selected]
    except KeyError as exc:
        available = ", ".join(sorted(_RUNTIMES))
        raise ValueError(
            f"Unknown AGENT_PROVIDER={selected!r}; available: {available}"
        ) from exc
    module = import_module(module_name)
    runtime_cls = getattr(module, class_name)
    return runtime_cls(
        root, pre_edit_hook, post_edit_hook, finalize_md_edit_fn,
        pre_bash_hook=pre_bash_hook,
    )
