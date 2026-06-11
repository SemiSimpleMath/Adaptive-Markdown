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

import asyncio
import os
import sys
from pathlib import Path

from aiohttp import web


ROOT = Path(__file__).resolve().parent
# DATA_ROOT defaults to ROOT, but AM_DATA_DIR overrides it (a desktop
# shell would use this to route user content under a per-user data
# dir when the install dir is read-only). We compute it locally here BEFORE importing
# am_docs so .env loading can use it; am_docs computes the same value
# (idempotent — env var read twice, same answer) and is the canonical
# source for downstream importers.
_data_env = os.environ.get("AM_DATA_DIR", "").strip()
DATA_ROOT = Path(_data_env).resolve() if _data_env else ROOT
# First-launch bootstrap: when AM_DATA_DIR points at a fresh dir, create it
# so .env / docs/ / components/ have a place to land. Skipped in the
# default repo case (ROOT always exists).
if _data_env:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
DOCS_ROOT = DATA_ROOT / "docs"
# Pre-tool-use writes are restricted to `docs/<slug>/current.md`; baseline.md
# is the immutable history-0 and stays untouchable by the agent. The slug
# regex itself comes from am_docs (imported below after load_dotenv runs);
# we shadow the name via re-export there.


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


# .env lives next to the user's docs/, not next to the code. This matters
# for the installed shell, where Program Files is read-only — the user's
# API keys need to land somewhere writable.
load_dotenv(DATA_ROOT / ".env")

