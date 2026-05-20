"""Agent runtime integration: hooks, path validation, runtime lifecycle.

Three concerns live here because they're all about the bridge between the
backend and the agent SDK:

  - **PreToolUse / PostToolUse hooks** that the SDK calls per Edit/Write
    (and on Windows, per Bash). The pre-hook validates the target path
    and snapshots; the post-hook validates the result and reverts on
    failure, or records a pending entry if the doc is in review mode.

  - **Path validation** (`_validate_agent_write_path`) — the gate that
    keeps the agent from writing outside `docs/<slug>/current.md`.
    Source-of-truth list of which paths the agent may touch.

  - **Runtime lifecycle** (init / shutdown / reset) — owns the global
    runtime reference on `state.runtime`.

Plus `_summarize_tool` — the one-line audit-log formatter for tool_use
events. Used by `run_turn` in am_ws to print `[tool] ...` lines.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import validators
from agent_runtime import create_runtime

from am_docs import (
    DOC_SLUG_RE as _DOC_SLUG_RE,
    DOCS_ROOT,
    ROOT,
    _doc_slug_from_path,
    list_all_docs,
)
from am_pending import _read_review_mode, add_pending_edit
from am_snapshots import save_snapshot
from am_state import state
from am_tracking import detect_id_drift

# Per-tool-call stash so post-edit hook can diff for drift + derive patches.
# Keyed by tool_use_id; value: {"file_path": str, "before_text": str,
#   "before_bytes": bytes, "snap_id": str}
_pre_edit_state: dict[str, dict] = {}
# Per-file consecutive-revert counter for the validator retry-cap.
_validate_revert_count: dict[str, int] = {}


def _validate_agent_write_path(file_path: str) -> tuple[bool, str]:
    """Decide whether the agent is allowed to Edit/Write a given path.

    The only writable target is `docs/<slug>/current.md`. Anything else —
    baseline.md (the immutable history-0), snaps/, project source files,
    dotfiles, absolute paths outside ROOT — is rejected. This is the
    root-cause defense against a successful prompt injection convincing
    the agent to clobber files outside its lane."""
    if not file_path:
        return False, "missing file_path"
    try:
        p = Path(file_path).resolve()
    except (OSError, ValueError):
        return False, f"invalid file_path: {file_path!r}"
    try:
        p.relative_to(DOCS_ROOT)
    except ValueError:
        return False, (
            f"writes outside docs/ are not permitted ({file_path!r}). "
            "The agent may only modify docs/<slug>/current.md."
        )
    if p.name != "current.md":
        return False, (
            f"only current.md is writable by the agent ({p.name!r}). "
            "baseline.md is immutable (history-0); snaps/ is managed "
            "by the backend; project files are off-limits."
        )
    if p.parent.parent != DOCS_ROOT:
        return False, (
            f"writable paths must be docs/<slug>/current.md, not "
            f"{file_path!r}. Slugs are direct children of docs/."
        )
    if not _DOC_SLUG_RE.match(p.parent.name):
        return False, (
            f"doc slug {p.parent.name!r} is not a valid name. Slugs must "
            "match [a-zA-Z0-9][a-zA-Z0-9-]{0,63}."
        )
    return True, ""


def _deny_pre_tool_use(reason: str) -> dict:
    """Build the PreToolUse hook response that vetoes the tool call."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


async def pre_tool_use_hook(input_data, tool_use_id, context):
    """Validate the target path, then capture pre-edit state for the .md
    snapshot/patch substrate.

    Path validation is the security gate against a successful prompt
    injection convincing the agent to clobber a file outside its lane —
    the hook is the last line of defense before bytes hit disk."""
    tool_input = input_data.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path", "")

    ok, reason = _validate_agent_write_path(file_path)
    if not ok:
        print(f"[pre-edit] REJECTED ({file_path!r}): {reason}", flush=True)
        return _deny_pre_tool_use(reason)

    try:
        p = Path(file_path)
        # Capture raw bytes for byte-perfect snapshot + revert, AND decoded
        # text for drift detection / pending entries. Different consumers,
        # different needs — feeding text through write_bytes after decode
        # would silently normalize \\r\\n -> \\n.
        before_bytes = p.read_bytes() if p.exists() else b""
        before_text = before_bytes.decode("utf-8") if before_bytes else ""
        snap_id = save_snapshot(p, before_bytes) if before_bytes else None
        _pre_edit_state[tool_use_id] = {
            "file_path": file_path,
            "before_text": before_text,
            "before_bytes": before_bytes,
            "snap_id": snap_id,
        }
    except Exception as e:
        print(f"[pre-edit] snapshot failed: {e}", flush=True)
    return {}


UNSAFE_BASH = os.environ.get("AM_UNSAFE_BASH") == "1"


