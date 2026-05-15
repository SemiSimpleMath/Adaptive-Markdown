"""Adaptive Markdown backend: aiohttp WS server.

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
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web, WSMsgType


ROOT = Path(__file__).resolve().parent
EXAMPLES_DIR = ROOT / "examples"   # ship-with sample docs (intro/paper/textbook)
DOCS_DIR = ROOT / "docs"            # user-imported / agent-created docs


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

from agent_runtime import DEFAULT_PROVIDER, create_runtime
from agent_runtime.base import AgentRuntime


def list_all_docs() -> list[str]:
    """All .md docs across examples/ and docs/, sorted, as ROOT-relative POSIX paths."""
    paths: list[str] = []
    for d in (EXAMPLES_DIR, DOCS_DIR):
        if d.exists():
            paths.extend(p.relative_to(ROOT).as_posix() for p in d.glob("*.md"))
    return sorted(paths)


def _doc_path_for(doc_param: str) -> Path | None:
    """Resolve a 'examples/X.md' or 'docs/X.md' path param to an absolute Path.

    Returns None if the param is malformed, escapes the doc roots, or points
    into a subdirectory like docs/raw/."""
    if not doc_param.endswith(".md"):
        return None
    if not (doc_param.startswith("examples/") or doc_param.startswith("docs/")):
        return None
    p = (ROOT / doc_param).resolve()
    try:
        p.relative_to(ROOT)
    except ValueError:
        return None
    if p.parent not in (EXAMPLES_DIR, DOCS_DIR):
        return None
    return p
DEFAULT_DOC = "examples/intro.md"

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
    """For each .md in EXAMPLES_DIR and DOCS_DIR, ensure its frontmatter has a doc_id."""
    minted = 0
    bases = [d for d in (EXAMPLES_DIR, DOCS_DIR) if d.exists()]
    mds = sorted(md for d in bases for md in d.glob("*.md"))
    for md in mds:
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


# ---- Patches: derived from before/after diffs ---------------------------
# After each agent edit, we compute a structured patch by diffing the source
# at block-level (using tracking IDs as block boundaries). Each changed
# tracked block becomes one op. Untracked content changes are skipped from
# the patch (they're agent-discretion before-IDs-existed work).

_HISTORY_ROOT = ROOT  # `.history/<doc-stem>/<snap-id>.md` lives in here


def _history_dir_for(doc_path: Path) -> Path:
    return _HISTORY_ROOT / ".history" / doc_path.stem


def _patches_dir_for(doc_path: Path) -> Path:
    return doc_path.with_suffix(doc_path.suffix + ".patches")


def save_snapshot(doc_path: Path, text: str) -> str:
    """Save text as a snapshot under .history/<stem>/, returning the snapshot id."""
    d = _history_dir_for(doc_path)
    d.mkdir(parents=True, exist_ok=True)
    snap_id = gen_id("snap")
    (d / f"{snap_id}.md").write_text(text, encoding="utf-8")
    return snap_id


def _norm_block(text: str) -> str:
    """Normalize block text for hashing: strip outer whitespace, collapse line endings."""
    return text.replace("\r\n", "\n").strip()


def _sha256(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()


def parse_tracked_blocks(text: str) -> dict[str, str]:
    """Walk source; return {track_id: block_text} for every tracked block.

    A "block" starts at the line containing `<!-- id:b-... -->` and continues
    until the next tracking comment OR end of file. The tracking comment
    itself is included as the first line of the block text.
    """
    blocks: dict[str, str] = {}
    lines = text.split("\n")
    cur_id: str | None = None
    cur_start: int = 0
    for i, line in enumerate(lines):
        m = _TRACK_ID_PATTERN.match(line.strip())
        if m:
            if cur_id is not None:
                blocks[cur_id] = "\n".join(lines[cur_start:i])
            cur_id = m.group(1)
            cur_start = i
    if cur_id is not None:
        blocks[cur_id] = "\n".join(lines[cur_start:])
    return blocks


def derive_patch_ops(before: str, after: str) -> list[dict]:
    """Compare tracked blocks in before vs after; emit replace/insert/delete ops."""
    before_blocks = parse_tracked_blocks(before)
    after_blocks = parse_tracked_blocks(after)
    ops: list[dict] = []
    # Replaced or unchanged
    for bid, before_text in before_blocks.items():
        if bid in after_blocks:
            after_text = after_blocks[bid]
            if _norm_block(before_text) == _norm_block(after_text):
                continue
            ops.append({
                "op": "replace",
                "block_id": bid,
                "before_hash": _sha256(_norm_block(before_text)),
                "after_text": after_text,
                "after_hash": _sha256(_norm_block(after_text)),
            })
        else:
            # Block disappeared — alias map records the tombstone separately.
            ops.append({
                "op": "delete",
                "block_id": bid,
                "before_hash": _sha256(_norm_block(before_text)),
            })
    # Newly-appeared tracked IDs (rare in our model since the agent doesn't
    # mint IDs — these typically come from lazy-mint or copy-paste).
    for bid, after_text in after_blocks.items():
        if bid not in before_blocks:
            ops.append({
                "op": "insert",
                "block_id": bid,
                "after_text": after_text,
                "after_hash": _sha256(_norm_block(after_text)),
            })
    return ops


def write_patch(doc_path: Path, parent_snap_id: str, ops: list[dict],
                author: str = "agent") -> str | None:
    """Write a patch JSON if there's at least one op; return its filename or None."""
    if not ops:
        return None
    pdir = _patches_dir_for(doc_path)
    pdir.mkdir(parents=True, exist_ok=True)
    patch_id = gen_id("p")
    payload = {
        "patch_id": patch_id,
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "parent": parent_snap_id,
        "doc": doc_path.name,
        "author": author,
        "ops": ops,
    }
    out = pdir / f"{patch_id}.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[patch] {patch_id} -> {doc_path.name} ({len(ops)} op{'s' if len(ops)!=1 else ''})",
          flush=True)
    return patch_id


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
        self.runtime: AgentRuntime | None = None
        self.busy = asyncio.Lock()
        self.current_provider: str = DEFAULT_PROVIDER
        self.current_model: str = ""

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


