"""Tests for am_html.find_blocks — the HTML-block extractor that
replaces regex-based <tag>...</tag> parsing across the codebase.

Covers the regression cases listed in the am_html scoping doc, plus
basics. Run as:
    python tests/test_am_html.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from am_html import find_blocks


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        global FAILED
        FAILED += 1


FAILED = 0


def test_simple():
    print("\n[basic]")
    src = "before <p>hello</p> after"
    blocks = find_blocks(src, "p")
    check("returns one block", len(blocks) == 1)
    b = blocks[0]
    check("body == 'hello'", b.body == "hello", f"got {b.body!r}")
    check("start at '<'", src[b.start] == "<")
    check("end after '>'", src[b.end - 1] == ">")
    check("body_start/body_end slice gives body",
          src[b.body_start:b.body_end] == "hello")
    check("line == 1", b.line == 1, f"got {b.line}")


def test_attributes():
    print("\n[attributes]")
    src = '<section class="agent-skill" id="x" data-flag>body</section>'
    blocks = find_blocks(src, "section")
    check("returns one block", len(blocks) == 1)
    attrs = blocks[0].attrs
    check("class parsed", attrs.get("class") == "agent-skill",
          f"got {attrs}")
    check("id parsed", attrs.get("id") == "x", f"got {attrs}")
    check("boolean attr parsed as ''", attrs.get("data-flag") == "",
          f"got {attrs}")


def test_attribute_with_gt_inside_quotes():
    """A `>` inside a quoted attribute value must not terminate the
    opening tag prematurely. This bit us in the past — markdown like
    `<section data-info="x > y">` was getting cut at the first `>`."""
    print("\n[attribute with > in quotes]")
    src = '<section data-info="x > y">body</section>'
    blocks = find_blocks(src, "section")
    check("returns one block", len(blocks) == 1)
    if blocks:
        check("body is 'body' (didn't terminate early)",
              blocks[0].body == "body",
              f"got body={blocks[0].body!r}, attrs={blocks[0].attrs}")
        check("attr captured with > inside",
              blocks[0].attrs.get("data-info") == "x > y",
              f"got {blocks[0].attrs}")


def test_script_opacity_unrelated_close():
    """A `</div>` inside a script body must NOT terminate the script
    — only `</script>` does. Regex `[\\s\\S]*?` matches non-greedy,
    so it picks the first `</script>`; that's fine. But the previous
    regex pattern was `(?:<\\/script\\s*>)` which doesn't get fooled
    by `</div>` — so this is a baseline check."""
    print("\n[script opacity: </div> inside doesn't terminate]")
    src = '<script>const x = "</div>"; console.log(x);</script>'
    blocks = find_blocks(src, "script")
    check("returns one block", len(blocks) == 1)
    if blocks:
        check("body includes the </div>",
              "</div>" in blocks[0].body,
              f"got {blocks[0].body!r}")


def test_script_opacity_matches_browser():
    """The browser TERMINATES the script at the FIRST `</script>`,
    even inside a JS comment or string. This bit us TWICE in
    renderMusicBlocks and renderDataBlocks. The validator should
    agree with the browser (extract what the browser would parse) so
    when the agent writes a broken script, validation catches it."""
    print("\n[script opacity: </script> in comment terminates (matches browser)]")
    src = '<script>// fake </script> end\nreal code</script>'
    blocks = find_blocks(src, "script")
    check("returns one block", len(blocks) == 1)
    if blocks:
        check("body terminates at FIRST </script> (browser behavior)",
              blocks[0].body == "// fake ",
              f"got {blocks[0].body!r}")


def test_nested_same_tag():
    """`<div><div>x</div></div>` — by contract, find_blocks returns
    TOP-LEVEL matches only. Nested same-tag instances are part of
    the outer block's body. The contract matches every real caller
    (validators / agent-skill) — none want "all <div>s including nested."
    The close-tag pairing must still be correct, so the outer's body
    is the full inner content (not just the prefix to the first `</div>`)."""
    print("\n[same-tag nesting: <div><div>x</div></div>]")
    src = '<div><div>inner</div></div>'
    blocks = find_blocks(src, "div")
    check("returns 1 block (top-level only)", len(blocks) == 1,
          f"got {len(blocks)}")
    if blocks:
        check("outer body wraps the inner (correct close pairing)",
              blocks[0].body == "<div>inner</div>",
              f"got outer body={blocks[0].body!r}")


def test_multiline_opening_tag():
    """Opening tag attributes spanning multiple lines."""
    print("\n[multi-line opening tag]")
    src = '<section\n  class="agent-skill"\n  id="x">body</section>'
    blocks = find_blocks(src, "section")
    check("returns one block", len(blocks) == 1)
    if blocks:
        check("attrs parsed across lines",
              blocks[0].attrs.get("class") == "agent-skill",
              f"got {blocks[0].attrs}")


def test_empty_body():
    print("\n[empty body]")
    src = '<style></style>'
    blocks = find_blocks(src, "style")
    check("returns one block", len(blocks) == 1)
    if blocks:
        check("body == ''", blocks[0].body == "", f"got {blocks[0].body!r}")


def test_self_closing_skipped():
    print("\n[self-closing skipped]")
    src = '<br /><br/><br>'
    blocks = find_blocks(src, "br")
    check("void element returns 0 blocks", len(blocks) == 0,
          f"got {len(blocks)}")


def test_where_filter():
    """Predicate filters by attrs; non-matching skipped (and we don't
    consume them — nested blocks inside a non-matching section could
    still be returned, though they aren't in this test)."""
    print("\n[where filter on class]")
    src = (
        '<section class="theorem">not me</section>'
        '<section class="agent-skill">match</section>'
        '<section>nope</section>'
    )
    blocks = find_blocks(
        src, "section",
        where=lambda a: a.get("class") == "agent-skill",
    )
    check("only the matching one returned", len(blocks) == 1,
          f"got {len(blocks)}")
    if blocks:
        check("body is 'match'", blocks[0].body == "match",
              f"got {blocks[0].body!r}")


def test_line_numbers():
    print("\n[line numbers]")
    src = "line 1\nline 2\n<style>css</style>\nline 4"
    blocks = find_blocks(src, "style")
    check("returns one block", len(blocks) == 1)
    if blocks:
        check("line == 3", blocks[0].line == 3, f"got {blocks[0].line}")


def test_unmatched_open():
    print("\n[unmatched opening tag]")
    src = '<div>orphan with no close'
    blocks = find_blocks(src, "div")
    check("returns 0 blocks (unmatched skipped)", len(blocks) == 0,
          f"got {len(blocks)}")


def test_markdown_context_tolerance():
    """Source is markdown, lots of non-tag content around blocks."""
    print("\n[markdown context]")
    src = """# Title

Some paragraph with `<code>` (literal) inline.

A list:
- one
- two

<figure class="data">
<script type="text/csv">
name,age
A,1
</script>
</figure>

More prose follows.
"""
    figs = find_blocks(src, "figure")
    scripts = find_blocks(src, "script")
    check("found 1 figure", len(figs) == 1, f"got {len(figs)}")
    check("found 1 script", len(scripts) == 1, f"got {len(scripts)}")
    if scripts:
        check("script body includes CSV rows",
              "name,age" in scripts[0].body and "A,1" in scripts[0].body,
              f"got {scripts[0].body!r}")
    if figs:
        check("figure body includes the script verbatim",
              '<script type="text/csv">' in figs[0].body,
              "figure body missing the inner script")


def test_substring_false_positive():
    """`<section1>` is NOT `<section>` — tag name match must be exact."""
    print("\n[no false-positive on tag-name substring]")
    src = '<section1>not me</section1><section>me</section>'
    blocks = find_blocks(src, "section")
    check("only the exact-name tag matches", len(blocks) == 1,
          f"got {len(blocks)} blocks")
    if blocks:
        check("body is 'me'", blocks[0].body == "me",
              f"got {blocks[0].body!r}")


def main() -> int:
    test_simple()
    test_attributes()
    test_attribute_with_gt_inside_quotes()
    test_script_opacity_unrelated_close()
    test_script_opacity_matches_browser()
    test_nested_same_tag()
    test_multiline_opening_tag()
    test_empty_body()
    test_self_closing_skipped()
    test_where_filter()
    test_line_numbers()
    test_unmatched_open()
    test_markdown_context_tolerance()
    test_substring_false_positive()
    print(f"\n{'ALL PASS' if FAILED == 0 else f'{FAILED} FAILURES'}")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
