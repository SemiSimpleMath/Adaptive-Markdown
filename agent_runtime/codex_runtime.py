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

    def __init__(self, root: Path, pre_edit_hook=None, post_edit_hook=None) -> None:
        self.root = root
        self.pre_edit_hook = pre_edit_hook
        self.post_edit_hook = post_edit_hook
        self.executable: str | None = None
        self._current_model = DEFAULT_MODEL

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
        if not self.executable:
            print(
                f"Codex CLI runtime unavailable: {CODEX_COMMAND!r} not found on PATH",
                flush=True,
            )
            return
        print(
            f"Codex CLI runtime ready (model={self._current_model}, "
            f"auth={CODEX_AUTH_MODE}, sandbox={CODEX_SANDBOX}, "
            f"approval={CODEX_APPROVAL_POLICY})",
            flush=True,
        )

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

        emitted_text = False
        before_docs = self._doc_mtimes()
        try:
            code, stderr, emitted_text, events = await self._run_codex_process(
                cmd,
                text,
                output_path,
                emitted_text,
            )
            for event in events:
                yield event

            final_text = self._read_output(output_path)
            if self._is_unsupported_model_error(stderr, final_text) and self._current_model != "default":
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
                    text,
                    output_path,
                    False,
                )
                for event in events:
                    yield event
                final_text = self._read_output(output_path)

            if final_text and not emitted_text:
                yield {"role": "assistant", "type": "text", "text": final_text}
            if code != 0:
                detail = stderr.strip() or f"codex exec exited with status {code}"
                yield {"type": "error", "text": detail}
            else:
                for rel in self._changed_docs(before_docs):
                    yield {"type": "doc_changed", "file": rel}
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

    def _changed_docs(self, before: dict[Path, int]) -> list[str]:
        after = self._doc_mtimes()
        changed = []
        for path, mtime in after.items():
            if before.get(path) == mtime:
                continue
            try:
                changed.append(path.relative_to(self.root).as_posix())
            except ValueError:
                continue
        return sorted(changed)

    def _read_output(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    def _is_unsupported_model_error(self, stderr: str, final_text: str) -> bool:
        haystack = f"{stderr}\n{final_text}".lower()
        return "model is not supported" in haystack or "requested model" in haystack and "not supported" in haystack

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
            label = event_type.replace(".", " ")
            return {"role": "assistant", "type": "thinking", "text": f"{label}..."}
        if event_type.endswith(".completed") or event_type.endswith(".done"):
            return None

        tool_name = data.get("name") or data.get("command")
        if tool_name:
            return {"role": "assistant", "type": "tool_use", "name": str(tool_name), "input": data}

        return None
