"""HTML-block extraction over markdown source.

Replaces the regex-based "find <tag>...</tag>" pattern that was
scattered across `validators.py`, `am_routes.py`, and (in the JS
side) `index.html`'s preserveStructuralBlocks. Regex misses real
edge cases: a literal `</script>` inside a JS template literal,
attributes containing `>`, opening tags split across multiple lines,
same-tag nesting. This module handles all of those correctly by
walking source character-by-character with awareness of HTML spec
semantics:

  - **Script / style opacity.** Inside `<script>` and `<style>`,
    NOTHING is HTML-parsed until the matching close tag. The
    browser's parser ends the script at the FIRST `</script>` token
    encountered — even one in a JS comment or template literal.
    `find_blocks` matches that behavior exactly so what the validator
    sees matches what the browser sees.

  - **Same-tag nesting for other tags.** A `<section>` can contain
    another `<section>`. We track depth via a stack and pair closing
    tags to the most-recently-opened same-named open.

  - **Markdown context tolerance.** The source is markdown, not pure
    HTML — most of it isn't tags at all. We only act on `<tag>`
    boundaries that match our target; everything else is skipped.

  - **Byte-accurate offsets.** Each Block carries start/end and
    body_start/body_end offsets, so callers can splice the source
    directly without re-scanning.

Not in scope: the JS-side preprocessor (`preserveStructuralBlocks`
in index.html) has the same bug class but lives in the browser
markdown render pipeline. A parallel `am_html.js` (or a switch to
`DOMParser`) would be the analogous fix there.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

# Tags whose bodies are opaque per HTML spec — only `</tag>` terminates
# them, regardless of what's inside (HTML comments, JS strings, nested
# tags don't count). Matches the browser parser's behavior.
_OPAQUE_TAGS = frozenset({"script", "style"})

# Void elements that have no body and no closing tag. We skip them
# entirely when scanning — a `<br>` or `<img>` in markdown is not
# something `find_blocks` could meaningfully return.
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "source", "track", "wbr",
})


@dataclass
class Block:
    """A single matched HTML block.

    Offsets are byte indices into the source string; `body` is the
    pre-sliced content for convenience, but callers that need to
    replace the block should splice via `source[:start] + new + source[end:]`.
    """
    tag: str
    attrs: dict[str, str]
    body: str
    start: int
    end: int
    body_start: int
    body_end: int
    line: int  # 1-indexed line of the opening '<'

    # Internal stack frame field — only meaningful while parsing is
    # in progress. Callers shouldn't read this.
    _stack_depth: int = field(default=0, repr=False)


_ATTR_RE = re.compile(
    r"""\s+
        ([A-Za-z_:][\w:.\-]*)                    # attr name
        (?:
          \s*=\s*
          (?:
              "([^"]*)"                          # double-quoted value
            | '([^']*)'                          # single-quoted value
            | ([^\s>]+)                          # unquoted value
          )
        )?
    """,
    re.VERBOSE,
)


def _parse_attrs(opening_tag_inner: str) -> dict[str, str]:
    """Parse attributes from the inner text of an opening tag — the
    bit between `<tagname` and `>`. Handles quoted / unquoted /
    boolean attributes. Returns a dict (last-wins on duplicates,
    matching browser parser behavior)."""
    out: dict[str, str] = {}
    for m in _ATTR_RE.finditer(opening_tag_inner):
        name = m.group(1).lower()
        value = m.group(2) if m.group(2) is not None else (
            m.group(3) if m.group(3) is not None else (
                m.group(4) if m.group(4) is not None else ""
            )
        )
        out[name] = value
    return out


def _match_opening_tag(source: str, i: int, target: str) -> dict | None:
    """If `source[i]` is the start of an opening `<target ...>` tag,
    return {end: int_offset_after_>, attrs: dict, self_closing: bool}.
    Otherwise return None.

    `i` should point at the `<` character. Case-insensitive match
    on tag name. Skips comments / processing instructions / closing
    tags — those are not what we're looking for."""
    if i >= len(source) or source[i] != '<':
        return None
    # Quick rejects: comment / CDATA / processing instruction / closing tag
    if source.startswith('<!', i) or source.startswith('<?', i) \
            or source.startswith('</', i):
        return None
    tlen = len(target)
    # Tag name must match exactly + be followed by whitespace, > or /
    if not source[i + 1:i + 1 + tlen].lower() == target.lower():
        return None
    next_char_idx = i + 1 + tlen
    if next_char_idx >= len(source):
        return None
    nc = source[next_char_idx]
    if nc not in (' ', '\t', '\n', '\r', '>', '/'):
        # Could be e.g. <section1> when target is "section" — not a match
        return None
    # Find the closing '>' of the opening tag, respecting quoted attribute
    # values (so a `>` inside `data-foo="x > y"` doesn't terminate us).
    j = next_char_idx
    in_quote: str | None = None
    while j < len(source):
        c = source[j]
        if in_quote:
            if c == in_quote:
                in_quote = None
            j += 1
            continue
        if c in ('"', "'"):
            in_quote = c
            j += 1
            continue
        if c == '>':
            inner = source[next_char_idx:j]
            self_closing = inner.endswith('/')
            if self_closing:
                inner = inner[:-1]
            return {
                "end": j + 1,
                "attrs": _parse_attrs(inner),
                "self_closing": self_closing,
            }
        j += 1
    # Ran off the end without finding `>` — malformed, treat as not a tag
    return None


