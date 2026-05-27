"""Unit tests for the source-edit core: locate a block by track_id, return its
EXACT markdown (block_source_span), and the pure replace / delete / insert
mutations behind /block-source, /delete-block, /insert-block.

Lossless by construction — styled spans, math, and full raw heading lines
round-trip byte-exact — and now every block TYPE works: a loose list, a GFM
table, a fenced block, or a whole <figure> come back as one editable unit.

    python -m pytest tests/test_block_source.py
    python tests/test_block_source.py
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from am_tracking import (  # noqa: E402
    block_source_span,
    delete_block_source,
    ensure_block_ids_text,
    insert_block_source,
    replace_block_source,
)


def span_get(text, tid):
    sp = block_source_span(text, tid)
    if sp is None:
        return None
    _id_line, s, e = sp
    return "\n".join(text.split("\n")[s:e])


def _ids(stamped):
    return re.findall(r"<!-- id:(b-[A-Z0-9]+) -->", stamped)


_DOC = (
    "# H\n\n"
    'A para with <span style="color:red">styled</span> text and $x^2$.\n\n'
    "Other para.\n"
)


# --- prose: exact source, lossless replace, sibling isolation ---------------

def test_get_returns_exact_source_with_span_and_math():
    stamped, _ = ensure_block_ids_text(_DOC)
    src = span_get(stamped, _ids(stamped)[1])
    assert src == 'A para with <span style="color:red">styled</span> text and $x^2$.'


def test_set_is_lossless_and_contained():
    stamped, _ = ensure_block_ids_text(_DOC)
    ids = _ids(stamped)
    new = 'A para with <span style="color:red">restyled</span> text and $x^2$ and $y^2$.'
    out = replace_block_source(stamped, ids[1], new)
    assert '<span style="color:red">restyled</span>' in out      # styling kept
    assert "$x^2$ and $y^2$" in out                              # math byte-exact
    assert " " not in out                                   # no nbsp gremlin
    assert span_get(out, ids[0]) == span_get(stamped, ids[0])    # heading untouched
    assert span_get(out, ids[2]) == span_get(stamped, ids[2])    # other para untouched


def test_heading_source_is_full_raw_line_no_munging():
    stamped, _ = ensure_block_ids_text("## Theorem (Rolle) {#rolle}\n\nbody\n")
    hid = _ids(stamped)[0]
    assert span_get(stamped, hid) == "## Theorem (Rolle) {#rolle}"
    out = replace_block_source(stamped, hid, "## Theorem (Rolle, revised) {#rolle}")
    assert "## Theorem (Rolle, revised) {#rolle}" in out
    assert out.count("## Theorem") == 1


def test_unknown_id_is_unlocatable():
    stamped, _ = ensure_block_ids_text(_DOC)
    assert span_get(stamped, "b-DOESNOTEXIST00") is None
    assert replace_block_source(stamped, "b-DOESNOTEXIST00", "x") is None


# --- every block type comes back as ONE editable unit -----------------------

def test_get_whole_loose_list():
    src = "Intro.\n\n- one\n- two\n\n- three\n- four\n"
    stamped, _ = ensure_block_ids_text(src)
    list_id = _ids(stamped)[1]                       # intro is [0], list is [1]
    assert span_get(stamped, list_id) == "- one\n- two\n\n- three\n- four"


def test_get_whole_table():
    src = "| a | b |\n| - | - |\n| 1 | 2 |\n\nafter\n"
    stamped, _ = ensure_block_ids_text(src)
    assert span_get(stamped, _ids(stamped)[0]) == "| a | b |\n| - | - |\n| 1 | 2 |"


def test_get_whole_fence_including_internal_blanks():
    src = "```py\nx = 1\n\ny = 2\n```\n\ndone\n"
    stamped, _ = ensure_block_ids_text(src)
    assert span_get(stamped, _ids(stamped)[0]) == "```py\nx = 1\n\ny = 2\n```"


def test_get_whole_structural_figure():
    src = 'Intro.\n\n<figure class="c">\n\nA **caption**.\n\n</figure>\n\nOutro.\n'
    stamped, _ = ensure_block_ids_text(src)
    fig_id = _ids(stamped)[1]
    assert span_get(stamped, fig_id) == (
        '<figure class="c">\n\nA **caption**.\n\n</figure>')


def test_set_list_is_lossless():
    src = "- one\n- two\n"
    stamped, _ = ensure_block_ids_text(src)
    out = replace_block_source(stamped, _ids(stamped)[0], "- one\n- two\n- three")
    assert span_get(out, _ids(out)[0]) == "- one\n- two\n- three"


# --- editing that SPLITS a block stamps the newcomers -----------------------

def test_edit_splitting_block_stamps_new_subblocks():
    stamped, _ = ensure_block_ids_text("# H\n\nJust a paragraph.\n")
    para_id = _ids(stamped)[1]
    out = replace_block_source(stamped, para_id, "Lead in:\n\n- one\n- two")
    # heading + (kept) paragraph id + a fresh id for the new list = 3
    assert len(_ids(out)) == 3
    # the new list is independently locatable / editable
    list_id = _ids(out)[2]
    assert span_get(out, list_id) == "- one\n- two"


# --- delete -----------------------------------------------------------------

def test_delete_removes_block_keeps_siblings():
    stamped, _ = ensure_block_ids_text(_DOC)
    ids = _ids(stamped)
    out = delete_block_source(stamped, ids[1])           # drop the styled para
    assert out is not None
    assert "styled" not in out                            # the block is gone
    assert span_get(out, ids[0]) == "# H"                 # heading survives
    assert span_get(out, ids[2]) == "Other para."         # other para survives
    # no doubled blank where the block used to be
    assert "\n\n\n" not in out


def test_delete_unknown_id_returns_none():
    stamped, _ = ensure_block_ids_text(_DOC)
    assert delete_block_source(stamped, "b-NOPE0000000000") is None


# --- insert -----------------------------------------------------------------

def test_insert_after_block_gets_new_id_and_is_editable():
    stamped, _ = ensure_block_ids_text(_DOC)
    heading_id = _ids(stamped)[0]
    out = insert_block_source(stamped, heading_id, "Brand new paragraph.", "b-NEW0000000001")
    assert out is not None
    assert "b-NEW0000000001" in out
    assert span_get(out, "b-NEW0000000001") == "Brand new paragraph."
    # original blocks all still present and locatable
    for tid in _ids(stamped):
        assert span_get(out, tid) is not None


def test_insert_at_top_when_anchor_empty():
    src = "---\ndoc_id: d-x\n---\n\n# Existing\n\nbody\n"
    stamped, _ = ensure_block_ids_text(src)
    out = insert_block_source(stamped, "", "# New top heading", "b-TOP0000000001")
    assert span_get(out, "b-TOP0000000001") == "# New top heading"
    # frontmatter is untouched
    assert out.startswith("---\ndoc_id: d-x\n---\n")
    # the new block precedes the old first heading in source order
    assert out.index("b-TOP0000000001") < out.index("# Existing")


def test_insert_unknown_anchor_returns_none():
    stamped, _ = ensure_block_ids_text(_DOC)
    assert insert_block_source(stamped, "b-GHOST000000000", "x", "b-NEW0000000002") is None


# --- audit regressions: fence-blind id resolution + forged handles ----------

def test_in_fence_id_literal_is_not_a_handle():
    # A doc teaching the AM format prints a literal id line inside a fence; it
    # must NOT resolve to (and let someone edit/delete) the real block after it.
    src = ("# Doc\n\n"
           "```markdown\n<!-- id:b-INNERFENCE0001 -->\n## Sample\n```\n\n"
           "Real paragraph.\n")
    stamped, _ = ensure_block_ids_text(src)
    assert span_get(stamped, "b-INNERFENCE0001") is None
    assert delete_block_source(stamped, "b-INNERFENCE0001") is None
    assert replace_block_source(stamped, "b-INNERFENCE0001", "x") is None


def test_forged_id_in_fragment_is_stripped_on_set():
    stamped, _ = ensure_block_ids_text("# H\n\nplain para\n")
    pid = _ids(stamped)[1]
    out = replace_block_source(stamped, pid, "<!-- id:b-FORGED0000001 -->\nnew text")
    assert "b-FORGED0000001" not in out       # forged handle stripped
    assert "new text" in out


def test_forged_id_in_insert_is_stripped():
    stamped, _ = ensure_block_ids_text("# H\n\nbody\n")
    out = insert_block_source(stamped, _ids(stamped)[0],
                              "<!-- id:b-FORGED0000002 -->\ninserted", "b-OK00000000001")
    assert "b-FORGED0000002" not in out and "inserted" in out
    assert span_get(out, "b-OK00000000001") == "inserted"


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
