"""Block-boundary detection (am_blocks): containers stay whole, structural
HTML tags merge, and stamping at block-starts never changes the render."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from am_blocks import _MD, block_spans, block_starts  # noqa: E402


def _kinds(body):
    return [k for _s, _e, k in block_spans(body)]


def _invariant(body):
    """The property test, with markdown-it-py standing in for the frontend:
    stamping an id before every block-start must not change the render
    (modulo the inserted comment nodes). The full-fidelity version runs in
    browser_smoke against the real viewer pipeline."""
    lines = body.split("\n")
    for s in sorted(set(block_starts(body)), reverse=True):
        lines.insert(s, "<!-- id:b-TEST -->")
    stamped = "\n".join(lines)
    norm = lambda h: re.sub(r"\s+", " ", re.sub(r"<!--.*?-->", "", h, flags=re.S)).strip()
    return norm(_MD.render(body)) == norm(_MD.render(stamped))


# --- Claim 1: CommonMark containers come back as ONE block ------------------

def test_loose_list_is_one_block():
    body = "Intro.\n\n- a\n- b\n\n- c\n- d\n"
    lists = [b for b in block_spans(body) if b[2] == "bullet_list_open"]
    assert len(lists) == 1, "loose list must not split into per-item blocks"
    assert lists[0][:2] == (2, 7)


def test_tight_list_is_one_block():
    body = "- a\n- b\n- c\n"
    lists = [b for b in block_spans(body) if b[2] == "bullet_list_open"]
    assert len(lists) == 1 and lists[0][:2] == (0, 3)


def test_ordered_list_is_one_block():
    body = "1. one\n2. two\n3. three\n"
    assert _kinds(body) == ["ordered_list_open"]


def test_blockquote_with_internal_blank_is_one_block():
    body = "> a\n> b\n>\n> c\n\nafter\n"
    sp = block_spans(body)
    quotes = [b for b in sp if b[2] == "blockquote_open"]
    assert len(quotes) == 1 and quotes[0][:2] == (0, 4)


def test_table_is_one_block():
    body = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n\nnext\n"
    tables = [b for b in block_spans(body) if b[2] == "table_open"]
    assert len(tables) == 1 and tables[0][:2] == (0, 4)


def test_fence_with_internal_blank_is_one_block():
    body = "```py\nx = 1\n\ny = 2\n```\n\ndone\n"
    fences = [b for b in block_spans(body) if b[2] == "fence"]
    assert len(fences) == 1 and fences[0][:2] == (0, 5)


def test_heading_and_paragraph():
    body = "# Title\n\nSome text.\n"
    assert _kinds(body) == ["heading_open", "paragraph_open"]
    assert block_starts(body) == [0, 2]


# --- Claim 2: structural HTML tags merge across blank lines -----------------

def test_figure_with_blank_caption_merges():
    body = 'Intro.\n\n<figure class="c">\n\ncaption\n\n</figure>\n\nOutro.\n'
    sp = block_spans(body)
    structural = [b for b in sp if b[2] == "structural"]
    assert len(structural) == 1 and structural[0][:2] == (2, 7)
    assert len(sp) == 3  # intro, figure, outro


def test_figure_wrapping_script_is_one_block():
    body = ('<figure class="v">\n<script>\nconst x=1;\n\nconst y=2;\n</script>\n'
            '\n<figcaption>hi</figcaption>\n\n</figure>\n')
    sp = block_spans(body)
    assert len(sp) == 1 and sp[0][2] == "structural"


def test_nested_section_figure_is_one_block():
    body = ('<section class="t">\n\n# Inner\n\n<figure>\n\nbody\n\n</figure>\n'
            '\nremark\n</section>\n')
    sp = block_spans(body)
    assert len(sp) == 1 and sp[0][2] == "structural"


def test_two_sibling_figures_stay_separate():
    body = "<figure>\n\none\n\n</figure>\n\n<figure>\n\ntwo\n\n</figure>\n"
    structural = [b for b in block_spans(body) if b[2] == "structural"]
    assert len(structural) == 2


def test_self_closing_div_not_merged():
    body = 'Before.\n\n<div class="s" />\n\nAfter.\n'
    sp = block_spans(body)
    assert len(sp) == 3
    assert not any(b[2] == "structural" for b in sp)


def test_unclosed_figure_does_not_swallow_rest():
    # No </figure>: must NOT merge the rest of the doc into one block.
    body = "<figure>\n\norphan open\n\nlater paragraph\n"
    sp = block_spans(body)
    assert not any(b[2] == "structural" for b in sp)
    assert len(sp) >= 2  # the later paragraph survives as its own block


# --- Claim 3: stamping invariant (markdown-it-py proxy) ---------------------

def test_invariant_on_battery():
    docs = [
        "Intro.\n\n- a\n- b\n\n- c\n",
        "> q\n>\n> q2\n\npara\n",
        "| a | b |\n|---|---|\n| 1 | 2 |\n",
        "```py\nx=1\n\ny=2\n```\n",
        '<figure>\n\ncap\n\n</figure>\n\ntail\n',
        "# H\n\ntext *em*\n\n1. one\n2. two\n\n> quote\n",
        "para one\n\npara two\nstill two\n\n## sub\n\nmore\n",
    ]
    for d in docs:
        assert _invariant(d), f"stamping changed the render for:\n{d}"


# --- structural merge must be code-aware and name-matched (audit) -----------

def test_figure_with_fenced_close_tag_stays_one_block():
    # A </figure> printed inside a fenced code SAMPLE must NOT end the figure.
    body = "<figure>\n\n```html\n</figure>\n```\n\nreal caption\n\n</figure>\n\ntail\n"
    sp = block_spans(body)
    assert len([b for b in sp if b[2] == "structural"]) == 1, sp
    assert sp[-1][2] == "paragraph_open"   # the tail isn't swallowed


def test_mismatched_close_does_not_rebalance_figure():
    # A self-contained <div> (or a stray </div>) inside a <figure> must not
    # close the figure — closes match the innermost open by name.
    body = "<figure>\n\n<div>x</div>\n\nmore\n\n</figure>\n\nafter\n"
    assert len([b for b in block_spans(body) if b[2] == "structural"]) == 1


def test_inline_code_tag_in_prose_not_counted():
    body = "A paragraph mentioning the `</figure>` tag inline.\n\nNext.\n"
    sp = block_spans(body)
    assert not any(b[2] == "structural" for b in sp) and len(sp) == 2


def test_invariant_with_fenced_struct_tags():
    assert _invariant("<figure>\n\n```\n</figure>\n```\n\ncap\n\n</figure>\n\nend\n")


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
