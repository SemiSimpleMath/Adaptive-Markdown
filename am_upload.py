"""Drop-to-upload route handlers.

POST /upload — readers drop a `.md` / `.tex` / `.pdf` / music file onto
the +Doc affordance. We sniff the extension, dispatch to the right
saver, and broadcast a "docs" update so every connected viewer sees
the new doc in its dropdown.

POST /upload-asset — drop an image / audio / video / data file onto an
open doc. Lands at `docs/<slug>/assets/<sanitized-name>` with a chat-
side system message telling the agent the asset is referenceable.

Format-specific paths live as private helpers (_upload_music_doc,
_upload_binary_doc) inside this module — they share the slug
derivation, the streaming size cap, the broadcast, and the response
shape, so they live together rather than in per-format modules.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from aiohttp import web

from am_convert import (
    _LLM_CONVERT_TEXT_EXTS,
    _convert_pdf_via_claude, _convert_tex_via_claude,
)
from am_docs import DOC_SLUG_RE, DOCS_ROOT, ROOT, ensure_doc_ids, list_all_docs
from am_origin import _require_localhost_origin
from am_state import state

# Optional: markitdown is used for server-side conversion of binary docs
# (PDF, DOCX, XLSX, PPTX). If it isn't installed, the binary upload path
# returns 501 rather than crashing the whole backend.
try:
    from markitdown import MarkItDown  # type: ignore
    _MARKITDOWN = MarkItDown()
except Exception:  # pragma: no cover - optional dep
    MarkItDown = None  # type: ignore
    _MARKITDOWN = None

# Caps and routing tables.
_UPLOAD_MAX_BYTES = 1 * 1024 * 1024  # 1 MB safety cap for text imports
_ASSET_MAX_BYTES = 25 * 1024 * 1024  # 25 MB cap for asset drops (images, audio, etc.)
_BINARY_CONVERT_MAX_BYTES = 25 * 1024 * 1024  # 25 MB cap for PDF/DOCX/... imports

# Binary doc formats we convert server-side. These don't go through the
# text-flow's NUL-byte guard / UTF-8 decode; they're saved as `original.<ext>`
# and the converter writes the resulting markdown to `current.md` +
# `baseline.md`. Reader gets a fully-converted doc back — the agent is not
# in the loop for the conversion itself.
_BINARY_CONVERT_EXTS = {".pdf", ".docx", ".pptx"}  # .xlsx → data-figure path

# Music file imports: .abc / .musicxml are text source we just wrap in a
# <figure class="music"> block; .mid / .midi are binary, saved as an
# asset that a <midi-player> references. All renderers lazy-load their
# CDN library only when a doc contains music, so zero cost when not used.
_MUSIC_TEXT_EXTS = {".abc", ".musicxml", ".mxl", ".xml"}
_MUSIC_BINARY_EXTS = {".mid", ".midi"}
_MUSIC_TEXT_MAX_BYTES = 2 * 1024 * 1024   # 2MB cap on music source text
_MUSIC_BINARY_MAX_BYTES = 5 * 1024 * 1024  # 5MB cap on MIDI binaries

# Data-table imports: .csv is text source we wrap in <figure class="data">
# with a <script type="text/csv"> inner element (script-with-non-JS-type
# trick so the browser doesn't try to HTML-parse the CSV). .xlsx is binary;
# the active sheet is extracted to CSV server-side via openpyxl and then
# follows the same path. Live grid (Tabulator) loads lazily in the iframe
# only when a doc contains data — zero cost when not used.
_DATA_TEXT_EXTS = {".csv"}
_DATA_BINARY_EXTS = {".xlsx"}
_DATA_TEXT_MAX_BYTES = 5 * 1024 * 1024   # 5MB cap on CSV source
_DATA_BINARY_MAX_BYTES = 25 * 1024 * 1024  # 25MB cap on XLSX binaries

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


def _music_inner_div_class(ext: str) -> str:
    """Map the upload extension to the inner div class the iframe runtime
    recognizes. .xml is treated as MusicXML when accompanied by an
    .mxl/.musicxml-style structure — there's no portable way to tell from
    extension alone, so we default to musicxml for .xml in this music
    upload path."""
    if ext == ".abc":
        return "abc"
    return "musicxml"


# Per-doc agent-skill block we bake into freshly-uploaded music docs.
# This is read by the agent in its chat preamble (via the
# `<section class="agent-skill">` convention) and tells it the substrate
# rules for THIS doc — saves it from re-deriving where the XML lives,
# which library renders it, and how to mutate safely. The mechanism is
# general (any auto-imported doc with a known renderer can ship its own
# context block); this constant happens to cover music.
_MUSIC_AGENT_SKILL = """<section class="agent-skill">

**Music doc — substrate rules**

This doc renders a music figure through the iframe runtime. The source
lives inside `<figure class="music">` either as `<div class="abc">…</div>`
(ABC notation, rendered by abcjs) or as
`<script type="application/vnd.recordare.musicxml+xml">…</script>`
(MusicXML, rendered by OpenSheetMusicDisplay 1.9, lazy-loaded). The
visible notation is a runtime-generated sibling div (`.abc-notation` or
`.musicxml-render`) — it isn't in the source, the renderer puts it there.