async def pre_bash_hook(input_data, tool_use_id, context):
    """Windows fallback: rewrite the agent's Bash command to run through
    `python -m sandbox`, which pins cwd to the active doc folder, curates
    env, and enforces a timeout. No-op on macOS/Linux/WSL2 where the CLI
    binary's real sandbox is in play.

    When the launcher passes `--unsafe-bash` (sets AM_UNSAFE_BASH=1),
    the wrap is also skipped — the agent's command runs with the
    backend's full env, network, and filesystem. Out-of-band activation
    only: nothing the agent can do in chat enables this.

    The hook rejects the call outright if no doc is active (defensive —
    every chat turn pins `state.current_doc_slug` before scheduling, so
    this should not normally happen)."""
    if os.name != "nt":
        return {}  # SDK sandbox handles enforcement on Unix-y systems

    if UNSAFE_BASH:
        return {}  # operator opted in at launch; let the command through

    tool_input = input_data.get("tool_input", {}) or {}
    cmd = tool_input.get("command", "")
    if not cmd:
        return {}

    slug = state.current_doc_slug
    if not slug:
        return _deny_pre_tool_use(
            "Bash refused: no active doc folder to scope the sandbox to. "
            "Open a doc first."
        )

    wrapped = (
        f'"{sys.executable}" -m sandbox '
        f'--slug "{slug}" --timeout 30 --cmd {json.dumps(cmd)}'
    )
    print(f"[pre-bash] wrapping cmd in sandbox (slug={slug!r})", flush=True)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {**tool_input, "command": wrapped},
        }
    }


async def finalize_md_edit(
    doc_path: Path,
    before_text: str | None,
    snap_id: str | None,
    author: str,
) -> None:
    """Shared post-edit substrate work — used by both the Claude post-hook
    (per Edit/Write) and the Codex per-turn finalizer.

    - If we have before_text but no snap_id, save a fresh snapshot now.
      (Claude's pre-hook already snapshotted; Codex snapshots post-turn.)
    - Detect any track-ID drift the agent may have introduced.
    - Broadcast doc_changed (by slug); refresh the doc list if this is a
      newly-visible doc.
    """
    if before_text is not None and snap_id is None:
        try:
            snap_id = save_snapshot(doc_path, before_text)
        except Exception as e:
            print(f"[snap] post-turn snapshot failed: {e}", flush=True)

    if before_text is not None:
        try:
            await detect_id_drift(doc_path, before_text)
        except Exception as e:
            print(f"[alias] drift detection failed: {e}", flush=True)

    slug = _doc_slug_from_path(doc_path)
    if slug:
        await state.broadcast({"type": "doc_changed", "doc": slug})
        # New doc visible to the doc list? Refresh.
        await state.broadcast({"type": "docs", "list": list_all_docs(), "doc": slug})