def _find_close_opaque(source: str, start_from: int, target: str) -> int | None:
    """Find the byte offset of the `<` in `</target>` after `start_from`,
    treating script/style bodies as opaque per HTML spec. Returns None
    if no matching close tag exists.

    Matches the browser's behavior: the FIRST `</target>` token
    terminates the body, even if it's inside a JS comment or string
    literal. This is intentional — the validator should see what the
    browser sees, so source that breaks in the browser also fails
    validation."""
    needle = f"</{target}".lower()
    i = start_from
    src_lower_view = source.lower()
    while True:
        idx = src_lower_view.find(needle, i)
        if idx < 0:
            return None
        # Confirm it's actually a closing tag end: must be followed by
        # whitespace or `>` (otherwise `</scripted>` would falsely match
        # `</script`).
        after = idx + len(needle)
        if after >= len(source):
            return None
        if source[after] in (' ', '\t', '\n', '\r', '>', '/'):
            return idx
        i = after


def _find_close_nesting(source: str, start_from: int, target: str) -> int | None:
    """For non-opaque tags: walk forward tracking same-tag nesting.
    Returns offset of the `<` in the matching `</target>`, accounting
    for nested `<target>...</target>` pairs in between."""
    needle_open = f"<{target}".lower()
    needle_close = f"</{target}".lower()
    src_lower_view = source.lower()
    depth = 1
    i = start_from
    while i < len(source):
        next_open = src_lower_view.find(needle_open, i)
        next_close = src_lower_view.find(needle_close, i)
        if next_close < 0:
            return None  # no closing tag at all
        # If an opening of the same tag comes first, increase depth
        if 0 <= next_open < next_close:
            # Verify it's really an opening tag (not a substring like
            # `<sectioned`). _match_opening_tag handles the verification.
            m = _match_opening_tag(source, next_open, target)
            if m:
                depth += 1
                i = m["end"]
                continue
            # Spurious substring match — skip past
            i = next_open + len(needle_open)
            continue
        # Verify the closing tag is really one (next char restriction)
        after = next_close + len(needle_close)
        if after < len(source) and source[after] in (' ', '\t', '\n', '\r', '>', '/'):
            depth -= 1
            if depth == 0:
                return next_close
            # Otherwise we matched a nested close; skip past
            i = after
            continue
        i = after


def find_blocks(
    source: str,
    tag: str,
    where: Callable[[dict[str, str]], bool] | None = None,
) -> list[Block]:
    """Scan `source` for all `<tag>...</tag>` blocks, returning one
    Block per matched pair.

    For `<script>` and `<style>`, body is opaque (terminates at FIRST
    `</tag>`, matching the browser). For other tags, same-tag nesting
    is tracked correctly via a stack.

    Optional `where` predicate filters by attributes — e.g.,
    `where=lambda a: a.get("class") == "agent-skill"`. Returned only
    when both the tag matches and the predicate returns truthy.

    Self-closing tags (`<br />`, `<img />`, etc.) are skipped — they
    have no body. Void elements are skipped even without the slash.

    **Top-level only.** Same-tag nesting is correctly resolved so the
    OUTER block's body contains the inner verbatim, but the inner is
    not returned as a separate Block. This matches how every real
    caller wants to use the result (validators want all top-level
    `<script>`; agent-skill wants all top-level sections; nested
    same-tag is either impossible or pathological). If you ever need
    nested matches, recurse on each block's body.

    Performance: O(n) for the source-length scan plus per-block close-tag
    search. Markdown source is small (50KB cap on inlined docs); the
    scanner runs in microseconds.
    """
    target = tag.lower()
    if target in _VOID_TAGS:
        return []  # void elements have no body, nothing to extract
    where = where or (lambda a: True)
    is_opaque = target in _OPAQUE_TAGS

    blocks: list[Block] = []
    i = 0
    line = 1
    src_len = len(source)

    while i < src_len:
        c = source[i]
        if c == '\n':
            line += 1
            i += 1
            continue
        if c != '<':
            i += 1
            continue
        m = _match_opening_tag(source, i, target)
        if not m:
            i += 1
            continue
        if m["self_closing"]:
            i = m["end"]
            continue
        if not where(m["attrs"]):
            # Tag matches but caller doesn't want it; skip past the
            # opening tag and resume scanning (could contain nested
            # blocks the caller WOULD want).
            i = m["end"]
            continue

        start = i
        body_start = m["end"]
        opening_line = line

        if is_opaque:
            body_end = _find_close_opaque(source, body_start, target)
        else:
            body_end = _find_close_nesting(source, body_start, target)

        if body_end is None:
            # Unmatched opening tag. Don't consume the whole rest of
            # the doc — just move past this opening and keep scanning.
            i = body_start
            continue

        # Advance past the closing tag: find the '>' after body_end.
        close_gt = source.find('>', body_end)
        end = close_gt + 1 if close_gt >= 0 else body_end

        blocks.append(Block(
            tag=target,
            attrs=m["attrs"],
            body=source[body_start:body_end],
            start=start,
            end=end,
            body_start=body_start,
            body_end=body_end,
            line=opening_line,
        ))

        # Update line count for the spanned region (so subsequent block
        # line numbers stay accurate).
        line += source[start:end].count('\n')
        i = end

    return blocks
