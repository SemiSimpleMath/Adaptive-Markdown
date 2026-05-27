"""Unit tests for the span logic behind GET/POST /block-source (source-edit):
locate a block by its track_id, return its EXACT markdown, replace it verbatim.
Lossless by construction — styled spans, math, and headings (full raw line, no
`## ` munging) round-trip byte-exact, and sibling blocks are untouched.

    python -m pytest tests/test_block_source.py
    python tests/test_block_source.py
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from am_tracking import ensure_block_ids_text, _find_block_span  # noqa: E402

# Mirror exactly what the handlers do: locate by track_id, slice / splice span.
def span_get(text, tid):
    lines = text.split("\n")
    sp = _find_block_span(lines, {"track_id": tid})
    if sp is None:
        return None
    s, e, _ = sp
    return "\n".join(lines[s:e])

def span_set(text, tid, new_src):
    lines = text.split("\n")
    s, e, _ = _find_block_span(lines, {"track_id": tid})
    return "\n".join(lines[:s] + new_src.split("\n") + lines[e:])

_DOC = (
    "# H\n\n"
    'A para with <span style="color:red">styled</span> text and $x^2$.\n\n'
    "Other para.\n"
)
def _ids(stamped):
    return re.findall(r"<!-- id:(b-[A-Z0-9]+) -->", stamped)


def test_get_returns_exact_source_with_span_and_math():
    stamped, _ = ensure_block_ids_text(_DOC)
    src = span_get(stamped, _ids(stamped)[1])
    assert src == 'A para with <span style="color:red">styled</span> text and $x^2$.'


def test_set_is_byte_lossless_and_contained():
    stamped, _ = ensure_block_ids_text(_DOC)
    ids = _ids(stamped)
    new = 'A para with <span style="color:red">restyled</span> text and $x^2$ and $y^2$.'
    out = span_set(stamped, ids[1], new)
    assert '<span style="color:red">restyled</span>' in out   # styling kept
    assert "$x^2$ and $y^2$" in out                            # math byte-exact
    assert " " not in out                                 # no nbsp gremlin
    assert span_get(out, ids[0]) == span_get(stamped, ids[0])  # heading untouched
    assert span_get(out, ids[2]) == span_get(stamped, ids[2])  # other para untouched


def test_heading_source_is_full_raw_line_no_munging():
    stamped, _ = ensure_block_ids_text("## Theorem (Rolle) {#rolle}\n\nbody\n")
    hid = _ids(stamped)[0]
    # GET returns the FULL raw heading line — `##` and `{#anchor}` included.
    assert span_get(stamped, hid) == "## Theorem (Rolle) {#rolle}"
    # SET replaces verbatim — no doubled `##` like the old edit_block munging.
    out = span_set(stamped, hid, "## Theorem (Rolle, revised) {#rolle}")
    assert "## Theorem (Rolle, revised) {#rolle}" in out
    assert out.count("## Theorem") == 1


def test_unknown_id_is_unlocatable():
    stamped, _ = ensure_block_ids_text(_DOC)
    assert span_get(stamped, "b-DOESNOTEXIST00") is None


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
