"""Per-doc HTTP route handlers.

Snapshot history, undo / restore / reset, inline edit-block, pending
accept / reject, and the agent-skill section CRUD. All share the same
shape: parse query, resolve the slug, mutate or read the doc, snapshot
if changed, broadcast doc_changed.

Stays bundled because they all manipulate the doc + snapshot store
through the same surface; splitting them would create
am_routes_history / am_routes_pending / am_routes_skills modules that
share the same imports and look identical at the top.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from aiohttp import web

import validators
from am_docs import (
    COMPONENT_SLUG_RE,
    COMPONENTS_ROOT,
    DATA_ROOT,
    DOC_SLUG_RE as _DOC_SLUG_RE,
    DOCS_ROOT,
    FRONTMATTER_RE as _FRONTMATTER_RE,
    _doc_path_for,
    _doc_slug_from_path,
    list_all_components,
)
from am_hooks import init_runtime, shutdown_runtime
from am_origin import _require_localhost_origin
from am_pending import (
    clear_pending,
    load_pending,
    remove_pending_edit,
)
from am_snapshots import (
    _history_dir_for,
    _history_zero_path,
    save_snapshot,
)
from am_state import state
from am_ids import gen_id
from am_tracking import (
    block_source_span, delete_block_source, insert_block_source,
    replace_block_source, resolve_alias, strip_block_ids, writer_hard_breaks,
)


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
    # Bytes-vs-bytes throughout: text-mode reads do universal-newline
    # normalization (\\r\\n -> \\n), so two files that differ only in line
    # endings would compare equal and we'd skip the safety snap — but the
    # subsequent restore (write_bytes) reintroduces the difference, leaving
    # the working copy in a state that no snap matches. Bytes avoid this
    # trap and give true round-trip equality.
    try:
        current = doc_path.read_bytes()
    except OSError:
        return None
    hist = _history_dir_for(doc_path)
    if hist.exists():
        snaps = sorted(hist.glob("snap-*.md"))
        if snaps:
            try:
                if snaps[-1].read_bytes() == current:
                    return None
            except OSError:
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


def _norm_lf(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _resolve_id(doc_path, track_id):
    """Resolve a (possibly stale) track_id through the alias map. None if blank
    or tombstoned (the block was dropped)."""
    tid = (track_id or "").strip()
    if not tid:
        return None
    return resolve_alias(doc_path, tid) or None


def _resolve_span(doc_path, text, track_id):
    """Resolve the id and locate its block: (id_line, start, end) file-line
    indices, or None. Works for every top-level block type — the extent comes
    from the renderer's grammar (block_source_span / am_blocks), so a loose
    list, a fenced block, a GFM table, or a whole <figure> resolve like prose."""
    rid = _resolve_id(doc_path, track_id)
    return block_source_span(text, rid) if rid else None


def _validation_errors(full: str):
    """Run the JS/CSS/SVG validator over a candidate doc; return the response
    payload list (capped) or [] if clean. Empty docs validate trivially."""
    errors = validators.validate_doc(full) if full.strip() else []
    return [{"kind": e.get("kind"), "line": e.get("line"),
             "message": e.get("message")} for e in errors[:5]]


async def _commit_doc(doc_path, slug, full, label, extra=None):
    """Validate -> snapshot-if-changed -> write -> broadcast. Shared tail of
    the block edit / delete / insert handlers."""
    details = _validation_errors(full)
    if details:
        return web.json_response(
            {"error": "validation failed", "details": details}, status=422)
    _snapshot_if_changed(doc_path)
    with doc_path.open("w", encoding="utf-8", newline="") as f:
        f.write(full)
    print(f"[{label}] docs/{slug}/current.md", flush=True)
    await state.broadcast({"type": "doc_changed", "doc": slug})
    return web.json_response({"ok": True, **(extra or {})})


async def get_block_source(request: web.Request) -> web.Response:
    """GET /block-source?doc=<slug>&id=<track_id> — return the block's EXACT
    markdown source. The inline source-editor fetches this, shows it in a
    textarea, and posts the edited text back. No DOM serialization anywhere —
    the source IS the source, so styled spans / math / spacing round-trip
    losslessly. Works for any block type (the editor opens lists, tables,
    fenced code and figures, not just prose)."""
    _require_localhost_origin(request)
    slug = (request.query.get("doc") or "").strip()
    track_id = (request.query.get("id") or "").strip()
    if not slug or not _DOC_SLUG_RE.match(slug):
        return web.json_response({"error": "bad doc slug"}, status=400)
    doc_path = DOCS_ROOT / slug / "current.md"
    if not doc_path.exists():
        return web.json_response({"error": "doc not found"}, status=404)
    text = doc_path.read_text(encoding="utf-8")
    span = _resolve_span(doc_path, text, track_id)
    if span is None:
        return web.json_response({"error": "could not locate this block"}, status=422)
    _id_line, start, end = span
    lines = text.split("\n")
    return web.json_response({"ok": True, "source": "\n".join(lines[start:end])})


async def set_block_source(request: web.Request) -> web.Response:
    """POST /block-source { doc, id, source } — replace the block's source span
    with `source` VERBATIM (the reader edited the raw markdown). Lossless by
    construction: we never reconstruct markdown from the rendered DOM."""
    _require_localhost_origin(request)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    slug = (data.get("doc") or "").strip()
    new_source = data.get("source", "")
    if not slug or not _DOC_SLUG_RE.match(slug):
        return web.json_response({"error": "bad doc slug"}, status=400)
    if not isinstance(new_source, str):
        return web.json_response({"error": "source must be string"}, status=400)
    doc_path = DOCS_ROOT / slug / "current.md"
    if not doc_path.exists():
        return web.json_response({"error": "doc not found"}, status=404)
    text = doc_path.read_text(encoding="utf-8")
    rid = _resolve_id(doc_path, data.get("id"))
    # Writer-friendly: a reader's typed line breaks become portable markdown
    # hard breaks (prose only — lists/code/HTML pass through). See
    # writer_hard_breaks.
    src = writer_hard_breaks(_norm_lf(new_source).rstrip("\n"))
    full = replace_block_source(text, rid, src) if rid else None
    if full is None:
        return web.json_response({"error": "could not locate this block"}, status=422)
    return await _commit_doc(doc_path, slug, full, "block-source")


async def delete_block(request: web.Request) -> web.Response:
    """POST /delete-block { doc, id } — remove a block entirely (its comment,
    body, and one trailing blank). The dropped id is tombstoned by the normal
    id-drift path on the next snapshot."""
    _require_localhost_origin(request)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    slug = (data.get("doc") or "").strip()
    if not slug or not _DOC_SLUG_RE.match(slug):
        return web.json_response({"error": "bad doc slug"}, status=400)
    doc_path = DOCS_ROOT / slug / "current.md"
    if not doc_path.exists():
        return web.json_response({"error": "doc not found"}, status=404)
    text = doc_path.read_text(encoding="utf-8")
    rid = _resolve_id(doc_path, data.get("id"))
    full = delete_block_source(text, rid) if rid else None
    if full is None:
        return web.json_response({"error": "could not locate this block"}, status=422)
    return await _commit_doc(doc_path, slug, full, "delete-block")


async def insert_block(request: web.Request) -> web.Response:
    """POST /insert-block { doc, after_id, source } — insert a NEW block after
    `after_id` (or at the top of the body if after_id is empty/null). Mints a
    fresh id, stamps sub-blocks so the newcomer is immediately editable, and
    returns the id."""
    _require_localhost_origin(request)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    slug = (data.get("doc") or "").strip()
    source = data.get("source", "")
    if not slug or not _DOC_SLUG_RE.match(slug):
        return web.json_response({"error": "bad doc slug"}, status=400)
    if not isinstance(source, str) or not source.strip():
        return web.json_response({"error": "source must be non-empty string"}, status=400)
    doc_path = DOCS_ROOT / slug / "current.md"
    if not doc_path.exists():
        return web.json_response({"error": "doc not found"}, status=404)
    text = doc_path.read_text(encoding="utf-8")
    after_raw = (data.get("after_id") or "").strip()
    after_id = _resolve_id(doc_path, after_raw) if after_raw else ""
    if after_raw and not after_id:
        return web.json_response(
            {"error": "could not locate the anchor block"}, status=422)
    new_id = gen_id("b")
    # Writer-friendly hard breaks for prose inserts (see writer_hard_breaks).
    src = writer_hard_breaks(_norm_lf(source).rstrip("\n"))
    full = insert_block_source(text, after_id, src, new_id)
    if full is None:
        return web.json_response(
            {"error": "could not locate the anchor block"}, status=422)
    return await _commit_doc(doc_path, slug, full, "insert-block",
                             extra={"id": new_id})


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


import am_html

_SKILL_NAME_RE = re.compile(
    r'^\s*##+\s+SKILL\s*:\s*(.+?)\s*$', re.IGNORECASE | re.MULTILINE,
)


def _find_agent_skill_blocks(source: str) -> list[dict]:
    """Locate every <section class="agent-skill">...</section> block.
    Returns one dict per block with byte offsets (`start`/`end` over the
    whole block including tags), the inner content stripped of leading/
    trailing whitespace, and a best-guess name pulled from the first
    `## SKILL: <name>` heading inside the block (or "untitled #N"
    fallback).

    Delegates HTML-block extraction to am_html.find_blocks. The
    previous regex required `class="agent-skill"` to be the literal
    first attribute value; the new path uses real attr parsing so
    multi-token class lists (`class="agent-skill highlighted"`),
    other attribute orders (`<section id="x" class="agent-skill">`),
    and single-quoted attrs all work."""
    blocks = am_html.find_blocks(
        source, "section",
        where=lambda a: "agent-skill" in (a.get("class") or "").split(),
    )
    out = []
    for i, b in enumerate(blocks):
        inner = b.body.strip("\n")
        # Heading-based name extraction. If absent, fall back so the
        # UI always has something to label the skill with.
        nm = _SKILL_NAME_RE.search(inner)
        name = nm.group(1).strip() if nm else f"untitled #{i + 1}"
        out.append({
            "index": i,
            "name": name,
            "content": inner,
            "start": b.start,
            "end": b.end,
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


async def save_baseline(request: web.Request) -> web.Response:
    """POST /save-baseline?doc=<slug> — copy current.md over baseline.md.

    Defensive: snapshot the EXISTING baseline first (if any) so the
    reader can recover via History if they pin the wrong state. The
    snap lives in the per-doc snaps/ dir like any other safety snap;
    its source path being baseline.md instead of current.md is invisible
    to the History panel — it just shows up as another restorable point.
    """
    _require_localhost_origin(request)
    doc_param = request.query.get("doc", "").strip()
    doc_path = _doc_path_for(doc_param)
    if doc_path is None:
        return web.json_response({"error": "bad doc slug"}, status=400)
    if not doc_path.exists():
        return web.json_response(
            {"error": f"no current.md for {doc_param!r}"}, status=404,
        )
    baseline = doc_path.parent / "baseline.md"
    # Safety: capture the previous baseline (if it existed) as a snap
    # so it's recoverable from the History panel.
    if baseline.exists():
        try:
            save_snapshot(baseline, baseline.read_bytes())
        except OSError as e:
            return web.json_response(
                {"error": f"could not snapshot old baseline: {e}"}, status=500,
            )
    # The canonical artifact stays pristine: strip the working copy's
    # system-stamped block ids before pinning it as the baseline. Fall back to
    # a verbatim byte copy if the working copy somehow isn't valid UTF-8
    # (shouldn't happen — agent + validator write UTF-8 — but don't 500).
    try:
        clean = strip_block_ids(doc_path.read_bytes().decode("utf-8"))
        baseline.write_text(clean, encoding="utf-8", newline="")
    except UnicodeDecodeError:
        baseline.write_bytes(doc_path.read_bytes())
    slug = _doc_slug_from_path(doc_path) or doc_param
    print(
        f"[save-baseline] docs/{slug}/baseline.md <- current.md (ids stripped)",
        flush=True,
    )
    return web.json_response({"doc": slug, "ok": True})


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


# Whitelist of env var names the first-run setup card may write. Keeps
# the endpoint from being a generic ".env writer" (a future foothold for
# anyone who tricks a reader into POSTing arbitrary keys).
_API_KEY_VARS = frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY"})
# Surface-shape validators per provider. Catches paste mistakes (extra
# whitespace, missing prefix) at the boundary so the user gets feedback
# now instead of a cryptic auth error later. Not a security check —
# the provider's auth service is what actually validates the key.
_API_KEY_SHAPE = {
    "ANTHROPIC_API_KEY": re.compile(r"^sk-ant-[A-Za-z0-9_\-]{20,}$"),
    "OPENAI_API_KEY":    re.compile(r"^sk-[A-Za-z0-9_\-]{20,}$"),
}


async def set_api_key(request: web.Request) -> web.Response:
    """POST /api-key — first-run setup card writes the user's API key to
    .env so subsequent backend starts pick it up. Also injects into the
    running process's os.environ (so any code path that reads env at
    call-time benefits without a restart), but the reader is told to
    restart anyway because the active agent runtime may have cached
    its auth from process start.

    Body JSON: {"var": "ANTHROPIC_API_KEY", "key": "sk-ant-…"}
    """
    _require_localhost_origin(request)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    var = (data.get("var") or "").strip()
    key = (data.get("key") or "").strip()
    if var not in _API_KEY_VARS:
        return web.json_response(
            {"error": f"unsupported env var: {var!r}"}, status=400,
        )
    shape = _API_KEY_SHAPE.get(var)
    if shape is not None and not shape.fullmatch(key):
        return web.json_response(
            {"error": f"{var} doesn't match the expected shape — paste error?"},
            status=400,
        )
    # .env lives in DATA_ROOT (which is AM_DATA_DIR if set, otherwise the
    # repo root). An installed shell will typically route this under a
    # per-user data dir so the API key persists across upgrades and
    # survives a read-only install location.
    env_path = DATA_ROOT / ".env"
    # Read existing .env, replace the line for this var (if present), or
    # append. Comment-prefixed and blank lines pass through untouched.
    lines: list[str] = []
    if env_path.exists():
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            return web.json_response(
                {"error": f"could not read .env: {e}"}, status=500,
            )
    new_line = f"{var}={key}"
    replaced = False
    for i, raw in enumerate(lines):
        s = raw.strip()
        if s.startswith("#") or "=" not in s:
            continue
        existing_key = s.split("=", 1)[0].strip()
        if existing_key == var:
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)
    try:
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        return web.json_response(
            {"error": f"could not write .env: {e}"}, status=500,
        )
    # Inject into current process so non-runtime call sites pick it up
    # (am_convert.py reads ANTHROPIC_API_KEY at call-time, for example).
    os.environ[var] = key
    print(f"[setup] wrote {var} to .env", flush=True)

    # Restart the agent runtime in-place so it captures the new key.
    # The Claude SDK reads ANTHROPIC_API_KEY at start() time, which
    # already ran with the key missing — without this restart the
    # user would have to kill + relaunch the whole backend, which is
    # impossible in an installed desktop shell. We tear down + reboot
    # the runtime; chat history resets (acceptable on first-run).
    try:
        await shutdown_runtime()
        await init_runtime()
    except Exception as e:
        print(f"[setup] runtime restart failed after key save: {e!r}",
              flush=True)
        return web.json_response({
            "ok": False,
            "error": (
                f"Saved {var} to .env, but failed to restart the agent "
                f"runtime: {type(e).__name__}: {e}. Close and relaunch the "
                "app to pick up the new key."
            ),
        }, status=500)

    # Notify any other open WS clients so their setup card clears too.
    # (Single-window is the common case; multi-tab still possible in dev.)
    await state.broadcast({
        "type": "api_key_set", "var": var,
        "provider": state.current_provider,
        "model": state.current_model,
    })
    print(f"[setup] runtime restarted with {var}", flush=True)
    return web.json_response({
        "ok": True, "var": var,
        "message": "Saved. The agent is ready — chat below.",
    })


def _read_component_metadata(slug: str) -> dict:
    """Parse a component's YAML-ish frontmatter into a metadata dict.
    Returns at minimum {"slug": slug}; adds name, description, tags,
    self_contained if the file has them. Robust to malformed YAML —
    we don't import pyyaml here (no other module in the project does
    either), so the parse is line-oriented and limited to the keys we
    care about."""
    out: dict = {"slug": slug}
    f = COMPONENTS_ROOT / f"{slug}.md"
    if not f.exists():
        return out
    try:
        src = f.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    m = _FRONTMATTER_RE.match(src)
    if not m:
        return out
    fm_text = m.group(1)
    # Hand-parsed: each key: value at top level. Skip indented continuation
    # lines (sub-mappings like `provenance: { ... }`). Tags can be either
    # an inline JSON list `[a, b]` or omitted.
    for raw in fm_text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#") or line.startswith(" "):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip().strip('"').strip("'")
        if key in ("name", "description"):
            out[key] = value
        elif key == "self_contained":
            out[key] = value.lower() in ("true", "yes", "1")
        elif key == "tags":
            # Inline list `[a, b, c]` — best-effort split.
            if value.startswith("[") and value.endswith("]"):
                inner = value[1:-1]
                out["tags"] = [t.strip().strip('"').strip("'")
                               for t in inner.split(",") if t.strip()]
    # Default name = slug if frontmatter omitted it.
    out.setdefault("name", slug)
    return out


async def list_components(request: web.Request) -> web.Response:
    """GET /components — return metadata for every saved component.
    Drives the agent's "list my components" path + the Components tab
    in the viewer."""
    _require_localhost_origin(request)
    slugs = list_all_components()
    return web.json_response({
        "components": [_read_component_metadata(s) for s in slugs],
    })


def _component_path_for(slug: str) -> Path | None:
    """Validate a slug and return its components/<slug>.md path.
    Returns None if the slug is malformed (the caller should 400)."""
    if not slug or not COMPONENT_SLUG_RE.match(slug):
        return None
    return COMPONENTS_ROOT / f"{slug}.md"


async def save_component(request: web.Request) -> web.Response:
    """POST /components/<slug> — write a component file directly from
    the viewer (Components-tab inline editor + Add-new flow). Body JSON:
    {"content": "<full .md including frontmatter>"}.

    Distinct from the agent's write path: this is a user-blessed direct
    write (the click came from the viewer chrome, not from a chat turn),
    so it skips the pre-edit hook + validator. We still validate the
    slug shape, ensure components/ exists, and reject content that's
    obviously not markdown (binary bytes / extreme size)."""
    _require_localhost_origin(request)
    slug = request.match_info.get("slug", "").strip()
    p = _component_path_for(slug)
    if p is None:
        return web.json_response(
            {"error": f"invalid component slug: {slug!r}"}, status=400,
        )
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    content = data.get("content", "")
    if not isinstance(content, str):
        return web.json_response(
            {"error": "content must be a string"}, status=400,
        )
    if len(content) > 256 * 1024:
        return web.json_response(
            {"error": "component too large (>256KB)"}, status=413,
        )
    try:
        COMPONENTS_ROOT.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="")
    except OSError as e:
        return web.json_response(
            {"error": f"could not write component: {e}"}, status=500,
        )
    print(f"[components] saved {slug}.md ({len(content)} chars)", flush=True)
    # The filesystem watcher will broadcast {type:"components"} within
    # ~1.5s, refreshing any open Components tab. No need to broadcast
    # here too.
    return web.json_response({
        "ok": True, "slug": slug, "size": len(content),
    })


async def delete_component(request: web.Request) -> web.Response:
    """DELETE /components/<slug> — remove a component file. The viewer's
    Components-tab Delete button gates this with a confirm()."""
    _require_localhost_origin(request)
    slug = request.match_info.get("slug", "").strip()
    p = _component_path_for(slug)
    if p is None:
        return web.json_response(
            {"error": f"invalid component slug: {slug!r}"}, status=400,
        )
    if not p.exists():
        return web.json_response(
            {"error": f"component {slug!r} not found"}, status=404,
        )
    try:
        p.unlink()
    except OSError as e:
        return web.json_response(
            {"error": f"could not delete component: {e}"}, status=500,
        )
    print(f"[components] deleted {slug}.md", flush=True)
    return web.json_response({"ok": True, "slug": slug})
