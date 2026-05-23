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
MAX_BUDGET_USD = float(os.environ.get("MAX_BUDGET_USD", "3.0"))


# Always-on AM rules + full skill body pinned to the cached system prompt.
#
# The Anthropic Skills mechanism is loadable-on-demand: the agent sees a
# ~50-token description and must invoke the Skill tool to pull the body
# into context. That model-judgment gate is fine for skills that apply to
# *specific* tasks, but adaptive-markdown is the agent's contract — it
# applies to EVERY edit. Empirically, even capable models (Sonnet 4.6)
# don't invoke the skill before editing unless explicitly told to, which
# means the rules in SKILL.md are routinely bypassed (the agent runs git,
# tries to write baseline.md, skips __doc.cleanup, etc.).
#
# Fix: read SKILL.md at runtime and append it directly to the cached
# system prompt. Costs ~11k tokens on the cached prefix per session
# (cache_create on first turn; cache_read on subsequent turns — pennies
# either way). The Skill tool stays in allowed_tools as belt-and-
# suspenders, but the body is now unconditional.
_AM_CRITICAL_RULES = """\
## Adaptive Markdown — always-on rules

You are editing a document inside the AM viewer. Operate on the *document*, \
not on AM's source code.

### MANDATORY first action each session

**Before any other tool use, invoke the `adaptive-markdown` Skill** to \
load the full conventions (block types, ID model, `__doc.cleanup` \
patterns, figure conventions, etc.). Check your conversation history \
first — if the skill body is already there (you invoked it earlier this \
session), skip; the content is cached. Otherwise invoke it before \
reading, writing, or editing any document.

The rules in that skill bind every edit you make. The safety floor below \
is the minimum you must observe even if the skill content somehow fails \
to load.

### Safety floor (always enforced)

- **Persistence model.** AM has its own snapshot pipeline \
(`docs/<slug>/snaps/`); every successful Edit is captured automatically. \
Never run `git`. Never reason about commits, branches, or gitignore. If a \
change appears not to "stick", the bug is in the doc body or the viewer's \
renderer pipeline, never in git.

- **Writable scope.** Only `docs/<slug>/current.md` is writable. \
`baseline.md` is the immutable history-0; `snaps/` is backend-managed; \
anything outside the active doc's folder is off-limits. The pre-edit hook \
will reject other writes — refusing earlier in chat is cleaner than \
hitting a hook error.

- **The iframe is your world.** Visual issues ("equations are broken", \
"the widget doesn't render", "the page looks wrong") live in the doc's \
own `<script>`/`<style>`/math source/figure spec. They do NOT live in \
the AM viewer's source code, which is out of scope.
"""


def _extract_usage(message) -> dict | None:
    """Pull token counts from a ResultMessage. The SDK exposes usage as
    either a dict or an object; handle both. Returns None if no usage
    fields are present, so callers can skip serializing an empty payload.

    The four interesting fields:
      input_tokens               — uncached input on this turn
      cache_creation_input_tokens — new content being written to the cache
      cache_read_input_tokens     — cached prefix read from the cache
      output_tokens               — model output

    For a fresh session, `cache_creation_input_tokens` on the first turn
    approximates the system+tools+skills prompt size — useful for
    comparing prompt growth across commits.
    """
    usage = getattr(message, "usage", None)
    if usage is None:
        return None
    keys = (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
    )
    out: dict[str, int] = {}
    for k in keys:
        v = usage.get(k) if isinstance(usage, dict) else getattr(usage, k, None)
        if isinstance(v, int):
            out[k] = v
    return out or None


