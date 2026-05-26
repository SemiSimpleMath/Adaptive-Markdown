"""Block-level tracking IDs (lazy mint on intent-to-reference).

When a chat arrives with a focused block lacking a track_id, mint one
server-side and write `<!-- id:b-... -->` immediately preceding the block
in source. Subsequent references use the now-stable ID.

Also: alias bookkeeping (when a block is renamed / merged / dropped, its
historical id is recorded so back-references resolve), block-span
finding for the inline-edit handler, and id-drift detection so the
snapshot path can tombstone disappeared ids.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from am_ids import gen_id
from am_docs import DOC_SLUG_RE, DOCS_ROOT, FRONTMATTER_RE

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


# --- Block classification (CommonMark-aware) -------------------------------
# ONE source of truth, shared by the inline-edit locator AND the eager
# id-stamper, so the two can never disagree about "what is a top-level
# paragraph/heading vs. a block we must not touch." Getting this right in a
# single place is the root-cause fix for the class of bugs where a hand-rolled
# first-character heuristic diverged from how the document actually parses.

# CommonMark HTML block-start tags (type 6): a line opening with one of these
# begins a structural HTML *block*. A line opening with any OTHER tag (e.g.
# <em>, <a>, <code>, <span>) is a paragraph that merely starts with inline
# HTML — which IS a stampable / inline-editable paragraph.
_HTML_BLOCK_TAGS = frozenset((
    "address", "article", "aside", "base", "basefont", "blockquote", "body",
    "caption", "center", "col", "colgroup", "dd", "details", "dialog", "dir",
    "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
    "frame", "frameset", "h1", "h2", "h3", "h4", "h5", "h6", "head", "header",
    "hr", "html", "iframe", "legend", "li", "link", "main", "menu",
    "menuitem", "nav", "noframes", "ol", "optgroup", "option", "p", "param",
    "section", "source", "summary", "table", "tbody", "td", "tfoot", "th",
    "thead", "title", "tr", "track", "ul",
))
# Type-1 raw tags: their bodies are opaque (a comment inside breaks the code /
# fails JS validation). Masked wholesale by the stamper.
_RAW_TAGS = ("script", "style", "pre", "textarea")

_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_ATX_RE = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
_SETEXT_RE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
_THEMATIC_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_LIST_RE = re.compile(r"^ {0,3}(?:[-*+][ \t]|\d{1,9}[.)][ \t])")
_HTML_TAG_OPEN_RE = re.compile(r"^ {0,3}</?([a-zA-Z][a-zA-Z0-9-]*)")


def _atx_heading(line: str) -> bool:
    return bool(_ATX_RE.match(line))


def _html_block_start(line: str) -> bool:
    """True if the line opens a CommonMark HTML *block* (structural tag,
    comment, or processing instruction) — NOT a paragraph that merely begins
    with an inline tag like <em>/<a>/<code>."""
    s = line.lstrip(" ")
    if s.startswith(("<!--", "<![CDATA[", "<!", "<?")):
        return True
    m = _HTML_TAG_OPEN_RE.match(line)
    if not m:
        return False
    tag = m.group(1).lower()
    return tag in _HTML_BLOCK_TAGS or tag in _RAW_TAGS


def _nonparagraph_block_start(line: str) -> bool:
    """True if the line begins a block we never stamp and never inline-edit:
    indented code, fenced code, list, blockquote, table row, thematic break,
    or a block-level / raw HTML opening. (ATX headings are handled separately —
    they ARE stampable / editable.)"""
    if not line.strip():
        return False
    if len(line) - len(line.lstrip(" ")) >= 4:          # indented code block
        return True
    if _FENCE_RE.match(line) or _LIST_RE.match(line) or _THEMATIC_RE.match(line):
        return True
    s = line.lstrip(" ")
    if s.startswith((">", "|")):                        # blockquote / table row
        return True
    if s.startswith("<") and _html_block_start(line):   # block-level HTML
        return True
    return False


def _raw_close_re(tag: str) -> re.Pattern:
    return re.compile(r"</" + re.escape(tag) + r"\s*>", re.IGNORECASE)


# A raw open tag must be followed by whitespace, `/`, or `>`, so `<style-ish>`
# (a custom element) is NOT a `<style>` open. Without this anchor an unclosed
# pseudo-tag would open a raw region that never closes and swallow the rest of
# the document (every later block silently loses its id).
_RAW_OPEN_SCAN = re.compile(r"<(script|style|pre|textarea)(?=[\s/>])", re.IGNORECASE)


def _scan_line_raw(line: str, raw_tag: str | None) -> str | None:
    """Walk one line through every raw-region transition; return the tag still
    open at end of line (or None).

    `raw_tag=None` means "not inside a raw region" — scan for an opening
    `<script>`/`<style>`/`<pre>`/`<textarea>`. A tag name means "inside that
    region" — scan for its matching close, then any reopen. Handles multiple
    transitions on one line, e.g. `</script><script>` (close then reopen =>
    still open), so a tracking comment can never be stamped into the body of a
    second adjacent script. Tokenizer-grade, mirroring the renderer's own
    HTML-block scanner rather than a line-local substring guess."""
    i = 0
    while True:
        if raw_tag is None:
            om = _RAW_OPEN_SCAN.search(line, i)
            if not om:
                return None
            raw_tag, i = om.group(1).lower(), om.end()
        else:
            cm = _raw_close_re(raw_tag).search(line, i)
            if not cm:
                return raw_tag
            raw_tag, i = None, cm.end()


def _find_line_after_track_comment(lines: list[str], track_id: str) -> int | None:
    """Index of the block line that the `<!-- id:track_id -->` comment marks
    — i.e. the next non-blank, non-comment line after it. None if the comment
    isn't present (e.g. an external formatter stripped it)."""
    for i, line in enumerate(lines):
        m = _TRACK_COMMENT_RE.match(line)
        if m and m.group(1).upper() == track_id.upper():
            j = i + 1
            while j < len(lines) and (
                not lines[j].strip() or _TRACK_COMMENT_RE.match(lines[j])
            ):
                j += 1
            return j if j < len(lines) else None
    return None