# Per-tool-call stash so post-edit hook can diff for drift + derive patches.
# Keyed by tool_use_id; value: {"file_path": str, "before_text": str, "snap_id": str}
_pre_edit_state: dict[str, dict] = {}


async def pre_tool_use_hook(input_data, tool_use_id, context):
    """Capture the .md file's content before an Edit/Write. Snapshots it under
    .history/ so we can reference the parent state in derived patches and so
    the user can roll back via the History button or Reset."""
    tool_input = input_data.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path", "")
    if file_path and file_path.endswith(".md"):
        try:
            p = Path(file_path)
            before_text = p.read_text(encoding="utf-8")
            snap_id = save_snapshot(p, before_text)
            _pre_edit_state[tool_use_id] = {
                "file_path": file_path,
                "before_text": before_text,
                "snap_id": snap_id,
            }
        except Exception as e:
            print(f"[pre-edit] snapshot failed: {e}", flush=True)
    return {}


async def finalize_md_edit(
    doc_path: Path,
    before_text: str | None,
    snap_id: str | None,
    author: str,
) -> None:
    """Shared post-edit substrate work, used by both the Claude post-hook
    (per Edit/Write) and the Codex per-turn finalizer.

    - If we have before_text but no snap_id, save a fresh snapshot now.
      (Claude's pre-hook already snapshotted; Codex snapshots post-turn
      from its in-memory before-state.)
    - Derive id-drift + patches if before_text is available.
    - Broadcast doc_changed; refresh the doc list if this was a new .md.
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
        try:
            after_text = doc_path.read_text(encoding="utf-8")
            ops = derive_patch_ops(before_text, after_text)
            if ops and snap_id:
                write_patch(
                    doc_path,
                    parent_snap_id=snap_id,
                    ops=ops,
                    author=author,
                )
        except Exception as e:
            print(f"[patch] derivation failed: {e}", flush=True)

    try:
        rel = doc_path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        rel = str(doc_path)
    await state.broadcast({"type": "doc_changed", "file": rel})
    if rel.endswith(".md") and (rel.startswith("examples/") or rel.startswith("docs/")):
        await state.broadcast({"type": "docs", "list": list_all_docs(), "doc": rel})


async def post_tool_use_hook(input_data, tool_use_id, context):
    """After an Edit/Write to a .md file: detect track-id drift, derive a
    patch from the before/after pair, then broadcast doc_changed."""
    tool_input = input_data.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path", "")
    if not file_path or not file_path.endswith(".md"):
        return {}

    stash = _pre_edit_state.pop(tool_use_id, None)
    before_text = stash["before_text"] if stash else None
    snap_id = stash["snap_id"] if stash else None
    await finalize_md_edit(
        Path(file_path),
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


async def run_turn(text: str):
    async with state.busy:
        if not state.runtime:
            await state.broadcast({"type": "error", "text": "Agent runtime not initialized"})
            return
        try:
            async for event in state.runtime.run_turn(text):
                await state.broadcast(event)
        except Exception as e:
            await state.broadcast({"type": "error", "text": f"agent error: {e!r}"})


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    async with state.client_lock:
        state.clients.add(ws)

    await ws.send_json({
        "type": "ready",
        "doc": DEFAULT_DOC,
        "docs": list_all_docs(),
        "provider": state.current_provider,
        "model": state.current_model,
        "models": state.runtime.model_aliases if state.runtime else [],
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

                # Optional per-turn model switch. Resets the runtime session
                # (model is set at session init, not per-query).
                requested_model = data.get("model") or ctx.get("model")
                if requested_model and state.runtime:
                    target = state.runtime.resolve_model(requested_model)
                    if target != state.current_model:
                        await state.broadcast({
                            "role": "assistant",
                            "type": "text",
                            "text": f"_Switching model → {target} (new conversation)_\n",
                        })
                        await reset_runtime_session(target)

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
                asyncio.create_task(reset_runtime_session(model))

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


# ---- Drop-to-upload ------------------------------------------------------
# POST /upload with multipart form, field name "file", .md only. Saves to
# examples/, mints doc_id, broadcasts updated docs list. Returns the
# relative path the client should switch to.
_UPLOAD_MAX_BYTES = 1 * 1024 * 1024  # 1 MB safety cap


async def upload_md(request: web.Request) -> web.Response:
    reader = await request.multipart()
    field = None
    async for part in reader:
        if part.name == "file":
            field = part
            break
    if field is None:
        return web.json_response({"error": "no file field"}, status=400)

    raw_name = field.filename or "uploaded.md"
    ext = Path(raw_name).suffix.lower()
    # Well-known text-ish formats route through the conversion path directly.
    # Anything else is allowed when the client passes ?allow_unknown=1 —
    # the agent then attempts a best-effort conversion.
    KNOWN_EXTS = {".md", ".tex", ".txt", ".rst", ".org"}
    allow_unknown = request.query.get("allow_unknown") in ("1", "true", "yes")
    if ext not in KNOWN_EXTS and not allow_unknown:
        return web.json_response(
            {"error": f"unsupported file type: {ext or '(no extension)'}",
             "kind": ext or "(no extension)",
             "known": sorted(KNOWN_EXTS),
             "unknown": True},
            status=415,
        )
    needs_conversion = ext != ".md"
    # Sanitize: strip path components, keep only safe chars
    safe_name = re.sub(r"[^\w.\-]", "_", Path(raw_name).name)
    if not safe_name or safe_name == ext:
        safe_name = f"uploaded{ext or '.bin'}"

    # User uploads now go into docs/ (not examples/, which is reserved for ship-with).
    if needs_conversion:
        raw_dir = DOCS_DIR / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        target = raw_dir / safe_name
    else:
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        target = DOCS_DIR / safe_name
    counter = 1
    base = target.stem
    while target.exists():
        target = target.with_name(f"{base}-{counter}{ext}")
        counter += 1

    # Stream-read with size cap
    total = 0
    chunks: list[bytes] = []
    while True:
        chunk = await field.read_chunk(size=64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > _UPLOAD_MAX_BYTES:
            return web.json_response({"error": "file too large (max 1MB)"}, status=413)
        chunks.append(chunk)
    raw = b"".join(chunks)
    # Strip UTF-8 BOM if present (Notepad / PowerShell often add this) so
    # the frontmatter regex's ^--- anchor still matches.
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    elif raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        # UTF-16 — re-encode as UTF-8
        raw = raw.decode("utf-16").encode("utf-8")
    text = raw.decode("utf-8", errors="replace")
    # Normalize line endings to LF
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # newline="" prevents Python's text-mode CRLF translation on Windows
    with target.open("w", encoding="utf-8", newline="") as f:
        f.write(text)

    rel = target.relative_to(ROOT).as_posix()

    if needs_conversion:
        # Non-.md files: viewer will ask the agent to convert. No doc_id mint,
        # no docs broadcast — the new .md doesn't exist yet.
        target_name = f"docs/{Path(safe_name).stem}.md"
        unknown_flag = ext not in KNOWN_EXTS
        print(f"[upload:raw] {rel} ({total} bytes, kind={ext}, unknown={unknown_flag})", flush=True)
        return web.json_response({
            "path": rel,
            "name": target.name,
            "kind": ext,
            "needs_conversion": True,
            "unknown": unknown_flag,
            "target": target_name,
        })

    # .md upload: mint doc_id and tell connected viewers
    ensure_doc_ids()
    await state.broadcast({"type": "docs", "list": list_all_docs(), "doc": rel})
    print(f"[upload] {target.name} ({total} bytes)", flush=True)
    return web.json_response({"path": rel, "name": target.name})


# ---- Reset to history-0 -------------------------------------------------
# POST /reset?doc=examples/<name>.md restores the working copy from the
# oldest snapshot under .history/<stem>/snap-*.md. ULID-prefixed names sort
# by mint time, so the lexicographic minimum is the earliest snap — i.e.,
# the state of the doc before any agent ever touched it (or before the
# backend first served it, whichever came first). For the truly-shipped
# version of a doc, `git checkout examples/<name>.md` is the answer.


async def list_history(request: web.Request) -> web.Response:
    """GET /history?doc=examples/X.md|docs/X.md — list snapshots captured by pre-edit hook."""
    doc_param = request.query.get("doc", "").strip()
    doc_path = _doc_path_for(doc_param)
    if doc_path is None:
        return web.json_response({"error": "bad doc path"}, status=400)
    hist = _history_dir_for(doc_path)
    snaps = []
    if hist.exists():
        for p in sorted(hist.glob("snap-*.md"), reverse=True):
            stat = p.stat()
            snaps.append({
                "snap_id": p.stem.replace("snap-", ""),
                "ts_ms": int(stat.st_mtime * 1000),
                "size": stat.st_size,
            })
    return web.json_response({"doc": doc_param, "snapshots": snaps})


def _snapshot_if_changed(doc_path: Path) -> str | None:
    """Capture current working-copy state as a fresh snap if it differs from
    the newest existing snap. Used by /reset, /undo, /restore_snapshot so a
    destructive UI action never silently destroys the user's current state —
    they can always recover via the History panel.

    Returns the new snap_id if a snapshot was taken, None if the working copy
    already matches the newest snap (no-op).

    Known limitation: with this in place, double-Undo can read the just-taken
    safety snap as the newest, producing a "redo" instead of a deeper undo.
    Acceptable v0 trade — losing recent work to a single Reset/Restore click
    is the much more common surprise. The longer-term fix is a HEAD-pointer
    or post-edit hook model (see ROADMAP, "Snapshot semantics")."""
    try:
        current = doc_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    hist = _history_dir_for(doc_path)
    if hist.exists():
        snaps = sorted(hist.glob("snap-*.md"))
        if snaps:
            try:
                if snaps[-1].read_text(encoding="utf-8") == current:
                    return None
            except (OSError, UnicodeDecodeError):
                pass  # treat as different and save defensively
    return save_snapshot(doc_path, current)


async def undo_doc(request: web.Request) -> web.Response:
    """POST /undo?doc=examples/X.md|docs/X.md — restore the most recent snapshot.

    Convenience over /restore_snapshot when the user just wants "go back one"
    without opening the history panel."""
    doc_param = request.query.get("doc", "").strip()
    doc_path = _doc_path_for(doc_param)
    if doc_path is None:
        return web.json_response({"error": "bad doc path"}, status=400)
    hist = _history_dir_for(doc_path)
    if not hist.exists():
        return web.json_response({"error": "no snapshots yet"}, status=404)
    snaps = sorted(hist.glob("snap-*.md"), reverse=True)
    if not snaps:
        return web.json_response({"error": "no snapshots yet"}, status=404)
    newest = snaps[0]  # bind BEFORE the safety snap so we restore to the
                       # pre-existing newest, not to the safety snap itself
    _snapshot_if_changed(doc_path)
    doc_path.write_bytes(newest.read_bytes())
    rel = doc_path.relative_to(ROOT).as_posix()
    await state.broadcast({"type": "doc_changed", "file": rel})
    snap_id = newest.stem.replace("snap-", "")
    print(f"[undo] {rel} <- snap-{snap_id}", flush=True)
    return web.json_response({"path": rel, "snap_id": snap_id, "ok": True})


async def restore_snapshot(request: web.Request) -> web.Response:
    """POST /restore_snapshot?doc=...&snap_id=... — overwrite working copy with snapshot."""
    doc_param = request.query.get("doc", "").strip()
    snap_id = request.query.get("snap_id", "").strip()
    doc_path = _doc_path_for(doc_param)
    if doc_path is None:
        return web.json_response({"error": "bad doc path"}, status=400)
    if not snap_id or not re.fullmatch(r"[A-Za-z0-9_-]{6,40}", snap_id):
        return web.json_response({"error": "bad snap_id"}, status=400)
    snap_path = _history_dir_for(doc_path) / f"snap-{snap_id}.md"
    if not snap_path.exists():
        return web.json_response({"error": "snapshot not found"}, status=404)
    _snapshot_if_changed(doc_path)
    doc_path.write_bytes(snap_path.read_bytes())
    rel = doc_path.relative_to(ROOT).as_posix()
    await state.broadcast({"type": "doc_changed", "file": rel})
    print(f"[restore] {rel} <- snap-{snap_id}", flush=True)
    return web.json_response({"path": rel, "snap_id": snap_id, "ok": True})


async def reset_doc(request: web.Request) -> web.Response:
    doc_param = request.query.get("doc", "").strip()
    doc_path = _doc_path_for(doc_param)
    if doc_path is None:
        return web.json_response({"error": "bad doc path"}, status=400)
    hist = _history_dir_for(doc_path)
    snaps = sorted(hist.glob("snap-*.md")) if hist.exists() else []
    if not snaps:
        return web.json_response(
            {"error": (
                f"no history snapshot for {doc_path.name} yet — Reset "
                "restores to the state before the first agent edit, but no "
                "agent has touched this doc. For the shipped version, run "
                f"`git checkout {doc_path.relative_to(ROOT).as_posix()}`."
            )},
            status=404,
        )
    history_zero = snaps[0]  # ULID-prefixed; lex-min = earliest mint
    _snapshot_if_changed(doc_path)
    doc_path.write_bytes(history_zero.read_bytes())
    rel = doc_path.relative_to(ROOT).as_posix()
    await state.broadcast({"type": "doc_changed", "file": rel})
    print(f"[reset] {rel} <- .history/{doc_path.stem}/{history_zero.name}",
          flush=True)
    return web.json_response({
        "path": rel,
        "snap_id": history_zero.stem.replace("snap-", ""),
        "ok": True,
    })


def ensure_history_zero() -> None:
    """For every .md under examples/ + docs/, capture a history-0 snapshot
    if no .history/<stem>/snap-*.md exists yet. Guarantees the Reset button
    always has something to restore to, even for docs the agent hasn't
    touched in this clone."""
    minted = 0
    bases = [d for d in (EXAMPLES_DIR, DOCS_DIR) if d.exists()]
    for d in bases:
        for md in d.glob("*.md"):
            hist = _history_dir_for(md)
            if hist.exists() and any(hist.glob("snap-*.md")):
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                print(f"[history] cannot read {md.name} for history-0: {e}",
                      flush=True)
                continue
            snap_id = save_snapshot(md, text)
            print(f"[history] minted history-0 for {md.name} -> snap-{snap_id}",
                  flush=True)
            minted += 1
    if minted:
        print(f"ensured history-0 ({minted} new)", flush=True)


async def on_startup(app: web.Application):
    ensure_doc_ids()
    ensure_history_zero()
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
    app.router.add_get("/ws", ws_handler)
    app.router.add_post("/upload", upload_md)
    app.router.add_get("/history", list_history)
    app.router.add_post("/undo", undo_doc)
    app.router.add_post("/restore_snapshot", restore_snapshot)
    app.router.add_post("/reset", reset_doc)
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
