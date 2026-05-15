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
import sys
from pathlib import Path


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

    # Import backend AFTER setting env vars — DEFAULT_PROVIDER is captured at
    # agent_runtime import time, which happens during backend import.
    from aiohttp import web
    import backend  # noqa: E402 — order is load-bearing

    chosen = os.environ.get("AGENT_PROVIDER", "claude").lower()
    port = int(os.environ.get("PORT", "8090"))
    print(f"[start] provider={chosen} ({source})", flush=True)
    print(f"Adaptive Markdown listening on http://127.0.0.1:{port}", flush=True)
    web.run_app(backend.make_app(), host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    main()
