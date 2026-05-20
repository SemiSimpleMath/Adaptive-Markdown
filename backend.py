"""Adaptive Markdown backend: aiohttp WS server.

Filesystem layout (doc-as-folder):
  docs/<slug>/baseline.md   — immutable history-0 (tracked in git for examples)
  docs/<slug>/current.md    — working copy the agent edits (gitignored)
  docs/<slug>/snaps/        — pre-edit snapshots (gitignored)
  docs/<slug>/original.<ext> — optional provenance, e.g. the .tex source

Routes:
  GET  /                       -> index.html
  GET  /docs/<slug>/current.md -> static doc fetch (also baseline.md)
  WS   /ws                     -> chat protocol

WS protocol — `doc` fields are SLUGS (e.g. "intro"), not paths:
  server -> client:
    {type:"ready", doc:"intro", docs:["intro","paper","textbook"]}
    {type:"doc_changed", doc:"intro"}
    {role:"user", type:"text", text:"..."}
    {role:"assistant", type:"text"|"thinking", text:"..."}
    {role:"assistant", type:"tool_use", name:"...", input:{...}}
    {type:"turn_done", session_id:"...", cost_usd:0.01}
    {type:"chat_reset"}
    {type:"error", text:"..."}

  client -> server:
    {type:"chat", text:"...", context:{doc:"<slug>", selections:[...]}}
    {type:"new_chat"}
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from aiohttp import web


ROOT = Path(__file__).resolve().parent
DOCS_ROOT = ROOT / "docs"            # all docs live under docs/<slug>/
# Pre-tool-use writes are restricted to `docs/<slug>/current.md`; baseline.md
# is the immutable history-0 and stays untouchable by the agent.
_DOC_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$", re.IGNORECASE)


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE pairs from .env without overriding shell env."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value


load_dotenv(ROOT / ".env")

# This file is the aiohttp wiring: static routes, doc-folder bootstrap,
# make_app(), startup/shutdown. The real work lives in the am_* modules.
# Tests / external callers should import their specific symbols from the
# canonical module (e.g. `from am_pending import save_pending`); don't
# add re-exports here.
from am_docs import (  # noqa: E402
    ensure_doc_ids, ensure_working_copies,
)
from am_hooks import init_runtime, shutdown_runtime  # noqa: E402
from am_origin import _check_static_origin  # noqa: E402
from am_routes import (  # noqa: E402
    list_history, undo_doc, restore_snapshot, edit_block,
    list_pending, accept_pending, reject_pending,
    list_doc_skills, update_doc_skill, delete_doc_skill,
    add_doc_skill, reset_doc, save_baseline, set_api_key,
)
from am_state import state  # noqa: E402
from am_upload import (  # noqa: E402
    upload_md, upload_asset, _ASSET_EXTS, _ASSET_BLOCKED_EXTS,
)
from am_ws import ws_handler  # noqa: E402


async def serve_index(request: web.Request) -> web.Response:
    return web.FileResponse(ROOT / "index.html")


async def serve_iframe_host(request: web.Request) -> web.Response:
    """The doc iframe is loaded from a DIFFERENT origin than the viewer
    (in dev: http://localhost:<port>/iframe-host vs the viewer at
    http://127.0.0.1:<port>/). Same backend, same port — the browser's
    same-origin policy treats them as separate origins because the
    hostname strings differ. That cross-origin gap is what lets the iframe
    use `allow-same-origin` (for its own cookies / Cache Storage / nested
    YouTube embeds) while still being firewalled from the viewer chrome's
    storage and DOM."""
    return web.FileResponse(ROOT / "iframe-host.html")


# Files outside this allowlist are NOT served — even if they exist under ROOT.
# Without this gate the catch-all route exposed .env, .history/, sidecar JSON,
# and every internal file in the project tree. Restrict to what the viewer
# (and only the viewer) actually fetches from its parent context.
_STATIC_ROOT_FILES = {"favicon.svg"}


async def serve_static(request: web.Request) -> web.Response:
    _check_static_origin(request)
    rel = request.match_info["path"]
    # Reject `..`, backslashes, leading slashes, empty segments.
    if (not rel or rel.startswith("/") or ".." in rel.replace("\\", "/").split("/")
            or "\\" in rel):
        raise web.HTTPNotFound()
    target = (ROOT / rel).resolve()
    try:
        target.relative_to(ROOT)
    except ValueError:
        raise web.HTTPNotFound()
    if not target.exists() or not target.is_file():
        raise web.HTTPNotFound()
    # Allowlist: a small set of root files, or docs/<slug>/current.md or
    # docs/<slug>/baseline.md. snaps/ is NOT served — it's local backend
    # state, not viewer-facing.
    if target.parent == ROOT:
        if target.name not in _STATIC_ROOT_FILES:
            raise web.HTTPNotFound()
    else:
        try:
            doc_rel = target.relative_to(DOCS_ROOT)
        except ValueError:
            raise web.HTTPNotFound()
        parts = doc_rel.parts
        # Allowed shapes under docs/<slug>/:
        #   - current.md or baseline.md (the doc files the viewer loads)
        #   - assets/<file>             (binary materials the doc embeds)
        # Everything else (snaps/, patches/, original.<ext>) is backend
        # state and must not be served to viewer-side iframes.
        ok = False
        if len(parts) == 2 and _DOC_SLUG_RE.match(parts[0]) \
                and parts[1] in ("current.md", "baseline.md"):
            ok = True
        elif len(parts) == 3 and _DOC_SLUG_RE.match(parts[0]) \
                and parts[1] == "assets":
            # Asset filename must have a recognized extension (defense in
            # depth — agents and users shouldn't be able to coax the server
            # into serving arbitrary file types).
            asset_ext = Path(parts[2]).suffix.lower()
            if (asset_ext in _ASSET_EXTS
                    and asset_ext not in _ASSET_BLOCKED_EXTS):
                ok = True
        if not ok:
            raise web.HTTPNotFound()
    return web.FileResponse(target)


def ensure_history_zero() -> None:
    """For every docs/<slug>/current.md without a sibling baseline.md, snap
    the current content AS the baseline. This handles user-created docs
    that haven't been formally given a history-0 — the Reset button always
    has something to restore to."""
    if not DOCS_ROOT.exists():
        return
    minted = 0
    for sub in sorted(DOCS_ROOT.iterdir()):
        if not sub.is_dir() or not _DOC_SLUG_RE.match(sub.name):
            continue
        current = sub / "current.md"
        baseline = sub / "baseline.md"
        if baseline.exists() or not current.exists():
            continue
        try:
            baseline.write_bytes(current.read_bytes())
        except OSError as e:
            print(f"[history] cannot mint baseline for {sub.name}: {e}",
                  flush=True)
            continue
        print(f"[history] minted baseline.md for {sub.name} from current.md",
              flush=True)
        minted += 1
    if minted:
        print(f"ensured baseline ({minted} new)", flush=True)


def ensure_skill_mirror() -> None:
    """Keep the Codex-discoverable copy of SKILL.md byte-identical to the
    Claude-discoverable one. The two paths exist because each runtime has its
    own skill-loading convention (Claude SDK reads `.claude/skills/...`;
    Codex adapter reads `.agents/skills/...`). Editing one and forgetting the
    other is the obvious failure mode — and it's a security failure when the
    drift is on the security-boundaries section. Sync defensively at startup."""
    src = ROOT / ".claude" / "skills" / "adaptive-markdown" / "SKILL.md"
    dst = ROOT / ".agents" / "skills" / "adaptive-markdown" / "SKILL.md"
    if not src.exists():
        return
    try:
        src_bytes = src.read_bytes()
    except OSError as e:
        print(f"[skill-mirror] can't read source {src}: {e}", flush=True)
        return
    if dst.exists():
        try:
            if dst.read_bytes() == src_bytes:
                return  # already in sync
        except OSError:
            pass
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        dst.write_bytes(src_bytes)
        print(f"[skill-mirror] synced {dst.relative_to(ROOT).as_posix()}",
              flush=True)
    except OSError as e:
        print(f"[skill-mirror] write failed for {dst}: {e}", flush=True)


async def on_startup(app: web.Application):
    ensure_working_copies()  # baseline.md -> current.md on fresh clones
    ensure_doc_ids()
    ensure_history_zero()    # current.md -> baseline.md for new user docs
    ensure_skill_mirror()
    await init_runtime()


async def on_cleanup(app: web.Application):
    # Close all WebSocket connections first so aiohttp's task cancellation
    # doesn't have to wait for IOCP reads to time out. On Windows the
    # ProactorEventLoop blocks in GetQueuedCompletionStatus while there are
    # pending overlapped I/O operations, which is what makes Ctrl+C feel
    # unreliable when browser tabs are open.
    async with state.client_lock:
        for ws in list(state.clients):
            try:
                await ws.close(code=1001, message=b"server shutting down")
            except Exception:
                pass
        state.clients.clear()
    await shutdown_runtime()


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", serve_index)
    app.router.add_get("/iframe-host", serve_iframe_host)
    app.router.add_get("/ws", ws_handler)
    app.router.add_post("/upload", upload_md)
    app.router.add_post("/upload-asset", upload_asset)
    app.router.add_get("/history", list_history)
    app.router.add_post("/undo", undo_doc)
    app.router.add_post("/restore_snapshot", restore_snapshot)
    app.router.add_post("/reset", reset_doc)
    app.router.add_post("/save-baseline", save_baseline)
    app.router.add_post("/api-key", set_api_key)
    app.router.add_post("/edit-block", edit_block)
    app.router.add_post("/add-doc-skill", add_doc_skill)
    app.router.add_get("/doc-skills", list_doc_skills)
    app.router.add_post("/update-doc-skill", update_doc_skill)
    app.router.add_post("/delete-doc-skill", delete_doc_skill)
    app.router.add_get("/pending", list_pending)
    app.router.add_post("/accept-pending", accept_pending)
    app.router.add_post("/reject-pending", reject_pending)
    app.router.add_get("/{path:.+}", serve_static)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    # Port: CLI arg wins, else PORT env var, else 8090.
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    else:
        port = int(os.environ.get("PORT", "8090"))
    print(f"Adaptive Markdown listening on http://127.0.0.1:{port}", flush=True)
    web.run_app(make_app(), host="127.0.0.1", port=port, print=None)