# This file is the aiohttp wiring: static routes, doc-folder bootstrap,
# make_app(), startup/shutdown. The real work lives in the am_* modules.
# Tests / external callers should import their specific symbols from the
# canonical module (e.g. `from am_pending import save_pending`); don't
# add re-exports here.
from am_docs import (  # noqa: E402
    COMPONENTS_ROOT, DOC_SLUG_RE as _DOC_SLUG_RE,
    ensure_doc_ids, ensure_working_copies,
    list_all_components, list_all_docs,
)
from am_tracking import ensure_block_ids, ensure_block_ids_for  # noqa: E402
from am_hooks import init_runtime, shutdown_runtime  # noqa: E402
from am_origin import _check_static_origin  # noqa: E402
from am_routes import (  # noqa: E402
    list_history, undo_doc, restore_snapshot,
    get_block_source, set_block_source, save_doc, delete_block, insert_block,
    list_pending, accept_pending, reject_pending,
    list_doc_skills, update_doc_skill, delete_doc_skill,
    add_doc_skill, migrate_doc_skill, reset_doc, save_baseline, set_api_key,
    list_components, save_component, delete_component,
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
    # Pick the on-disk base by URL prefix: `docs/...` and `components/...`
    # are user content (DATA_ROOT, possibly overridden by AM_DATA_DIR);
    # everything else (favicon.svg, etc.) is shipped code (ROOT). When
    # DATA_ROOT == ROOT (the default), both branches resolve identically.
    first = rel.split("/", 1)[0]
    base = DATA_ROOT if first in ("docs", "components") else ROOT
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise web.HTTPNotFound()
    if not target.exists() or not target.is_file():
        raise web.HTTPNotFound()
    # Allowlist: a small set of root files; docs/<slug>/current.md or
    # baseline.md or assets/<file>; or components/<slug>.md. snaps/,
    # patches/, original.<ext>, etc. are NOT served — they're local
    # backend state, not viewer-facing.
    if target.parent == ROOT:
        if target.name not in _STATIC_ROOT_FILES:
            raise web.HTTPNotFound()
    else:
        ok = False
        # Components: flat components/<slug>.md.
        try:
            comp_rel = target.relative_to(COMPONENTS_ROOT)
        except ValueError:
            comp_rel = None
        if comp_rel is not None and len(comp_rel.parts) == 1 \
                and target.suffix.lower() == ".md" \
                and _DOC_SLUG_RE.match(target.stem):
            ok = True
        # Docs: docs/<slug>/{current,baseline}.md or docs/<slug>/assets/<file>.
        if not ok:
            try:
                doc_rel = target.relative_to(DOCS_ROOT)
            except ValueError:
                raise web.HTTPNotFound()
            parts = doc_rel.parts
            if len(parts) == 2 and _DOC_SLUG_RE.match(parts[0]) \
                    and parts[1] in ("current.md", "baseline.md"):
                ok = True
            elif len(parts) == 3 and _DOC_SLUG_RE.match(parts[0]) \
                    and parts[1] == "assets":
                # Asset filename must have a recognized extension (defense
                # in depth — agents and users shouldn't be able to coax
                # the server into serving arbitrary file types).
                asset_ext = Path(parts[2]).suffix.lower()
                if (asset_ext in _ASSET_EXTS
                        and asset_ext not in _ASSET_BLOCKED_EXTS):
                    ok = True
        if not ok:
            raise web.HTTPNotFound()
    # Single read choke point: the viewer fetches current.md here before every
    # render and every inline edit, so normalizing on serve guarantees the
    # "every top-level block carries a stable id" invariant no matter which
    # writer produced the file — without scattering calls across every write
    # path (and missing some). Idempotent; baseline.md is left pristine.
    if target.name == "current.md" and target.parent.parent == DOCS_ROOT:
        ensure_block_ids_for(target)
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


def ensure_agent_config_in_data_root() -> None:
    """When DATA_ROOT diverges from ROOT (installed shell case), mirror
    the agent-config dirs (`.claude/`, `.agents/`) from ROOT to DATA_ROOT.

    The Claude Agent SDK loads its skill files + settings.json from the
    runtime's cwd (`.claude/skills/`, `.claude/settings.json`). The
    runtime cwd is DATA_ROOT (so the agent's relative paths like
    `docs/<slug>/current.md` resolve under the user's data dir, where
    the files actually live). But those config files SHIP with the code,
    not the data, so on a fresh installed copy DATA_ROOT/.claude/ is
    empty and the agent loads no skill / no settings — degraded
    behavior, no error.

    Fix: copy ROOT/.claude/ and ROOT/.agents/ trees into DATA_ROOT each
    startup. Uses dirs_exist_ok so any per-user files the user dropped
    into DATA_ROOT/.claude/ survive — we overwrite shipped files but
    don't wipe extras.

    No-op when DATA_ROOT == ROOT (the default repo case)."""
    if DATA_ROOT == ROOT:
        return
    import shutil  # local import — only the install-mode path needs it
    for sub in (".claude", ".agents"):
        src = ROOT / sub
        if not src.is_dir():
            continue
        dst = DATA_ROOT / sub
        try:
            shutil.copytree(src, dst, dirs_exist_ok=True)
            print(f"[agent-config] mirrored {sub}/ to DATA_ROOT", flush=True)
        except OSError as e:
            print(
                f"[agent-config] failed to mirror {sub}/ to DATA_ROOT: {e}",
                flush=True,
            )


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


async def watch_docs_list(poll_seconds: float = 1.5) -> None:
    """Poll docs/ + components/ and broadcast slug-list changes.

    Without this, the viewer's doc dropdown only refreshes when a file
    lands via the upload endpoint — drops via a file manager, `git pull`,
    manual mkdir, etc. stay invisible until the user reloads. Same for
    components/ — saves from the agent broadcast their own update, but
    out-of-band changes (git pull, manual file copy) wouldn't otherwise
    surface. Polling at 1.5s is plenty fast for human-pace filesystem
    changes and avoids the watchdog library (recursive monitors on
    Windows fire on every file in a bulk operation, and the
    cross-platform event semantics drift)."""
    last_docs = tuple(list_all_docs())
    last_components = tuple(list_all_components())
    while True:
        try:
            await asyncio.sleep(poll_seconds)
        except asyncio.CancelledError:
            return
        # Docs
        try:
            now_docs = tuple(list_all_docs())
        except OSError:
            now_docs = last_docs
        if now_docs != last_docs:
            added = sorted(set(now_docs) - set(last_docs))
            removed = sorted(set(last_docs) - set(now_docs))
            last_docs = now_docs
            try:
                await state.broadcast({"type": "docs", "list": list(now_docs)})
            except Exception as e:
                print(f"[docs-watcher] broadcast failed: {e}", flush=True)
            if added or removed:
                msg_parts = []
                if added: msg_parts.append("added: " + ", ".join(added))
                if removed: msg_parts.append("removed: " + ", ".join(removed))
                print(f"[docs-watcher] docs {' · '.join(msg_parts)}", flush=True)
        # Components
        try:
            now_components = tuple(list_all_components())
        except OSError:
            now_components = last_components
        if now_components != last_components:
            added = sorted(set(now_components) - set(last_components))
            removed = sorted(set(last_components) - set(now_components))
            last_components = now_components
            try:
                await state.broadcast(
                    {"type": "components", "list": list(now_components)}
                )
            except Exception as e:
                print(f"[docs-watcher] component broadcast failed: {e}",
                      flush=True)
            if added or removed:
                msg_parts = []
                if added: msg_parts.append("added: " + ", ".join(added))
                if removed: msg_parts.append("removed: " + ", ".join(removed))
                print(f"[docs-watcher] components {' · '.join(msg_parts)}",
                      flush=True)


async def on_startup(app: web.Application):
    ensure_working_copies()  # baseline.md -> current.md on fresh clones
    ensure_doc_ids()
    ensure_history_zero()    # current.md -> baseline.md for new user docs
    ensure_block_ids()       # stamp tracking ids on every current.md block
    ensure_skill_mirror()
    # Order matters: agent config has to be in DATA_ROOT BEFORE the runtime
    # boots, because the SDK reads .claude/{settings,skills}/ from cwd
    # (which init_runtime sets to DATA_ROOT) at start() time.
    ensure_agent_config_in_data_root()
    await init_runtime()
    # Filesystem polling: broadcast doc-list changes that didn't come
    # through the upload endpoint (out-of-band file drops, git pulls).
    app["docs_watcher"] = asyncio.create_task(watch_docs_list())


async def on_cleanup(app: web.Application):
    # Stop the docs watcher first so it doesn't try to broadcast onto
    # WebSockets we're about to close.
    task = app.get("docs_watcher")
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
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
    app.router.add_get("/components", list_components)
    app.router.add_post("/components/{slug}", save_component)
    app.router.add_delete("/components/{slug}", delete_component)
    app.router.add_get("/block-source", get_block_source)
    app.router.add_post("/block-source", set_block_source)
    app.router.add_post("/save-doc", save_doc)
    app.router.add_post("/delete-block", delete_block)
    app.router.add_post("/insert-block", insert_block)
    app.router.add_post("/add-doc-skill", add_doc_skill)
    app.router.add_get("/doc-skills", list_doc_skills)
    app.router.add_post("/update-doc-skill", update_doc_skill)
    app.router.add_post("/delete-doc-skill", delete_doc_skill)
    app.router.add_post("/migrate-doc-skill", migrate_doc_skill)
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
