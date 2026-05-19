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

import validators
from agent_runtime import DEFAULT_PROVIDER, create_runtime
from agent_runtime.base import AgentRuntime

# Optional: markitdown is used for server-side conversion of binary docs
# (PDF, DOCX, XLSX, PPTX). If it isn't installed, the import endpoint for
# those formats returns 501 rather than crashing the whole backend.
try:
    from markitdown import MarkItDown  # type: ignore
    _MARKITDOWN = MarkItDown()
except Exception:  # pragma: no cover - optional dep
    MarkItDown = None  # type: ignore
    _MARKITDOWN = None


def list_all_docs() -> list[str]:
    """Slugs of every doc — one per docs/<slug>/ folder that has a current.md
    (or a baseline.md awaiting first-run init). Sorted."""
    if not DOCS_ROOT.exists():
        return []
    slugs: list[str] = []
    for sub in DOCS_ROOT.iterdir():
        if not sub.is_dir():
            continue
        if not _DOC_SLUG_RE.match(sub.name):
            continue  # ignore weird names; agents never create them
        if (sub / "current.md").exists() or (sub / "baseline.md").exists():
            slugs.append(sub.name)
    return sorted(slugs)


def _doc_path_for(doc_param: str) -> Path | None:
    """Resolve a doc slug (e.g. 'intro') to its current.md absolute Path.

    Returns None if the slug is malformed or the doc folder doesn't exist.
    The agent only ever writes to current.md; baseline.md is immutable."""
    if not doc_param or not _DOC_SLUG_RE.match(doc_param):
        return None
    p = (DOCS_ROOT / doc_param / "current.md").resolve()
    try:
        p.relative_to(DOCS_ROOT)
    except ValueError:
        return None
    if p.parent.parent != DOCS_ROOT:
        return None
    # current.md may not exist yet on a fresh clone; the boot init creates it
    # from baseline.md. Either way, return the canonical path.
    return p


def _doc_slug_from_path(file_path: str | Path) -> str | None:
    """Inverse of _doc_path_for: given an absolute or ROOT-relative path that
    points at docs/<slug>/current.md, return the slug. Otherwise None."""
    try:
        p = Path(file_path).resolve()
        rel = p.relative_to(DOCS_ROOT)
    except (ValueError, OSError):
        return None
    parts = rel.parts
    if len(parts) != 2 or parts[1] != "current.md":
        return None
    if not _DOC_SLUG_RE.match(parts[0]):
        return None
    return parts[0]


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


DEFAULT_DOC = "intro"  # slug; resolves to docs/intro/current.md

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
    """Ensure every docs/<slug>/current.md has a `doc_id` in its frontmatter."""
    if not DOCS_ROOT.exists():
        return
    minted = 0
    for sub in sorted(DOCS_ROOT.iterdir()):
        if not sub.is_dir() or not _DOC_SLUG_RE.match(sub.name):
            continue
        md = sub / "current.md"
        if not md.exists():
            continue
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
        print(f"  minted doc_id={new_id} for {sub.name}/current.md", flush=True)
    if minted:
        print(f"ensured doc_ids ({minted} new)", flush=True)


def ensure_working_copies() -> None:
    """First-run / fresh-clone init: for every docs/<slug>/baseline.md that
    lacks a sibling current.md, copy baseline.md -> current.md. Ensures the
    viewer always has something to render and the agent always has a file
    to Edit, even on a freshly cloned repo where only baselines are in git."""
    if not DOCS_ROOT.exists():
        return
    created = 0
    for sub in sorted(DOCS_ROOT.iterdir()):
        if not sub.is_dir() or not _DOC_SLUG_RE.match(sub.name):
            continue
        baseline = sub / "baseline.md"
        current = sub / "current.md"
        if baseline.exists() and not current.exists():
            current.write_bytes(baseline.read_bytes())
            created += 1
            print(f"  initialised current.md for {sub.name} from baseline",
                  flush=True)
    if created:
        print(f"ensured working copies ({created} new)", flush=True)


# ---- Block-level tracking IDs (lazy mint on intent-to-reference) --------
# When a chat arrives with a focused block lacking a track_id, mint one
# server-side and write `<!-- id:b-... -->` immediately preceding the block
# in source. Subsequent references use the now-stable ID.
_HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.+?)(?:\s*\{([^}]+)\})?\s*$")
_TRACK_COMMENT_RE = re.compile(r"^\s*<!--\s*id\s*:\s*(b-[A-Z0-9-]+)\s*-->\s*$", re.IGNORECASE)


# Strip inline markdown markers for fuzzy block-matching. Source lines have
# `**bold**` / `_italic_` / `[text](url)` / `` `code` ``; the frontend signature
# is derived from the rendered DOM (which doesn't), so direct string-match
# fails on any formatted block. Math `$...$` is handled separately via the
# am-math-keep `data-source` mechanism on the frontend, so we leave it alone.
_INLINE_MD_STRIP = (
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),
    (re.compile(r"__([^_]+)__"), r"\1"),
    (re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)"), r"\1"),
    (re.compile(r"(?<!_)_([^_\n]+)_(?!_)"), r"\1"),
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),
)


def _strip_inline_markdown(s: str) -> str:
    out = s
    for pat, repl in _INLINE_MD_STRIP:
        out = pat.sub(repl, out)
    return out


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

    # Heading match by text. Compare against both the raw heading text and
    # an inline-markdown-stripped version so a heading like "## **Bold**
    # Title" matches a frontend label of "Bold Title".
    if label:
        for i, line in enumerate(lines):
            m = _HEADING_LINE_RE.match(line)
            if not m:
                continue
            heading_text = m.group(2).strip()
            heading_stripped = _strip_inline_markdown(heading_text)
            for candidate in (heading_text, heading_stripped):
                if candidate == label or candidate.startswith(label[:60]):
                    return i

    # Paragraph / list-item / other prose: match by first ~60 normalized chars.
    # Compare against both raw and inline-markdown-stripped versions of the
    # source line so a paragraph with `**bold**` matches the rendered
    # frontend excerpt that has no asterisks.
    if excerpt:
        sig_first = re.sub(r"\s+", " ", excerpt[:80]).strip()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "<!--", ":::", "```")):
                continue
            for candidate in (stripped, _strip_inline_markdown(stripped)):
                cand_first = re.sub(r"\s+", " ", candidate[:80]).strip()
                if cand_first.startswith(sig_first[:40]):
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


# ---- Snapshot store ------------------------------------------------------
# Every doc folder owns its history: docs/<slug>/baseline.md is the immutable
# history-0 (tracked in git for ship-with docs), docs/<slug>/snaps/snap-*.md
# holds pre-edit captures. The structured "patches" sidecar that earlier
# versions wrote has been retired — diffs can be recomputed from snapshots
# if a review pipeline ever needs them.


def _history_dir_for(doc_path: Path) -> Path:
    """Per-doc snap directory: docs/<slug>/snaps/.

    Caller passes the .../current.md (or .../baseline.md) — we use parent."""
    return doc_path.parent / "snaps"


def _history_zero_path(doc_path: Path) -> Path | None:
    """Locate the history-0 snapshot — always docs/<slug>/baseline.md.

    No more fallback to oldest snap: baseline.md is the canonical Reset
    target. If it's missing on a user-created doc, the user needs to
    explicitly establish a baseline (future "save as baseline" action)."""
    baseline = doc_path.parent / "baseline.md"
    return baseline if baseline.exists() else None


def save_snapshot(doc_path: Path, text: str) -> str:
    """Save text as a snapshot under docs/<slug>/snaps/, returning the id."""
    d = _history_dir_for(doc_path)
    d.mkdir(parents=True, exist_ok=True)
    # gen_id("snap") already prefixes "snap-" — don't double-prefix.
    snap_id = gen_id("snap")
    (d / f"{snap_id}.md").write_text(text, encoding="utf-8")
    return snap_id


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


def _find_block_span(
    lines: list[str], signature: dict,
) -> tuple[int, int, str] | None:
    """Locate (start_line, end_line_exclusive, kind) for the block matching
    `signature`. Returns None if the block can't be located unambiguously.

    Supported kinds (MVP): 'heading' (one line), 'paragraph' (consecutive
    non-blank, non-special lines). Other block types (list, blockquote,
    fenced code, table, HTML block) return None so the caller refuses
    inline edit and the reader uses Source view for those.
    """
    idx = _find_block_line(lines, signature)
    if idx is None:
        return None
    line = lines[idx]
    stripped = line.strip()
    if not stripped:
        return None
    if _HEADING_LINE_RE.match(line):
        return (idx, idx + 1, "heading")
    # Refuse non-paragraph blocks: lists, fenced code, tables, HTML blocks,
    # blockquotes, deprecated directives. Source view handles those.
    refuse_prefixes = ("-", "*", "+", ">", "|", "<!--", "<", "```", ":::", "~~~")
    if stripped.startswith(refuse_prefixes):
        return None
    if re.match(r"^\d+\.\s", stripped):  # numbered list
        return None
    # Paragraph: consume consecutive non-blank, non-special lines.
    end = idx + 1
    while end < len(lines):
        nxt = lines[end].strip()
        if not nxt:
            break
        if nxt.startswith(refuse_prefixes) or _HEADING_LINE_RE.match(lines[end]):
            break
        if re.match(r"^\d+\.\s", nxt):
            break
        end += 1
    return (idx, end, "paragraph")


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


