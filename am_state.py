"""Backend-wide singleton state.

The `state` global is read and mutated from many places: the WS handler,
HTTP routes, agent hooks, upload paths. Lifted into its own module so
those callers can import it without going through `backend`, which lets
us split backend.py into smaller pieces without circular imports.

The runtime reference is None until init_runtime() runs at server boot;
the broadcast() helper handles the per-client send timeout that
prevents an orphaned WS from blocking new handshakes.
"""
from __future__ import annotations

import asyncio
import json

from aiohttp import web

from agent_runtime import AgentRuntime, DEFAULT_PROVIDER


class State:
    def __init__(self):
        self.clients: set[web.WebSocketResponse] = set()
        self.client_lock = asyncio.Lock()
        self.runtime: AgentRuntime | None = None
        self.busy = asyncio.Lock()
        self.current_provider: str = DEFAULT_PROVIDER
        self.current_model: str = ""
        # Reference to the in-flight `run_turn` task so /cancel can
        # interrupt it. Cleared when the turn finishes (either way).
        self.current_turn_task: asyncio.Task | None = None
        # Slug of the doc this turn is acting on, stashed before run_turn
        # so the PreToolUse Bash hook can pin the sandbox cwd to the
        # right doc folder. Cleared when the turn ends.
        self.current_doc_slug: str | None = None

    async def broadcast(self, msg: dict) -> None:
        text = json.dumps(msg)
        dead = []
        async with self.client_lock:
            for ws in self.clients:
                if ws.closed:
                    dead.append(ws)
                    continue
                try:
                    # 2s per-client timeout: an orphaned WS whose browser
                    # navigated away can keep the TCP send buffer full
                    # until the heartbeat (30s) detects it. Holding the
                    # client_lock that long blocks every new WS handshake
                    # downstream. Treat slow sends as dead and move on.
                    await asyncio.wait_for(ws.send_str(text), timeout=2.0)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)


state = State()