**To mutate the score from a widget, pick ONE pattern.** Don't mix them
in the same widget unless you have a reason — they have different
semantics about what the source file ends up containing.

1. *In-place transformation* — when the renderer has its own API for
   what you want (e.g. transpose, key change in OSMD). Source XML stays
   in the original key forever; the change is render-time only.

   ```js
   const r = window.__doc.getRenderer(figure);
   if (r && r.kind === 'musicxml') {
     const ns = window.opensheetmusicdisplay;
     if (ns && ns.TransposeCalculator && !r.instance.TransposeCalculator) {
       r.instance.TransposeCalculator = new ns.TransposeCalculator();
     }
     r.instance.Sheet.Transpose = semitones;
     r.instance.UpdateGraphic();
     r.instance.render();
   }
   ```

2. *Source mutation + full re-render* — when the renderer has no
   API and the source itself should change (edit specific notes, rewrite
   lyrics, add chord symbols). Edit the script's `textContent` with
   **string-level operations** (`String.prototype.replace`, regex), not
   `DOMParser` + `XMLSerializer`. Serializers strip DOCTYPE, add
   `xmlns=""`, reorder attributes, normalize whitespace — OSMD then
   rejects the result with "Document is not a valid 'partwise' MusicXML".

   ```js
   const script = figure.querySelector(
     'script[type="application/vnd.recordare.musicxml+xml"]'
   );
   script.textContent = script.textContent.replace(/<step>C<\\/step>/g, '<step>D</step>');
   await window.__doc.rerender(figure);
   ```

`window.__doc.rerender(figureEl)` re-runs the music renderer on the
figure; safe to call whenever the source changes.

</section>
"""


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
    if not slug_base or not DOC_SLUG_RE.match(slug_base):
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
            "</figure>\n\n"
            + _MUSIC_AGENT_SKILL
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
            + "\n</figure>\n\n"
            + _MUSIC_AGENT_SKILL
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


# Per-doc agent-skill for data-table docs. Same mechanism as
# _MUSIC_AGENT_SKILL — baked in at upload so the agent reads the
# substrate rules in the chat preamble and doesn't re-derive them.
_DATA_AGENT_SKILL = """<section class="agent-skill">

**Data-table doc — substrate rules**