# ---- Pending-changes substrate (phase 1: disk shape only) ----------------
# Per-doc sidecar `docs/<slug>/pending.json` holding proposed edits the
# reader hasn't accepted yet. This file's helpers are the storage layer;
# nothing in the agent or viewer wires into them yet. Phases 2+ (PreToolUse
# routing, preamble pending-apply, renderer overlay, Accept/Reject UI,
# human-inline-edit-rejects-pending) layer on top. See ROADMAP > Open
# design questions > "Pending-changes substrate" for the full design.
#
# Entry schema (one object per Edit / Write tool call):
#   {
#     "id": "pe-<ulid>",                # stable; assigned on add
#     "tool_use_id": <opaque agent id>, # for cross-reference with chat log
#     "block": {                         # block signature, same shape as
#       "track_id": ...,                 # the selection / edit-block API
#       "anchor_id": ...,
#       "label": ...,
#       "excerpt": ...,
#     },
#     "old_text": <pre-edit source>,
#     "new_text": <proposed source>,
#     "agent_label": "<provider>:<model>",
#     "created_at": "<iso8601>",
#   }
#
# File-level shape:
#   { "version": 1, "doc": <slug>, "edits": [<entry>, ...] }

_PENDING_SCHEMA_VERSION = 1
_PENDING_REQUIRED_FIELDS = ("tool_use_id", "block", "old_text", "new_text",
                            "agent_label")


def _pending_path(slug: str) -> Path | None:
    """Return the pending sidecar path for a doc slug, or None if the slug
    is malformed or the doc directory doesn't exist."""
    if not slug or not _DOC_SLUG_RE.match(slug):
        return None
    doc_dir = DOCS_ROOT / slug
    if not doc_dir.is_dir():
        return None
    return doc_dir / "pending.json"


def _empty_pending(slug: str) -> dict:
    return {
        "version": _PENDING_SCHEMA_VERSION,
        "doc": slug,
        "edits": [],
    }


def load_pending(slug: str) -> dict:
    """Read pending.json for a doc. Returns an empty-shaped dict (with the
    schema version and an empty edits list) when the sidecar is missing
    or unreadable — callers don't need to special-case "no pending yet."""
    p = _pending_path(slug)
    if p is None or not p.exists():
        return _empty_pending(slug)
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[pending] failed to load {p}: {e!r}", flush=True)
        return _empty_pending(slug)
    if not isinstance(data, dict):
        return _empty_pending(slug)
    # Don't trust the file blindly; coerce missing or wrong-typed fields
    # back to the default shape rather than letting downstream code crash.
    data.setdefault("version", _PENDING_SCHEMA_VERSION)
    data.setdefault("doc", slug)
    edits = data.get("edits")
    if not isinstance(edits, list):
        edits = []
    data["edits"] = [e for e in edits if isinstance(e, dict)]
    return data


def save_pending(slug: str, data: dict) -> None:
    """Write pending.json for a doc. Raises ValueError if the slug is bad
    or the doc directory doesn't exist (callers should `load_pending`
    first to get a valid shape rather than constructing dicts by hand)."""
    p = _pending_path(slug)
    if p is None:
        raise ValueError(f"pending: doc {slug!r} does not exist")
    # newline="" suppresses Python's text-mode CRLF translation on Windows
    # so the on-disk JSON is byte-stable across platforms.
    with p.open("w", encoding="utf-8", newline="") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def clear_pending(slug: str) -> bool:
    """Delete the pending sidecar entirely. Returns True if a file was
    removed, False if no sidecar existed."""
    p = _pending_path(slug)
    if p is None or not p.exists():
        return False
    p.unlink(missing_ok=True)
    return True


def _pending_block_key(block: dict) -> str:
    """Stable key for a block signature, used to detect "same block" when
    deciding whether to replace an existing pending entry vs stack a new
    one. Mirrors keyOf() on the iframe side: kind > track_id > anchor_id
    > text excerpt prefix.

    The `kind: "doc"` key is the v0 whole-file granularity used by the
    PostToolUse hook — every Edit/Write to a doc lands as a single
    pending entry per doc, replacing the previous one. Sub-block keys
    come in once we decompose edits by block in phase 3+.
    """
    if not isinstance(block, dict):
        return ""
    kind = block.get("kind")
    if kind == "doc":
        return "doc"
    track = block.get("track_id")
    if track:
        return f"t:{track}"
    anchor = block.get("anchor_id")
    if anchor:
        return f"a:{anchor}"
    excerpt = (block.get("excerpt") or "")[:200]
    return f"x:{excerpt}"


_REVIEW_MODE_TRUTHY = frozenset({
    "pending", "review", "on", "true", "yes", "1",
})


def _read_review_mode(doc_path: Path) -> bool:
    """Return True if the doc's frontmatter declares review_mode is on.

    Per the design, review mode is per-doc — a paper-in-review is marked
    once and stays that way across fresh chats, instead of forcing the
    reader to remember per-session toggles. Frontmatter key:
        review_mode: pending
    Truthy values: pending / review / on / true / yes / 1. Anything else
    (or missing) is off.
    """
    try:
        text = doc_path.read_text(encoding="utf-8") if doc_path.exists() else ""
    except (OSError, UnicodeDecodeError):
        return False
    if not text:
        return False
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return False
    fm_body = m.group(1)
    for raw_line in fm_body.splitlines():
        line = raw_line.strip()
        if not line.startswith("review_mode"):
            continue
        if ":" not in line:
            continue
        value = line.split(":", 1)[1].strip().strip('"').strip("'").lower()
        return value in _REVIEW_MODE_TRUTHY
    return False


def add_pending_edit(slug: str, entry: dict) -> str:
    """Append (or replace) a pending edit for the doc. Returns the entry id.

    Per the design, successive agent proposals on the same block REPLACE
    rather than stack — otherwise the accept/reject UI has to expose a
    stack which is more cognitive load than it's worth in v0. Block
    identity comes from the signature via _pending_block_key.

    Replacement semantics: the new entry inherits the ORIGINAL `old_text`
    from the existing same-block entry — successive proposals on a block
    update only the proposed `new_text`. Otherwise rejection couldn't
    walk back to the true pre-pending state for that block (you'd revert
    only to the previous proposal, not to the source-of-truth before
    pending mode started touching this block).

    Raises ValueError on missing required fields or a bad slug.
    """
    missing = [f for f in _PENDING_REQUIRED_FIELDS if f not in entry]
    if missing:
        raise ValueError(f"pending edit missing fields: {missing}")
    if not isinstance(entry.get("block"), dict):
        raise ValueError("pending edit 'block' must be a dict signature")
    data = load_pending(slug)
    edit_id = entry.get("id") or gen_id("pe")
    new_entry = dict(entry)
    new_entry["id"] = edit_id
    new_entry.setdefault(
        "created_at",
        datetime.now(timezone.utc).isoformat(),
    )
    key = _pending_block_key(new_entry["block"])
    existing_same_block = [
        e for e in data["edits"]
        if _pending_block_key(e.get("block", {})) == key
    ]
    if existing_same_block:
        prior = existing_same_block[0]
        new_entry["old_text"] = prior.get("old_text", new_entry["old_text"])
        # Stable created_at across refinements so the UI doesn't see the
        # entry "jump to the front" every time the agent refines it.
        if "created_at" in prior:
            new_entry["created_at"] = prior["created_at"]
    data["edits"] = [
        e for e in data["edits"]
        if _pending_block_key(e.get("block", {})) != key
    ]
    data["edits"].append(new_entry)
    save_pending(slug, data)
    return edit_id


def remove_pending_edit(slug: str, edit_id: str) -> bool:
    """Remove a pending edit by id. Returns True if removed; False if no
    matching entry. If removing the last entry, clears the sidecar."""
    data = load_pending(slug)
    before = len(data["edits"])
    data["edits"] = [e for e in data["edits"] if e.get("id") != edit_id]
    if len(data["edits"]) == before:
        return False
    if data["edits"]:
        save_pending(slug, data)
    else:
        clear_pending(slug)
    return True


def find_pending_for_block(slug: str, block: dict) -> dict | None:
    """Return the pending entry whose block signature matches `block`,
    or None if there's no pending edit for that block. Used by the
    eventual "human inline edit rejects matching pending" interaction."""
    target_key = _pending_block_key(block)
    if not target_key:
        return None
    data = load_pending(slug)
    for e in data["edits"]:
        if _pending_block_key(e.get("block", {})) == target_key:
            return e
    return None


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


# Per-tool-call stash so post-edit hook can diff for drift + derive patches.
# Keyed by tool_use_id; value: {"file_path": str, "before_text": str, "snap_id": str}
_pre_edit_state: dict[str, dict] = {}
# Per-file consecutive-revert counter for the validator retry-cap.
_validate_revert_count: dict[str, int] = {}


def _safe_inline_doc_slug(slug: str) -> Path | None:
    """Resolve a chat-context doc slug to the absolute path of its
    current.md iff it's safe to inline into the agent's preamble.

    Read-side counterpart to `_validate_agent_write_path`. The chat
    context comes from the client, so a malicious or buggy front-end
    could otherwise coax the backend into reading arbitrary files."""
    p = _doc_path_for(slug)
    if p is None or not p.is_file():
        return None
    return p


