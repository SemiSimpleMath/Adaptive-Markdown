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
from urllib.parse import urlparse

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

import validators
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


# ---- Origin enforcement -------------------------------------------------
# The viewer is meant to be reached only from a browser tab pointed at this
# same local backend. A cross-origin page (or another localhost service) can
# still try to drive our endpoints — WebSockets are not subject to the
# Same-Origin Policy at the connection level, and POSTs ride CSRF if not
# checked. Reject any request whose Origin doesn't match the local host.

def _is_localhost_origin(request: web.Request) -> bool:
    """True iff the Origin header is http://(127.0.0.1|localhost):<port> and
    matches the request's Host header. Caller decides what to do when Origin
    is absent (allowed for top-level navigation, rejected for state changes)."""
    origin = request.headers.get("Origin", "")
    if not origin:
        return False
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme != "http":
        return False
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        return False
    if parsed.netloc != request.host:
        return False
    return True


def _require_localhost_origin(request: web.Request) -> None:
    """Reject the request if it carries a non-localhost Origin header.

    Missing Origin is *allowed* — per the Fetch spec, browsers omit the
    Origin header for same-origin GET/HEAD requests, so a strict "Origin
    required" check would reject legitimate same-origin viewer fetches
    (history, doc loads) in Chrome and Firefox. The threat we want to stop
    is cross-origin and DNS-rebinding requests, which *always* set Origin
    to a non-localhost value — and those we still reject."""
    if request.headers.get("Origin") and not _is_localhost_origin(request):
        raise web.HTTPForbidden(text="origin not allowed")


# Same behavior as _require_localhost_origin; kept as a separate name to
# document intent at the call sites (static resource serving vs state-
# mutating endpoints).
_check_static_origin = _require_localhost_origin


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


def _history_zero_path(doc_path: Path) -> Path | None:
    """Locate the history-0 snapshot for this doc.

    Prefers a stable-named baseline.md (tracked in git for ship-with example
    docs, so a fresh clone has an authoritative Reset target) over an
    arbitrary ULID-prefixed snap. Falls back to the oldest snap-*.md if no
    baseline.md exists — keeps user-uploaded docs/ working the same way."""
    hist = _history_dir_for(doc_path)
    baseline = hist / "baseline.md"
    if baseline.exists():
        return baseline
    if hist.exists():
        snaps = sorted(hist.glob("snap-*.md"))
        if snaps:
            return snaps[0]
    return None


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
# Per-file consecutive-revert counter for the validator retry-cap.
_validate_revert_count: dict[str, int] = {}


def _safe_inline_doc_path(rel: str) -> Path | None:
    """Resolve a chat-context doc path to an absolute Path iff it's safe
    to inline into the agent's preamble — under examples/ or docs/, .md
    suffix, exists as a regular file. Otherwise None.

    Read-side counterpart to `_validate_agent_write_path`. The chat
    context comes from the client, so a malicious or buggy front-end
    could otherwise coax the backend into reading and inlining arbitrary
    files (`.env`, `backend.py`, `/etc/passwd`) into agent context."""
    if not rel or not rel.endswith(".md"):
        return None
    try:
        p = (ROOT / rel).resolve()
        rel_posix = p.relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return None
    if not (rel_posix.startswith("examples/")
            or rel_posix.startswith("docs/")):
        return None
    if not p.is_file():
        return None
    return p


def _validate_agent_write_path(file_path: str) -> tuple[bool, str]:
    """Decide whether the agent is allowed to Edit/Write a given path.

    Allowed writes: direct children of examples/ or docs/, .md suffix only.
    Anything else — project source files, dotfiles like .env, .history/ (the
    backend manages snapshots), absolute paths like ~/.ssh, paths outside
    ROOT — is rejected. This is the root-cause defense against a successful
    prompt injection convincing the agent to clobber arbitrary files."""
    if not file_path:
        return False, "missing file_path"
    try:
        p = Path(file_path).resolve()
    except (OSError, ValueError):
        return False, f"invalid file_path: {file_path!r}"
    try:
        p.relative_to(ROOT)
    except ValueError:
        return False, (
            f"writes outside the project root are not permitted ({file_path!r}). "
            "The agent may only modify .md files under examples/ or docs/."
        )
    if p.suffix.lower() != ".md":
        return False, (
            f"only .md files are writable by the agent ({p.name!r}). "
            "Project source files (backend.py, index.html, etc.) and "
            "configuration files (.env, .gitignore, SKILL.md) are off-limits."
        )
    if p.parent not in (EXAMPLES_DIR, DOCS_DIR):
        return False, (
            f"only files directly under examples/ or docs/ are writable "
            f"({file_path!r}). Subdirectories like docs/raw/ and .history/ "
            "are managed by the backend, not the agent."
        )
    return True, ""


