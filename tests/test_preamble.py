"""Unit tests for am_preamble.build_doc_context — the inline/skip decision
that keeps the active doc from being re-billed as fresh input on every
turn.

Pure function, no I/O — runs as plain pytest (`pytest tests/test_preamble.py`)
or standalone (`python tests/test_preamble.py`).
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from am_preamble import build_doc_context

CAP = 50 * 1024  # the Haiku/Sonnet inline cap used by am_ws
REL = "docs/intro/current.md"


def _sig(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_first_sight_inlines_full_body_and_reports_sig():
    body = "# Intro\n\nHello world.\n"
    lines, sig_to_store, inlined = build_doc_context(
        "intro", REL, body, CAP, prev_sig=None
    )
    joined = "\n".join(lines)
    # Full body is inlined verbatim, inside the fences.
    assert f"=== doc:{REL} ===" in joined
    assert body in joined
    assert "=== end doc ===" in joined
    # The caller is told to persist the new signature.
    assert sig_to_store == _sig(body)
    assert inlined is True


def test_unchanged_skips_inline_and_stores_nothing():
    body = "# Intro\n\nHello world.\n"
    lines, sig_to_store, inlined = build_doc_context(
        "intro", REL, body, CAP, prev_sig=_sig(body)
    )
    joined = "\n".join(lines)
    # Short pointer, NOT the full body.
    assert "UNCHANGED" in joined
    assert f"=== doc:{REL} ===" not in joined
    assert body not in joined
    # Nothing new to persist; doc is still "in context" (cached above).
    assert sig_to_store is None
    assert inlined is True


def test_changed_reinlines_with_new_sig():
    body = "# Intro\n\nEdited since last turn.\n"
    stale = _sig("# Intro\n\nThe OLD contents.\n")
    lines, sig_to_store, inlined = build_doc_context(
        "intro", REL, body, CAP, prev_sig=stale
    )
    joined = "\n".join(lines)
    assert f"=== doc:{REL} ===" in joined
    assert body in joined
    assert sig_to_store == _sig(body)
    assert sig_to_store != stale
    assert inlined is True


def test_over_cap_falls_back_to_read_on_demand():
    body = "x" * (CAP + 1)
    lines, sig_to_store, inlined = build_doc_context(
        "intro", REL, body, CAP, prev_sig=None
    )
    joined = "\n".join(lines)
    assert "too large to inline" in joined
    assert f"=== doc:{REL} ===" not in joined
    # Non-inline path never records a signature, and the doc is NOT in ctx.
    assert sig_to_store is None
    assert inlined is False


def test_opus_path_never_inlines():
    # am_ws passes inline_cap = -1 for Opus so even a tiny doc reads on
    # demand (its Read result is what gets cached in history).
    body = "# Intro\n\nTiny.\n"
    lines, sig_to_store, inlined = build_doc_context(
        "intro", REL, body, inline_cap=-1, prev_sig=None
    )
    joined = "\n".join(lines)
    assert "too large to inline" in joined
    assert sig_to_store is None
    assert inlined is False


def test_unreadable_doc_gives_minimal_pointer():
    lines, sig_to_store, inlined = build_doc_context(
        "intro", REL, doc_text=None, inline_cap=CAP, prev_sig="anything"
    )
    joined = "\n".join(lines)
    assert REL in joined
    assert f"=== doc:{REL} ===" not in joined
    assert "UNCHANGED" not in joined
    assert sig_to_store is None
    assert inlined is False


def test_cap_boundary_is_inclusive():
    # len == cap inlines; len == cap + 1 does not. Locks the boundary the
    # original `len(doc_text) <= INLINE_DOC_CAP` check had.
    at_cap = "y" * CAP
    _, sig_at, inlined_at = build_doc_context(
        "intro", REL, at_cap, CAP, prev_sig=None
    )
    assert inlined_at is True and sig_at == _sig(at_cap)

    over_cap = "y" * (CAP + 1)
    _, sig_over, inlined_over = build_doc_context(
        "intro", REL, over_cap, CAP, prev_sig=None
    )
    assert inlined_over is False and sig_over is None


def test_round_trip_state_machine():
    """The invariant the whole optimization rests on: feeding a turn's
    returned signature back as the next turn's prev_sig yields a skip, and
    a content change breaks the skip and re-inlines."""
    t1 = "# Doc\n\nv1\n"
    # Turn 1: first sight -> inline, get a sig.
    _, sig1, inl1 = build_doc_context("d", REL, t1, CAP, prev_sig=None)
    assert inl1 and sig1 is not None

    # Turn 2: same content, prev = sig1 -> skip.
    l2, sig2, inl2 = build_doc_context("d", REL, t1, CAP, prev_sig=sig1)
    assert "UNCHANGED" in "\n".join(l2)
    assert sig2 is None and inl2 is True

    # Turn 3: edited content, prev still sig1 -> re-inline with a new sig.
    t3 = "# Doc\n\nv2 (edited)\n"
    l3, sig3, inl3 = build_doc_context("d", REL, t3, CAP, prev_sig=sig1)
    assert t3 in "\n".join(l3)
    assert sig3 == _sig(t3) and sig3 != sig1 and inl3 is True


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok: {t.__name__}")
    print(f"\n{len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