def _find_block_line(lines: list[str], signature: dict) -> int | None:
    """Locate the line index where the block matching `signature` begins.

    Strategy, most-stable first:
      1. `track_id` — the `<!-- id:b-... -->` comment. Immune to text drift,
         duplicate prefixes, math, and edits elsewhere in the doc. This is the
         primary path now that ids are stamped on every top-level block.
      2. `anchor_id` — `## Foo {#anchor}` (headings with explicit anchors).
      3. `label` — heading text.
      4. `excerpt` — fuzzy first-chars match on a paragraph line. Last resort;
         only used when the block has no id (e.g. an external tool stripped
         the comment). Skips non-paragraph lines so a prefix collision can't
         land on a list/table/HTML line and get refused.
    Returns None if the block cannot be located unambiguously.
    """
    track_id = (signature.get("track_id") or "").strip()
    label = (signature.get("label") or "").strip()
    excerpt = (signature.get("excerpt") or "").strip()
    anchor_id = (signature.get("anchor_id") or "").strip()

    # 1) Stable identity — the tracking comment.
    if track_id:
        idx = _find_line_after_track_comment(lines, track_id)
        if idx is not None:
            return idx
        # Comment not found — fall through to content matching.

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
            # Only true paragraph lines are eligible. Skip headings and
            # non-paragraph block starts using the SAME classifier the stamper
            # uses, so a shared first-40 prefix can't mis-land on a list/table/
            # HTML line (which _find_block_span would refuse). A paragraph that
            # merely starts with inline HTML (<em>…) stays eligible.
            if (not stripped or _atx_heading(line)
                    or stripped.startswith(":::")
                    or _nonparagraph_block_start(line)):
                continue
            for candidate in (stripped, _strip_inline_markdown(stripped)):
                cand_first = re.sub(r"\s+", " ", candidate[:80]).strip()
                if cand_first.startswith(sig_first[:40]):
                    return i

    return None


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
    if not line.strip():
        return None
    if _HEADING_LINE_RE.match(line):
        return (idx, idx + 1, "heading")
    # Setext heading (text underlined by === / ---): refuse — editing the text
    # line alone would orphan the underline. Source view handles it.
    if idx + 1 < len(lines) and lines[idx + 1].strip() and _SETEXT_RE.match(lines[idx + 1]):
        return None
    # Refuse non-paragraph blocks (lists, fenced code, tables, blockquotes,
    # block-level HTML) via the shared classifier — Source view handles those.
    if _nonparagraph_block_start(line):
        return None
    # Paragraph: consume consecutive non-blank lines until the next block start.
    end = idx + 1
    while end < len(lines):
        nxt = lines[end]
        if (not nxt.strip() or _atx_heading(nxt)
                or _nonparagraph_block_start(nxt)
                or (nxt.strip() and _SETEXT_RE.match(nxt))):
            break
        end += 1
    return (idx, end, "paragraph")


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