def _validate_agent_write_path(file_path: str) -> tuple[bool, str]:
    """Decide whether the agent is allowed to Edit/Write a given path.

    The only writable target is `docs/<slug>/current.md`. Anything else —
    baseline.md (the immutable history-0), snaps/, project source files,
    dotfiles, absolute paths outside ROOT — is rejected. This is the
    root-cause defense against a successful prompt injection convincing
    the agent to clobber files outside its lane."""
    if not file_path:
        return False, "missing file_path"
    try:
        p = Path(file_path).resolve()
    except (OSError, ValueError):
        return False, f"invalid file_path: {file_path!r}"
    try:
        p.relative_to(DOCS_ROOT)
    except ValueError:
        return False, (
            f"writes outside docs/ are not permitted ({file_path!r}). "
            "The agent may only modify docs/<slug>/current.md."
        )
    if p.name != "current.md":
        return False, (
            f"only current.md is writable by the agent ({p.name!r}). "
            "baseline.md is immutable (history-0); snaps/ is managed "
            "by the backend; project files are off-limits."
        )
    if p.parent.parent != DOCS_ROOT:
        return False, (
            f"writable paths must be docs/<slug>/current.md, not "
            f"{file_path!r}. Slugs are direct children of docs/."
        )
    if not _DOC_SLUG_RE.match(p.parent.name):
        return False, (
            f"doc slug {p.parent.name!r} is not a valid name. Slugs must "
            "match [a-zA-Z0-9][a-zA-Z0-9-]{0,63}."
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

    Path validation is the security gate against a successful prompt
    injection convincing the agent to clobber a file outside its lane —
    the hook is the last line of defense before bytes hit disk."""
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


UNSAFE_BASH = os.environ.get("AM_UNSAFE_BASH") == "1"


async def pre_bash_hook(input_data, tool_use_id, context):
    """Windows fallback: rewrite the agent's Bash command to run through
    `python -m sandbox`, which pins cwd to the active doc folder, curates
    env, and enforces a timeout. No-op on macOS/Linux/WSL2 where the CLI
    binary's real sandbox is in play.

    When the launcher passes `--unsafe-bash` (sets AM_UNSAFE_BASH=1),
    the wrap is also skipped — the agent's command runs with the
    backend's full env, network, and filesystem. Out-of-band activation
    only: nothing the agent can do in chat enables this.

    The hook rejects the call outright if no doc is active (defensive —
    every chat turn pins `state.current_doc_slug` before scheduling, so
    this should not normally happen)."""
    if os.name != "nt":
        return {}  # SDK sandbox handles enforcement on Unix-y systems

    if UNSAFE_BASH:
        return {}  # operator opted in at launch; let the command through

    tool_input = input_data.get("tool_input", {}) or {}
    cmd = tool_input.get("command", "")
    if not cmd:
        return {}

    slug = state.current_doc_slug
    if not slug:
        return _deny_pre_tool_use(
            "Bash refused: no active doc folder to scope the sandbox to. "
            "Open a doc first."
        )

    wrapped = (
        f'"{sys.executable}" -m sandbox '
        f'--slug "{slug}" --timeout 30 --cmd {json.dumps(cmd)}'
    )
    print(f"[pre-bash] wrapping cmd in sandbox (slug={slug!r})", flush=True)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {**tool_input, "command": wrapped},
        }
    }


async def finalize_md_edit(
    doc_path: Path,
    before_text: str | None,
    snap_id: str | None,
    author: str,
) -> None:
    """Shared post-edit substrate work — used by both the Claude post-hook
    (per Edit/Write) and the Codex per-turn finalizer.

    - If we have before_text but no snap_id, save a fresh snapshot now.
      (Claude's pre-hook already snapshotted; Codex snapshots post-turn.)
    - Detect any track-ID drift the agent may have introduced.
    - Broadcast doc_changed (by slug); refresh the doc list if this is a
      newly-visible doc.
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

    slug = _doc_slug_from_path(doc_path)
    if slug:
        await state.broadcast({"type": "doc_changed", "doc": slug})
        # New doc visible to the doc list? Refresh.
        await state.broadcast({"type": "docs", "list": list_all_docs(), "doc": slug})


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

    # Pending-changes substrate (phase 2): if the doc declares
    # `review_mode: pending` in its frontmatter, record this edit as a
    # pending entry that the reader can later Accept (keep) or Reject
    # (revert to old_text). The bytes still land on disk normally — the
    # design's "current.md doesn't move" framing is dropped in favor of
    # "bytes land, pending tracks revocability" so successive Edits in a
    # turn see each other's effects and the SDK doesn't break.
    #
    # MVP granularity: one whole-file entry per doc, replacing on each
    # subsequent edit. Phase 3+ will decompose into per-block entries.
    if _read_review_mode(p):
        slug = _doc_slug_from_path(p)
        if slug:
            try:
                add_pending_edit(slug, {
                    "tool_use_id": tool_use_id or "",
                    "block": {"kind": "doc"},
                    "old_text": before_text or "",
                    "new_text": new_text,
                    "agent_label": (
                        f"{state.current_provider}:{state.current_model}"
                    ),
                })
                print(
                    f"[pending] recorded edit on docs/{slug}/current.md "
                    f"(review_mode=on)",
                    flush=True,
                )
            except Exception as e:
                # Pending-recording is non-load-bearing — a failure here
                # shouldn't abort the edit. The bytes are already on disk.
                print(f"[pending] failed to record: {e!r}", flush=True)

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
        pre_bash_hook=pre_bash_hook,
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
        cancelled = False
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
        except asyncio.CancelledError:
            # Reader hit /cancel. Tell the UI, mark the turn done, and
            # propagate so the surrounding task transitions to cancelled.
            cancelled = True
            print("[turn] cancelled by reader", flush=True)
            await state.broadcast({
                "role": "assistant", "type": "text",
                "text": "_Turn cancelled by reader._",
            })
            await state.broadcast({"type": "turn_done", "cancelled": True})
            raise
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
            tag = " (cancelled)" if cancelled else ""
            print(f"[turn] done in {dt:.1f}s, {tool_count} tool call(s){cost_str}{tag}",
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
                # Insertion is the "click in a gap between blocks" affordance.
                # Mutually exclusive with selections client-side.
                insertion = ctx.get("insertion")
                if not isinstance(insertion, dict):
                    insertion = None

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
                    await state.broadcast({"type": "doc_changed", "doc": doc})

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
                    safe_path = _safe_inline_doc_slug(doc)
                    doc_text: str | None = None
                    if safe_path is not None:
                        try:
                            doc_text = safe_path.read_text(encoding="utf-8")
                        except (OSError, UnicodeDecodeError):
                            doc_text = None
                    rel_for_agent = (
                        safe_path.relative_to(ROOT).as_posix()
                        if safe_path is not None else f"docs/{doc}/current.md"
                    )
                    if doc_text is not None and len(doc_text) <= INLINE_DOC_CAP:
                        lines = doc_text.count("\n") + 1
                        preamble.append(
                            f'The reader is viewing "{doc}" — file path '
                            f'`{rel_for_agent}` ({lines} lines, '
                            f"{len(doc_text)} bytes). When you need to edit, "
                            "this is the file. The current contents are "
                            "inlined below — do NOT call Read on this file "
                            "unless YOU have edited it since this preamble "
                            "(your own edits invalidate the inlined copy):\n\n"
                            f"=== doc:{rel_for_agent} ===\n{doc_text}\n=== end doc ==="
                        )
                        doc_inlined = True
                        # Per-doc agent skill: any section in the body with
                        # class="agent-skill" is meta authored for you, not
                        # for the reader (the viewer hides it via CSS).
                        # Surface it explicitly so you don't gloss over it
                        # as "just a block in the doc."
                        if 'class="agent-skill"' in doc_text \
                                or "class='agent-skill'" in doc_text:
                            preamble.append(
                                "This doc carries one or more "
                                "`<section class=\"agent-skill\">…</section>` "
                                "blocks in its body. Those sections are the "
                                "doc's working contract — voice, formatting, "
                                "structural conventions specific to this doc. "
                                "They override generic guidance in the global "
                                "adaptive-markdown skill when they conflict. "
                                "Treat them as authoritative; preserve them "
                                "across edits unless the reader explicitly "
                                "asks you to change them."
                            )
                    elif doc_text is not None:
                        preamble.append(
                            f'The reader is viewing "{doc}" — file path '
                            f"`{rel_for_agent}` ({len(doc_text)} bytes — "
                            "too large to inline). When you need to edit, "
                            "this is the file. Use `Read` with `offset`/"
                            "`limit` to load specific ranges rather than "
                            "the whole file."
                        )
                    else:
                        preamble.append(
                            f'The reader is viewing "{doc}" — file path '
                            f"`{rel_for_agent}`. When you need to edit, "
                            "this is the file."
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
                elif insertion is not None:
                    before = insertion.get("before") if isinstance(insertion.get("before"), dict) else None
                    after = insertion.get("after") if isinstance(insertion.get("after"), dict) else None

                    def _fmt(b: dict | None, role: str) -> str:
                        if not b:
                            return f"  - {role}: (none — {'top' if role == 'block-before' else 'end'} of doc)"
                        bits = []
                        if b.get("id"):    bits.append(f'id="{b["id"]}"')
                        if b.get("label"): bits.append(f'label="{b["label"]}"')
                        ex = (b.get("excerpt") or "").strip()
                        if len(ex) > 240:
                            ex = ex[:240] + "…"
                        info = ", ".join(bits) if bits else "(unlabeled block)"
                        ex_line = f"\n    excerpt: ```{ex}```" if ex else ""
                        return f"  - {role}: {info}{ex_line}"

                    preamble.append(
                        "The reader has chosen an INSERTION POINT (not a "
                        "block selection). New content should be inserted "
                        "into the doc at this gap:\n\n"
                        + _fmt(before, "block-before") + "\n"
                        + _fmt(after, "block-after") + "\n\n"
                        "When they say 'insert here', 'add here', 'put it "
                        "here', they mean this gap. In your Edit, use the "
                        "block immediately before OR after as the "
                        "`old_string` anchor — match that block verbatim, "
                        "then put the new content directly before or after "
                        "it in `new_string`. Do not modify the surrounding "
                        "blocks; only insert between them. If the request "
                        "is also for content that conceptually belongs "
                        "elsewhere (e.g. 'add it at the top of the doc'), "
                        "honor the explicit instruction over this gap."
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
                print(f"[ws] chat doc={doc!r} selections={len(selections)}"
                      f"{' insertion' if insertion else ''}",
                      flush=True)
                # Pin the sandbox cwd to this turn's doc folder. The
                # PreToolUse Bash hook reads this when wrapping the
                # agent's command through `python -m sandbox`. Set BEFORE
                # the task is scheduled so the hook never sees a stale
                # (or None) slug from a previous turn.
                state.current_doc_slug = doc if isinstance(doc, str) else None
                turn_task = asyncio.create_task(run_turn(prompt))
                state.current_turn_task = turn_task

                def _clear_turn_task(t, _tt=turn_task):
                    if state.current_turn_task is _tt:
                        state.current_turn_task = None
                    state.current_doc_slug = None
                turn_task.add_done_callback(_clear_turn_task)

            elif data.get("type") == "cancel":
                # /cancel from the reader — interrupt the in-flight turn.
                # state.busy is held by run_turn for the whole turn, so this
                # is the only way to free the agent without restarting.
                t = state.current_turn_task
                if t and not t.done():
                    print("[ws] cancel requested", flush=True)
                    t.cancel()
                else:
                    await state.broadcast({
                        "role": "assistant", "type": "text",
                        "text": "_Nothing to cancel — no turn is running._",
                    })

            elif data.get("type") == "new_chat":
                model = data.get("model")
                asyncio.create_task(reset_runtime_session(model))

    finally:
        async with state.client_lock:
            state.clients.discard(ws)
    return ws


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


# ---- Drop-to-upload ------------------------------------------------------
# POST /upload with multipart form, field name "file", .md only. Saves to
# examples/, mints doc_id, broadcasts updated docs list. Returns the
# relative path the client should switch to.
_UPLOAD_MAX_BYTES = 1 * 1024 * 1024  # 1 MB safety cap for text imports
_ASSET_MAX_BYTES = 25 * 1024 * 1024  # 25 MB cap for asset drops (images, audio, etc.)
_BINARY_CONVERT_MAX_BYTES = 25 * 1024 * 1024  # 25 MB cap for PDF/DOCX/... imports

# Binary doc formats we convert server-side. These don't go through the
# text-flow's NUL-byte guard / UTF-8 decode; they're saved as `original.<ext>`
# and the converter writes the resulting markdown to `current.md` +
# `baseline.md`. Reader gets a fully-converted doc back — the agent is not
# in the loop for the conversion itself.
_BINARY_CONVERT_EXTS = {".pdf", ".docx", ".pptx", ".xlsx"}

# PDF conversion: Claude vision is the default (dramatically better at
# preserving headings, lists, math). Markitdown is the offline fallback.
# AM_PDF_BACKEND env var: "auto" (default — Claude, fall back to markitdown),
# "claude" (Claude only, fail loud), "markitdown" (skip Claude entirely).
_PDF_CLAUDE_MAX_BYTES = 30 * 1024 * 1024  # margin under Anthropic's 32MB limit
_PDF_CLAUDE_MODEL = "claude-sonnet-4-5"
_PDF_CLAUDE_MAX_TOKENS = 16384

_PDF_CONVERT_PROMPT = (
    "Convert this PDF to adaptive-markdown. Strict rules:\n"
    "- Use `#` for the document title, `##` for top-level sections, "
    "`###` for subsections.\n"
    "- Use markdown bullet lists (`- `) for any list of items in the source.\n"
    "- Preserve inline emphasis: bold becomes `**...**`, italic becomes "
    "`_..._`.\n"
    "- Preserve math: inline as `$...$`, display as `$$...$$`. If the source "
    "has expressions written as plain text (e.g. `x(t+1) = f(x(t))`), render "
    "them as math.\n"
    "- Tables become GitHub-flavored markdown tables.\n"
    "- Do NOT add commentary, summary, or any preamble. Output ONLY the "
    "markdown body of the document.\n"
    "- Preserve all paragraphs verbatim where possible; do not paraphrase."
)

# LaTeX-source conversion uses the same Claude path as PDF but takes the
# .tex content as text rather than as a rasterized document. Same model,
# same budget, same fallback behavior (returns None on no-key / oversize /
# API failure so the caller can fall through to the agent-mediated path).
_TEX_CLAUDE_MAX_BYTES = 500 * 1024  # 500KB of source — plenty for one paper
_LLM_CONVERT_TEXT_EXTS = {".tex"}

_TEX_CONVERT_PROMPT = (
    "Convert this LaTeX source to adaptive-markdown. Strict rules:\n"
    "- Drop the preamble (`\\documentclass`, `\\usepackage`, `\\newcommand`, "
    "etc.). Custom macros that are actually used in the body should be "
    "expanded inline.\n"
    "- `\\title{...}` → `#`. `\\section` → `##`. `\\subsection` → `###`. "
    "`\\subsubsection` → `####`.\n"
    "- Preserve math: inline as `$...$`, display as `$$...$$`. KaTeX-"
    "compatible syntax. Convert `\\[...\\]` → `$$...$$` and `\\(...\\)` → "
    "`$...$`.\n"
    "- `\\begin{itemize}` → markdown `- ` bullet list. `\\begin{enumerate}` "
    "→ markdown `1.` numbered list.\n"
    "- amsthm environments (`\\begin{theorem}`, `\\begin{lemma}`, "
    "`\\begin{proposition}`, `\\begin{corollary}`, `\\begin{definition}`, "
    "`\\begin{example}`, `\\begin{proof}`) → "
    "`<section class=\"<kind>\">...</section>` with an inner `<h3>` if the "
    "source had a name in `[...]`.\n"
    "- `\\begin{figure}` → `<figure>...</figure>`. `\\caption{...}` → "
    "`<figcaption>...</figcaption>`. TikZ source stays as ```tikz fenced "
    "blocks.\n"
    "- `\\begin{tabular}` → GitHub-flavored markdown table.\n"
    "- `\\textbf{...}` → `**...**`. `\\emph{...}` / `\\textit{...}` → "
    "`_..._`. `\\texttt{...}` → backtick-quoted code.\n"
    "- `\\cite{key}` → `[key]`. `\\ref{x}` → drop the ref-target ID but "
    "keep the surrounding sentence intact.\n"
    "- Strip `%`-comment lines.\n"
    "- Do NOT add commentary or preamble. Output ONLY the markdown body.\n"
    "- Preserve all content. Do not paraphrase. Do not summarize."
)


async def _convert_tex_via_claude(source_text: str) -> str | None:
    """Send LaTeX source to Claude and ask for adaptive-markdown back.
    Returns the converted text on success, or None if Claude is
    unavailable / failed (caller falls back to agent-mediated conversion)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[upload:tex:claude] no ANTHROPIC_API_KEY — skipping Claude path",
              flush=True)
        return None
    enc = source_text.encode("utf-8")
    if len(enc) > _TEX_CLAUDE_MAX_BYTES:
        print(f"[upload:tex:claude] source too large ({len(enc)}B > "
              f"{_TEX_CLAUDE_MAX_BYTES}B) — skipping Claude path", flush=True)
        return None

    def _call() -> str | None:
        import json as _json
        import urllib.request
        import urllib.error
        body = {
            "model": _PDF_CLAUDE_MODEL,
            "max_tokens": _PDF_CLAUDE_MAX_TOKENS,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "--- LaTeX source ---\n"
                            + source_text
                            + "\n--- end source ---\n\n"
                            + _TEX_CONVERT_PROMPT
                        ),
                    },
                ],
            }],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=_json.dumps(body).encode("utf-8"),
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                payload = _json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:500]
            print(f"[upload:tex:claude] HTTP {e.code}: {err}", flush=True)
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"[upload:tex:claude] network error: {e!r}", flush=True)
            return None
        parts = payload.get("content", []) or []
        text = "\n".join(
            p.get("text", "") for p in parts if p.get("type") == "text"
        ).strip()
        return text or None

    return await asyncio.to_thread(_call)


async def _convert_pdf_via_claude(pdf_bytes: bytes) -> str | None:
    """Send a PDF to Claude and ask for adaptive-markdown back. Returns the
    converted text on success, or None if Claude is unavailable / failed
    (caller should fall back to markitdown)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[upload:pdf:claude] no ANTHROPIC_API_KEY — skipping Claude path",
              flush=True)
        return None
    if len(pdf_bytes) > _PDF_CLAUDE_MAX_BYTES:
        print(f"[upload:pdf:claude] PDF too large ({len(pdf_bytes)}B > "
              f"{_PDF_CLAUDE_MAX_BYTES}B) — skipping Claude path", flush=True)
        return None

    def _call() -> str | None:
        import base64
        import json as _json
        import urllib.request
        import urllib.error
        body = {
            "model": _PDF_CLAUDE_MODEL,
            "max_tokens": _PDF_CLAUDE_MAX_TOKENS,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.b64encode(pdf_bytes).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": _PDF_CONVERT_PROMPT},
                ],
            }],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=_json.dumps(body).encode("utf-8"),
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                payload = _json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:500]
            print(f"[upload:pdf:claude] HTTP {e.code}: {err}", flush=True)
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"[upload:pdf:claude] network error: {e!r}", flush=True)
            return None
        parts = payload.get("content", []) or []
        text = "\n".join(
            p.get("text", "") for p in parts if p.get("type") == "text"
        ).strip()
        return text or None

    return await asyncio.to_thread(_call)

