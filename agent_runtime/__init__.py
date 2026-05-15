"""Agent runtime registry."""
from __future__ import annotations

import os
from importlib import import_module
from pathlib import Path
from typing import Any, Callable

from .base import AgentRuntime


DEFAULT_PROVIDER = os.environ.get("AGENT_PROVIDER", "claude").lower()

_RUNTIMES = {
    "claude": ("agent_runtime.claude_runtime", "ClaudeRuntime"),
    "codex": ("agent_runtime.codex_runtime", "CodexRuntime"),
}


def create_runtime(
    provider: str | None,
    root: Path,
    pre_edit_hook: Callable[..., Any],
    post_edit_hook: Callable[..., Any],
) -> AgentRuntime:
    """Create a provider-specific runtime at the single provider boundary."""
    selected = (provider or DEFAULT_PROVIDER).lower()
    try:
        module_name, class_name = _RUNTIMES[selected]
    except KeyError as exc:
        available = ", ".join(sorted(_RUNTIMES))
        raise ValueError(
            f"Unknown AGENT_PROVIDER={selected!r}; available: {available}"
        ) from exc
    module = import_module(module_name)
    runtime_cls = getattr(module, class_name)
    return runtime_cls(root, pre_edit_hook, post_edit_hook)