# ---------------------------------------------------------------------------
# Eager block-id normalization.
#
# Inline-edit and insertion locate a block by its stable <!-- id:b-... -->
# tracking comment, not by fuzzy text matching. For that to be reliable the
# id must already be present, so the system stamps one before every top-level
# heading and paragraph. System-maintained — the agent never hand-authors
# these. The block model is the shared CommonMark-aware classifier above, so
# the stamper and the locator agree by construction.
#
# SAFETY (load-bearing): a <!-- ... --> comment is harmless inside
# <section>/<figure>/<aside> (every renderer ignores comments), but inside a
# fenced code block it renders as literal text / breaks the fence, and inside
# a <script>/<style>/<pre> body it corrupts the code and fails JS validation.
# Those two opaque region kinds are masked wholesale (they legitimately
# contain blank lines) and never stamped inside — including a raw tag that
# rides behind a wrapper on the same line, e.g. `<figure><script>`.
# ---------------------------------------------------------------------------


def ensure_block_ids_text(text: str) -> tuple[str, int]:
    """Return (normalized_text, n_added): stamp a tracking id before every
    top-level heading and paragraph that lacks one. Idempotent and strictly
    additive (only ever inserts `<!-- id:b-... -->` lines). Safe across YAML
    frontmatter, fenced code, and raw <script>/<style>/<pre> blocks. Expects
    LF-normalized input."""
    m = FRONTMATTER_RE.match(text)
    prefix = text[: m.end()] if m else ""
    body = text[m.end():] if m else text

    out: list[str] = []
    added = 0
    in_fence = False
    fence_marker = ""
    raw_tag: str | None = None       # open raw <script>/<style>/<pre> tag, if any
    at_boundary = True               # a block may begin on this line
    last_was_track_comment = False

    def stamp():
        nonlocal added
        if not last_was_track_comment:
            out.append(f"<!-- id:{gen_id('b')} -->")
            added += 1

    for line in body.split("\n"):
        # --- opaque regions: copy verbatim, never stamp inside, blank lines
        #     inside them do NOT open a boundary ---
        if in_fence:
            out.append(line)
            fm = _FENCE_RE.match(line)
            if (fm and not fm.group(3).strip()
                    and fm.group(2)[0] == fence_marker[0]
                    and len(fm.group(2)) >= len(fence_marker)):
                in_fence, at_boundary = False, True   # a block may start after
            continue
        if raw_tag is not None:
            # Inside a raw region: scan for close(s) and any reopen on this
            # line (handles </script><script>). Never stamp inside.
            raw_tag = _scan_line_raw(line, raw_tag)
            out.append(line)
            if raw_tag is None:
                at_boundary = True
            continue

        if not line.strip():
            # A blank opens a boundary but does NOT clear "previous block had
            # an id" — `<!-- id -->\n\n## H` still means H is id'd.
            out.append(line)
            at_boundary = True
            continue

        # --- openers of opaque regions (checked before classification so a
        #     `<figure><script>` body is masked regardless of the wrapper) ---
        fm = _FENCE_RE.match(line)
        if fm:
            in_fence, fence_marker = True, fm.group(2)
            out.append(line)
            at_boundary, last_was_track_comment = False, False
            continue
        # Raw open — only on a top-level HTML line (<=3 spaces indent, per
        # CommonMark) so prose like "use the `<script>` tag" can't trip it and
        # a 4-space-indented code sample is left to the classifier.
        if re.match(r"^ {0,3}<", line):
            nt = _scan_line_raw(line, None)
            if nt is not None:
                raw_tag = nt
                out.append(line)
                at_boundary, last_was_track_comment = False, False
                continue
        if _TRACK_COMMENT_RE.match(line):
            out.append(line)
            at_boundary, last_was_track_comment = True, True
            continue

        # --- classification (shared with the locator) ---
        # An ATX heading always begins its own block (it interrupts a
        # paragraph), so it's stampable regardless of an intervening blank —
        # this is what fixes `text\n# H` and `# H1\n## H2`.
        if _atx_heading(line):
            stamp()
            out.append(line)
            at_boundary, last_was_track_comment = True, False
            continue

        if at_boundary:
            if _nonparagraph_block_start(line):
                # list / quote / table / hr / block-HTML / indented code:
                # never stamped, never recursed into.
                out.append(line)
                at_boundary, last_was_track_comment = False, False
                continue
            # A real paragraph — including one that starts with inline HTML
            # (<em>…), which the old first-char heuristic wrongly skipped.
            stamp()
            out.append(line)
            at_boundary, last_was_track_comment = False, False
            continue

        # Continuation line of the current block — copy, never stamp.
        out.append(line)
        last_was_track_comment = False

    return prefix + "\n".join(out), added