This doc renders a data table through the iframe runtime. The source
lives inside `<figure class="data">` as
`<script type="text/csv">…CSV…</script>` (script-with-non-JS-type so
the browser doesn't HTML-parse the CSV content). The visible table is
a runtime-generated sibling `.data-grid` div rendered via Tabulator
(lazy-loaded from CDN). The script element stays in the DOM as the
source of truth.

**To mutate the table from a widget, pick ONE pattern.** Don't mix
them in the same widget unless you have a reason — they have
different semantics about what the source CSV ends up containing.

1. *In-place transformation* — when the visible table should change
   without rewriting the source (filter by column, sort, hide rows,
   highlight cells). Use the Tabulator instance:

   ```js
   const r = window.__doc.getRenderer(figure);
   if (r && r.kind === 'csv') {
     r.instance.setFilter('status', '=', 'active');
     // or: r.instance.setSort('name', 'asc');
     // or: r.instance.selectRow([2, 5, 7]);
   }
   ```

   Source CSV stays as-is. Tabulator API docs:
   https://tabulator.info/docs/5.5

2. *Source mutation + full re-render* — when the source CSV itself
   should change (add/remove rows, edit cell values). Edit the
   script's `textContent` with **string-level operations on CSV
   lines** (split on `\\n`, rejoin), not by parsing into objects and
   re-serializing — round-tripping through a parser loses quoting,
   whitespace, and trailing-comma conventions.

   ```js
   const script = figure.querySelector('script[type="text/csv"]');
   const lines = script.textContent.split('\\n');
   lines.push('99,New Row,active');
   script.textContent = lines.join('\\n');
   await window.__doc.rerender(figure);
   ```

`window.__doc.rerender(figureEl)` re-runs the data renderer; safe to
call whenever the source changes.

</section>
"""


def _xlsx_to_csv(raw: bytes) -> tuple[str | None, str]:
    """Extract the active sheet of an XLSX to a CSV string. Returns
    (csv_text, error_message). csv_text is None if extraction failed;
    error_message names the failure (missing-dep, parse-error, empty-sheet).

    openpyxl is an optional dep — the data-figure path advertises XLSX
    support only when it's importable, otherwise this returns a clear
    install-instruction error and the caller responds 501."""
    try:
        import openpyxl  # type: ignore
    except ImportError:
        return None, (
            "XLSX support requires openpyxl. "
            "Install with `pip install openpyxl` and restart the server."
        )
    import csv as _csv
    import io
    try:
        wb = openpyxl.load_workbook(
            io.BytesIO(raw), data_only=True, read_only=True,
        )
    except Exception as e:
        return None, f"could not read XLSX: {type(e).__name__}: {e}"
    try:
        ws = wb.active
        if ws is None:
            return None, "XLSX has no active sheet"
        buf = io.StringIO()
        writer = _csv.writer(buf, lineterminator="\n")
        rows_written = 0
        for row in ws.iter_rows(values_only=True):
            writer.writerow(["" if c is None else c for c in row])
            rows_written += 1
        if rows_written == 0:
            return None, "XLSX active sheet is empty"
        return buf.getvalue().rstrip("\n"), ""
    finally:
        wb.close()


async def _upload_data_doc(
    request: web.Request,
    field: "web.BodyPartReader",
    raw_name: str,
    ext: str,
) -> web.Response:
    """Drop a .csv or .xlsx file onto +Doc and you get a new doc whose
    body is a single `<figure class="data">` block containing the CSV
    source. The iframe runtime renders it via Tabulator on first view.
    XLSX is server-side-extracted to CSV (active sheet, first sheet if
    no active marker) via openpyxl before wrapping."""
    is_binary = ext in _DATA_BINARY_EXTS
    cap = _DATA_BINARY_MAX_BYTES if is_binary else _DATA_TEXT_MAX_BYTES

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

    if is_binary:
        csv_text, err = _xlsx_to_csv(raw)
        if csv_text is None:
            status = 501 if "openpyxl" in err else 422
            return web.json_response({"error": err}, status=status)
    else:
        # CSV text path: strip BOM, normalize line endings, trim trailing
        # whitespace. NUL guard skipped — CSV files sometimes contain
        # nothing-special-but-binary-looking bytes in fields; we trust
        # the .csv extension here.
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        elif raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
            raw = raw.decode("utf-16").encode("utf-8")
        csv_text = (raw.decode("utf-8", errors="replace")
                    .replace("\r\n", "\n").replace("\r", "\n")
                    .rstrip("\n"))
        if not csv_text:
            return web.json_response(
                {"error": "CSV is empty after decoding"}, status=400,
            )

    # Slug derivation matches the rest of the upload paths.
    raw_stem = Path(raw_name).stem
    slug_base = re.sub(r"[^a-z0-9-]+", "-", raw_stem.lower()).strip("-")
    if not slug_base or not DOC_SLUG_RE.match(slug_base):
        slug_base = "data"
    slug = slug_base
    counter = 1
    while (DOCS_ROOT / slug).exists():
        counter += 1
        slug = f"{slug_base}-{counter}"
    doc_dir = DOCS_ROOT / slug
    doc_dir.mkdir(parents=True, exist_ok=True)
    title = raw_stem

    body_md = (
        f'---\ntitle: "{title}"\n---\n\n'
        f"# {title}\n\n"
        '<figure class="data">\n'
        '<script type="text/csv" class="data-csv-source">\n'
        + csv_text
        + '\n</script>\n'
        "</figure>\n\n"
        + _DATA_AGENT_SKILL
    )

    (doc_dir / "current.md").write_text(body_md, encoding="utf-8", newline="")
    (doc_dir / "baseline.md").write_text(body_md, encoding="utf-8", newline="")
    ensure_doc_ids()
    await state.broadcast({
        "type": "docs", "list": list_all_docs(), "doc": slug,
    })
    print(
        f"[upload:data] docs/{slug}/current.md "
        f"({total}B {ext} → {len(csv_text)} chars CSV)",
        flush=True,
    )
    return web.json_response({
        "path": f"docs/{slug}/current.md",
        "slug": slug,
        "name": "current.md",
        "kind": ext,
        "converted": True,
        "converter": "data",
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
    if not slug_base or not DOC_SLUG_RE.match(slug_base):
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

    # Data-table imports: .csv (text) or .xlsx (binary, extracted to CSV
    # via openpyxl) → `<figure class="data">` rendered live via Tabulator.
    # Sits before the binary-doc branch so .xlsx is owned here; falls back
    # nowhere else if openpyxl is absent — _upload_data_doc returns a 501
    # with install instructions in that case.
    if ext in _DATA_TEXT_EXTS or ext in _DATA_BINARY_EXTS:
        return await _upload_data_doc(request, field, raw_name, ext)

    # Binary-doc imports (PDF, DOCX, PPTX) go through markitdown server-side.
    # They're not text and would fail the NUL-byte guard below; branch out
    # before any of the text-handling.
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
    if not slug_base or not DOC_SLUG_RE.match(slug_base):
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


async def upload_asset(request: web.Request) -> web.Response:
    """POST /upload-asset?doc=<slug> with multipart `file` — saves the
    file to docs/<slug>/assets/<sanitized-name>. The agent gets told via
    a chat notice that the asset is now referenceable as
    `assets/<name>` from the doc body."""
    _require_localhost_origin(request)
    slug = (request.query.get("doc") or "").strip()
    doc_dir = DOCS_ROOT / slug if slug else None
    if not slug or not DOC_SLUG_RE.match(slug) or doc_dir is None \
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