async def post_tool_use_hook(input_data, tool_use_id, context):
    """After an Edit/Write to a .md file: validate the post-edit content;
    on validation failure, revert to the pre-edit snapshot and surface the
    errors to the agent via `additionalContext` so it retries inside the
    same turn. On success, detect track-id drift, derive a patch, and
    broadcast doc_changed (via finalize_md_edit)."""
    tool_input = input_data.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path", "")
    if not file_path or not file_path.endswith(".md"):
        return {}

    stash = _pre_edit_state.pop(tool_use_id, None)
    before_text = stash["before_text"] if stash else None
    before_bytes = stash.get("before_bytes") if stash else None
    snap_id = stash["snap_id"] if stash else None
    p = Path(file_path)

    # Validate post-edit content. Empty file (e.g., the edit deleted
    # everything) skips validation — that's a deliberate-looking action,
    # not the kind of corruption the validators are meant to catch.
    try:
        new_text = p.read_text(encoding="utf-8") if p.exists() else ""
    except (OSError, UnicodeDecodeError):
        new_text = ""
    errors = validators.validate_doc(new_text) if new_text.strip() else []

    # Debug capture: when AM_DEBUG_CAPTURE is set, dump every attempted
    # post-edit content keyed by tool_use_id so we can autopsy what the
    # agent actually wrote before the revert. Captured regardless of
    # validation outcome so we see passing edits too.
    if os.environ.get("AM_DEBUG_CAPTURE"):
        try:
            cap_dir = ROOT / "tests" / "_haiku_capture"
            cap_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%H%M%S-%f")
            cap_path = cap_dir / (
                f"{stamp}-{(tool_use_id or 'unknown')[:8]}-"
                f"{'FAIL' if errors else 'OK'}.md"
            )
            cap_path.write_text(new_text, encoding="utf-8")
            if errors:
                err_path = cap_path.with_suffix(".errors.txt")
                err_path.write_text(
                    "\n".join(
                        f"[{e['kind']} @ line {e['line']}] {e['message']}"
                        for e in errors
                    ),
                    encoding="utf-8",
                )
        except OSError as e:
            print(f"[debug-capture] failed: {e}", flush=True)

    if errors:
        # Revert: restore before_text if the file existed pre-edit, or
        # delete it if this was a brand-new file. Skip finalize_md_edit
        # entirely — on-disk state matches what the viewer already has.
        try:
            if before_bytes:
                # write_bytes is byte-exact — the pre-edit file's line
                # endings, BOM, and trailing-newline state survive revert.
                p.write_bytes(before_bytes)
                action = "reverted"
            elif before_text:
                # Defensive fallback if stash predates the bytes capture.
                with p.open("w", encoding="utf-8", newline="") as f:
                    f.write(before_text)
                action = "reverted"
            else:
                p.unlink(missing_ok=True)
                action = "deleted new file"
            # Log the actual error content so the operator can see what
            # tripped the validator — not just the count. When the agent
            # gets stuck in a retry loop, this is the visibility that
            # tells you whether the validator is catching a real bug or
            # misfiring on valid code.
            print(
                f"[validate] FAILED on {file_path}, {action} "
                f"({len(errors)} err); telling agent",
                flush=True,
            )
            for e in errors:
                print(
                    f"[validate]   {e['kind']} @ line {e['line']}:",
                    flush=True,
                )
                for ln in e["message"].split("\n"):
                    print(f"[validate]     {ln}", flush=True)
        except OSError as e:
            print(f"[validate] revert failed: {e}", flush=True)
        # Retry-cap: track consecutive reverts on this file. After 3 in a
        # row, every further failure tells the agent the loop is stopped
        # — durable until a clean edit lands. Without durability the
        # counter would reset on the cap-fire turn and the agent could
        # burn three more turns before the next cap.
        revert_count = _validate_revert_count.get(file_path, 0) + 1
        _validate_revert_count[file_path] = revert_count
        agent_msg = validators.format_for_agent(errors, file_path)
        if revert_count == 3:
            # First time at the cap — surface to the reader so they know
            # something's going sideways with the agent.
            await state.broadcast({
                "role": "assistant", "type": "text",
                "text": (
                    f"_Validator rejected 3 consecutive edits to "
                    f"`{file_path}`. The retry loop is now paused — the "
                    "agent has been told to stop and report. Most recent "
                    "error:_\n\n```\n" + agent_msg + "\n```"
                ),
            })
        if revert_count >= 3:
            # Tell the agent unambiguously to stop trying and report.
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        f"RETRY LOOP STOPPED. This is failure #{revert_count} "
                        f"on {file_path} in a row. Do NOT attempt another "
                        "Edit on this file in this turn. Reply to the "
                        "reader: explain what you tried to do, paste the "
                        "validator error verbatim, and ask them to "
                        "intervene (clear chat, restore from history, or "
                        "rephrase the request). The next clean edit on "
                        "this file resets the counter.\n\n" + agent_msg
                    ),
                }
            }
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": agent_msg,
            }
        }
    # Clean edit — reset the consecutive-revert counter for this file.
    _validate_revert_count.pop(file_path, None)

    # Pending-changes substrate (phase 2): if the doc declares
    # `review_mode: pending` in its frontmatter, record this edit as a
    # pending entry that the reader can later Accept (keep) or Reject
    # (revert to old_text). The bytes still land on disk normally — the
    # design's "current.md doesn't move" framing is dropped in favor of
    # "bytes land, pending tracks revocability" so successive Edits in a
    # turn see each other's effects and the SDK doesn't break.
    #
    # MVP granularity: one whole-file entry per doc, replacing on each
    # subsequent edit. Phase 3+ will decompose into per-block entries.
    if _read_review_mode(p):
        slug = _doc_slug_from_path(p)
        if slug:
            try:
                add_pending_edit(slug, {
                    "tool_use_id": tool_use_id or "",
                    "block": {"kind": "doc"},
                    "old_text": before_text or "",
                    "new_text": new_text,
                    "agent_label": (
                        f"{state.current_provider}:{state.current_model}"
                    ),
                })
                print(
                    f"[pending] recorded edit on docs/{slug}/current.md "
                    f"(review_mode=on)",
                    flush=True,
                )
            except Exception as e:
                # Pending-recording is non-load-bearing — a failure here
                # shouldn't abort the edit. The bytes are already on disk.
                print(f"[pending] failed to record: {e!r}", flush=True)

    await finalize_md_edit(
        p,
        before_text,
        snap_id,
        author=f"agent:{state.current_model}",
    )
    return {}


async def init_runtime(model: str | None = None):
    state.runtime = create_runtime(
        state.current_provider,
        ROOT,
        pre_tool_use_hook,
        post_tool_use_hook,
        finalize_md_edit,
        pre_bash_hook=pre_bash_hook,
    )
    await state.runtime.start(model)
    state.current_model = state.runtime.current_model
    await state.broadcast({
        "type": "model_changed",
        "provider": state.current_provider,
        "model": state.current_model,
    })