def ensure_block_ids_for(path: Path) -> int:
    """Normalize one current.md in place. Reads as LF (the working-copy
    convention), writes back only when ids were added. Returns n_added."""
    try:
        text = path.read_bytes().decode("utf-8")
    except Exception:
        return 0
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    new_text, added = ensure_block_ids_text(text)
    if added:
        with path.open("w", encoding="utf-8", newline="") as f:
            f.write(new_text)
    return added


def ensure_block_ids() -> None:
    """First-run / startup backfill: stamp block ids on every docs/<slug>/
    current.md. Idempotent — a second pass adds nothing."""
    if not DOCS_ROOT.exists():
        return
    total = 0
    for sub in sorted(DOCS_ROOT.iterdir()):
        if not sub.is_dir() or not DOC_SLUG_RE.match(sub.name):
            continue
        md = sub / "current.md"
        if md.exists():
            total += ensure_block_ids_for(md)
    if total:
        print(f"ensured block ids ({total} new)", flush=True)


def strip_block_ids(text: str) -> str:
    """Remove the system-stamped `<!-- id:b-... -->` comment lines, producing a
    clean canonical artifact from the labeled working copy (save-as-baseline,
    export) so the committed / shared `.md` stays pristine.

    Fence/raw-aware, mirroring the stamper: only TOP-LEVEL id comments are
    dropped, so a literal id-syntax line a human authored inside a fenced code
    sample or a <script>/<style> body — e.g. a doc that teaches the AM format —
    is preserved. The exact inverse of ensure_block_ids_text; line endings of
    kept lines are preserved."""
    out: list[str] = []
    in_fence = False
    fence_marker = ""
    raw_tag: str | None = None
    for line in text.split("\n"):
        if in_fence:
            out.append(line)
            fm = _FENCE_RE.match(line)
            if (fm and not fm.group(3).strip()
                    and fm.group(2)[0] == fence_marker[0]
                    and len(fm.group(2)) >= len(fence_marker)):
                in_fence = False
            continue
        if raw_tag is not None:
            raw_tag = _scan_line_raw(line, raw_tag)
            out.append(line)
            continue
        fm = _FENCE_RE.match(line)
        if fm:
            in_fence, fence_marker = True, fm.group(2)
            out.append(line)
            continue
        if re.match(r"^ {0,3}<", line):
            nt = _scan_line_raw(line, None)
            if nt is not None:
                raw_tag = nt
                out.append(line)
                continue
        if _TRACK_COMMENT_RE.match(line):
            continue              # top-level system id comment — drop it
        out.append(line)
    return "\n".join(out)