# Extensions that the doc-area drop UX treats as "asset" (lands under
# docs/<slug>/assets/) rather than as a new-doc candidate. Anything not in
# this set falls through to the existing new-doc / convert flow.
_ASSET_EXTS = {
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif", ".bmp", ".ico",
    # Audio
    ".mp3", ".ogg", ".wav", ".flac", ".m4a", ".aac",
    # Video
    ".mp4", ".webm", ".mov", ".mkv",
    # Data (small, opaque-to-the-agent-but-script-readable)
    ".csv", ".json", ".parquet",
}


# Music file imports: .abc / .musicxml are text source we just wrap in a
# <figure class="music"> block; .mid / .midi are binary, saved as an
# asset that a <midi-player> references. All renderers lazy-load their
# CDN library only when a doc contains music, so zero cost when not used.
_MUSIC_TEXT_EXTS = {".abc", ".musicxml", ".mxl", ".xml"}
_MUSIC_BINARY_EXTS = {".mid", ".midi"}
_MUSIC_TEXT_MAX_BYTES = 2 * 1024 * 1024   # 2MB cap on music source text
_MUSIC_BINARY_MAX_BYTES = 5 * 1024 * 1024  # 5MB cap on MIDI binaries


def _music_inner_div_class(ext: str) -> str:
    """Map the upload extension to the inner div class the iframe runtime
    recognizes. .xml is treated as MusicXML when accompanied by an
    .mxl/.musicxml-style structure — there's no portable way to tell from
    extension alone, so we default to musicxml for .xml in this music
    upload path."""
    if ext == ".abc":
        return "abc"
    return "musicxml"


