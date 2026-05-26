"""Unit tests for the inline-edit block locator (am_tracking._find_block_line
/ _find_block_span) after the identity-first rewrite.

The bug these guard against: location used to be a fuzzy first-40-char text
match that ignored the stable track_id, so a shared prefix between two blocks
(or a prefix colliding with a list/HTML line) produced the wrong block — or
"could not locate / refusing to inline-edit". Now track_id wins, with a
hardened excerpt fallback.

    python -m pytest tests/test_block_locate.py
    python tests/test_block_locate.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from am_tracking import (  # noqa: E402
    _find_block_line,
    _find_block_span,
    _find_line_after_track_comment,
    resolve_alias,
    save_aliases,
)

A = "b-AAAAAAAAAAAAAAAA"
B = "b-BBBBBBBBBBBBBBBB"
C = "b-CCCCCCCCCCCCCCCC"


def test_find_by_track_id():
    lines = [
        f"<!-- id:{A} -->", "## Heading", "",
        f"<!-- id:{B} -->", "Paragraph text.", "",
    ]
    assert _find_block_line(lines, {"track_id": B}) == 4
    assert _find_block_line(lines, {"track_id": A}) == 1


def test_track_comment_skips_blank_and_chained_comments():
    lines = [f"<!-- id:{A} -->", "", "Para.", ""]
    assert _find_line_after_track_comment(lines, A) == 2


def test_track_id_beats_stale_excerpt_collision():
    # Two paragraphs share their first 40+ chars. A stale signature carries the
    # FIRST paragraph's excerpt but the SECOND's track_id; identity must win.
    lines = [
        f"<!-- id:{A} -->",
        "The function is continuous on the closed interval here.",
        "",
        f"<!-- id:{B} -->",
        "The function is continuous on the closed interval there.",
        "",
    ]
    sig = {
        "track_id": B,
        "excerpt": "The function is continuous on the closed interval here.",
    }
    assert _find_block_line(lines, sig) == 4


def test_excerpt_fallback_skips_refused_list_line():
    # No track_id (e.g. an external formatter stripped the comment). A list
    # item shares the paragraph's prefix; the fallback must skip the list line
    # and land on the real paragraph instead of getting refused.
    lines = [
        "- Apple pie is delicious and warm.",
        "",
        "Apple pie is delicious and warm too.",
        "",
    ]
    idx = _find_block_line(lines, {"excerpt": "Apple pie is delicious and warm"})
    assert idx == 2
    assert _find_block_span(lines, {"excerpt": "Apple pie is delicious and warm"}) \
        == (2, 3, "paragraph")


def test_span_classifies_track_id_target_as_paragraph():
    lines = [f"<!-- id:{C} -->", "A paragraph.", ""]
    assert _find_block_span(lines, {"track_id": C}) == (1, 2, "paragraph")


def test_span_classifies_track_id_target_as_heading():
    lines = [f"<!-- id:{C} -->", "## Theorem (Rolle's) {#rolle}", ""]
    assert _find_block_span(lines, {"track_id": C}) == (1, 2, "heading")


def test_missing_track_id_falls_through_to_excerpt():
    # track_id present in the signature but absent from the source (stripped)
    # -> we don't fail; we fall back to the excerpt.
    lines = ["Some ordinary paragraph here.", ""]
    sig = {"track_id": "b-GONEGONEGONEGONE", "excerpt": "Some ordinary paragraph"}
    assert _find_block_line(lines, sig) == 0


def test_resolve_alias_follows_and_tombstones():
    d = tempfile.mkdtemp()
    p = pathlib.Path(d) / "current.md"
    p.write_text("x", encoding="utf-8")
    save_aliases(p, {"b-OLD": "b-NEW"})
    assert resolve_alias(p, "b-OLD") == "b-NEW"
    assert resolve_alias(p, "b-NEW") == "b-NEW"   # not in map -> itself
    save_aliases(p, {"b-DEAD": None})
    assert resolve_alias(p, "b-DEAD") is None     # tombstoned -> dropped


def test_excerpt_fallback_finds_emphasis_led_paragraph():
    # *Important*… renders as "Important…"; _strip_inline_markdown removes the
    # asterisks so the excerpt matches. The old hardened skip-set wrongly
    # skipped any line starting with '*', stranding such paragraphs.
    lines = ["*Important*: the interval is closed and bounded here.", ""]
    idx = _find_block_line(
        lines,
        {"excerpt": "Important: the interval is closed and bounded here."},
    )
    assert idx == 0


def test_track_id_match_is_case_insensitive():
    lines = [f"<!-- id:{A} -->", "A paragraph.", ""]
    assert _find_block_line(lines, {"track_id": A.lower()}) == 1


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
