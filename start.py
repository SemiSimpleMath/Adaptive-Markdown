"""Adaptive Markdown launcher.

Pick a provider once, then plain `python start.py` remembers it.

    python start.py --claude        # use Claude Agent SDK (default on first run)
    python start.py --codex         # use Codex CLI
    python start.py                 # use whatever was last selected
    python start.py --port 9000     # override port

Precedence for picking the provider, highest first:
    1. --claude / --codex CLI flag (also persists for next run)
    2. AGENT_PROVIDER shell environment variable
    3. .am-provider file from a previous --claude/--codex run
    4. Default: claude
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path


def _ensure_utf8_mode() -> None:
    """Ensure Python is running with UTF-8 mode active.

    Python's I/O encoding is fixed at interpreter startup based on the
    environment AT THAT MOMENT. Setting `PYTHONUTF8=1` inside the program
    is too late — it only affects child processes, not this one.

    On Windows, the default locale encoding (cp1252) causes the Claude
    Agent SDK's subprocess pipes to choke on characters like `⟹` / `→` /
    `π` flowing through the agent context. The reliable fix is to detect
    we're not in UTF-8 mode and re-exec Python with `PYTHONUTF8=1` set.

    The first call to start.py respawns once; the relaunched interpreter
    has UTF-8 enabled everywhere (sys.stdin/out/err, locale.getpreferred-
    encoding(), and crucially the subprocess pipe defaults).
    """
    if sys.platform != "win32":
        return  # Linux/macOS terminals are already UTF-8

    already_utf8 = (
        getattr(sys.flags, "utf8_mode", 0) == 1
        or os.environ.get("PYTHONUTF8") == "1"
        or os.environ.get("AM_UTF8_RESPAWNED") == "1"
    )
    if already_utf8:
        # Belt-and-suspenders: also set the Windows console to UTF-8 so
        # subprocesses we spawn (the Claude Code CLI under Bun, the Codex
        # CLI) inherit a UTF-8 console too.
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            os.system("chcp 65001 > nul 2>&1")
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
        return

    # Not in UTF-8 mode — respawn with the right env. Loop guard via
    # AM_UTF8_RESPAWNED prevents infinite restart if PYTHONUTF8 somehow
    # doesn't stick.
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["AM_UTF8_RESPAWNED"] = "1"
    rc = subprocess.call([sys.executable] + sys.argv, env=env)
    sys.exit(rc)


_ensure_utf8_mode()


def _install_sigint_watchdog(grace_seconds: float = 3.0) -> None:
    """Ctrl+C on Windows can block in aiohttp's task cancellation while the
    IOCP event loop waits for pending overlapped reads (open WebSocket, idle
    HTTP keep-alive). Give the normal cleanup `grace_seconds` to finish;
    after that, force-exit so the user isn't stuck staring at a hung shell.

    The signal handler still raises KeyboardInterrupt so the loop's cleanup
    path runs (and main()'s existing `os._exit(0)` fires when it returns).
    The thread-based timer is a safety net for the case where cleanup
    itself hangs — main() never gets a chance to call `os._exit(0)`."""
    def handler(signum, frame):
        sys.stderr.write(
            f"\n[shutdown] Ctrl+C received; force-exit in {grace_seconds}s "
            f"if cleanup hangs (Windows IOCP quirk).\n"
        )
        sys.stderr.flush()
        timer = threading.Timer(grace_seconds, lambda: os._exit(130))
        timer.daemon = True
        timer.start()
        raise KeyboardInterrupt()
    signal.signal(signal.SIGINT, handler)


ROOT = Path(__file__).resolve().parent
PROVIDER_FILE = ROOT / ".am-provider"
VALID_PROVIDERS = ("claude", "codex")


def _read_persisted_provider() -> str | None:
    if not PROVIDER_FILE.exists():
        return None
    try:
        value = PROVIDER_FILE.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    if value not in VALID_PROVIDERS:
        print(
            f"[start] ignoring corrupt {PROVIDER_FILE.name} (got {value!r})",
            flush=True,
        )
        return None
    return value


def _persist_provider(name: str) -> None:
    try:
        PROVIDER_FILE.write_text(name + "\n", encoding="utf-8")
    except OSError as e:
        print(f"[start] warning: could not persist provider choice: {e}",
              flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Adaptive Markdown launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--claude", dest="provider", action="store_const", const="claude",
        help="use the Claude Agent SDK runtime (also persists for next run)",
    )
    group.add_argument(
        "--codex", dest="provider", action="store_const", const="codex",
        help="use the Codex CLI runtime (also persists for next run)",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="override backend port (default: PORT env or 8090)",
    )
    parser.add_argument(
        "--unsafe-bash", action="store_true",
        help=(
            "[WINDOWS ONLY] disable the subprocess sandbox around the "
            "agent's Bash tool. The agent's Bash will run with your full "
            "env (incl. API keys), unrestricted filesystem access, and "
            "the open internet. Use only when you trust every doc in the "
            "workspace — a prompt-injected doc can exfiltrate or wreck "
            "things in this mode. No effect on macOS/Linux/WSL2, where "
            "the CLI's built-in sandbox is always on."
        ),
    )
    args = parser.parse_args()

    if args.provider:
        _persist_provider(args.provider)
        os.environ["AGENT_PROVIDER"] = args.provider
        source = f"flag, saved to {PROVIDER_FILE.name}"
    elif os.environ.get("AGENT_PROVIDER"):
        source = "AGENT_PROVIDER env var"
    else:
        persisted = _read_persisted_provider()
        if persisted:
            os.environ["AGENT_PROVIDER"] = persisted
            source = f"{PROVIDER_FILE.name}"
        else:
            source = "default"

    if args.port is not None:
        os.environ["PORT"] = str(args.port)

    if args.unsafe_bash:
        os.environ["AM_UNSAFE_BASH"] = "1"

    _install_sigint_watchdog(3.0)

    # Import backend AFTER setting env vars — DEFAULT_PROVIDER is captured at
    # agent_runtime import time, which happens during backend import.
    from aiohttp import web
    import backend  # noqa: E402 — order is load-bearing

    chosen = os.environ.get("AGENT_PROVIDER", "claude").lower()
    port = int(os.environ.get("PORT", "8090"))
    print(f"[start] provider={chosen} ({source})", flush=True)
    print(f"Adaptive Markdown listening on http://127.0.0.1:{port}", flush=True)
    if args.unsafe_bash and sys.platform.startswith("win"):
        # Out-of-band warning so it's impossible to miss in the launch
        # output. Agent Bash will run with full env / network / FS.
        print(
            "\n"
            "  +----------------------------------------------------------+\n"
            "  |  UNSAFE-BASH MODE ENABLED                                |\n"
            "  |                                                          |\n"
            "  |  Agent Bash runs WITHOUT sandbox. A prompt-injected doc  |\n"
            "  |  can exfiltrate files, env vars (incl. API keys), and    |\n"
            "  |  reach the open internet from your machine.              |\n"
            "  |                                                          |\n"
            "  |  Disable: restart without --unsafe-bash                  |\n"
            "  +----------------------------------------------------------+\n",
            flush=True,
        )
    elif args.unsafe_bash:
        print(
            "[start] --unsafe-bash is a no-op on this platform (the CLI "
            "sandbox is already in effect).",
            flush=True,
        )
    try:
        web.run_app(backend.make_app(), host="127.0.0.1", port=port, print=None)
    except KeyboardInterrupt:
        # Suppress the traceback if a second Ctrl+C arrives during cleanup —
        # aiohttp's `_cancel_tasks` re-enters the event loop, and a SIGINT
        # mid-IOCP-poll otherwise dumps a wall of Windows asyncio internals.
        print("[start] interrupted; shutting down", flush=True)
        # Hard-exit instead of falling off the end of main(). The Claude
        # Agent SDK can leave non-daemon background threads (or pending
        # subprocess waiters) alive after __aexit__ returns, and Python's
        # interpreter blocks at process-exit waiting for them to die.
        # Result: the user sees the "interrupted; shutting down" line but
        # the prompt never returns. os._exit bypasses that wait and exits
        # immediately. We've already gotten past aiohttp's cleanup (which
        # closed WS connections + shut down the runtime), so there's no
        # owned resource left to flush. Codex's runtime doesn't hit this
        # because each turn was a child subprocess that already exited.
        os._exit(0)


if __name__ == "__main__":
    main()
