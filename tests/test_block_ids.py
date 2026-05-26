"""Unit tests for the eager block-id normalizer (am_tracking.ensure_block_ids_text).

The normalizer stamps `<!-- id:b-... -->` before top-level headings and
paragraphs so inline-edit / insertion can locate blocks by a stable id
instead of fuzzy text matching. The load-bearing safety property is that it
NEVER inserts inside a fenced code block or a <script>/<style>/<pre> body —
a comment there would render as literal text, break a fence, or fail JS
validation. These tests cover the happy path, idempotency, and (hardest)
the corruption hazards.

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


def test_stamps_heading_and_paragraph():
    src = "# Title\n\nA plain paragraph.\n"
    out, n = ensure_block_ids_text(src)
    assert n == 2, out
    # Each id sits immediately before its block.
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
    # No id lands inside the frontmatter block.
    fm = out.split("---\n", 2)
    assert "id:b-" not in (fm[1] if len(fm) > 2 else "")
    assert n == 2


def test_multiline_paragraph_gets_one_id():
    src = "Line one of a wrapped\nparagraph that spans\nthree source lines.\n"
    out, n = ensure_block_ids_text(src)
    assert n == 1, out
    assert out.startswith("<!-- id:b-")


def test_existing_id_reused_adjacent():
    src = "<!-- id:b-EXISTING000000AA -->\n## Heading\n\nBody.\n"
    out, n = ensure_block_ids_text(src)
    # Heading already has an id; only the paragraph gets a fresh one.
    assert n == 1, out
    assert "b-EXISTING000000AA" in out
    assert len(_ids(out)) == 2


def test_existing_id_reused_blank_separated():
    src = "<!-- id:b-EXISTING000000AA -->\n\n## Heading\n\nBody.\n"
    out, n = ensure_block_ids_text(src)
    assert n == 1, out          # heading still considered id'd across the blank
    assert len(_ids(out)) == 2


def test_fenced_code_with_blank_lines_never_stamped():
    src = (
        "Intro.\n\n"
        "```python\n"
        "def f():\n"
        "\n"                    # blank line INSIDE the fence — the hazard
        "    return 1\n"
        "```\n\n"
        "After code.\n"
    )
    out, n = ensure_block_ids_text(src)
    # The code block is preserved byte-for-byte (no id smuggled inside).
    assert "```python\ndef f():\n\n    return 1\n```" in out
    # Only the two surrounding paragraphs were stamped.
    assert n == 2, out


def test_tilde_fence_with_blank_lines_never_stamped():
    src = "~~~\nline a\n\nline b\n~~~\n\nPara.\n"
    out, n = ensure_block_ids_text(src)
    assert "~~~\nline a\n\nline b\n~~~" in out
    assert n == 1, out          # only the trailing paragraph


def test_script_body_with_blank_lines_never_stamped():
    src = (
        "<figure>\n"
        "<script>\n"
        "const x = 1;\n"
        "\n"                    # blank line INSIDE the script — the hazard
        "const y = 2;\n"
        "</script>\n"
        "<figcaption>Cap</figcaption>\n"
        "</figure>\n"
    )
    out, n = ensure_block_ids_text(src)
    # Nothing stampable here (every top-level line opens with '<'); critically,
    # no id appears anywhere inside the script body.
    assert n == 0, out
    assert "<script>\nconst x = 1;\n\nconst y = 2;\n</script>" in out


def test_style_body_with_blank_lines_never_stamped():
    src = "<style>\n.a { color: red; }\n\n.b { color: blue; }\n</style>\n\nPara.\n"
    out, n = ensure_block_ids_text(src)
    assert "<style>\n.a { color: red; }\n\n.b { color: blue; }\n</style>" in out
    assert n == 1, out


def test_prose_mention_of_script_tag_is_not_a_raw_block():
    # A <script> mention mid-line (not at line start) must not flip raw mode,
    # else the following paragraph would silently never be stamped.
    src = "Use the `<script>` tag carefully.\n\nNext paragraph.\n"
    out, n = ensure_block_ids_text(src)
    assert n == 2, out


def test_lists_quotes_tables_hr_not_stamped():
    src = (
        "- bullet one\n- bullet two\n\n"
        "1. first\n2. second\n\n"
        "> a quote\n\n"
        "| a | b |\n| - | - |\n\n"
        "---\n\n"
        "A real paragraph.\n"
    )
    out, n = ensure_block_ids_text(src)
    # Only the final paragraph is stampable.
    assert n == 1, out
    assert re.search(r"<!-- id:b-[A-Z0-9]+ -->\nA real paragraph\.", out)


def test_inner_paragraph_inside_section_is_stamped():
    # Inner markdown inside a <section> (blank-line surrounded) renders as a
    # real, inline-editable paragraph — stamping it is desirable and a comment
    # there is harmless.
    src = (
        "<section class=\"theorem\">\n\n"
        "This is **inner** prose.\n\n"
        "</section>\n"
    )
    out, n = ensure_block_ids_text(src)
    assert n == 1, out
    assert re.search(r"<!-- id:b-[A-Z0-9]+ -->\nThis is \*\*inner\*\* prose\.", out)
    # The section tags themselves are not stamped.
    assert "<!-- id:b-" not in out.split("This is")[0].split("\n\n")[0]


def test_no_frontmatter_first_line_stamped():
    src = "Just a paragraph, no frontmatter.\n"
    out, n = ensure_block_ids_text(src)
    assert n == 1
    assert out.startswith("<!-- id:b-")


def test_one_liner_script_does_not_open_raw_region():
    # <script>...</script> closed on the same line must not swallow what follows.
    src = "<script>window.x=1;</script>\n\nReal paragraph.\n"
    out, n = ensure_block_ids_text(src)
    assert n == 1, out          # the paragraph after the one-liner script
    assert re.search(r"<!-- id:b-[A-Z0-9]+ -->\nReal paragraph\.", out)


def test_same_line_figure_script_never_stamped():
    # The CRITICAL the audit caught: a raw tag riding behind a wrapper on the
    # same line (<figure><script>) must still mask the body — no id in the JS.
    src = (
        "<figure><script>\n"
        "const x = 1;\n"
        "\n"
        "const y = 2;\n"
        "</script></figure>\n"
    )
    out, n = ensure_block_ids_text(src)
    assert n == 0, out
    assert "<!-- id:" not in out
    assert "const x = 1;\n\nconst y = 2;" in out


def test_div_wrapped_script_never_stamped():
    src = '<div class="fig"><script>\na = 1;\n\nb = 2;\n</script></div>\n'
    out, n = ensure_block_ids_text(src)
    assert "<!-- id:" not in out, out
    assert n == 0


def test_inline_html_led_paragraph_is_stamped():
    # MAJOR the audit caught: a paragraph that starts with an inline tag is a
    # paragraph, not an HTML block — it must get an id.
    for src in (
        "<em>Important</em> note about the limit.\n",
        '<a href="#x">See</a> the theorem above.\n',
        "<code>f(x)</code> denotes the function.\n",
    ):
        out, n = ensure_block_ids_text(src)
        assert n == 1, (src, out)
        assert out.startswith("<!-- id:b-")


def test_block_level_html_not_stamped():
    for src in (
        "<div>block content</div>\n",
        '<section class="theorem">x</section>\n',
        "<figure>x</figure>\n",
    ):
        out, n = ensure_block_ids_text(src)
        assert n == 0, (src, out)


def test_tight_heading_after_paragraph_is_stamped():
    out, n = ensure_block_ids_text("Some paragraph.\n# Heading\n")
    assert n == 2, out
    assert re.search(r"<!-- id:b-[A-Z0-9]+ -->\n# Heading", out)


def test_consecutive_headings_each_stamped():
    out, n = ensure_block_ids_text("# H1\n## H2\n### H3\n")
    assert n == 3, out


def test_paragraph_after_fence_close_is_stamped():
    out, n = ensure_block_ids_text("```\ncode\n```\nPara right after.\n")
    assert n == 1, out                       # only the paragraph; not the fence
    assert "<!-- id:" not in out.split("```")[1]   # nothing inside the fence
    assert re.search(r"<!-- id:b-[A-Z0-9]+ -->\nPara right after\.", out), out


def test_unclosed_fence_no_stamp_inside():
    # An authoring error (unclosed fence) must never cause a stamp inside it.
    src = "Intro.\n\n```\nstill open\n\nmore code\n"
    out, n = ensure_block_ids_text(src)
    assert n == 1, out                       # only the intro paragraph
    assert "```\nstill open\n\nmore code" in out


def test_unclosed_script_no_stamp_inside():
    src = "Intro.\n\n<script>\na = 1;\n\nb = 2;\n"
    out, n = ensure_block_ids_text(src)
    assert n == 1, out
    assert "<script>\na = 1;\n\nb = 2;" in out


def test_nested_adjacent_scripts_one_line_never_stamped():
    # Re-audit CRITICAL: a close+reopen on one line (</script><script>) must
    # keep masking — no id stamped into the SECOND script's body.
    src = (
        "<figure><script>\n"
        "a = 1;\n"
        "</script><script>\n"
        "\n"                 # blank line inside the SECOND script
        "b = 2;\n"
        "</script></figure>\n"
    )
    out, n = ensure_block_ids_text(src)
    assert n == 0, out
    assert "<!-- id:" not in out
    assert "</script><script>\n\nb = 2;\n</script>" in out


def test_hyphenated_pseudo_tag_does_not_swallow_doc():
    # Re-audit MAJOR: <style-ish> must NOT open a raw region (its close
    # </style-ish> wouldn't match </style>), else it swallows the rest of the
    # document and every later block silently loses its id.
    src = "<style-ish>x</style-ish>\n\nReal paragraph after the pseudo tag.\n"
    out, n = ensure_block_ids_text(src)
    assert re.search(r"<!-- id:b-[A-Z0-9]+ -->\nReal paragraph", out), out
    assert n >= 1


def test_pre_and_textarea_wrapped_never_stamped():
    for tag in ("pre", "textarea"):
        src = f"<figure><{tag}>\nline one\n\nline two\n</{tag}></figure>\n"
        out, n = ensure_block_ids_text(src)
        assert "<!-- id:" not in out, (tag, out)
        assert n == 0, (tag, out)


def test_strictly_additive_over_corpus():
    # The normalizer must ONLY insert id-comment lines — never alter, reorder,
    # or drop other content — and be idempotent. Verify strip(out)==strip(in)
    # across a battery that includes every hazard.
    corpus = [
        "# H\n\npara\n",
        "<figure><script>\na;\n\nb;\n</script></figure>\n",
        "<figure><script>\na;\n</script><script>\nb;\n</script></figure>\n",
        "```\ncode\n\nmore\n```\n\nafter\n",
        "- a\n- b\n\n> quote\n\n| t | u |\n",
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
    # A literal id-syntax line inside a fenced code sample (e.g. a doc that
    # teaches the AM format) must NOT be stripped — strip is fence-aware and
    # symmetric with the fence-aware stamper.
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
    # The clean canonical artifact = strip(working copy). Stripping the ids
    # from a freshly-stamped doc must reproduce the original source exactly.
    for src in (
        "# Title\n\nA paragraph.\n\nAnother one.\n",
        "<figure><script>\nconst a = 1;\n</script></figure>\n\nPara.\n",
        "## Heading {#anchor}\n\nbody with **bold** and `code`\n",
        "text\n# tight heading\nmore\n",
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