async def _upload_music_doc(
    request: web.Request,
    field: "web.BodyPartReader",
    raw_name: str,
    ext: str,
) -> web.Response:
    """Drop a .abc / .musicxml / .mid file onto +Doc and you get a new
    doc whose body is a single `<figure class="music">` block. The iframe
    runtime renders it via abcjs / OSMD / html-midi-player on first view."""
    is_binary = ext in _MUSIC_BINARY_EXTS
    cap = _MUSIC_BINARY_MAX_BYTES if is_binary else _MUSIC_TEXT_MAX_BYTES

    total = 0
    chunks: list[bytes] = []
    while True:
        chunk = await field.read_chunk(size=64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            return web.json_response(
                {"error": f"file too large (max {cap // (1024 * 1024)}MB "
                          f"for {ext} imports)"},
                status=413,
            )
        chunks.append(chunk)
    raw = b"".join(chunks)
    if total == 0:
        return web.json_response({"error": "empty file"}, status=400)

    # Slug derivation matches the rest of the upload paths.
    raw_stem = Path(raw_name).stem
    slug_base = re.sub(r"[^a-z0-9-]+", "-", raw_stem.lower()).strip("-")
    if not slug_base or not _DOC_SLUG_RE.match(slug_base):
        slug_base = "music"
    slug = slug_base
    counter = 1
    while (DOCS_ROOT / slug).exists():
        counter += 1
        slug = f"{slug_base}-{counter}"
    doc_dir = DOCS_ROOT / slug
    doc_dir.mkdir(parents=True, exist_ok=True)
    title = raw_stem

    if is_binary:
        # MIDI: save as an asset, reference via <midi-player>.
        assets_dir = doc_dir / "assets"
        assets_dir.mkdir(exist_ok=True)
        # Sanitize the original filename — same allowlist as upload_asset.
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(raw_name).name)
        if not safe_name.lower().endswith(ext):
            safe_name = f"score{ext}"
        (assets_dir / safe_name).write_bytes(raw)
        body_md = (
            f'---\ntitle: "{title}"\n---\n\n'
            f"# {title}\n\n"
            '<figure class="music">\n'
            f'<midi-player src="assets/{safe_name}" sound-font></midi-player>\n'
            "<figcaption>MIDI playback — synthesized in-browser.</figcaption>\n"
            "</figure>\n"
        )
    else:
        # .mxl is compressed MusicXML (zip with the .musicxml inside).
        # Extract the main score file so we can wrap it in a script tag
        # like a regular .musicxml upload.
        if ext == ".mxl":
            import io
            import zipfile
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    # META-INF/container.xml points at the main score,
                    # but for v0 just pick the first non-META-INF .xml /
                    # .musicxml entry — works for ~all real-world .mxl files.
                    score_name = None
                    for name in zf.namelist():
                        if name.startswith("META-INF/"):
                            continue
                        if name.lower().endswith((".xml", ".musicxml")):
                            score_name = name
                            break
                    if score_name is None:
                        return web.json_response(
                            {"error": "no .xml / .musicxml entry inside "
                                      "the .mxl archive"},
                            status=422,
                        )
                    raw = zf.read(score_name)
            except (zipfile.BadZipFile, OSError) as e:
                return web.json_response(
                    {"error": f"could not read .mxl archive: {e}"},
                    status=422,
                )

        text = raw.decode("utf-8", errors="replace")
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if ext == ".abc":
            # ABC content is plain ASCII (no `<` or `>`); the browser
            # parses `<div class="abc">…</div>` cleanly and textContent
            # returns the source verbatim. div is fine here.
            inner = (
                '<div class="abc">\n'
                + text
                + '\n</div>'
            )
        else:
            # MusicXML / generic .xml: contains `<score-partwise>` and
            # other tags the browser's HTML parser would consume,
            # leaving textContent with no markup at all. A <script> with
            # a non-JS type is treated as opaque data — the browser
            # never parses its content as HTML, so textContent returns
            # the XML byte-for-byte for OpenSheetMusicDisplay to chew on.
            inner = (
                '<script type="application/vnd.recordare.musicxml+xml" '
                'class="music-musicxml-source">\n'
                + text
                + '\n</script>'
            )
        body_md = (
            f'---\ntitle: "{title}"\n---\n\n'
            f"# {title}\n\n"
            '<figure class="music">\n'
            + inner
            + "\n</figure>\n"
        )

    (doc_dir / "current.md").write_text(body_md, encoding="utf-8", newline="")
    (doc_dir / "baseline.md").write_text(body_md, encoding="utf-8", newline="")
    ensure_doc_ids()
    await state.broadcast({
        "type": "docs", "list": list_all_docs(), "doc": slug,
    })
    print(
        f"[upload:music] docs/{slug}/current.md ({total}B {ext})",
        flush=True,
    )
    return web.json_response({
        "path": f"docs/{slug}/current.md",
        "slug": slug,
        "name": "current.md",
        "kind": ext,
        "converted": True,
        "converter": "music",
    })


