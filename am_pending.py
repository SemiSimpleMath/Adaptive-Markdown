"""Pending-changes substrate (disk + helpers).

Per-doc sidecar `docs/<slug>/pending.json` holding proposed edits the
reader hasn't accepted yet. This module is the storage layer; backend
wiring (PreToolUse routing, preamble pending-apply, renderer overlay,
Accept/Reject UI, human-inline-edit-rejects-pending) layers on top.
See ROADMAP > Open design questions > "Pending-changes substrate"
for the full design.

Entry schema (one object per Edit / Write tool call):
  {
    "id": "pe-<ulid>",                # stable; assigned on add
    "tool_use_id": <opaque agent id>, # for cross-reference with chat log
    "block": {                         # block signature, same shape as
      "track_id": ...,                 # the selection / edit-block API
      "anchor_id": ...,
      "label": ...,
      "excerpt": ...,
    },
    "old_text": <pre-edit source>,
    "new_text": <proposed source>,
    "agent_label": "<provider>:<model>",
    "created_at": "<iso8601>",
  }

File-level shape:
  { "version": 1, "doc": <slug>, "edits": [<entry>, ...] }
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from am_ids import gen_id

# Local copies of the constants backend.py also defines. Duplication is one
# regex per module — cheaper than introducing a sixth "paths" module just
# to hold three values.
_ROOT = Path(__file__).resolve().parent
_DOCS_ROOT = _ROOT / "docs"
_DOC_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$", re.IGNORECASE)
_FRONTMATTER_RE = re.compile(r"^---\r?\n([\s\S]*?)\r?\n---\r?\n")

_PENDING_SCHEMA_VERSION = 1
_PENDING_REQUIRED_FIELDS = ("tool_use_id", "block", "old_text", "new_text",
                            "agent_label")


def _pending_path(slug: str) -> Path | None:
    """Return the pending sidecar path for a doc slug, or None if the slug
    is malformed or the doc directory doesn't exist."""
    if not slug or not _DOC_SLUG_RE.match(slug):
        return None
    doc_dir = _DOCS_ROOT / slug
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
