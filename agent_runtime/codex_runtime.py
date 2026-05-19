"""Codex CLI runtime adapter."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import AsyncIterator

import validators

from .base import AgentEvent


# History cap: how many {user, assistant} pairs to keep when replaying
# conversation context into each turn's prompt. Each pair averages a few
# KB; at ~21 KB skill + N pairs * ~5 KB, ten pairs keeps prompts under
# ~75 KB which is comfortable. Configurable via env for stress tests.
HISTORY_MAX_PAIRS = int(os.environ.get("CODEX_HISTORY_TURNS", "10"))


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
# Disable network egress from inside the workspace-write sandbox. Codex CLI's
# Linux/Mac sandboxes (Landlock + Seccomp) honor this; on Windows the OS-level
# network isolation is weaker, so a hostile prompt could still reach the
# network via Windows APIs the sandbox doesn't intercept. This is the
# strongest defense Codex offers — the rest is policy (see SKILL.md security
# section + path-validated pre-edit hook in backend.py). Set the env var to
# "true" to re-enable network access if a workflow genuinely needs it.
CODEX_WORKSPACE_NETWORK = os.environ.get("CODEX_WORKSPACE_NETWORK", "false").lower() in (
    "1", "true", "yes",
)


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
        pre_bash_hook=None,
    ) -> None:
        # Codex doesn't (yet) expose Bash to the agent through this adapter,
        # so the pre_bash_hook is accepted for signature parity and ignored.
        _ = pre_bash_hook
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
            # Cap replayed history at HISTORY_MAX_PAIRS so the prompt prefix
            # doesn't grow unboundedly over a long session. We pair-walk
            # backwards from the end so we always keep the most recent
            # {user, assistant} pairs intact, even if the list happens to
            # be unbalanced (e.g. trailing user msg with no reply yet).
            keep = self._history
            truncated = False
            n_pairs = HISTORY_MAX_PAIRS
            if len(self._history) > n_pairs * 2:
                keep = self._history[-(n_pairs * 2):]
                truncated = True
            history_body = "\n\n".join(
                f"[{turn['role']}]\n{turn['text']}" for turn in keep
            )
            if truncated:
                history_body = (
                    "[earlier conversation truncated — context preserved for "
                    f"the last {n_pairs} turn(s) only]\n\n" + history_body
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
        # Bytes of every protected file (everything outside the agent's
        # allowed write scope). The post-turn revert validator compares
        # against this to detect unauthorized writes — see
        # _revert_unauthorized_writes for the why and the scope.
        before_protected = self._protected_snapshot()
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
                final_text = self._read_output(output_path)
                if code == 0:
                    # Stick with the fallback for the rest of the session so
                    # we don't pay the retry round-trip on every turn.
                    self._current_model = "default"

            # _jsonl_to_event surfaces turn.completed as its own turn_done so
            # token usage propagates upward. Don't yield it here — we emit a
            # single terminal turn_done below, merging usage if present.
            captured_usage = None
            for event in events:
                if event.get("type") == "turn_done":
                    if event.get("usage") is not None:
                        captured_usage = event["usage"]
                    continue
                yield event

            if final_text and not emitted_text:
                yield {"role": "assistant", "type": "text", "text": final_text}
            if code != 0:
                detail = stderr.strip() or f"codex exec exited with status {code}"
                yield {"type": "error", "text": detail}
            else:
                # Revert any unauthorized writes BEFORE the finalize step.
                # finalize_md_edit_fn writes new snapshots to .history/ which
                # would otherwise show as "protected files modified" relative
                # to before_protected. Doing the revert first means finalize
                # operates on a clean post-codex state and its own writes
                # don't trip the validator.
                reverted = self._revert_unauthorized_writes(before_protected)
                if reverted:
                    listing = "\n".join(f"- `{r}`" for r in reverted)
                    yield {
                        "type": "error",
                        "text": (
                            "⚠️ Codex attempted to write outside the allowed "
                            "scope. The following changes were reverted:\n"
                            f"{listing}\n\n"
                            "Only `examples/*.md` and `docs/*.md` may be "
                            "modified by the agent. This usually means a "
                            "prompt injection from document content; review "
                            "the source you opened and consider clearing the "
                            "current chat."
                        ),
                    }
                # Content validators (JS / CSS / SVG / directives) on each
                # changed .md. On failure: revert to before_state and yield
                # an error so the user can re-prompt. Codex can't feed the
                # errors back into its own tool-use loop the way Claude does
                # via PostToolUse `additionalContext` — see ROADMAP
                # "Validator retry-loop parity (deferred — has trigger)".
                raw_changed_paths = self._changed_paths(before_docs)
                changed_paths: list[Path] = []
                for path in raw_changed_paths:
                    try:
                        new_text = (
                            path.read_text(encoding="utf-8")
                            if path.exists() else ""
                        )
                    except (OSError, UnicodeDecodeError):
                        new_text = ""
                    errors = (
                        validators.validate_doc(new_text)
                        if new_text.strip() else []
                    )
                    if not errors:
                        changed_paths.append(path)
                        continue
                    before_text = before_state.get(path.resolve())
                    try:
                        if before_text:
                            with path.open(
                                "w", encoding="utf-8", newline="",
                            ) as f:
                                f.write(before_text)
                            action = "reverted"
                        else:
                            path.unlink(missing_ok=True)
                            action = "deleted new file"
                        print(
                            f"[codex-validate] FAILED on {path}, "
                            f"{action} ({len(errors)} err)",
                            flush=True,
                        )
                        for ve in errors:
                            print(
                                f"[codex-validate]   {ve['kind']} @ line "
                                f"{ve['line']}:",
                                flush=True,
                            )
                            for ln in ve["message"].split("\n"):
                                print(
                                    f"[codex-validate]     {ln}",
                                    flush=True,
                                )
                    except OSError as e:
                        print(
                            f"[codex-validate] revert failed: {e}",
                            flush=True,
                        )
                    try:
                        rel = path.resolve().relative_to(
                            self.root,
                        ).as_posix()
                    except ValueError:
                        rel = str(path)
                    yield {
                        "type": "error",
                        "text": validators.format_for_agent(errors, rel),
                    }
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
                terminal: AgentEvent = {
                    "type": "turn_done",
                    "session_id": None,
                    "cost_usd": None,
                }
                if captured_usage is not None:
                    terminal["usage"] = captured_usage
                yield terminal
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
            # Override config.toml to keep network egress off inside the
            # sandbox by default. Defense-in-depth: a hostile doc that
            # successfully prompt-injects shouldn't be able to phone home.
            "-c",
            f"sandbox_workspace_write.network_access={'true' if CODEX_WORKSPACE_NETWORK else 'false'}",
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
        stderr_task: asyncio.Task | None = None
        try:
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
        finally:
            # If we exit via cancellation (reader hit /cancel) or any other
            # exception, the codex subprocess may still be running. Without
            # this kill the process keeps consuming the user's agent quota
            # and writing to the workspace after we've stopped paying
            # attention to its output. Codex parity with Claude's /cancel,
            # which closes the SDK client cleanly via the SDK's own
            # cancellation semantics.
            if proc.returncode is None:
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    await proc.wait()
                except BaseException:
                    pass
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
                try:
                    await stderr_task
                except BaseException:
                    pass

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

    # ---- Post-turn write-path validator ----------------------------------
    # Codex CLI's workspace-write sandbox makes the entire project root
    # writable, and its hook system doesn't fire on file edits (only on Bash
    # commands). So we can't gate Codex writes at edit time the way the
    # Claude pre-edit hook does. Instead: snapshot every protected file
    # before the turn, compare after, and revert anything that changed
    # outside the allowed scope. The bytes briefly hit disk during the turn,
    # but they're undone before the next turn — and before the backend's
    # finalize step runs, so legitimate history-snapshot writes don't get
    # tangled in the revert.

    # Directories not worth fingerprinting (caches, VCS metadata, virtualenvs).
    # The agent shouldn't write here; if it does, partial cache reverts would
    # cause more noise than they prevent. .git in particular is dangerous to
    # rewrite piecemeal.
    _PROTECTED_WALK_SKIP_DIRS = frozenset({
        ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    })
    # Skip files larger than this when building the protected snapshot —
    # binaries that big are almost certainly not the kind of thing a prompt
    # injection plausibly wants to rewrite, and snapshotting them on every
    # turn is expensive. The 1 MB cap also matches the upload-size limit.
    _PROTECTED_FILE_SIZE_CAP = 1 * 1024 * 1024

    def _is_writable_path(self, path: Path) -> bool:
        """True if `path` is a legitimate write target. The agent may write
        to `examples/<name>.md` and `docs/<name>.md`; the backend's upload
        handler writes to `docs/raw/` in response to user drops. Everything
        else under the project root is protected."""
        try:
            rel = path.resolve().relative_to(self.root)
        except (ValueError, OSError):
            return False
        parts = rel.parts
        if not parts:
            return False
        if (parts[0] in ("examples", "docs")
                and len(parts) == 2
                and parts[1].lower().endswith(".md")):
            return True
        # docs/raw/ is the user-upload landing zone; the backend (not the
        # agent) writes here, and an upload can race a turn-in-progress.
        # Treat as outside the validator's revert scope.
        if (parts[0] == "docs" and len(parts) >= 2 and parts[1] == "raw"):
            return True
        return False

    def _walk_protected_files(self):
        """Yield Path objects for every file in the project tree worth
        validating. Prunes noise dirs and size-capped files."""
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [
                d for d in dirnames if d not in self._PROTECTED_WALK_SKIP_DIRS
            ]
            for fn in filenames:
                full = Path(dirpath) / fn
                try:
                    if full.stat().st_size > self._PROTECTED_FILE_SIZE_CAP:
                        continue
                except OSError:
                    continue
                yield full

    def _protected_snapshot(self) -> dict[Path, bytes]:
        """Map absolute Path -> bytes for every protected file. Run before
        each turn; compared against the same walk after the turn."""
        snap: dict[Path, bytes] = {}
        for p in self._walk_protected_files():
            if self._is_writable_path(p):
                continue
            try:
                snap[p.resolve()] = p.read_bytes()
            except OSError:
                pass
        return snap

    def _safe_rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def _revert_unauthorized_writes(self, before: dict[Path, bytes]) -> list[str]:
        """Detect and revert changes to protected files. Modified or
        deleted files are restored from `before`. New files in protected
        scope are deleted. Returns relative paths of every change reverted
        — suitable for surfacing in chat as a security alert."""
        reverted: list[str] = []
        # Files that existed before — restore them if modified or deleted.
        for path, before_bytes in before.items():
            try:
                current = path.read_bytes() if path.exists() else None
            except OSError:
                continue
            if current == before_bytes:
                continue
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(before_bytes)
                rel = self._safe_rel(path)
                reverted.append(rel)
                print(f"[codex-revert] reverted {rel}", flush=True)
            except OSError as e:
                print(f"[codex-revert] failed to revert {path}: {e}", flush=True)
        # New files appearing in protected scope — delete them. (Note: a user
        # drop landing in docs/raw/ during a turn is in the writable-path
        # set, so we won't touch those — see _is_writable_path.)
        before_paths = set(before.keys())
        for p in self._walk_protected_files():
            if self._is_writable_path(p):
                continue
            resolved = p.resolve()
            if resolved in before_paths:
                continue
            try:
                p.unlink()
                rel = self._safe_rel(p)
                reverted.append(f"{rel} (new file deleted)")
                print(f"[codex-revert] deleted new {rel}", flush=True)
            except OSError as e:
                print(f"[codex-revert] failed to delete {p}: {e}", flush=True)
        return reverted

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
        """Translate one line of `codex exec --json` stdout into a viewer event.

        Schema (per OpenAI Codex CLI docs):
        - thread.started / turn.started: bookkeeping, dropped.
        - turn.completed: carries a `usage` block with token counts; we
          surface it as turn_done so the backend can merge token usage into
          its own terminal turn_done.
        - turn.failed: dropped (could become a visible error in future).
        - error: top-level error message, surfaced as-is.
        - item.started: a unit of work begins. The real payload is at
          `item` with `type` in {command_execution, agent_message,
          reasoning, file_change, mcp_tool_call, web_search, plan,
          plan_update}. We surface most as tool_use; reasoning is dropped
          as noise.
        - item.updated: streaming deltas — not yet surfaced.
        - item.completed: only useful for `agent_message` (final assistant
          text). Other completions are no-ops because the started event
          already showed the tool was invoked.
        """
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None

        event_type = data.get("type") or ""
        if not event_type:
            return None

        if event_type == "error":
            return {"type": "error", "text": str(data.get("message") or data)}

        if event_type in ("thread.started", "turn.started", "turn.failed"):
            return None

        if event_type == "turn.completed":
            out: AgentEvent = {"type": "turn_done"}
            usage = data.get("usage")
            if usage is not None:
                out["usage"] = usage
            return out

        item = data.get("item") or {}
        item_type = item.get("type")

        if event_type == "item.started":
            if not item:
                return None
            if item_type == "reasoning":
                return None
            return {
                "role": "assistant",
                "type": "tool_use",
                "name": str(item_type or "unknown"),
                "input": item,
            }

        if event_type == "item.completed":
            if item_type == "agent_message":
                text = item.get("text") or ""
                if text:
                    return {"role": "assistant", "type": "text", "text": str(text)}
            return None

        if event_type == "item.updated":
            return None

        return None
