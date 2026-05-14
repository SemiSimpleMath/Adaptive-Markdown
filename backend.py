"""v2 backend: aiohttp WS server wrapping Claude Agent SDK.

Differences from v1:
- No on-disk compile step. The browser renders markdown live.
- doc_changed events carry the file path the agent modified; the viewer
  fetches and DOM-patches in place.
- Single skill: adaptive-markdown.

Routes:
  GET  /                       -> index.html
  GET  /<file path>            -> static files (examples/*.md, *.html)
  WS   /ws                     -> chat protocol

WS protocol:
  server -> client:
    {type:"ready", doc:"examples/intro.md", docs:["examples/intro.md",...]}
    {type:"doc_changed", file:"examples/intro.md"}
    {role:"user", type:"text", text:"..."}
    {role:"assistant", type:"text"|"thinking", text:"..."}
    {role:"assistant", type:"tool_use", name:"...", input:{...}}
    {type:"turn_done", session_id:"...", cost_usd:0.01}
    {type:"chat_reset"}
    {type:"error", text:"..."}

  client -> server:
    {type:"chat", text:"...", context:{doc, selections:[...]}}
    {type:"new_chat"}
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

from aiohttp import web, WSMsgType
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, HookMatcher,
    AssistantMessage, ResultMessage,
    TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock,
)


ROOT = Path(__file__).resolve().parent
EXAMPLES_DIR = ROOT / "examples"
DEFAULT_DOC = "examples/intro.md"

# Model selection. Defaults to Haiku for low cost; the viewer can override
# per-turn via the chat context. Env-var sets the initial value.
DEFAULT_MODEL = os.environ.get("MODEL", "claude-haiku-4-5-20251001")
MAX_BUDGET_USD = float(os.environ.get("MAX_BUDGET_USD", "1.0"))

# Friendly model names → SDK model strings.
MODEL_ALIASES = {
    "haiku":  "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-7",
}


# ---- ULID-style sticky identifiers --------------------------------------
# Short, time-sortable, prefixed. Format: {prefix}-{10ts}{6rand} = 18 chars.
# Timestamp (Crockford base32, ms precision) lexicographically sorts by
# mint order. 6 random chars (30 bits) prevent collisions inside a single
# millisecond — safe for hundreds of mints per ms.
_CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

def _b32_encode(n: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_CROCKFORD_BASE32[n & 0x1F])
        n >>= 5
    return "".join(reversed(out))

def gen_id(prefix: str) -> str:
    """Mint a sortable, prefixed sticky ID. e.g. gen_id('d') -> 'd-01HNVQ7E9KMX2BNF'."""
    ts_ms = int(time.time() * 1000)
    ts_part = _b32_encode(ts_ms, 10)
    rand_bits = int.from_bytes(os.urandom(4), "big") & ((1 << 30) - 1)
    rand_part = _b32_encode(rand_bits, 6)
    return f"{prefix}-{ts_part}{rand_part}"


# ---- Doc identity --------------------------------------------------------
# Every .md gets a stable doc_id in its frontmatter on first sight. The ID
# is preserved across renames, agent edits, history snapshots, etc.
_FRONTMATTER_RE = re.compile(r"^---\r?\n([\s\S]*?)\r?\n---\r?\n")
_HAS_DOC_ID_RE = re.compile(r"^doc_id\s*:", re.MULTILINE)

def ensure_doc_ids() -> None:
    """For each .md in EXAMPLES_DIR, ensure its frontmatter declares a doc_id."""
    if not EXAMPLES_DIR.exists():
        return
    minted = 0
    for md in sorted(EXAMPLES_DIR.glob("*.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        m = _FRONTMATTER_RE.match(text)
        if not m:
            continue  # no frontmatter — leave alone for now
        fm_body = m.group(1)
        if _HAS_DOC_ID_RE.search(fm_body):
            continue
        new_id = gen_id("d")
        # Insert doc_id at the top of the frontmatter, preserving everything else
        # byte-for-byte (no YAML round-trip — keeps quote styles, comments, order).
        new_fm_body = f"doc_id: {new_id}\n{fm_body}"
        new_text = f"---\n{new_fm_body}\n---\n" + text[m.end():]
        md.write_text(new_text, encoding="utf-8")
        minted += 1
        print(f"  minted doc_id={new_id} for {md.name}", flush=True)
    if minted:
        print(f"ensured doc_ids ({minted} new)", flush=True)


# ---- Block-level tracking IDs (lazy mint on intent-to-reference) --------
# When a chat arrives with a focused block lacking a track_id, mint one
# server-side and write `<!-- id:b-... -->` immediately preceding the block
# in source. Subsequent references use the now-stable ID.
_HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.+?)(?:\s*\{([^}]+)\})?\s*$")
_TRACK_COMMENT_RE = re.compile(r"^\s*<!--\s*id\s*:\s*(b-[A-Z0-9-]+)\s*-->\s*$", re.IGNORECASE)


def _find_block_line(lines: list[str], signature: dict) -> int | None:
    """Locate the line index where the block matching `signature` begins.

    Strategy: prefer matching headings by their text (modulo {#id} attr).
    Fall back to matching paragraphs by first ~60 chars of normalized text.
    Returns None if the block cannot be located unambiguously.
    """
    label = (signature.get("label") or "").strip()
    excerpt = (signature.get("excerpt") or "").strip()
    anchor_id = (signature.get("anchor_id") or "").strip()

    # If we have an anchor id, the source line is `## Foo {#anchor_id}`
    if anchor_id:
        anchor_pat = re.compile(r"\{#" + re.escape(anchor_id) + r"\b")
        for i, line in enumerate(lines):
            if anchor_pat.search(line):
                return i

    # Heading match by text
    if label:
        for i, line in enumerate(lines):
            m = _HEADING_LINE_RE.match(line)
            if not m:
                continue
            heading_text = m.group(2).strip()
            if heading_text == label or heading_text.startswith(label[:60]):
                return i

    # Paragraph / list-item / other prose: match by first ~60 normalized chars
    if excerpt:
        sig_first = re.sub(r"\s+", " ", excerpt[:80]).strip()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "<!--", ":::", "```")):
                continue
            line_first = re.sub(r"\s+", " ", stripped[:80]).strip()
            if line_first.startswith(sig_first[:40]):
                return i

    return None


def _aliases_path(doc_path: Path) -> Path:
    return doc_path.with_suffix(doc_path.suffix + ".id-aliases.json")


def load_aliases(doc_path: Path) -> dict[str, str | None]:
    p = _aliases_path(doc_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_aliases(doc_path: Path, aliases: dict) -> None:
    _aliases_path(doc_path).write_text(
        json.dumps(aliases, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def resolve_alias(doc_path: Path, old_id: str) -> str | None:
    """Follow alias chain. Returns the current canonical ID, or None if the
    historical ID was explicitly dropped (tombstoned).
    """
    aliases = load_aliases(doc_path)
    seen: set[str] = set()
    cur = old_id
    while cur in aliases:
        if cur in seen:
            return None  # cycle — corrupt state
        seen.add(cur)
        nxt = aliases[cur]
        if nxt is None:
            return None  # dropped
        cur = nxt
    return cur


_TRACK_ID_PATTERN = re.compile(r"<!--\s*id\s*:\s*(b-[A-Z0-9-]+)\s*-->", re.IGNORECASE)


def extract_track_ids(text: str) -> set[str]:
    """Find every `<!-- id:b-... -->` tracking ID in source."""
    return set(m.group(1) for m in _TRACK_ID_PATTERN.finditer(text))


async def detect_id_drift(doc_path: Path, before_text: str) -> None:
    """Compare before/after track-id sets. Record disappearances in the
    alias map as tombstones (None marker). Path-compress while we're at it.

    Doesn't (yet) try to detect merge targets by content similarity —
    that's a follow-on. For now, "this ID is gone" is recorded explicitly.
    """
    try:
        after_text = doc_path.read_text(encoding="utf-8")
    except Exception:
        return
    before_ids = extract_track_ids(before_text)
    after_ids = extract_track_ids(after_text)
    dropped = before_ids - after_ids
    if not dropped:
        return
    aliases = load_aliases(doc_path)
    changed = False
    for d in dropped:
        if d not in aliases:
            aliases[d] = None  # tombstone — we know it's gone but not why
            changed = True
            print(f"[alias] {d} -> dropped (in {doc_path.name})", flush=True)
    if changed:
        save_aliases(doc_path, aliases)


def mint_track_id_for(doc_path: Path, signature: dict) -> str | None:
    """Insert `<!-- id:b-... -->` before the matching block. Returns the new
    track_id, or None if the block couldn't be located. If a tracking
    comment already precedes the block, returns that existing ID instead
    of minting a new one (idempotent)."""
    try:
        text = doc_path.read_text(encoding="utf-8")
    except Exception:
        return None
    lines = text.split("\n")
    idx = _find_block_line(lines, signature)
    if idx is None:
        return None
    # If the immediately preceding non-blank line is already a tracking comment,
    # reuse that ID rather than minting a duplicate.
    j = idx - 1
    while j >= 0 and lines[j].strip() == "":
        j -= 1
    if j >= 0:
        m = _TRACK_COMMENT_RE.match(lines[j])
        if m:
            return m.group(1)
    new_id = gen_id("b")
    lines.insert(idx, f"<!-- id:{new_id} -->")
    doc_path.write_text("\n".join(lines), encoding="utf-8")
    return new_id


class State:
    def __init__(self):
        self.clients: set[web.WebSocketResponse] = set()
        self.client_lock = asyncio.Lock()
        self.sdk: ClaudeSDKClient | None = None
        self.busy = asyncio.Lock()
        self.current_model: str = DEFAULT_MODEL

    async def broadcast(self, msg: dict) -> None:
        text = json.dumps(msg)
        dead = []
        async with self.client_lock:
            for ws in self.clients:
                try:
                    await ws.send_str(text)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)


state = State()


# Per-tool-call stash of pre-edit content so post-edit hook can diff for drift.
_pre_edit_state: dict[str, dict[str, str]] = {}


async def pre_tool_use_hook(input_data, tool_use_id, context):
    """Capture the .md file's content before an Edit/Write so post-hook can
    detect tracking-ID drift (disappearances → alias-map tombstones)."""
    tool_input = input_data.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path", "")
    if file_path and file_path.endswith(".md"):
        try:
            content = Path(file_path).read_text(encoding="utf-8")
            _pre_edit_state[tool_use_id] = {file_path: content}
        except Exception:
            pass
    return {}


async def post_tool_use_hook(input_data, tool_use_id, context):
    """After an Edit/Write to a .md file: detect track-id drift, then broadcast.

    No on-disk compile — the browser renders markdown live, so we just notify
    the viewer that the source changed after handling alias bookkeeping.
    """
    tool_input = input_data.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path", "")
    if not file_path or not file_path.endswith(".md"):
        return {}

    # Detect alias drift if we captured before-state.
    before = _pre_edit_state.pop(tool_use_id, {}).get(file_path)
    if before is not None:
        try:
            await detect_id_drift(Path(file_path), before)
        except Exception as e:
            print(f"[alias] drift detection failed: {e}", flush=True)

    p = Path(file_path)
    try:
        rel = p.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        rel = file_path
    await state.broadcast({"type": "doc_changed", "file": rel})
    return {}


def resolve_model(name: str | None) -> str:
    """Map a friendly name (haiku/sonnet/opus) or pass-through SDK model id."""
    if not name:
        return DEFAULT_MODEL
    return MODEL_ALIASES.get(name.lower(), name)


async def init_sdk(model: str | None = None):
    chosen = resolve_model(model)
    options = ClaudeAgentOptions(
        cwd=str(ROOT),
        setting_sources=["project"],
        skills="all",
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        permission_mode="acceptEdits",
        model=chosen,
        max_budget_usd=MAX_BUDGET_USD,
        hooks={
            "PreToolUse":  [HookMatcher(matcher="Edit|Write", hooks=[pre_tool_use_hook])],
            "PostToolUse": [HookMatcher(matcher="Edit|Write", hooks=[post_tool_use_hook])],
        },
    )
    state.sdk = ClaudeSDKClient(options=options)
    await state.sdk.__aenter__()
    state.current_model = chosen
    print(f"Claude Agent SDK ready (model={chosen}, budget=${MAX_BUDGET_USD}/turn)",
          flush=True)
    await state.broadcast({"type": "model_changed", "model": chosen})


async def shutdown_sdk():
    if state.sdk:
        try:
            await state.sdk.__aexit__(None, None, None)
        finally:
            state.sdk = None


async def reset_sdk_session(model: str | None = None):
    await shutdown_sdk()
    await init_sdk(model)
    await state.broadcast({"type": "chat_reset"})


def block_to_dict(block):
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


async def run_turn(text: str):
    async with state.busy:
        if not state.sdk:
            await state.broadcast({"type": "error", "text": "SDK not initialized"})
            return
        try:
            await state.sdk.query(text)
            async for message in state.sdk.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        d = block_to_dict(block)
                        if d:
                            await state.broadcast({"role": "assistant", **d})
                elif isinstance(message, ResultMessage):
                    await state.broadcast({
                        "type": "turn_done",
                        "session_id": message.session_id,
                        "cost_usd": message.total_cost_usd,
                    })
        except Exception as e:
            await state.broadcast({"type": "error", "text": f"agent error: {e!r}"})


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    async with state.client_lock:
        state.clients.add(ws)

    docs = sorted(
        p.relative_to(ROOT).as_posix()
        for p in EXAMPLES_DIR.glob("*.md")
    )
    await ws.send_json({
        "type": "ready",
        "doc": DEFAULT_DOC,
        "docs": docs,
        "model": state.current_model,
        "models": list(MODEL_ALIASES.keys()),
    })

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except Exception:
                continue

            if data.get("type") == "chat":
                text = (data.get("text") or "").strip()
                ctx = data.get("context") or {}
                if not text:
                    continue

                # Optional per-turn model switch. Resets the SDK session
                # (model is set at client init, not per-query).
                requested_model = data.get("model") or ctx.get("model")
                if requested_model:
                    target = resolve_model(requested_model)
                    if target != state.current_model:
                        await state.broadcast({
                            "role": "assistant",
                            "type": "text",
                            "text": f"_Switching model → {target} (new conversation)_\n",
                        })
                        await reset_sdk_session(target)

                doc = (ctx.get("doc") or "").strip() or None
                selections = ctx.get("selections") or []
                if not isinstance(selections, list):
                    selections = []

                # Lazy-mint track_ids for any focused selections that lack one.
                # The block becomes stably-identifiable from this point on.
                doc_changed_after_mint = False
                if doc:
                    doc_path = ROOT / doc
                    for s in selections:
                        if isinstance(s, dict) and not s.get("track_id"):
                            new_id = mint_track_id_for(doc_path, s)
                            if new_id:
                                s["track_id"] = new_id
                                if not s.get("id"):
                                    s["id"] = new_id
                                doc_changed_after_mint = True
                                print(f"[mint] track_id={new_id} for "
                                      f"selection label={s.get('label')!r}",
                                      flush=True)
                if doc_changed_after_mint and doc:
                    await state.broadcast({"type": "doc_changed", "file": doc})

                # Echo user's text verbatim.
                await state.broadcast({"role": "user", "type": "text", "text": text})

                # Augment prompt with doc path + focused blocks.
                preamble = []
                if doc:
                    preamble.append(
                        f'The reader is viewing the document at "{doc}". When you '
                        "need to edit, this is the file."
                    )
                if len(selections) == 1:
                    s = selections[0]
                    info = []
                    if s.get("id"):    info.append(f'id="{s["id"]}"')
                    if s.get("label"): info.append(f'label="{s["label"]}"')
                    info_str = ", ".join(info)
                    excerpt = s.get("excerpt", "")
                    excerpt_block = (
                        f"\n\nFocused block content (excerpt up to 2000 chars):\n"
                        f"```\n{excerpt}\n```"
                        if excerpt else ""
                    )
                    preamble.append(
                        f"The reader has clicked on a specific block, giving it "
                        f"focus ({info_str}).{excerpt_block}\n\n"
                        "When they say 'this', 'here', 'it', 'this section', "
                        "'under it', 'above it' — they mean this focused block. "
                        "If you need surrounding context, Read the source file."
                    )
                elif len(selections) > 1:
                    blocks_str = "\n\n".join(
                        f"Block {i+1}: id={s.get('id') or '(none)'!r}, "
                        f"label={s.get('label') or ''!r}\n"
                        f"```\n{s.get('excerpt', '')}\n```"
                        for i, s in enumerate(selections)
                    )
                    preamble.append(
                        f"The reader has selected {len(selections)} blocks. When "
                        "they say 'these', 'them', 'this group', 'all of these' — "
                        "they mean these blocks. If the operation applies "
                        "independently to each block, consider parallel subagents.\n\n"
                        f"{blocks_str}"
                    )

                prompt = ("[" + "\n\n".join(preamble) + "]\n\n" + text
                          if preamble else text)
                print(f"[ws] chat doc={doc!r} selections={len(selections)}",
                      flush=True)
                asyncio.create_task(run_turn(prompt))

            elif data.get("type") == "new_chat":
                model = data.get("model")
                asyncio.create_task(reset_sdk_session(model))

    finally:
        async with state.client_lock:
            state.clients.discard(ws)
    return ws


async def serve_index(request: web.Request) -> web.Response:
    return web.FileResponse(ROOT / "index.html")


async def serve_static(request: web.Request) -> web.Response:
    rel = request.match_info["path"]
    # Disallow `..` traversal.
    target = (ROOT / rel).resolve()
    try:
        target.relative_to(ROOT)
    except ValueError:
        raise web.HTTPForbidden()
    if not target.exists() or not target.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(target)


async def on_startup(app: web.Application):
    ensure_doc_ids()
    await init_sdk()


async def on_cleanup(app: web.Application):
    await shutdown_sdk()


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", serve_index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/{path:.+}", serve_static)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    print(f"v2 backend on http://127.0.0.1:{port}", flush=True)
    web.run_app(make_app(), host="127.0.0.1", port=port, print=None)
