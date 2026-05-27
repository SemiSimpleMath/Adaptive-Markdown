"""Top-level block boundaries from the renderer's own grammar.

The single source of truth for "where does each top-level block begin and end
in the source." Used by the id-stamper (one id per block) and by the
block-source edit / insert / delete handlers.

Built on markdown-it-py — a port of the same markdown-it engine the iframe
renders with — so the backend and the frontend agree on block structure *by
construction*, instead of a hand-rolled line scanner that can drift from the
renderer. That drift was the root cause of the whole edge-case class (loose
lists split mid-list, <figure> torn at blank lines, </script><script>
re-opens): the stamper and the renderer were two different parsers that could
disagree. Now there is one grammar.

`block_spans()` returns one (start_line, end_line, kind) per top-level block:
  * CommonMark containers — loose lists, blockquotes, GFM tables, fenced code
    — come back whole, straight from markdown-it's token source maps.
  * type-6 structural HTML tags (<figure>/<section>/<div>/<aside>/<details>/
    <article>), which CommonMark terminates at an internal blank line, are
    depth-merged back into one unit — mirroring the frontend's pre-extraction
    of exactly those tags. (type-1 raw tags — script/style/pre/textarea —
    already span blank lines as a single html_block, so a <script> nested in a
    <figure> rides along inside it with no special handling.)

Guarded by an invariant the test suite checks against the REAL frontend
renderer: render(stamp(doc)) == render(doc) modulo comment nodes. If a stamp
point ever splits a block, that test goes red — so unforeseen grammar corners
are caught mechanically rather than enumerated by hand.
"""
from __future__ import annotations

import re

from markdown_it import MarkdownIt

# Mirror the frontend buildMd() config (index.html): html + linkify, breaks
# off, typographer OFF (it would curl apostrophes and break KaTeX primes).
# The `commonmark` preset + the table rule is exactly the frontend's block
# grammar. markdown-it-attrs only attaches inline {.class} attributes and never
# moves a block boundary; linkify / strikethrough are inline-only too — so none
# of them affect what this module computes.
_MD = (
    MarkdownIt(
        "commonmark",
        {"html": True, "linkify": True, "breaks": False, "typographer": False},
    )
    .enable("table")
)

# type-6 structural tags whose bodies are markdown-with-blank-lines. The
# frontend lifts these out before markdown-it so the blanks don't terminate
# them; here we let CommonMark split them and merge by tag depth.
#
# Opens are anchored at the start of a line (a real block-level HTML opener is
# always at col <= 3 — this avoids miscounting an inline `<div>` mention). The
# close is matched anywhere on the line, because a structural block frequently
# closes mid-line (e.g. `</script></figure>`) and markdown-it folds such a
# trailing close into a PARAGRAPH token — whose `.content` is empty (the text
# lives in a child inline token). So we count tags from the SOURCE LINES each
# top-level token spans, never from `token.content`.
_STRUCT_TAGS = ("figure", "section", "aside", "div", "details", "article")
_OPEN_RE = re.compile(
    r"(?im)^[ \t]{0,3}<(" + "|".join(_STRUCT_TAGS) + r")(?:\s[^>]*?)?(/?)>"
)
_CLOSE_RE = re.compile(r"(?i)</(" + "|".join(_STRUCT_TAGS) + r")\s*>")
# Spans whose contents are LITERAL, not markup: a structural tag mentioned in a
# code span or a <script>/<style>/<pre>/<textarea> body must never count toward
# the open/close balance (the AM-format tutorial that prints `</figure>` in a
# fence is the case that bites).
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_RAW_REGION_RE = re.compile(r"(?is)<(script|style|pre|textarea)\b.*?</\1\s*>")


def _struct_events(text: str):
    """Ordered (kind, tag) structural-tag events in a chunk of SOURCE. Opens
    are anchored at line-start (a real block opener sits at col <= 3) and skip
    self-closing `<div/>`; closes match anywhere (a structural block commonly
    closes mid-line, e.g. `</script></figure>`). Complete raw regions and
    inline code spans are removed first so tags quoted as text don't count."""
    text = _RAW_REGION_RE.sub("", text)
    text = _INLINE_CODE_RE.sub("", text)
    events = []
    for m in _OPEN_RE.finditer(text):
        if m.group(2) != "/":
            events.append((m.start(), "open", m.group(1).lower()))
    for m in _CLOSE_RE.finditer(text):
        events.append((m.start(), "close", m.group(1).lower()))
    events.sort(key=lambda e: e[0])
    return [(kind, tag) for _pos, kind, tag in events]


def _apply(stack: list[str], events) -> list[str]:
    """Fold tag events into a nesting stack. A close pops only when it matches
    the innermost open (by name) — a mismatched `</div>` while a `<figure>` is
    open is ignored, so one malformed tag can't rebalance the wrong block."""
    for kind, tag in events:
        if kind == "open":
            stack.append(tag)
        elif stack and stack[-1] == tag:
            stack.pop()
    return stack


def block_spans(body: str) -> list[tuple[int, int, str]]:
    """Return (start_line, end_line_exclusive, kind) for each top-level block
    of `body`, in source order. Lines are 0-based into ``body.split("\\n")``.

    `body` must already have YAML frontmatter stripped (markdown-it would
    otherwise mis-parse the `---` fences). Structural HTML blocks are merged
    into a single span with kind ``"structural"``; everything else carries the
    markdown-it token type ("paragraph_open", "bullet_list_open", "fence", …).
    """
    lines = body.split("\n")
    toks = [
        t for t in _MD.parse(body)
        if t.level == 0 and t.map and t.nesting in (0, 1)
    ]

    def events(tok):
        # Fenced / indented code is literal — it never contributes tags.
        if tok.type in ("fence", "code_block"):
            return []
        return _struct_events("\n".join(lines[tok.map[0]:tok.map[1]]))

    spans: list[tuple[int, int, str]] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        start, end, kind = t.map[0], t.map[1], t.type
        stack = _apply([], events(t))
        if stack:
            # This block opens a structural tag that doesn't close within its
            # own lines. Absorb following blocks until the tags balance —
            # committing ONLY if they do, so an unclosed <figure> stays a lone
            # html_block (matching the frontend) instead of greedily swallowing
            # the rest of the document.
            j = i
            while stack and j + 1 < len(toks):
                j += 1
                _apply(stack, events(toks[j]))
            if not stack:
                end, kind, i = toks[j].map[1], "structural", j
        spans.append((start, end, kind))
        i += 1
    return spans


def block_starts(body: str) -> list[int]:
    """Just the start line of each top-level block (see ``block_spans``)."""
    return [s for s, _e, _k in block_spans(body)]
