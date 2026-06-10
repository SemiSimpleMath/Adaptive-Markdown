"""Unit tests for the writing-surface conversion (am_tracking.writer_hard_breaks
+ _looks_like_prose): a reader's typed line breaks become portable markdown
hard breaks, but only inside plain prose — lists/tables/code/HTML pass through.

Runs as plain pytest or standalone (`python tests/test_writer_breaks.py`)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from am_tracking import writer_hard_breaks, _looks_like_prose

HB = "  "  # two trailing spaces == markdown hard break


# ---- prose: line breaks become hard breaks --------------------------------

def test_single_line_unchanged():
    assert writer_hard_breaks("Just one line.") == "Just one line."


def test_two_prose_lines_get_a_hard_break():
    assert writer_hard_breaks("line one\nline two") == f"line one{HB}\nline two"


def test_blank_line_stays_a_paragraph_break():
    src = "para one line a\npara one line b\n\npara two"
    assert writer_hard_breaks(src) == (
        f"para one line a{HB}\npara one line b\n\npara two"
    )


def test_trailing_whitespace_normalized_then_break_added():
    # existing trailing spaces/tabs are trimmed before the hard break is added,
    # so a re-save is idempotent rather than accreting spaces.
    assert writer_hard_breaks("a   \nb") == f"a{HB}\nb"
    assert writer_hard_breaks(f"a{HB}\nb") == f"a{HB}\nb"


def test_last_line_never_gets_a_break():
    assert writer_hard_breaks("a\nb\n") == f"a{HB}\nb\n"


# ---- non-prose: passed through verbatim -----------------------------------

def test_bullet_list_untouched():
    src = "- first\n- second\n- third"
    assert writer_hard_breaks(src) == src
    assert _looks_like_prose(src) is False


def test_ordered_list_untouched():
    src = "1. first\n2. second"
    assert writer_hard_breaks(src) == src


def test_fenced_code_untouched():
    src = "```js\nconst a = 1;\nconst b = 2;\n```"
    assert writer_hard_breaks(src) == src
    assert _looks_like_prose(src) is False


def test_table_untouched():
    src = "| a | b |\n| - | - |\n| 1 | 2 |"
    assert writer_hard_breaks(src) == src


def test_heading_untouched():
    assert writer_hard_breaks("# Title") == "# Title"
    # a heading followed by prose is still not pure prose -> left verbatim
    src = "## Section\nbody text here"
    assert writer_hard_breaks(src) == src
    assert _looks_like_prose(src) is False


def test_blockquote_untouched():
    src = "> quoted line one\n> quoted line two"
    assert writer_hard_breaks(src) == src


def test_html_block_untouched():
    src = "<figure>\n<canvas></canvas>\n</figure>"
    assert writer_hard_breaks(src) == src
    assert _looks_like_prose(src) is False


# ---- the gate -------------------------------------------------------------

def test_looks_like_prose_true_cases():
    assert _looks_like_prose("plain sentence") is True
    assert _looks_like_prose("two\nlines of prose") is True
    assert _looks_like_prose("para one\n\npara two") is True
    # leading emphasis is NOT a list marker (no space after the *)
    assert _looks_like_prose("*emphasis* leads this line") is True


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok: {t.__name__}")
    print(f"\n{len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
