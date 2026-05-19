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
