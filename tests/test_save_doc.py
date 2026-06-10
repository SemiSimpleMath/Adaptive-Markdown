"""Unit tests for am_tracking.combine_doc — the whole-body save path behind
the WYSIWYG editor's /save-doc route: keep the doc's frontmatter verbatim,
swap in the editor-serialized body, re-stamp tracking ids.

Runs as plain pytest or standalone (`python tests/test_save_doc.py`)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from am_tracking import combine_doc

EXISTING = (
    "---\n"
    "doc_id: d-01ABCDEFGHJKMNPQ\n"
    "title: My Notes\n"
    "audience: novice\n"
    "---\n"
    "\n"
    "<!-- id:b-01OLDOLDOLDOLDOL -->\n"
    "# My Notes\n"
    "\n"
    "<!-- id:b-01OLD2OLD2OLD2OL -->\n"
    "The old first paragraph.\n"
)

# What the editor sends back: body only, NO tracking-id comments.
NEW_BODY = "# My Notes\n\nA rewritten first paragraph.\n\nAnd a brand new one.\n"


def test_frontmatter_preserved_verbatim():
    out = combine_doc(EXISTING, NEW_BODY)
    assert out.startswith(
        "---\n"
        "doc_id: d-01ABCDEFGHJKMNPQ\n"
        "title: My Notes\n"
        "audience: novice\n"
        "---\n"
    ), out[:120]


def test_one_blank_line_between_frontmatter_and_body():
    out = combine_doc(EXISTING, NEW_BODY)
    # frontmatter close, exactly one blank line, then the first stamped block
    assert re.search(r"---\n\n<!-- id:b-[A-Z0-9]+ -->\n# My Notes", out), out[:200]


def test_new_body_swapped_in_old_gone():
    out = combine_doc(EXISTING, NEW_BODY)
    assert "A rewritten first paragraph." in out
    assert "And a brand new one." in out
    assert "The old first paragraph." not in out


def test_tracking_ids_restamped_fresh():
    out = combine_doc(EXISTING, NEW_BODY)
    ids = re.findall(r"<!-- id:(b-[A-Z0-9]+) -->", out)
    # editor sent none; backend mints one per top-level block (h1 + 2 paras)
    assert len(ids) >= 3, ids
    # the old ids are gone — fresh stamping, not carried over
    assert "b-01OLDOLDOLDOLDOL" not in out
    assert "b-01OLD2OLD2OLD2OL" not in out


def test_trailing_newline_guaranteed():
    assert combine_doc(EXISTING, "# H\n\nno trailing newline").endswith("\n")


def test_no_frontmatter_doc():
    out = combine_doc("# Just a body\n\ntext\n", "# New title\n\nfresh body\n")
    assert not out.startswith("---")
    assert "# New title" in out
    assert "fresh body" in out
    assert "<!-- id:b-" in out  # still stamped


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok: {t.__name__}")
    print(f"\n{len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