async def shutdown_runtime():
    """Tear down the active runtime. Wrapped in a wait_for timeout so a
    misbehaving SDK that won't unwind (e.g., Claude SDK __aexit__ blocking
    on MCP cleanup, or a Codex subprocess still alive) doesn't hang Ctrl+C
    indefinitely. After the timeout we drop the reference and let the
    process exit anyway."""
    if not state.runtime:
        return
    try:
        await asyncio.wait_for(state.runtime.shutdown(), timeout=5.0)
    except asyncio.TimeoutError:
        print("[shutdown] runtime didn't unwind in 5s; exiting anyway",
              flush=True)
    except Exception as e:
        print(f"[shutdown] error during runtime shutdown: {e!r}", flush=True)
    finally:
        state.runtime = None


async def reset_runtime_session(model: str | None = None):
    await shutdown_runtime()
    await init_runtime(model)
    await state.broadcast({"type": "chat_reset"})


def _summarize_tool(name: str, inp: dict | None) -> str:
    """One-line description of a tool_use event for the stdout audit log.

    Covers two distinct event shapes:
    - Claude SDK tool names (Read, Edit, Write, Glob, Grep, Bash, Web*) with
      Claude's flat input dict.
    - Codex CLI item types from `codex exec --json` (command_execution,
      file_change, mcp_tool_call, web_search, plan, plan_update, agent_message)
      where `inp` is the item payload itself.

    Unknown names fall through with a generic payload-key hint.
    """
    if not isinstance(inp, dict):
        inp = {}
    name = name or ""
    # Claude SDK tools
    if name == "Read":      return f"Read     {inp.get('file_path', '?')}"
    if name == "Edit":      return f"Edit     {inp.get('file_path', '?')}"
    if name == "Write":     return f"Write    {inp.get('file_path', '?')}"
    if name == "Glob":      return f"Glob     pattern={inp.get('pattern', '?')!r}"
    if name == "Grep":      return f"Grep     pattern={inp.get('pattern', '?')!r}"
    if name == "Bash":
        cmd = (inp.get("command", "") or "").strip()
        return f"Bash     {cmd[:100]}"
    if name == "WebFetch":  return f"WebFetch {inp.get('url', '?')}"
    if name == "WebSearch": return f"WebSearch {inp.get('query', '?')}"
    # Codex CLI item types
    if name == "command_execution":
        cmd = inp.get("command", "")
        if isinstance(cmd, list):
            cmd = " ".join(str(x) for x in cmd)
        return f"command_execution {str(cmd).strip()[:100]}"
    if name == "file_change":
        changes = inp.get("changes") or []
        paths = [c.get("path", "?") for c in changes if isinstance(c, dict)]
        if not paths:
            return "file_change (no changes)"
        head = paths[0]
        more = f" +{len(paths) - 1}" if len(paths) > 1 else ""
        return f"file_change {head}{more}"
    if name == "mcp_tool_call":
        server = inp.get("server", "?")
        tool = inp.get("tool", "?")
        return f"mcp_tool_call {server}/{tool}"
    if name == "web_search":
        return f"web_search {str(inp.get('query', '')).strip()[:80]}"
    if name in ("plan", "plan_update"):
        text = str(inp.get("text", "")).strip().splitlines()
        head = text[0][:80] if text else ""
        return f"plan      {head}"
    if name == "agent_message":
        # Surfaces in audit log if backend ever logs message items as tools;
        # normally this path isn't hit because agent_message is yielded as
        # a text event, not a tool_use.
        return f"agent_message"
    # Legacy Codex names from earlier CLI versions — keep them recognizable
    # so older Codex builds don't fall through to the generic branch.
    if name in ("apply_patch", "apply_patch_call"):
        return "apply_patch"
    if name in ("shell", "local_shell", "local_shell_call"):
        cmd = inp.get("command", "")
        if isinstance(cmd, list):
            cmd = " ".join(str(x) for x in cmd)
        return f"shell    {str(cmd).strip()[:100]}"
    if name == "function_call":
        return f"function {inp.get('name', '?')}"
    # Unknown tool — surface distinguishing keys from the payload so the
    # audit log isn't useless. Helpful while we're still learning what
    # Codex CLI's JSONL events look like in the wild.
    interesting = [k for k in ("name", "command", "tool", "kind", "path",
                                "file_path", "query", "type", "id")
                   if inp.get(k)]
    if interesting:
        extras = ", ".join(f"{k}={str(inp[k])[:40]!r}" for k in interesting[:3])
        return f"{name:<8s} {extras}"
    return f"{name:<8s} {{...}}"
