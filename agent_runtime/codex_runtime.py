"""Codex CLI runtime adapter."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import AsyncIterator

from .base import AgentEvent


CODEX_AUTH_MODE = os.environ.get(
    "CODEX_AUTH_MODE",
    "api-key" if os.environ.get("OPENAI_API_KEY") else "chatgpt",
).lower()
_API_KEY_MODE = CODEX_AUTH_MODE in {"api", "api-key", "apikey"}
_DEFAULT_FAST_MODEL = "codex-mini-latest" if _API_KEY_MODE else "default"
_DEFAULT_MODELS = (
    "codex-mini-latest,default,gpt-5.2-codex,gpt-5.4-mini,gpt-5.3-codex"
    if _API_KEY_MODE
    else "default,gpt-5.4-mini,gpt-5.3-codex,gpt-5.2-codex,codex-mini-latest"
)
FAST_MODEL = os.environ.get("CODEX_FAST_MODEL", _DEFAULT_FAST_MODEL)
DEFAULT_MODEL = os.environ.get("CODEX_MODEL", FAST_MODEL)
CODEX_MODELS = [
    item.strip()
    for item in os.environ.get("CODEX_MODELS", _DEFAULT_MODELS).split(",")
    if item.strip()
]
CODEX_COMMAND = os.environ.get("CODEX_COMMAND", "codex")
CODEX_SANDBOX = os.environ.get("CODEX_SANDBOX", "workspace-write")
CODEX_APPROVAL_POLICY = os.environ.get("CODEX_APPROVAL_POLICY", "never")


class CodexRuntime:
    """Adaptive Markdown runtime backed by `codex exec`.

    This adapter deliberately contains Codex CLI details here, away from the
    model-agnostic backend. Each turn currently starts a fresh non-interactive
    Codex exec session; if the CLI exposes stable resumable JSONL semantics for
    long-lived app sessions, that can evolve inside this file.
    """

    def __init__(
        self,
        root: Path,
        pre_edit_hook=None,
        post_edit_hook=None,
        finalize_md_edit_fn=None,
    ) -> None:
        self.root = root
        self.pre_edit_hook = pre_edit_hook
        self.post_edit_hook = post_edit_hook
        # Codex CLI has no per-edit hook. Backend provides this callable so
        # the runtime can replay the substrate work (snapshot + patch +
        # broadcast) for each .md file that changed during the turn.
        self.finalize_md_edit_fn = finalize_md_edit_fn
        self.executable: str | None = None
        self._current_model = DEFAULT_MODEL
        # Codex CLI has no built-in equivalent of Claude's project-skill loader,
        # so we inject the adaptive-markdown skill into every turn's prompt.
        # Cached once at start(); identical prefix across turns is cache-friendly.
        self._skill_path = root / ".agents" / "skills" / "adaptive-markdown" / "SKILL.md"
        self._skill_text: str | None = None
        # Each codex exec is a fresh subprocess with no memory of prior turns.
        # We replay the conversation by prepending {user, assistant} pairs to
        # the prompt. Reset by start() — i.e. on new_chat or model switch.
        self._history: list[dict] = []

    @property
    def current_model(self) -> str:
        return self._current_model

    @property
    def model_aliases(self) -> list[str]:
        aliases = []
        for model in (self._current_model, FAST_MODEL, *CODEX_MODELS, "default"):
            if model not in aliases:
                aliases.append(model)
        return aliases

    def resolve_model(self, name: str | None) -> str:
        if not name or name == "default":
            return DEFAULT_MODEL
        return name

    async def start(self, model: str | None = None) -> None:
        self.executable = shutil.which(CODEX_COMMAND)
        self._current_model = self.resolve_model(model)
        self._skill_text = self._load_skill()
        self._history = []
        if not self.executable:
            print(
                f"Codex CLI runtime unavailable: {CODEX_COMMAND!r} not found on PATH",
                flush=True,
            )
            return
        skill_status = (
            f"skill={self._skill_path.name} ({len(self._skill_text)} chars)"
            if self._skill_text
            else "skill=NONE (Codex will operate without adaptive-markdown conventions)"
        )
        print(
            f"Codex CLI runtime ready (model={self._current_model}, "
            f"auth={CODEX_AUTH_MODE}, sandbox={CODEX_SANDBOX}, "
            f"approval={CODEX_APPROVAL_POLICY}, {skill_status})",
            flush=True,
        )

    def _load_skill(self) -> str | None:
        if not self._skill_path.exists():
            return None
        try:
            text = self._skill_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"warn: skill read failed at {self._skill_path}: {e}", flush=True)
            return None
        # Strip YAML frontmatter — metadata for the loader, not model instructions.
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end != -1:
                text = text[end + 5:]
        return text.strip() or None

    def _build_prompt(self, text: str) -> str:
        blocks = []
        if self._skill_text:
            blocks.append(
                "You are an adaptive-markdown editing agent operating on a "
                "Markdown document in this workspace. The skill below is your "
                "contract — follow it precisely when reading or editing.\n\n"
                "=== BEGIN SKILL: adaptive-markdown ===\n"
                f"{self._skill_text}\n"
                "=== END SKILL ==="
            )
        if self._history:
            history_body = "\n\n".join(
                f"[{turn['role']}]\n{turn['text']}" for turn in self._history
            )
            blocks.append(
                "Prior turns in this conversation (oldest first). Use them "
                "for context; do not re-do work already completed.\n\n"
                "=== BEGIN CONVERSATION HISTORY ===\n"
                f"{history_body}\n"
                "=== END HISTORY ==="
            )
        if not blocks:
            return text
        blocks.append(f"Current user request:\n{text}")
        return "\n\n".join(blocks)

    async def shutdown(self) -> None:
        return None

    async def reset(self, model: str | None = None) -> None:
        await self.start(model)

    async def run_turn(self, text: str) -> AsyncIterator[AgentEvent]:
        if not self.executable:
            yield {
                "type": "error",
                "text": (
                    "Codex runtime is selected, but the Codex CLI was not found. "
                    "Install Codex or set CODEX_COMMAND to the executable path."
                ),
            }
            return
        if _API_KEY_MODE and not os.environ.get("OPENAI_API_KEY"):
            yield {
                "type": "error",
                "text": (
                    "Codex API-key mode is selected, but OPENAI_API_KEY is not "
                    "visible to the backend process. Add it to .env or start "
                    "the backend from a shell where it is set."
                ),
            }
            return

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".txt",
            delete=False,
        ) as tmp:
            output_path = Path(tmp.name)

        cmd = self._build_command(output_path, self._current_model)
        prompt = self._build_prompt(text)

        emitted_text = False
        before_docs = self._doc_mtimes()
        # In-memory before-state of every .md file. Used by the post-turn
        # finalizer to derive .history/ snapshots + .patches/ from the same
        # before/after pairs the Claude pre/post hooks produce.
        before_state = self._read_md_snapshot()
        try:
            code, stderr, emitted_text, events = await self._run_codex_process(
                cmd,
                prompt,
                output_path,
                emitted_text,
            )

            final_text = self._read_output(output_path)
            # The unsupported-model error typically arrives in the JSONL event
            # stream (not stderr), so scan event payloads too. Without this,
            # the auto-retry never fires and the user sees the raw API error.
            event_error_text = "\n".join(
                str(e.get("text", "")) for e in events if e.get("type") == "error"
            )
            should_retry = (
                self._current_model != "default"
                and self._is_unsupported_model_error(stderr, final_text, event_error_text)
            )

            if should_retry:
                # Don't yield the first attempt's events — they would surface
                # the model-not-supported error to the chat even though we're
                # about to recover from it. Only the retry notice is shown.
                yield {
                    "role": "assistant",
                    "type": "text",
                    "text": (
                        f"_Model {self._current_model} is not supported by this "
                        "Codex account; retrying with Codex default._\n"
                    ),
                }
                output_path.write_text("", encoding="utf-8")
                fallback_cmd = self._build_command(output_path, "default")
                code, stderr, emitted_text, events = await self._run_codex_process(
                    fallback_cmd,
                    prompt,
                    output_path,
                    False,
                )
                for event in events:
                    yield event
                final_text = self._read_output(output_path)
                # Stick with the fallback for the rest of the session so we
                # don't pay the retry round-trip on every turn.
                if code == 0:
                    self._current_model = "default"
            else:
                for event in events:
                    yield event

            if final_text and not emitted_text:
                yield {"role": "assistant", "type": "text", "text": final_text}
            if code != 0:
                detail = stderr.strip() or f"codex exec exited with status {code}"
                yield {"type": "error", "text": detail}
            else:
                changed_paths = self._changed_paths(before_docs)
                if self.finalize_md_edit_fn:
                    author = f"agent:codex:{self._current_model}"
                    for path in changed_paths:
                        before_text = before_state.get(path.resolve())
                        try:
                            await self.finalize_md_edit_fn(
                                path, before_text, None, author,
                            )
                        except Exception as e:
                            print(
                                f"[codex] finalize_md_edit failed for {path}: {e}",
                                flush=True,
                            )
                else:
                    # No finalizer wired (legacy or unit tests) — fall back
                    # to bare doc_changed events so the viewer still updates.
                    for path in changed_paths:
                        try:
                            rel = path.relative_to(self.root).as_posix()
                        except ValueError:
                            continue
                        yield {"type": "doc_changed", "file": rel}
                # Record this turn for the next prompt. Only on success so
                # failed turns don't poison the history.
                self._history.append({"role": "user", "text": text})
                if final_text:
                    self._history.append({"role": "assistant", "text": final_text})
                yield {
                    "type": "turn_done",
                    "session_id": None,
                    "cost_usd": None,
                }
        finally:
            try:
                output_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _build_command(self, output_path: Path, model: str) -> list[str]:
        cmd = [
            self.executable,
            "--ask-for-approval",
            CODEX_APPROVAL_POLICY,
            "exec",
            "--json",
            "--cd",
            str(self.root),
            "--sandbox",
            CODEX_SANDBOX,
            "--output-last-message",
            str(output_path),
        ]
        if model != "default":
            cmd.extend(["--model", model])
        cmd.append("-")
        return cmd

    async def _run_codex_process(
        self,
        cmd: list[str],
        text: str,
        output_path: Path,
        emitted_text: bool,
    ) -> tuple[int, str, bool, list[AgentEvent]]:
        events: list[AgentEvent] = []
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.root),
            env=os.environ.copy(),
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None
        proc.stdin.write(text.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        stderr_task = asyncio.create_task(self._collect_stderr(proc.stderr))
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            event = self._jsonl_to_event(line)
            if event:
                emitted_text = emitted_text or (
                    event.get("role") == "assistant" and event.get("type") == "text"
                )
                events.append(event)

        code = await proc.wait()
        stderr = await stderr_task
        return code, stderr, emitted_text, events

    async def _collect_stderr(self, stream: asyncio.StreamReader) -> str:
        chunks = []
        async for raw_line in stream:
            chunks.append(raw_line.decode("utf-8", errors="replace"))
        return "".join(chunks)

    def _doc_mtimes(self) -> dict[Path, int]:
        docs: dict[Path, int] = {}
        for dirname in ("examples", "docs"):
            root = self.root / dirname
            if not root.exists():
                continue
            for path in root.glob("*.md"):
                try:
                    docs[path.resolve()] = path.stat().st_mtime_ns
                except OSError:
                    continue
        return docs

    def _read_md_snapshot(self) -> dict[Path, str]:
        """In-memory text of every .md under examples/ + docs/ keyed by
        absolute Path. Pretty much what the Claude pre-hook does, but for
        every file at once, before codex runs.

        A file that can't be decoded as UTF-8 (rare — usually a stray
        encoding artifact in a working copy) is skipped silently. The
        downstream finalize_md_edit_fn handles missing before-text by
        broadcasting doc_changed without snapshot/patch derivation.
        """
        snap: dict[Path, str] = {}
        for dirname in ("examples", "docs"):
            root = self.root / dirname
            if not root.exists():
                continue
            for path in root.glob("*.md"):
                try:
                    snap[path.resolve()] = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as e:
                    print(f"[codex] skip pre-snapshot of {path.name}: {e}",
                          flush=True)
                    continue
        return snap

    def _changed_paths(self, before: dict[Path, int]) -> list[Path]:
        """Absolute Paths of .md files whose mtime changed during the turn."""
        after = self._doc_mtimes()
        changed = [p for p, m in after.items() if before.get(p) != m]
        return sorted(changed)

    def _changed_docs(self, before: dict[Path, int]) -> list[str]:
        out = []
        for path in self._changed_paths(before):
            try:
                out.append(path.relative_to(self.root).as_posix())
            except ValueError:
                continue
        return out

    def _read_output(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    def _is_unsupported_model_error(
        self,
        stderr: str,
        final_text: str,
        event_errors: str = "",
    ) -> bool:
        haystack = f"{stderr}\n{final_text}\n{event_errors}".lower()
        return (
            "model is not supported" in haystack
            or ("requested model" in haystack and "not supported" in haystack)
        )

    def _jsonl_to_event(self, line: str) -> AgentEvent | None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return {"role": "assistant", "type": "thinking", "text": line}

        event_type = data.get("type") or ""
        if event_type == "error":
            return {"type": "error", "text": str(data.get("message") or data)}

        text = (
            data.get("text")
            or data.get("message")
            or data.get("delta")
            or data.get("content")
        )
        if text and event_type in {
            "assistant.message",
            "assistant.text",
            "message",
            "response.output_text.delta",
            "response.output_text.done",
        }:
            return {"role": "assistant", "type": "text", "text": str(text)}

        if event_type.endswith(".started"):
            # Codex marks tool/shell/patch calls with `.started` events. Emit
            # tool_use so the viewer's activity strip can show something
            # specific instead of staying on "Working...". Pull the best-
            # available identifier and pass the raw payload through as
            # `input` for `friendlyToolStatus` to mine.
            raw_name = data.get("name") or data.get("command")
            if not raw_name:
                # Derive from event_type, e.g. "function_call.started" -> "function_call"
                raw_name = event_type.rsplit(".started", 1)[0]
            return {
                "role": "assistant",
                "type": "tool_use",
                "name": str(raw_name),
                "input": data,
            }
        if event_type.endswith(".completed") or event_type.endswith(".done"):
            return None

        tool_name = data.get("name") or data.get("command")
        if tool_name:
            return {"role": "assistant", "type": "tool_use", "name": str(tool_name), "input": data}

        return None