async def _upload_binary_doc(
    request: web.Request,
    field: "web.BodyPartReader",
    raw_name: str,
    ext: str,
) -> web.Response:
    """Server-side conversion path for binary doc formats (PDF, DOCX, ...).

    Saves the upload as `docs/<slug>/original.<ext>`, runs markitdown to
    produce markdown, and writes the result to `current.md` + `baseline.md`.
    The agent is not involved — the reader gets a fully-converted doc back."""
    # Stream-read with a 25 MB cap. We accept binary bytes verbatim — no
    # NUL guard, no decode. The blocklist check earlier in the path already
    # rejected dangerous types; PDF/DOCX/etc. are inert document containers.
    total = 0
    chunks: list[bytes] = []
    while True:
        chunk = await field.read_chunk(size=64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > _BINARY_CONVERT_MAX_BYTES:
            return web.json_response(
                {"error": "file too large (max 25MB for binary imports)"},
                status=413,
            )
        chunks.append(chunk)
    raw = b"".join(chunks)
    if total == 0:
        return web.json_response({"error": "empty file"}, status=400)

    # Decide which converter(s) to try. For PDFs the default is Claude
    # vision with a markitdown fallback; non-PDFs go straight to markitdown.
    backend_pref = os.environ.get("AM_PDF_BACKEND", "auto").lower()
    if ext == ".pdf":
        try_claude = backend_pref in ("auto", "claude")
        try_markitdown_fallback = backend_pref != "claude"
    else:
        try_claude = False
        try_markitdown_fallback = True
    if not try_claude and _MARKITDOWN is None:
        return web.json_response(
            {"error": "markitdown is not installed on this server. "
                      "Install with `pip install \"markitdown[pdf]\"` to enable "
                      "DOCX / XLSX / PPTX import, or set ANTHROPIC_API_KEY for "
                      "Claude-based PDF conversion."},
            status=501,
        )

    # Slug derivation matches upload_md's text path so the resulting doc
    # looks identical to a plain .md upload from the outside.
    raw_stem = Path(raw_name).stem
    slug_base = re.sub(r"[^a-z0-9-]+", "-", raw_stem.lower()).strip("-")
    if not slug_base or not _DOC_SLUG_RE.match(slug_base):
        slug_base = "uploaded"
    slug = slug_base
    counter = 1
    while (DOCS_ROOT / slug).exists():
        counter += 1
        slug = f"{slug_base}-{counter}"
    doc_dir = DOCS_ROOT / slug
    doc_dir.mkdir(parents=True, exist_ok=True)
    original_path = doc_dir / f"original{ext}"
    original_path.write_bytes(raw)

    md_text: str | None = None
    converter_used = ""

    # Path A: Claude vision (PDF only, env permitting, API key present).
    if try_claude:
        md_text = await _convert_pdf_via_claude(raw)
        if md_text:
            converter_used = "claude"

    # Path B: markitdown (fallback for PDF, primary for DOCX/XLSX/PPTX).
    if not md_text and try_markitdown_fallback and _MARKITDOWN is not None:
        try:
            result = await asyncio.to_thread(
                _MARKITDOWN.convert, str(original_path),
            )
            md_text = (result.text_content or "").strip()
            if md_text:
                converter_used = "markitdown"
        except Exception as e:
            print(
                f"[upload:binary] markitdown failed: {type(e).__name__}: {e}",
                flush=True,
            )

    if not md_text:
        # Both converters declined / failed — roll back so we don't leave an
        # orphan original.<ext> with no markdown beside it.
        import shutil
        shutil.rmtree(doc_dir, ignore_errors=True)
        if backend_pref == "claude" and ext == ".pdf":
            return web.json_response(
                {"error": "Claude PDF conversion failed and fallback is "
                          "disabled (AM_PDF_BACKEND=claude). Check the server "
                          "log for the API error."},
                status=502,
            )
        return web.json_response(
            {"error": "conversion produced empty markdown — the file may be "
                      "image-only, unreadable, or all converters failed"},
            status=422,
        )

    md_text = md_text.replace("\r\n", "\n").replace("\r", "\n") + "\n"

    current_md = doc_dir / "current.md"
    baseline_md = doc_dir / "baseline.md"
    # newline="" prevents Python's text-mode CRLF translation on Windows
    with current_md.open("w", encoding="utf-8", newline="") as f:
        f.write(md_text)
    with baseline_md.open("w", encoding="utf-8", newline="") as f:
        f.write(md_text)

    ensure_doc_ids()
    await state.broadcast({"type": "docs", "list": list_all_docs(), "doc": slug})
    print(
        f"[upload:binary] docs/{slug}/current.md "
        f"({len(md_text)} chars from {total}B {ext} via {converter_used})",
        flush=True,
    )
    return web.json_response({
        "path": f"docs/{slug}/current.md",
        "slug": slug,
        "name": current_md.name,
        "kind": ext,
        "converted": True,
        "converter": converter_used,
    })


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

    # Music file imports: wrap source / MIDI binary in a `<figure class=
    # "music">` block and save as a new doc. Iframe runtime lazy-loads
    # the right renderer (abcjs / OSMD / html-midi-player) on view.
    if ext in _MUSIC_TEXT_EXTS or ext in _MUSIC_BINARY_EXTS:
        return await _upload_music_doc(request, field, raw_name, ext)

    # Binary-doc imports (PDF, DOCX, XLSX, PPTX) go through markitdown
    # server-side. They're not text and would fail the NUL-byte guard below;
    # branch out before any of the text-handling.
    if ext in _BINARY_CONVERT_EXTS:
        return await _upload_binary_doc(request, field, raw_name, ext)

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
    # Sanitize the raw filename into a slug (lowercase, alnum+dash).
    raw_stem = Path(raw_name).stem
    slug_base = re.sub(r"[^a-z0-9-]+", "-", raw_stem.lower()).strip("-")
    if not slug_base or not _DOC_SLUG_RE.match(slug_base):
        slug_base = "uploaded"
    # Find a free slug under docs/.
    slug = slug_base
    counter = 1
    while (DOCS_ROOT / slug).exists():
        counter += 1
        slug = f"{slug_base}-{counter}"
    doc_dir = DOCS_ROOT / slug
    doc_dir.mkdir(parents=True, exist_ok=True)
    # Non-.md drops land at docs/<slug>/original.<ext>; .md drops land at
    # docs/<slug>/current.md (with a sibling baseline.md as the immutable
    # history-0 derived from the upload).
    if needs_conversion:
        target = doc_dir / f"original{ext}"
    else:
        target = doc_dir / "current.md"

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

    # Server-side conversion via Claude for selected text formats (.tex).
    # original.<ext> is already on disk; we just need to produce current.md
    # and baseline.md from Claude's output. If Claude is unavailable or
    # fails, fall through to the agent-mediated path below.
    backend_pref = os.environ.get("AM_PDF_BACKEND", "auto").lower()
    if ext in _LLM_CONVERT_TEXT_EXTS and backend_pref in ("auto", "claude"):
        md_text = await _convert_tex_via_claude(text)
        if md_text:
            md_text = md_text.replace("\r\n", "\n").replace("\r", "\n")
            if not md_text.endswith("\n"):
                md_text += "\n"
            current_md = doc_dir / "current.md"
            baseline_md = doc_dir / "baseline.md"
            with current_md.open("w", encoding="utf-8", newline="") as f:
                f.write(md_text)
            with baseline_md.open("w", encoding="utf-8", newline="") as f:
                f.write(md_text)
            ensure_doc_ids()
            await state.broadcast({
                "type": "docs", "list": list_all_docs(), "doc": slug,
            })
            print(
                f"[upload:tex:claude] docs/{slug}/current.md "
                f"({len(md_text)} chars from {total}B {ext})",
                flush=True,
            )
            return web.json_response({
                "path": f"docs/{slug}/current.md",
                "slug": slug,
                "name": current_md.name,
                "kind": ext,
                "converted": True,
                "converter": "claude",
            })

    if needs_conversion:
        # Non-.md upload: lives at docs/<slug>/original.<ext>. The agent
        # will convert it into docs/<slug>/current.md + baseline.md. No
        # doc_id mint yet (no .md file exists).
        unknown_flag = ext not in KNOWN_EXTS
        print(f"[upload:raw] {rel} ({total} bytes, slug={slug}, kind={ext},"
              f" unknown={unknown_flag})", flush=True)
        return web.json_response({
            "path": rel,
            "slug": slug,
            "name": target.name,
            "kind": ext,
            "needs_conversion": True,
            "unknown": unknown_flag,
            "target": f"docs/{slug}/current.md",
        })

    # .md upload: also stamp baseline.md so Reset has something to restore.
    baseline = doc_dir / "baseline.md"
    if not baseline.exists():
        baseline.write_bytes(target.read_bytes())
    ensure_doc_ids()
    await state.broadcast({"type": "docs", "list": list_all_docs(), "doc": slug})
    print(f"[upload] docs/{slug}/current.md ({total} bytes)", flush=True)
    return web.json_response({"path": rel, "slug": slug, "name": target.name})


# Asset blocklist mirrors the upload blocklist — executable / archive / macro-
# bearing formats can never be assets, no matter what extension was on the file.
_ASSET_BLOCKED_EXTS = {
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


async def upload_asset(request: web.Request) -> web.Response:
    """POST /upload-asset?doc=<slug> with multipart `file` — saves the
    file to docs/<slug>/assets/<sanitized-name>. The agent gets told via
    a chat notice that the asset is now referenceable as
    `assets/<name>` from the doc body."""
    _require_localhost_origin(request)
    slug = (request.query.get("doc") or "").strip()
    doc_dir = DOCS_ROOT / slug if slug else None
    if not slug or not _DOC_SLUG_RE.match(slug) or doc_dir is None \
            or not doc_dir.is_dir():
        return web.json_response(
            {"error": "bad or unknown doc slug"}, status=400,
        )

    reader = await request.multipart()
    field = None
    async for part in reader:
        if part.name == "file":
            field = part
            break
    if field is None:
        return web.json_response({"error": "no file field"}, status=400)

    raw_name = field.filename or "asset.bin"
    ext = Path(raw_name).suffix.lower()
    if ext in _ASSET_BLOCKED_EXTS:
        return web.json_response(
            {"error": f"refused: {ext} files are not allowed as assets",
             "kind": ext, "blocked": True},
            status=415,
        )
    if ext not in _ASSET_EXTS:
        return web.json_response(
            {"error": f"unsupported asset type: {ext or '(no extension)'}",
             "kind": ext or "(no extension)",
             "supported": sorted(_ASSET_EXTS)},
            status=415,
        )

    # Sanitize the filename: strip path components, restrict to safe chars,
    # preserve extension. Collisions get a "-N" suffix.
    safe_stem = re.sub(r"[^\w.\-]", "_", Path(raw_name).stem) or "asset"
    safe_name = f"{safe_stem}{ext}"
    assets_dir = doc_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    target = assets_dir / safe_name
    counter = 1
    while target.exists():
        counter += 1
        target = assets_dir / f"{safe_stem}-{counter}{ext}"

    # Stream-read with size cap. No text decoding — assets are bytes.
    total = 0
    with target.open("wb") as out:
        while True:
            chunk = await field.read_chunk(size=64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _ASSET_MAX_BYTES:
                out.close()
                target.unlink(missing_ok=True)
                return web.json_response(
                    {"error": f"asset too large (max "
                              f"{_ASSET_MAX_BYTES // (1024*1024)} MB)"},
                    status=413,
                )
            out.write(chunk)

    rel_in_doc = f"assets/{target.name}"
    rel_full = target.relative_to(ROOT).as_posix()
    size_kb = total / 1024
    size_str = (
        f"{size_kb:.1f} KB" if size_kb < 1024
        else f"{size_kb / 1024:.2f} MB"
    )
    # Tell the agent (and the reader, via chat) that the asset is now
    # available. The agent picks it up on the next chat turn as
    # conversation history.
    await state.broadcast({
        "role": "user", "type": "text",
        "text": (
            f"_(System: reader dropped `{rel_in_doc}` ({size_str}) into "
            f"`docs/{slug}/assets/`. Reference it from `current.md` as "
            f"`assets/{target.name}` — e.g. `<img src=\"assets/"
            f"{target.name}\" alt=\"...\">` for an image, `<audio src=\"...\">` "
            f"for audio, etc.)_"
        ),
    })
    print(f"[asset] docs/{slug}/{rel_in_doc} ({total} bytes)", flush=True)
    return web.json_response({
        "ok": True,
        "doc": slug,
        "path": rel_full,
        "name": target.name,
        "ref": rel_in_doc,
        "bytes": total,
    })


# ---- Reset to history-0 -------------------------------------------------
# POST /reset?doc=<slug> restores docs/<slug>/current.md from baseline.md
# (the immutable history-0 sitting next to it in the doc folder). The
# baseline is the canonical Reset target — no more "oldest snap" guesswork.


async def list_history(request: web.Request) -> web.Response:
    """GET /history?doc=<slug> — list snapshots captured by pre-edit hook."""
    _require_localhost_origin(request)
    doc_param = request.query.get("doc", "").strip()
    doc_path = _doc_path_for(doc_param)
    if doc_path is None:
        return web.json_response({"error": "bad doc slug"}, status=400)
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
    """POST /undo?doc=<slug> — restore the most recent snapshot."""
    _require_localhost_origin(request)
    doc_param = request.query.get("doc", "").strip()
    doc_path = _doc_path_for(doc_param)
    if doc_path is None:
        return web.json_response({"error": "bad doc slug"}, status=400)
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
    slug = _doc_slug_from_path(doc_path) or doc_param
    # Any pending entries referenced the just-overwritten current.md;
    # they're now orphaned and would silently corrupt source on Reject
    # (restoring an old_text that no longer matches reality). Clear them
    # alongside the source restore.
    cleared = clear_pending(slug) if slug else False
    await state.broadcast({"type": "doc_changed", "doc": slug})
    if cleared:
        await state.broadcast({"type": "pending_changed", "doc": slug})
    snap_id = newest.stem.replace("snap-", "")
    print(
        f"[undo] docs/{slug}/current.md <- snap-{snap_id}"
        f"{' (pending cleared)' if cleared else ''}",
        flush=True,
    )
    return web.json_response({"doc": slug, "snap_id": snap_id, "ok": True})


async def restore_snapshot(request: web.Request) -> web.Response:
    """POST /restore_snapshot?doc=<slug>&snap_id=... — restore from a snap."""
    _require_localhost_origin(request)
    doc_param = request.query.get("doc", "").strip()
    snap_id = request.query.get("snap_id", "").strip()
    doc_path = _doc_path_for(doc_param)
    if doc_path is None:
        return web.json_response({"error": "bad doc slug"}, status=400)
    if not snap_id or not re.fullmatch(r"[A-Za-z0-9_-]{6,40}", snap_id):
        return web.json_response({"error": "bad snap_id"}, status=400)
    snap_path = _history_dir_for(doc_path) / f"snap-{snap_id}.md"
    if not snap_path.exists():
        return web.json_response({"error": "snapshot not found"}, status=404)
    _snapshot_if_changed(doc_path)
    doc_path.write_bytes(snap_path.read_bytes())
    slug = _doc_slug_from_path(doc_path) or doc_param
    # Same orphan-clear rationale as /undo: pending entries reference
    # the current.md state we just overwrote.
    cleared = clear_pending(slug) if slug else False
    await state.broadcast({"type": "doc_changed", "doc": slug})
    if cleared:
        await state.broadcast({"type": "pending_changed", "doc": slug})
    print(
        f"[restore] docs/{slug}/current.md <- snap-{snap_id}"
        f"{' (pending cleared)' if cleared else ''}",
        flush=True,
    )
    return web.json_response({"doc": slug, "snap_id": snap_id, "ok": True})


async def edit_block(request: web.Request) -> web.Response:
    """POST /edit-block — replace a single block's source with plain text.

    Body JSON: { doc: <slug>, block: <signature>, new_text: <plain text> }
    where signature is the blockInfo the iframe posts for selections
    (track_id / anchor_id / label / excerpt).

    MVP scope: paragraphs and headings only. Lists, code, tables, raw HTML
    blocks return 422 — the reader edits those via the Source view because
    plaintext replacement would corrupt the source structure.
    """
    _require_localhost_origin(request)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    slug = (data.get("doc") or "").strip()
    block = data.get("block") or {}
    new_text = data.get("new_text", "")
    if not slug or not _DOC_SLUG_RE.match(slug):
        return web.json_response({"error": "bad doc slug"}, status=400)
    if not isinstance(block, dict):
        return web.json_response({"error": "block must be object"}, status=400)
    if not isinstance(new_text, str):
        return web.json_response({"error": "new_text must be string"}, status=400)
    new_text = new_text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")

    doc_path = DOCS_ROOT / slug / "current.md"
    if not doc_path.exists():
        return web.json_response({"error": "doc not found"}, status=404)

    source = doc_path.read_text(encoding="utf-8")
    lines = source.split("\n")
    span = _find_block_span(lines, block)
    if span is None:
        return web.json_response(
            {"error": "could not locate or refusing to inline-edit this block "
                      "(supported: paragraphs and headings). Use the Source "
                      "view for lists, code, tables, and HTML blocks."},
            status=422,
        )
    start, end, kind = span

    # Heading edit: keep the original `## ` prefix + optional `{#anchor}`
    # suffix; replace only the visible text.
    if kind == "heading":
        m = _HEADING_LINE_RE.match(lines[start])
        if m:
            hashes = m.group(1)
            anchor = m.group(3)
            anchor_suffix = f" {{{anchor}}}" if anchor else ""
            new_first_line = (
                f"{hashes} {new_text.splitlines()[0] if new_text else ''}"
                f"{anchor_suffix}"
            )
            new_lines = lines[:start] + [new_first_line] + lines[end:]
        else:
            new_lines = lines[:start] + [new_text] + lines[end:]
    else:
        new_lines = lines[:start] + [new_text] + lines[end:]
    new_source = "\n".join(new_lines)

    errors = validators.validate_doc(new_source) if new_source.strip() else []
    if errors:
        return web.json_response(
            {"error": "validation failed",
             "details": [
                 {"kind": e.get("kind"), "line": e.get("line"),
                  "message": e.get("message")}
                 for e in errors[:5]
             ]},
            status=422,
        )

    _snapshot_if_changed(doc_path)
    with doc_path.open("w", encoding="utf-8", newline="") as f:
        f.write(new_source)
    print(
        f"[edit-block] docs/{slug}/current.md ({kind}, "
        f"start={start} end={end} → {len(new_text)} chars)",
        flush=True,
    )
    await state.broadcast({"type": "doc_changed", "doc": slug})
    return web.json_response({"ok": True, "kind": kind})


async def list_pending(request: web.Request) -> web.Response:
    """GET /pending?doc=<slug> — return the pending sidecar's contents
    (or the empty shape if there's no sidecar yet). The viewer polls
    this when a doc loads and when doc_changed broadcasts so it can
    surface the Review tab + indicator."""
    _require_localhost_origin(request)
    slug = (request.query.get("doc") or "").strip()
    if not slug or not _DOC_SLUG_RE.match(slug):
        return web.json_response({"error": "bad doc slug"}, status=400)
    return web.json_response(load_pending(slug))


async def accept_pending(request: web.Request) -> web.Response:
    """POST /accept-pending?doc=<slug>[&id=<edit-id>] — accept one pending
    entry (or all of them if id is omitted). Accept = keep current.md
    as-is and drop the entry from the sidecar; the bytes are already on
    disk per the bytes-land model. No source edits needed."""
    _require_localhost_origin(request)
    slug = (request.query.get("doc") or "").strip()
    if not slug or not _DOC_SLUG_RE.match(slug):
        return web.json_response({"error": "bad doc slug"}, status=400)
    edit_id = (request.query.get("id") or "").strip()
    data = load_pending(slug)
    if not data["edits"]:
        return web.json_response({"ok": True, "accepted": 0})
    if edit_id:
        removed = remove_pending_edit(slug, edit_id)
        accepted = 1 if removed else 0
    else:
        accepted = len(data["edits"])
        clear_pending(slug)
    print(f"[pending] accept slug={slug} id={edit_id or '*'} "
          f"({accepted} entries cleared)", flush=True)
    # Doc bytes didn't change, but the viewer needs to know pending is
    # gone so it can hide the Review tab / indicator.
    await state.broadcast({"type": "pending_changed", "doc": slug})
    return web.json_response({"ok": True, "accepted": accepted})


async def reject_pending(request: web.Request) -> web.Response:
    """POST /reject-pending?doc=<slug>[&id=<edit-id>] — reject one pending
    entry (or all if id omitted). Reject = restore the old_text recorded
    with the entry into current.md, snapshot+broadcast, drop the entry."""
    _require_localhost_origin(request)
    slug = (request.query.get("doc") or "").strip()
    if not slug or not _DOC_SLUG_RE.match(slug):
        return web.json_response({"error": "bad doc slug"}, status=400)
    edit_id = (request.query.get("id") or "").strip()
    data = load_pending(slug)
    if not data["edits"]:
        return web.json_response({"ok": True, "rejected": 0})

    doc_path = DOCS_ROOT / slug / "current.md"
    if not doc_path.exists():
        return web.json_response({"error": "doc not found"}, status=404)

    entries = data["edits"]
    if edit_id:
        targets = [e for e in entries if e.get("id") == edit_id]
        if not targets:
            return web.json_response(
                {"error": f"no pending entry with id {edit_id!r}"},
                status=404,
            )
    else:
        targets = list(entries)

    # MVP: whole-doc kind entries. For each target, restore its old_text
    # as the entire current.md. Multiple whole-doc entries shouldn't
    # coexist because same-block replacement collapses them, but if we
    # see more than one (other kinds in future) we walk them in reverse.
    _snapshot_if_changed(doc_path)
    for entry in reversed(targets):
        kind = (entry.get("block") or {}).get("kind")
        if kind == "doc":
            old_text = entry.get("old_text") or ""
            with doc_path.open("w", encoding="utf-8", newline="") as f:
                f.write(old_text)
        else:
            # Per-block reject for non-"doc" kinds will land when phase 3+
            # decomposes the granularity. For now, refuse rather than
            # corrupt the source.
            return web.json_response(
                {"error": f"reject not yet implemented for block kind "
                          f"{kind!r}; only 'doc' is supported in v0"},
                status=422,
            )
        remove_pending_edit(slug, entry["id"])

    print(f"[pending] reject slug={slug} id={edit_id or '*'} "
          f"({len(targets)} entries reverted)", flush=True)
    await state.broadcast({"type": "doc_changed", "doc": slug})
    await state.broadcast({"type": "pending_changed", "doc": slug})
    return web.json_response({"ok": True, "rejected": len(targets)})


_AGENT_SKILL_RE = re.compile(
    r'<section\s+class="agent-skill"[^>]*>([\s\S]*?)</section\s*>',
    re.IGNORECASE,
)
_SKILL_NAME_RE = re.compile(
    r'^\s*##+\s+SKILL\s*:\s*(.+?)\s*$', re.IGNORECASE | re.MULTILINE,
)


def _find_agent_skill_blocks(source: str) -> list[dict]:
    """Locate every <section class="agent-skill">...</section> block.
    Returns one dict per block with byte offsets (`start`/`end` over the
    whole block including tags), the inner content stripped of leading/
    trailing whitespace, and a best-guess name pulled from the first
    `## SKILL: <name>` heading inside the block (or "untitled #N"
    fallback)."""
    out = []
    for i, m in enumerate(_AGENT_SKILL_RE.finditer(source)):
        inner = m.group(1).strip("\n")
        # Heading-based name extraction. If absent, fall back so the
        # UI always has something to label the skill with.
        nm = _SKILL_NAME_RE.search(inner)
        name = nm.group(1).strip() if nm else f"untitled #{i + 1}"
        out.append({
            "index": i,
            "name": name,
            "content": inner,
            "start": m.start(),
            "end": m.end(),
        })
    return out


def _render_agent_skill_block(content: str) -> str:
    """Build the `<section class="agent-skill">...</section>` wrapper
    around a body. Whitespace pattern matches what add_doc_skill / the
    editor write so round-tripping through the parser stays clean."""
    body = (content or "").strip("\n")
    return (
        '<section class="agent-skill">\n\n'
        + body
        + '\n\n</section>'
    )


async def list_doc_skills(request: web.Request) -> web.Response:
    """GET /doc-skills?doc=<slug> — list every agent-skill section in
    the doc with its label + content + position. Drives the Skills
    manager modal."""
    _require_localhost_origin(request)
    slug = (request.query.get("doc") or "").strip()
    if not slug or not _DOC_SLUG_RE.match(slug):
        return web.json_response({"error": "bad doc slug"}, status=400)
    doc_path = DOCS_ROOT / slug / "current.md"
    if not doc_path.exists():
        return web.json_response({"error": "doc not found"}, status=404)
    source = doc_path.read_text(encoding="utf-8")
    skills = _find_agent_skill_blocks(source)
    # Drop the byte offsets from the response — those are server-side
    # only; the index is the stable handle for update/delete.
    return web.json_response({
        "skills": [
            {"index": s["index"], "name": s["name"], "content": s["content"]}
            for s in skills
        ],
    })


async def update_doc_skill(request: web.Request) -> web.Response:
    """POST /update-doc-skill?doc=<slug>&index=N — replace the Nth
    agent-skill section's inner content with the body's `content`
    field. The wrapping `<section class="agent-skill">…</section>`
    is preserved."""
    _require_localhost_origin(request)
    slug = (request.query.get("doc") or "").strip()
    if not slug or not _DOC_SLUG_RE.match(slug):
        return web.json_response({"error": "bad doc slug"}, status=400)
    try:
        idx = int(request.query.get("index", "-1"))
    except (TypeError, ValueError):
        return web.json_response({"error": "index must be an integer"}, status=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    new_content = body.get("content")
    if not isinstance(new_content, str):
        return web.json_response({"error": "missing 'content'"}, status=400)

    doc_path = DOCS_ROOT / slug / "current.md"
    if not doc_path.exists():
        return web.json_response({"error": "doc not found"}, status=404)
    source = doc_path.read_text(encoding="utf-8")
    blocks = _find_agent_skill_blocks(source)
    if idx < 0 or idx >= len(blocks):
        return web.json_response(
            {"error": f"no agent-skill section at index {idx} "
                      f"(this doc has {len(blocks)})"},
            status=404,
        )
    block = blocks[idx]
    new_block = _render_agent_skill_block(new_content)
    new_source = source[: block["start"]] + new_block + source[block["end"]:]

    errors = validators.validate_doc(new_source) if new_source.strip() else []
    if errors:
        return web.json_response(
            {"error": "validation failed",
             "details": [
                 {"kind": e.get("kind"), "line": e.get("line"),
                  "message": e.get("message")}
                 for e in errors[:3]
             ]},
            status=422,
        )

    _snapshot_if_changed(doc_path)
    with doc_path.open("w", encoding="utf-8", newline="") as f:
        f.write(new_source)
    print(f"[update-doc-skill] docs/{slug}/current.md index={idx}", flush=True)
    await state.broadcast({"type": "doc_changed", "doc": slug})
    return web.json_response({"ok": True, "index": idx})


async def delete_doc_skill(request: web.Request) -> web.Response:
    """POST /delete-doc-skill?doc=<slug>&index=N — remove the Nth
    agent-skill section entirely."""
    _require_localhost_origin(request)
    slug = (request.query.get("doc") or "").strip()
    if not slug or not _DOC_SLUG_RE.match(slug):
        return web.json_response({"error": "bad doc slug"}, status=400)
    try:
        idx = int(request.query.get("index", "-1"))
    except (TypeError, ValueError):
        return web.json_response({"error": "index must be an integer"}, status=400)

    doc_path = DOCS_ROOT / slug / "current.md"
    if not doc_path.exists():
        return web.json_response({"error": "doc not found"}, status=404)
    source = doc_path.read_text(encoding="utf-8")
    blocks = _find_agent_skill_blocks(source)
    if idx < 0 or idx >= len(blocks):
        return web.json_response(
            {"error": f"no agent-skill section at index {idx}"},
            status=404,
        )
    block = blocks[idx]
    new_source = source[: block["start"]] + source[block["end"]:]
    # Collapse runs of 3+ blank lines that the removal may have produced.
    new_source = re.sub(r"\n{3,}", "\n\n", new_source)
    _snapshot_if_changed(doc_path)
    with doc_path.open("w", encoding="utf-8", newline="") as f:
        f.write(new_source)
    print(f"[delete-doc-skill] docs/{slug}/current.md index={idx}", flush=True)
    await state.broadcast({"type": "doc_changed", "doc": slug})
    return web.json_response({"ok": True, "index": idx})


async def add_doc_skill(request: web.Request) -> web.Response:
    """POST /add-doc-skill?doc=<slug> — append an empty agent-skill section
    to the doc body so the author has somewhere to write working-contract
    text without learning the `<section class="agent-skill">` wrapper by
    hand. Body JSON `{ "name": "<short label>" }` is optional; defaults
    to "untitled". The viewer typically follows up by switching to Source
    view so the author can fill in the placeholder."""
    _require_localhost_origin(request)
    slug = (request.query.get("doc") or "").strip()
    if not slug or not _DOC_SLUG_RE.match(slug):
        return web.json_response({"error": "bad doc slug"}, status=400)
    doc_path = DOCS_ROOT / slug / "current.md"
    if not doc_path.exists():
        return web.json_response({"error": "doc not found"}, status=404)

    body: dict = {}
    if request.content_length:
        try:
            body = await request.json()
        except Exception:
            body = {}
    if not isinstance(body, dict):
        body = {}
    name = (body.get("name") or "").strip() or "untitled"
    # Optional pre-filled content from the Skills manager modal. If the
    # caller didn't supply any, we fall back to the placeholder body so
    # an empty + Skill click still gets a useful stub.
    raw_content = body.get("content")
    custom_content = raw_content.strip() if isinstance(raw_content, str) else ""

    source = doc_path.read_text(encoding="utf-8")
    if custom_content:
        skill_inner = custom_content
    else:
        # Just a heading line — the manager's editor is the place to
        # write the actual content. No verbose placeholder cluttering
        # the source / preview.
        skill_inner = f"## SKILL: {name}"
    skill_block = _render_agent_skill_block(skill_inner) + "\n\n"
    # Insert right after the frontmatter (or at the very top if there's
    # none). The agent reads the doc top-down via the inlined preamble,
    # so placing the contract near the top means the agent absorbs the
    # contract before processing body content — putting it at the bottom
    # forces the agent to read the body first under generic guidance,
    # then learn the contract too late to influence anything. Also makes
    # the skill discoverable in Source view without scrolling. Doc view
    # is unchanged (display:none either way).
    m = _FRONTMATTER_RE.match(source)
    if m:
        head = source[: m.end()]
        tail = source[m.end():].lstrip("\n")
        new_source = head + "\n" + skill_block + tail
    else:
        new_source = skill_block + source.lstrip("\n")

    errors = validators.validate_doc(new_source) if new_source.strip() else []
    if errors:
        return web.json_response(
            {"error": "validation failed",
             "details": [
                 {"kind": e.get("kind"), "line": e.get("line"),
                  "message": e.get("message")}
                 for e in errors[:3]
             ]},
            status=422,
        )

    _snapshot_if_changed(doc_path)
    with doc_path.open("w", encoding="utf-8", newline="") as f:
        f.write(new_source)
    print(f"[add-doc-skill] docs/{slug}/current.md (name={name!r})", flush=True)
    await state.broadcast({"type": "doc_changed", "doc": slug})
    return web.json_response({"ok": True, "name": name})


async def reset_doc(request: web.Request) -> web.Response:
    """POST /reset?doc=<slug> — restore current.md from baseline.md (history-0)."""
    _require_localhost_origin(request)
    doc_param = request.query.get("doc", "").strip()
    doc_path = _doc_path_for(doc_param)
    if doc_path is None:
        return web.json_response({"error": "bad doc slug"}, status=400)
    history_zero = _history_zero_path(doc_path)
    if history_zero is None:
        return web.json_response(
            {"error": (
                f"no baseline.md for {doc_param!r} — this doc has no "
                "history-0 to reset to. For a user-created doc, save a "
                "baseline first (future action); for a ship-with example, "
                f"run `git checkout docs/{doc_param}/baseline.md`."
            )},
            status=404,
        )
    _snapshot_if_changed(doc_path)
    doc_path.write_bytes(history_zero.read_bytes())
    slug = _doc_slug_from_path(doc_path) or doc_param
    # Pending entries reference the just-overwritten current.md and would
    # corrupt source on Reject. Clear them.
    cleared = clear_pending(slug) if slug else False
    await state.broadcast({"type": "doc_changed", "doc": slug})
    if cleared:
        await state.broadcast({"type": "pending_changed", "doc": slug})
    print(
        f"[reset] docs/{slug}/current.md <- baseline.md"
        f"{' (pending cleared)' if cleared else ''}",
        flush=True,
    )
    return web.json_response({"doc": slug, "snap_id": "baseline", "ok": True})


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
