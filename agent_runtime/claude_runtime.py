"""Claude Agent SDK runtime adapter."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    SandboxNetworkConfig,
    SandboxSettings,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from .base import AgentEvent


DEFAULT_MODEL = os.environ.get("MODEL", "claude-haiku-4-5-20251001")
MAX_BUDGET_USD = float(os.environ.get("MAX_BUDGET_USD", "1.0"))

MODEL_ALIASES = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
}


class ClaudeRuntime:
    """Adaptive Markdown runtime backed by the Claude Agent SDK."""

    def __init__(
        self,
        root: Path,
        pre_edit_hook: Callable[..., Any],
        post_edit_hook: Callable[..., Any],
        finalize_md_edit_fn: Callable[..., Any] | None = None,
        pre_bash_hook: Callable[..., Any] | None = None,
    ) -> None:
        self.root = root
        self.pre_edit_hook = pre_edit_hook
        self.post_edit_hook = post_edit_hook
        # Unused — the SDK's hook system handles the substrate per Edit/Write.
        # Accepted for signature parity with CodexRuntime.
        self.finalize_md_edit_fn = finalize_md_edit_fn
        # Hooked on `Bash`. Used on Windows where the CLI sandbox no-ops; the
        # hook rewrites the command through `python -m sandbox` so the agent
        # runs in a subprocess with a curated env, scoped cwd, and timeout.
        # No-op on macOS/Linux/WSL2 (CLI sandbox handles it).
        self.pre_bash_hook = pre_bash_hook
        self.client: ClaudeSDKClient | None = None
        self._current_model = DEFAULT_MODEL

    @property
    def current_model(self) -> str:
        return self._current_model

    @property
    def model_aliases(self) -> list[str]:
        return list(MODEL_ALIASES.keys())

    def resolve_model(self, name: str | None) -> str:
        """Map a friendly name (haiku/sonnet/opus) or pass through SDK ids."""
        if not name:
            return DEFAULT_MODEL
        return MODEL_ALIASES.get(name.lower(), name)

    async def start(self, model: str | None = None) -> None:
        chosen = self.resolve_model(model)

        # The agent gets Read/Write/Edit/Glob/Grep + Bash. Bash unlocks
        # structured-data transforms (MusicXML transpose, CSV pivot,
        # image resize, etc.) that pure text editing can't do
        # ergonomically.
        #
        # Platform split on enforcement:
        #
        #   macOS / Linux / WSL2 — the CLI binary's built-in sandbox
        #     (Apple Seatbelt / bubblewrap) is real. Pass `sandbox=` and
        #     trust it.
        #
        #   Windows — the CLI prints `Sandbox disabled: windows is not
        #     supported` and runs commands unsandboxed. We instead wire a
        #     PreToolUse hook on `Bash` that rewrites the command through
        #     `python -m sandbox`, which runs it in a subprocess with
        #     curated env, scoped cwd, and timeout (best-effort; not
        #     airtight, see `sandbox.py`).
        is_windows = sys.platform.startswith("win")

        sandbox_cfg: SandboxSettings | None = None
        if not is_windows:
            sandbox_cfg = SandboxSettings(
                enabled=True,
                autoAllowBashIfSandboxed=True,
                allowUnsandboxedCommands=False,
                excludedCommands=[],
                network=SandboxNetworkConfig(
                    allowedDomains=[],
                    deniedDomains=[],
                    allowManagedDomainsOnly=False,
                    allowLocalBinding=True,
                    allowUnixSockets=[],
                    allowAllUnixSockets=False,
                    allowMachLookup=[],
                    httpProxyPort=0,
                    socksProxyPort=0,
                ),
            )

        pre_tool_use_hooks = [
            HookMatcher(matcher="Edit|Write", hooks=[self.pre_edit_hook])
        ]
        if is_windows and self.pre_bash_hook is not None:
            pre_tool_use_hooks.append(
                HookMatcher(matcher="Bash", hooks=[self.pre_bash_hook])
            )

        options = ClaudeAgentOptions(
            cwd=str(self.root),
            setting_sources=["project"],
            skills="all",
            allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
            sandbox=sandbox_cfg,
            permission_mode="acceptEdits",
            model=chosen,
            max_budget_usd=MAX_BUDGET_USD,
            hooks={
                "PreToolUse": pre_tool_use_hooks,
                "PostToolUse": [
                    HookMatcher(matcher="Edit|Write", hooks=[self.post_edit_hook])
                ],
            },
        )
        self.client = ClaudeSDKClient(options=options)
        await self.client.__aenter__()
        self._current_model = chosen
        print(
            f"Claude Agent SDK ready (model={chosen}, budget=${MAX_BUDGET_USD}/turn)",
            flush=True,
        )

    async def shutdown(self) -> None:
        if self.client:
            try:
                await self.client.__aexit__(None, None, None)
            finally:
                self.client = None

    async def reset(self, model: str | None = None) -> None:
        await self.shutdown()
        await self.start(model)

    async def run_turn(self, text: str) -> AsyncIterator[AgentEvent]:
        if not self.client:
            yield {"type": "error", "text": "Agent runtime not initialized"}
            return

        await self.client.query(text)
        async for message in self.client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    event = self._block_to_event(block)
                    if event:
                        yield {"role": "assistant", **event}
            elif isinstance(message, ResultMessage):
                yield {
                    "type": "turn_done",
                    "session_id": message.session_id,
                    "cost_usd": message.total_cost_usd,
                }

    def _block_to_event(self, block: Any) -> AgentEvent | None:
        if isinstance(block, TextBlock):
            return {"type": "text", "text": block.text}
        if isinstance(block, ThinkingBlock):
            return {"type": "thinking", "text": block.thinking}
        if isinstance(block, ToolUseBlock):
            return {"type": "tool_use", "name": block.name, "input": block.input}
        if isinstance(block, ToolResultBlock):
            content = block.content
            if isinstance(content, list):
                content = " ".join(
                    c.get("text", "") for c in content if isinstance(c, dict)
                )
            return {
                "type": "tool_result",
                "ok": not bool(block.is_error),
                "text": str(content) if content else "",
            }
        return None