def _format_result_error(message, budget_usd: float) -> str:
    """Translate a ResultMessage with subtype != 'success' into a
    human-readable error for chat. Without this, the viewer sees a
    `turn_done` with a cost but no explanation of why the agent
    stopped mid-task — looks like the chat hung."""
    subtype = (getattr(message, "subtype", "") or "").strip()
    cost = getattr(message, "total_cost_usd", None)
    api_status = getattr(message, "api_error_status", None)
    if subtype == "error_max_budget_usd":
        return (
            f"Turn stopped: per-turn budget cap (${budget_usd:g}) reached "
            f"(spent ≈ ${cost:.2f}). "
            "Raise the cap by setting MAX_BUDGET_USD in .env (e.g. "
            "`MAX_BUDGET_USD=5.0`) and restart the backend. Or switch to "
            "a cheaper model — Haiku is ~30× cheaper than Opus per turn."
        )
    if subtype == "error_max_turns":
        return (
            "Turn stopped: max-turns cap reached. The agent kept calling "
            "tools without finishing. Try a more specific prompt, or break "
            "the task into smaller asks."
        )
    if api_status:
        return (
            f"Turn stopped: provider returned HTTP {api_status}. "
            + ("Rate limited — wait a moment and retry." if api_status == 429
               else "Provider error — check status.anthropic.com.")
        )
    errs = getattr(message, "errors", None)
    err_tail = ""
    if isinstance(errs, list) and errs:
        err_tail = " — " + "; ".join(str(e) for e in errs[:2])
    return f"Turn stopped: {subtype or 'unknown error'}{err_tail}"

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
        #
        # Operator opt-out (any platform): when AM_UNSAFE_BASH=1, skip
        # both sandboxing paths. The agent's Bash runs with the
        # backend's full env, network, and filesystem. Set by
        # `python start.py --unsafe-bash`. Out-of-band only; nothing
        # in chat can flip it.
        is_windows = sys.platform.startswith("win")
        unsafe_bash = os.environ.get("AM_UNSAFE_BASH") == "1"

        sandbox_cfg: SandboxSettings | None = None
        if not is_windows and not unsafe_bash:
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
        # The Windows pre_bash_hook checks AM_UNSAFE_BASH internally and
        # no-ops when it's set, so we still register the hook — keeping
        # the registration unconditional means the wrap re-engages
        # automatically if the env var ever flips off mid-process
        # (defense in depth; not a flow the launcher actually exposes).
        if is_windows and self.pre_bash_hook is not None:
            pre_tool_use_hooks.append(
                HookMatcher(matcher="Bash", hooks=[self.pre_bash_hook])
            )

        options = ClaudeAgentOptions(
            cwd=str(self.root),
            setting_sources=["project"],
            skills="all",
            # "Skill" in allowed_tools is what actually makes the
            # adaptive-markdown skill loadable. Without it, the SDK
            # injects only each skill's ~50-token frontmatter description
            # into the system prompt and gives the agent NO mechanism
            # to load the body — i.e., the 44 KB of AM rules in
            # .claude/skills/adaptive-markdown/SKILL.md would be
            # unreachable. The agent must invoke the Skill tool by
            # name to actually pull the body into context, so this
            # tool listing is load-bearing.
            allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash", "Skill"],
            sandbox=sandbox_cfg,
            permission_mode="acceptEdits",
            model=chosen,
            max_budget_usd=MAX_BUDGET_USD,
            # exclude_dynamic_sections strips per-session bits (cwd,
            # git status, auto-memory) from the system prompt so the
            # prompt-caching prefix stays stable across restarts and
            # across users. Within a single session this is a no-op
            # (the stable system prompt is cached either way); the
            # win is on session restarts and on shared installs.
            #
            # append_system_prompt pins the critical safety floor AND
            # the full AM skill body to the cached prefix, so the agent
            # has both unconditionally — no Skill-tool invocation
            # required. See _AM_CRITICAL_RULES + _load_am_skill_body
            # comments for why we inline rather than rely on the SDK's
            # on-demand skill loading.
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "exclude_dynamic_sections": True,
                # NOTE: the SDK key is "append" (per SystemPromptPreset's
                # TypedDict). Unknown keys are silently dropped — using
                # the wrong name (e.g. "append_system_prompt") fails
                # silently with zero behavior change.
                #
                # Why we don't inline the full SKILL.md body here:
                # Anthropic's SDK passes the appended content via CLI
                # command-line args to the bundled `claude.exe`. Windows'
                # CreateProcess hard-caps the command line at ~32k chars,
                # and SKILL.md alone is ~44 KB. So a long append blows up
                # backend startup with `WinError 206: filename or
                # extension is too long`. Keep `append` to the small
                # critical-rules floor; full body loading needs a
                # file-based path (CLAUDE.md auto-load, etc.).
                "append": _AM_CRITICAL_RULES,
            },
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
                # ResultMessage.subtype indicates HOW the turn ended.
                # "success" means normal completion; anything starting
                # with "error" means the SDK aborted (budget exhausted,
                # API error, permission denial, etc.). Without surfacing
                # these, the viewer just looks frozen — agent emitted
                # some tool calls, then nothing. Make the failure mode
                # visible in chat so the reader knows what to do.
                if message.is_error or (
                    message.subtype and message.subtype != "success"
                ):
                    yield {
                        "type": "error",
                        "text": _format_result_error(
                            message, MAX_BUDGET_USD,
                        ),
                    }
                event: AgentEvent = {
                    "type": "turn_done",
                    "session_id": message.session_id,
                    "cost_usd": message.total_cost_usd,
                }
                u = _extract_usage(message)
                if u:
                    event["usage"] = u
                yield event

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