def _deny_pre_tool_use(reason: str) -> dict:
    """Build the PreToolUse hook response that vetoes the tool call."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


async def pre_tool_use_hook(input_data, tool_use_id, context):
    """Validate the target path, then capture pre-edit state for the .md
    snapshot/patch substrate.

    Path validation is the security gate: even though `allowed_tools` excludes
    Bash and the agent is told via SKILL.md to stay in the doc tree, a
    successful prompt injection could still try to Edit/Write a sensitive
    file. The hook is the last line of defense before bytes hit disk."""
    tool_input = input_data.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path", "")

    ok, reason = _validate_agent_write_path(file_path)
    if not ok:
        print(f"[pre-edit] REJECTED ({file_path!r}): {reason}", flush=True)
        return _deny_pre_tool_use(reason)

    try:
        p = Path(file_path)
        before_text = p.read_text(encoding="utf-8") if p.exists() else ""
        snap_id = save_snapshot(p, before_text) if before_text else None
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
    """After an Edit/Write to a .md file: validate the post-edit content;
    on validation failure, revert to the pre-edit snapshot and surface the
    errors to the agent via `additionalContext` so it retries inside the
    same turn. On success, detect track-id drift, derive a patch, and
    broadcast doc_changed (via finalize_md_edit)."""
    tool_input = input_data.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path", "")
    if not file_path or not file_path.endswith(".md"):
        return {}

    stash = _pre_edit_state.pop(tool_use_id, None)
    before_text = stash["before_text"] if stash else None
    snap_id = stash["snap_id"] if stash else None
    p = Path(file_path)

    # Validate post-edit content. Empty file (e.g., the edit deleted
    # everything) skips validation — that's a deliberate-looking action,
    # not the kind of corruption the validators are meant to catch.
    try:
        new_text = p.read_text(encoding="utf-8") if p.exists() else ""
    except (OSError, UnicodeDecodeError):
        new_text = ""
    errors = validators.validate_doc(new_text) if new_text.strip() else []

    # Debug capture: when AM_DEBUG_CAPTURE is set, dump every attempted
    # post-edit content keyed by tool_use_id so we can autopsy what the
    # agent actually wrote before the revert. Captured regardless of
    # validation outcome so we see passing edits too.
    if os.environ.get("AM_DEBUG_CAPTURE"):
        try:
            cap_dir = ROOT / "tests" / "_haiku_capture"
            cap_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%H%M%S-%f")
            cap_path = cap_dir / (
                f"{stamp}-{(tool_use_id or 'unknown')[:8]}-"
                f"{'FAIL' if errors else 'OK'}.md"
            )
            cap_path.write_text(new_text, encoding="utf-8")
            if errors:
                err_path = cap_path.with_suffix(".errors.txt")
                err_path.write_text(
                    "\n".join(
                        f"[{e['kind']} @ line {e['line']}] {e['message']}"
                        for e in errors
                    ),
                    encoding="utf-8",
                )
        except OSError as e:
            print(f"[debug-capture] failed: {e}", flush=True)

    if errors:
        # Revert: restore before_text if the file existed pre-edit, or
        # delete it if this was a brand-new file. Skip finalize_md_edit
        # entirely — on-disk state matches what the viewer already has.
        try:
            if before_text:
                with p.open("w", encoding="utf-8", newline="") as f:
                    f.write(before_text)
                action = "reverted"
            else:
                p.unlink(missing_ok=True)
                action = "deleted new file"
            # Log the actual error content so the operator can see what
            # tripped the validator — not just the count. When the agent
            # gets stuck in a retry loop, this is the visibility that
            # tells you whether the validator is catching a real bug or
            # misfiring on valid code.
            print(
                f"[validate] FAILED on {file_path}, {action} "
                f"({len(errors)} err); telling agent",
                flush=True,
            )
            for e in errors:
                print(
                    f"[validate]   {e['kind']} @ line {e['line']}:",
                    flush=True,
                )
                for ln in e["message"].split("\n"):
                    print(f"[validate]     {ln}", flush=True)
        except OSError as e:
            print(f"[validate] revert failed: {e}", flush=True)
        # Retry-cap: track consecutive reverts on this file. After 3 in a
        # row, every further failure tells the agent the loop is stopped
        # — durable until a clean edit lands. Without durability the
        # counter would reset on the cap-fire turn and the agent could
        # burn three more turns before the next cap.
        revert_count = _validate_revert_count.get(file_path, 0) + 1
        _validate_revert_count[file_path] = revert_count
        agent_msg = validators.format_for_agent(errors, file_path)
        if revert_count == 3:
            # First time at the cap — surface to the reader so they know
            # something's going sideways with the agent.
            await state.broadcast({
                "role": "assistant", "type": "text",
                "text": (
                    f"_Validator rejected 3 consecutive edits to "
                    f"`{file_path}`. The retry loop is now paused — the "
                    "agent has been told to stop and report. Most recent "
                    "error:_\n\n```\n" + agent_msg + "\n```"
                ),
            })
        if revert_count >= 3:
            # Tell the agent unambiguously to stop trying and report.
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        f"RETRY LOOP STOPPED. This is failure #{revert_count} "
                        f"on {file_path} in a row. Do NOT attempt another "
                        "Edit on this file in this turn. Reply to the "
                        "reader: explain what you tried to do, paste the "
                        "validator error verbatim, and ask them to "
                        "intervene (clear chat, restore from history, or "
                        "rephrase the request). The next clean edit on "
                        "this file resets the counter.\n\n" + agent_msg
                    ),
                }
            }
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": agent_msg,
            }
        }
    # Clean edit — reset the consecutive-revert counter for this file.
    _validate_revert_count.pop(file_path, None)

    await finalize_md_edit(
        p,
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


def _summarize_tool(name: str, inp: dict | None) -> str:
    """One-line description of a tool_use event for the stdout audit log.

    Covers two distinct event shapes:
    - Claude SDK tool names (Read, Edit, Write, Glob, Grep, Bash, Web*) with
      Claude's flat input dict.
    - Codex CLI item types from `codex exec --json` (command_execution,
      file_change, mcp_tool_call, web_search, plan, plan_update, agent_message)
      where `inp` is the item payload itself.

    Unknown names fall through with a generic payload-key hint.
    """
    if not isinstance(inp, dict):
        inp = {}
    name = name or ""
    # Claude SDK tools
    if name == "Read":      return f"Read     {inp.get('file_path', '?')}"
    if name == "Edit":      return f"Edit     {inp.get('file_path', '?')}"
    if name == "Write":     return f"Write    {inp.get('file_path', '?')}"
    if name == "Glob":      return f"Glob     pattern={inp.get('pattern', '?')!r}"
    if name == "Grep":      return f"Grep     pattern={inp.get('pattern', '?')!r}"
    if name == "Bash":
        cmd = (inp.get("command", "") or "").strip()
        return f"Bash     {cmd[:100]}"
    if name == "WebFetch":  return f"WebFetch {inp.get('url', '?')}"
    if name == "WebSearch": return f"WebSearch {inp.get('query', '?')}"
    # Codex CLI item types
    if name == "command_execution":
        cmd = inp.get("command", "")
        if isinstance(cmd, list):
            cmd = " ".join(str(x) for x in cmd)
        return f"command_execution {str(cmd).strip()[:100]}"
    if name == "file_change":
        changes = inp.get("changes") or []
        paths = [c.get("path", "?") for c in changes if isinstance(c, dict)]
        if not paths:
            return "file_change (no changes)"
        head = paths[0]
        more = f" +{len(paths) - 1}" if len(paths) > 1 else ""
        return f"file_change {head}{more}"
    if name == "mcp_tool_call":
        server = inp.get("server", "?")
        tool = inp.get("tool", "?")
        return f"mcp_tool_call {server}/{tool}"
    if name == "web_search":
        return f"web_search {str(inp.get('query', '')).strip()[:80]}"
    if name in ("plan", "plan_update"):
        text = str(inp.get("text", "")).strip().splitlines()
        head = text[0][:80] if text else ""
        return f"plan      {head}"
    if name == "agent_message":
        # Surfaces in audit log if backend ever logs message items as tools;
        # normally this path isn't hit because agent_message is yielded as
        # a text event, not a tool_use.
        return f"agent_message"
    # Legacy Codex names from earlier CLI versions — keep them recognizable
    # so older Codex builds don't fall through to the generic branch.
    if name in ("apply_patch", "apply_patch_call"):
        return "apply_patch"
    if name in ("shell", "local_shell", "local_shell_call"):
        cmd = inp.get("command", "")
        if isinstance(cmd, list):
            cmd = " ".join(str(x) for x in cmd)
        return f"shell    {str(cmd).strip()[:100]}"
    if name == "function_call":
        return f"function {inp.get('name', '?')}"
    # Unknown tool — surface distinguishing keys from the payload so the
    # audit log isn't useless. Helpful while we're still learning what
    # Codex CLI's JSONL events look like in the wild.
    interesting = [k for k in ("name", "command", "tool", "kind", "path",
                                "file_path", "query", "type", "id")
                   if inp.get(k)]
    if interesting:
        extras = ", ".join(f"{k}={str(inp[k])[:40]!r}" for k in interesting[:3])
        return f"{name:<8s} {extras}"
    return f"{name:<8s} {{...}}"


async def run_turn(text: str):
    async with state.busy:
        if not state.runtime:
            await state.broadcast({"type": "error", "text": "Agent runtime not initialized"})
            return
        # Per-turn audit metrics: prove (in stdout) that the agent is actually
        # making distinct tool calls vs. one-shotting a Write of the whole
        # doc. One [tool] line per tool_use event, one [turn] summary line.
        t0 = time.time()
        tool_count = 0
        cost: float | None = None
        try:
            async for event in state.runtime.run_turn(text):
                etype = event.get("type", "")
                if etype == "tool_use":
                    tool_count += 1
                    # A malformed payload shouldn't abort the turn — the audit
                    # log is a diagnostic, not load-bearing.
                    try:
                        summary = _summarize_tool(
                            event.get("name", ""), event.get("input"),
                        )
                    except Exception as e:
                        summary = (
                            f"{event.get('name', '?')} "
                            f"(summary failed: {type(e).__name__})"
                        )
                    print(f"[tool] {summary}", flush=True)
                elif etype == "turn_done":
                    c = event.get("cost_usd")
                    if isinstance(c, (int, float)):
                        cost = c
                await state.broadcast(event)
        except Exception as e:
            import traceback
            print(f"[turn] FAILED with {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            await state.broadcast({
                "type": "error",
                "text": f"agent error: {type(e).__name__}: {e}",
            })
        finally:
            dt = time.time() - t0
            cost_str = f", cost=${cost:.4f}" if cost is not None else ""
            print(f"[turn] done in {dt:.1f}s, {tool_count} tool call(s){cost_str}",
                  flush=True)


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    _require_localhost_origin(request)
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

                # Augment prompt with doc contents (inlined) + focused blocks.
                # Pre-loading the active doc into context spares the agent a
                # Read tool round-trip every turn — for AM, the doc IS the
                # subject of the conversation, so the agent will Read it
                # almost always. Inlining is amortized by prompt caching
                # across turns where the doc doesn't change. Soft cap of
                # 50 KB; above that, fall back to Read-on-demand.
                INLINE_DOC_CAP = 50 * 1024
                preamble = []
                doc_inlined = False
                if doc:
                    safe_path = _safe_inline_doc_path(doc)
                    doc_text: str | None = None
                    if safe_path is not None:
                        try:
                            doc_text = safe_path.read_text(encoding="utf-8")
                        except (OSError, UnicodeDecodeError):
                            doc_text = None
                    if doc_text is not None and len(doc_text) <= INLINE_DOC_CAP:
                        lines = doc_text.count("\n") + 1
                        preamble.append(
                            f'The reader is viewing "{doc}" ({lines} lines, '
                            f"{len(doc_text)} bytes). When you need to edit, "
                            "this is the file. The current contents are "
                            "inlined below — do NOT call Read on this file "
                            "unless YOU have edited it since this preamble "
                            "(your own edits invalidate the inlined copy):\n\n"
                            f"=== doc:{doc} ===\n{doc_text}\n=== end doc ==="
                        )
                        doc_inlined = True
                    elif doc_text is not None:
                        preamble.append(
                            f'The reader is viewing "{doc}" ('
                            f"{len(doc_text)} bytes — too large to inline). "
                            "When you need to edit, this is the file. Use "
                            "`Read` with `offset`/`limit` to load specific "
                            "ranges rather than the whole file."
                        )
                    else:
                        preamble.append(
                            f'The reader is viewing the document at "{doc}". '
                            "When you need to edit, this is the file."
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
                    surrounding_note = (
                        "Surrounding context is in the inlined doc above — "
                        "no need to Read."
                        if doc_inlined else
                        "If you need surrounding context, Read the source file."
                    )
                    preamble.append(
                        f"The reader has clicked on a specific block, giving it "
                        f"focus ({info_str}).{excerpt_block}\n\n"
                        "When they say 'this', 'here', 'it', 'this section', "
                        "'under it', 'above it' — they mean this focused "
                        f"block. {surrounding_note}"
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
    # Allowlist: a small set of root files, plus markdown docs that live
    # directly under examples/ or docs/ (not docs/raw/ or other subdirs).
    if target.parent == ROOT:
        if target.name not in _STATIC_ROOT_FILES:
            raise web.HTTPNotFound()
    elif target.parent in (EXAMPLES_DIR, DOCS_DIR):
        if target.suffix.lower() != ".md":
            raise web.HTTPNotFound()
    else:
        raise web.HTTPNotFound()
    return web.FileResponse(target)


# ---- Drop-to-upload ------------------------------------------------------
# POST /upload with multipart form, field name "file", .md only. Saves to
# examples/, mints doc_id, broadcasts updated docs list. Returns the
# relative path the client should switch to.
_UPLOAD_MAX_BYTES = 1 * 1024 * 1024  # 1 MB safety cap


async def upload_md(request: web.Request) -> web.Response:
    _require_localhost_origin(request)
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
    # Hard blocklist: extensions we refuse outright even with allow_unknown=1.
    # These are either executable (Windows/Linux), DLL-like, archive containers
    # (the agent can't see inside), or office-with-macros formats. A user can't
    # consent away this list — there's no legitimate "convert .exe to markdown."
    BLOCKED_EXTS = {
        ".exe", ".bat", ".cmd", ".com", ".scr", ".pif",
        ".ps1", ".psm1", ".psd1", ".sh", ".bash", ".zsh", ".fish",
        ".vbs", ".vbe", ".jse", ".wsf", ".wsh", ".hta",
        ".jar", ".class", ".msi", ".msp", ".apk", ".app", ".deb", ".rpm",
        ".dll", ".so", ".dylib", ".sys", ".drv", ".o", ".a", ".lib",
        ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
        ".iso", ".dmg", ".img",
        ".docm", ".xlsm", ".pptm", ".dotm", ".xltm", ".potm",
        ".lnk", ".url", ".desktop",
    }
    allow_unknown = request.query.get("allow_unknown") in ("1", "true", "yes")
    if ext in BLOCKED_EXTS:
        return web.json_response(
            {"error": f"refused: {ext} files are not allowed (executable / archive / binary)",
             "kind": ext, "blocked": True},
            status=415,
        )
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
    # Binary-content guard: text formats don't contain NUL bytes. If the
    # first chunk has any, this is binary data masquerading as a text format
    # (a renamed PDF, a corrupted file, a payload). Refuse before saving —
    # we don't want it on disk and the agent can't usefully read it anyway.
    if b"\x00" in raw[:4096]:
        return web.json_response(
            {"error": "refused: file appears to be binary (NUL bytes in first 4KB). "
                      "Adaptive-markdown only handles text formats.",
             "kind": ext or "(no extension)", "binary": True},
            status=415,
        )
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
    _require_localhost_origin(request)
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
    _require_localhost_origin(request)
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
    _require_localhost_origin(request)
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
    _require_localhost_origin(request)
    doc_param = request.query.get("doc", "").strip()
    doc_path = _doc_path_for(doc_param)
    if doc_path is None:
        return web.json_response({"error": "bad doc path"}, status=400)
    history_zero = _history_zero_path(doc_path)
    if history_zero is None:
        return web.json_response(
            {"error": (
                f"no history snapshot for {doc_path.name} yet — Reset "
                "restores to the state before the first agent edit, but no "
                "agent has touched this doc. For the shipped version, run "
                f"`git checkout {doc_path.relative_to(ROOT).as_posix()}`."
            )},
            status=404,
        )
    _snapshot_if_changed(doc_path)
    doc_path.write_bytes(history_zero.read_bytes())
    rel = doc_path.relative_to(ROOT).as_posix()
    await state.broadcast({"type": "doc_changed", "file": rel})
    print(f"[reset] {rel} <- .history/{doc_path.stem}/{history_zero.name}",
          flush=True)
    snap_id = (history_zero.stem.replace("snap-", "")
               if history_zero.stem != "baseline" else "baseline")
    return web.json_response({
        "path": rel,
        "snap_id": snap_id,
        "ok": True,
    })


def ensure_history_zero() -> None:
    """For every .md under examples/ + docs/, capture a history-0 snapshot
    if no Reset target exists yet. Guarantees the Reset button always has
    something to restore to, even for docs the agent hasn't touched in
    this clone.

    Counts a doc as already having a Reset target if either:
    - a tracked baseline.md is present (the shipped history-0 for examples), or
    - any snap-*.md is present (local pre-edit snapshot history)."""
    minted = 0
    bases = [d for d in (EXAMPLES_DIR, DOCS_DIR) if d.exists()]
    for d in bases:
        for md in d.glob("*.md"):
            hist = _history_dir_for(md)
            if (hist / "baseline.md").exists():
                continue
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
    ensure_doc_ids()
    ensure_history_zero()
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
