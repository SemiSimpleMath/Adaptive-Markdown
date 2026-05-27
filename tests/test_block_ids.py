"""Unit tests for the eager block-id normalizer (am_tracking.ensure_block_ids_text).

The normalizer stamps `<!-- id:b-... -->` before EVERY top-level block so inline
edit / insert / delete can locate blocks by a stable id instead of fuzzy text
matching. Block boundaries come from am_blocks (the renderer's own markdown-it
grammar), so the stamp always lands on a block's OUTER start. The load-bearing
safety property is unchanged: a stamp NEVER lands inside a fenced code block,
a <script>/<style>/<pre> body, or a structural <figure>/<section> body — a
comment there would render as literal text, break a fence, or fail JS
validation. A structural HTML block is one editable unit: it gets a single id
before it, and its body (inner prose, nested figures, scripts) is left intact.

Runnable two ways:
    python -m pytest tests/test_block_ids.py
    python tests/test_block_ids.py        # no pytest needed
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from am_tracking import ensure_block_ids_text, strip_block_ids  # noqa: E402

_ID_LINE = re.compile(r"^<!-- id:b-[A-Z0-9]+ -->$", re.MULTILINE)


def _ids(text: str) -> list[str]:
    return _ID_LINE.findall(text)


# --- happy path -------------------------------------------------------------

def test_stamps_heading_and_paragraph():
    src = "# Title\n\nA plain paragraph.\n"
    out, n = ensure_block_ids_text(src)
    assert n == 2, out
    assert re.search(r"<!-- id:b-[A-Z0-9]+ -->\n# Title", out)
    assert re.search(r"<!-- id:b-[A-Z0-9]+ -->\nA plain paragraph\.", out)


def test_idempotent():
    src = "# Title\n\nPara one.\n\nPara two.\n"
    once, n1 = ensure_block_ids_text(src)
    twice, n2 = ensure_block_ids_text(once)
    assert n1 == 3
    assert n2 == 0
    assert twice == once


def test_frontmatter_untouched():
    src = "---\ndoc_id: d-X\ntitle: T\n---\n\n# Heading\n\nBody.\n"
    out, n = ensure_block_ids_text(src)
    fm = out.split("---\n", 2)
    assert "id:b-" not in (fm[1] if len(fm) > 2 else "")
    assert n == 2


def test_multiline_paragraph_gets_one_id():
    src = "Line one of a wrapped\nparagraph that spans\nthree source lines.\n"
    out, n = ensure_block_ids_text(src)
    assert n == 1, out
    assert out.startswith("<!-- id:b-")


def test_no_frontmatter_first_line_stamped():
    src = "Just a paragraph, no frontmatter.\n"
    out, n = ensure_block_ids_text(src)
    assert n == 1
    assert out.startswith("<!-- id:b-")


def test_inline_html_led_paragraph_is_stamped():
    # A paragraph that starts with an INLINE tag is a paragraph, not an HTML
    # block — markdown-it agrees, and it gets an id.
    for src in (
        "<em>Important</em> note about the limit.\n",
        '<a href="#x">See</a> the theorem above.\n',
        "<code>f(x)</code> denotes the function.\n",
    ):
        out, n = ensure_block_ids_text(src)
        assert n == 1, (src, out)
        assert out.startswith("<!-- id:b-")


# --- idempotency with pre-existing ids --------------------------------------

def test_existing_id_reused_adjacent():
    src = "<!-- id:b-EXISTING000000AA -->\n## Heading\n\nBody.\n"
    out, n = ensure_block_ids_text(src)
    assert n == 1, out                 # only the paragraph gets a fresh id
    assert "b-EXISTING000000AA" in out
    assert len(_ids(out)) == 2


def test_existing_id_reused_blank_separated():
    src = "<!-- id:b-EXISTING000000AA -->\n\n## Heading\n\nBody.\n"
    out, n = ensure_block_ids_text(src)
    assert n == 1, out
    assert len(_ids(out)) == 2


# --- every top-level block type now gets exactly one id, BEFORE it ----------

def test_loose_list_gets_one_id_before_not_per_item():
    # THE headline fix: a loose list (blank-separated items) is ONE block, so
    # it gets ONE id before it — never an id between items (which would split
    # the rendered list into several <ul>s).
    src = "- item one\n- item two\n\n- item three\n- item four\n"
    out, n = ensure_block_ids_text(src)
    assert n == 1, out
    assert out.startswith("<!-- id:b-")
    # no id smuggled between the items
    assert _ID_LINE.findall(out.split("- item one")[1]) == []


def test_lists_quotes_tables_hr_each_stamped_once():
    src = (
        "- bullet one\n- bullet two\n\n"
        "1. first\n2. second\n\n"
        "> a quote\n\n"
        "| a | b |\n| - | - |\n\n"
        "---\n\n"
        "A real paragraph.\n"
    )
    out, n = ensure_block_ids_text(src)
    assert n == 6, out                 # list, ordered list, quote, table, hr, para
    assert re.search(r"<!-- id:b-[A-Z0-9]+ -->\n- bullet one", out)
    assert re.search(r"<!-- id:b-[A-Z0-9]+ -->\n> a quote", out)
    assert re.search(r"<!-- id:b-[A-Z0-9]+ -->\nA real paragraph\.", out)


def test_tight_heading_after_paragraph_is_stamped():
    out, n = ensure_block_ids_text("Some paragraph.\n# Heading\n")
    assert n == 2, out
    assert re.search(r"<!-- id:b-[A-Z0-9]+ -->\n# Heading", out)


def test_consecutive_headings_each_stamped():
    out, n = ensure_block_ids_text("# H1\n## H2\n### H3\n")
    assert n == 3, out


def test_block_level_html_is_stamped_as_one_unit():
    for src in (
        "<div>block content</div>\n",
        '<section class="theorem">x</section>\n',
        "<figure>x</figure>\n",
    ):
        out, n = ensure_block_ids_text(src)
        assert n == 1, (src, out)
        assert out.startswith("<!-- id:b-")


def test_one_liner_script_is_stamped_before():
    src = "<script>window.x=1;</script>\n\nReal paragraph.\n"
    out, n = ensure_block_ids_text(src)
    assert n == 2, out                 # the script block + the paragraph
    assert re.search(r"<!-- id:b-[A-Z0-9]+ -->\nReal paragraph\.", out)


# --- SAFETY: never a stamp INSIDE a fence / raw / structural body -----------

def test_fenced_code_stamped_before_never_inside():
    src = (
        "Intro.\n\n"
        "```python\ndef f():\n\n    return 1\n```\n\n"
        "After code.\n"
    )
    out, n = ensure_block_ids_text(src)
    assert n == 3, out                 # intro, fence, after — all stamped BEFORE
    assert "```python\ndef f():\n\n    return 1\n```" in out  # body intact
    assert re.search(r"<!-- id:b-[A-Z0-9]+ -->\n```python", out)


def test_tilde_fence_stamped_before_never_inside():
    src = "~~~\nline a\n\nline b\n~~~\n\nPara.\n"
    out, n = ensure_block_ids_text(src)
    assert n == 2, out
    assert "~~~\nline a\n\nline b\n~~~" in out


def test_paragraph_after_fence_close_is_stamped():
    out, n = ensure_block_ids_text("```\ncode\n```\nPara right after.\n")
    assert n == 2, out                          # the fence and the paragraph
    assert "<!-- id:" not in out.split("```")[1]  # nothing inside the fence
    assert re.search(r"<!-- id:b-[A-Z0-9]+ -->\nPara right after\.", out), out


def test_unclosed_fence_no_stamp_inside():
    src = "Intro.\n\n```\nstill open\n\nmore code\n"
    out, n = ensure_block_ids_text(src)
    assert n == 2, out                          # intro + the (unclosed) fence
    assert "```\nstill open\n\nmore code" in out


def test_unclosed_script_no_stamp_inside():
    src = "Intro.\n\n<script>\na = 1;\n\nb = 2;\n"
    out, n = ensure_block_ids_text(src)
    assert n == 2, out
    assert "<script>\na = 1;\n\nb = 2;" in out


def test_style_block_stamped_before_never_inside():
    src = "<style>\n.a { color: red; }\n\n.b { color: blue; }\n</style>\n\nPara.\n"
    out, n = ensure_block_ids_text(src)
    assert n == 2, out
    assert "<style>\n.a { color: red; }\n\n.b { color: blue; }\n</style>" in out


def test_figure_wrapping_script_one_id_body_intact():
    # Structural <figure> wrapping a blank-lined <script>: ONE id before the
    # figure, nothing inside (no id in the JS, which would fail validation).
    src = (
        "<figure>\n<script>\nconst x = 1;\n\nconst y = 2;\n</script>\n"
        "<figcaption>Cap</figcaption>\n</figure>\n"
    )
    out, n = ensure_block_ids_text(src)
    assert n == 1, out
    assert out.startswith("<!-- id:b-")
    assert "<script>\nconst x = 1;\n\nconst y = 2;\n</script>" in out
    # the single id is BEFORE the figure, not in the body
    assert _ID_LINE.findall(out.split("<script>")[1]) == []


def test_same_line_figure_script_one_id_body_intact():
    src = (
        "<figure><script>\nconst x = 1;\n\nconst y = 2;\n</script></figure>\n"
    )
    out, n = ensure_block_ids_text(src)
    assert n == 1, out
    assert "const x = 1;\n\nconst y = 2;" in out
    assert _ID_LINE.findall(out.split("<script>")[1]) == []


def test_div_wrapped_script_one_id_body_intact():
    src = '<div class="fig"><script>\na = 1;\n\nb = 2;\n</script></div>\n'
    out, n = ensure_block_ids_text(src)
    assert n == 1, out
    assert "a = 1;\n\nb = 2;" in out
    assert _ID_LINE.findall(out.split("<script>")[1]) == []


def test_nested_adjacent_scripts_one_line_body_intact():
    # close+reopen on one line (</script><script>) inside a structural figure:
    # one id before the figure, no id smuggled into the SECOND script.
    src = (
        "<figure><script>\na = 1;\n</script><script>\n\nb = 2;\n</script></figure>\n"
    )
    out, n = ensure_block_ids_text(src)
    assert n == 1, out
    assert "</script><script>\n\nb = 2;\n</script>" in out


def test_pre_and_textarea_wrapped_body_intact():
    for tag in ("pre", "textarea"):
        src = f"<figure><{tag}>\nline one\n\nline two\n</{tag}></figure>\n"
        out, n = ensure_block_ids_text(src)
        assert n == 1, (tag, out)
        assert f"<{tag}>\nline one\n\nline two\n</{tag}>" in out
        assert _ID_LINE.findall(out.split(f"<{tag}>")[1]) == []


def test_section_is_one_editable_unit():
    # A structural <section> is ONE editable block: a single id before it, and
    # its inner prose is NOT separately stamped (you edit the whole section).
    src = '<section class="theorem">\n\nThis is **inner** prose.\n\n</section>\n'
    out, n = ensure_block_ids_text(src)
    assert n == 1, out
    assert out.startswith("<!-- id:b-")
    assert _ID_LINE.findall(out.split("<section")[1]) == []  # none inside


def test_prose_mention_of_script_tag_is_not_a_raw_block():
    src = "Use the `<script>` tag carefully.\n\nNext paragraph.\n"
    out, n = ensure_block_ids_text(src)
    assert n == 2, out


def test_hyphenated_pseudo_tag_does_not_swallow_doc():
    # <style-ish> is not a raw tag; the paragraph after it must still be stamped.
    src = "<style-ish>x</style-ish>\n\nReal paragraph after the pseudo tag.\n"
    out, n = ensure_block_ids_text(src)
    assert re.search(r"<!-- id:b-[A-Z0-9]+ -->\nReal paragraph", out), out
    assert n >= 1


# --- additive / strip inverse (invariants that must always hold) ------------

def test_strictly_additive_over_corpus():
    corpus = [
        "# H\n\npara\n",
        "<figure><script>\na;\n\nb;\n</script></figure>\n",
        "<figure><script>\na;\n</script><script>\nb;\n</script></figure>\n",
        "```\ncode\n\nmore\n```\n\nafter\n",
        "- a\n- b\n\n> quote\n\n| t | u |\n| - | - |\n",
        "<style-ish>x</style-ish>\n\np\n",
        "<section>\n\ninner prose\n\n</section>\n",
        "text\n# tight heading\nmore text\n",
        "---\ndoc_id: d-X\n---\n\n# Title\n\nBody.\n",
    ]
    only_id = re.compile(r"^<!-- id:b-[A-Z0-9-]+ -->$")
    strip = lambda t: "\n".join(l for l in t.split("\n") if not only_id.match(l))
    for src in corpus:
        out, _ = ensure_block_ids_text(src)
        assert strip(out) == strip(src), f"non-additive on: {src!r}"
        out2, n2 = ensure_block_ids_text(out)
        assert n2 == 0 and out2 == out, f"non-idempotent on: {src!r}"


def test_strip_preserves_id_comment_inside_fence():
    src = (
        "# Doc\n\n"
        "```markdown\n"
        "<!-- id:b-EXAMPLE000000AA -->\n"
        "## Theorem\n"
        "```\n\n"
        "Real paragraph.\n"
    )
    stamped, _ = ensure_block_ids_text(src)
    stripped = strip_block_ids(stamped)
    assert "<!-- id:b-EXAMPLE000000AA -->" in stripped   # in-fence sample kept
    assert stripped == src                                # exact inverse


def test_strip_is_inverse_of_stamp():
    for src in (
        "# Title\n\nA paragraph.\n\nAnother one.\n",
        "<figure><script>\nconst a = 1;\n</script></figure>\n\nPara.\n",
        "## Heading {#anchor}\n\nbody with **bold** and `code`\n",
        "text\n# tight heading\nmore\n",
        "- a\n- b\n\n- c\n\n> q\n\n| x | y |\n| - | - |\n",
    ):
        stamped, _ = ensure_block_ids_text(src)
        assert strip_block_ids(stamped) == src, src


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
