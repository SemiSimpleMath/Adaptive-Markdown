"""Quick sanity probe for the pending-changes substrate helpers.

Not part of the smoke harness (no HTTP / playwright); just exercises
load/add/remove/replace/find on the in-process functions. Run with:
    python tests/test_pending_helpers.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from am_pending import (
    load_pending, save_pending, add_pending_edit, remove_pending_edit,
    clear_pending, find_pending_for_block, _empty_pending,
    _read_review_mode,
)


def main() -> int:
    slug = "intro"
    clear_pending(slug)

    d = load_pending(slug)
    assert d["version"] == 1, d
    assert d["doc"] == slug, d
    assert d["edits"] == [], d
    print("empty load: ok")

    eid1 = add_pending_edit(slug, {
        "tool_use_id": "tu-1",
        "block": {"track_id": "b-XYZ", "label": "para 1",
                  "excerpt": "first paragraph"},
        "old_text": "First para.",
        "new_text": "First paragraph (revised).",
        "agent_label": "claude:sonnet-4-6",
    })
    assert eid1.startswith("pe-"), eid1
    d = load_pending(slug)
    assert len(d["edits"]) == 1
    assert d["edits"][0]["id"] == eid1
    print(f"add 1: ok ({eid1})")

    eid2 = add_pending_edit(slug, {
        "tool_use_id": "tu-2",
        "block": {"anchor_id": "intro", "label": "h1",
                  "excerpt": "Welcome"},
        "old_text": "# Welcome",
        "new_text": "# Welcome to AM",
        "agent_label": "claude:sonnet-4-6",
    })
    d = load_pending(slug)
    assert len(d["edits"]) == 2
    print(f"add 2: ok ({eid2})")

    # Replace edit 1 (same block signature).
    eid3 = add_pending_edit(slug, {
        "tool_use_id": "tu-3",
        "block": {"track_id": "b-XYZ", "label": "para 1",
                  "excerpt": "first paragraph"},
        "old_text": "First para.",
        "new_text": "First paragraph (revised AGAIN).",
        "agent_label": "claude:sonnet-4-6",
    })
    d = load_pending(slug)
    assert len(d["edits"]) == 2, (
        f"expected 2 entries after same-block replace, got {len(d['edits'])}"
    )
    ids = [e["id"] for e in d["edits"]]
    assert eid1 not in ids, "old entry should have been replaced"
    assert eid2 in ids and eid3 in ids
    print(f"replace same-block: ok ({eid3})")

    found = find_pending_for_block(slug, {"track_id": "b-XYZ"})
    assert found is not None and found["id"] == eid3
    print("find by track_id: ok")

    found_anchor = find_pending_for_block(slug, {"anchor_id": "intro"})
    assert found_anchor is not None and found_anchor["id"] == eid2
    print("find by anchor_id: ok")

    missing = find_pending_for_block(slug, {"track_id": "b-nope"})
    assert missing is None
    print("find miss: ok")

    assert remove_pending_edit(slug, eid3) is True
    d = load_pending(slug)
    assert len(d["edits"]) == 1
    print("remove: ok")

    assert remove_pending_edit(slug, eid2) is True
    p = ROOT / "docs" / slug / "pending.json"
    assert not p.exists(), "sidecar should be cleared after the last remove"
    print("clear on last remove: ok")

    try:
        add_pending_edit(slug, {"tool_use_id": "tu-x"})
        raise AssertionError("should have raised on missing fields")
    except ValueError as e:
        print(f"validation: ok ({e})")

    try:
        save_pending("does-not-exist-anywhere", _empty_pending("x"))
        raise AssertionError("should have raised on bad slug")
    except ValueError as e:
        print(f"bad slug: ok ({e})")

    clear_pending(slug)

    # Same-block REPLACE preserves the ORIGINAL old_text. This is the
    # phase 1 -> phase 2 refinement: rejection must walk back to the
    # true pre-pending state, not just to the previous proposal.
    add_pending_edit(slug, {
        "tool_use_id": "tu-a",
        "block": {"kind": "doc"},
        "old_text": "ORIGINAL",
        "new_text": "FIRST PROPOSAL",
        "agent_label": "claude:sonnet-4-6",
    })
    add_pending_edit(slug, {
        "tool_use_id": "tu-b",
        "block": {"kind": "doc"},
        "old_text": "SECOND BASIS — should be discarded",
        "new_text": "SECOND PROPOSAL",
        "agent_label": "claude:sonnet-4-6",
    })
    d = load_pending(slug)
    assert len(d["edits"]) == 1, d
    e = d["edits"][0]
    assert e["old_text"] == "ORIGINAL", (
        f"replace must preserve original old_text, got {e['old_text']!r}"
    )
    assert e["new_text"] == "SECOND PROPOSAL", e
    print("replace preserves original old_text: ok")
    clear_pending(slug)

    # _read_review_mode: per-doc frontmatter toggle.
    import tempfile

    def write_doc(text):
        p = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8",
        )
        p.write(text)
        p.close()
        return Path(p.name)

    no_fm = write_doc("# No frontmatter\n\nBody.\n")
    assert _read_review_mode(no_fm) is False
    no_fm.unlink()
    print("review_mode (no frontmatter): off")

    fm_no_key = write_doc(
        "---\ndoc_id: d-x\ntitle: Test\n---\n\n# Title\n\nBody.\n"
    )
    assert _read_review_mode(fm_no_key) is False
    fm_no_key.unlink()
    print("review_mode (key absent): off")

    fm_off = write_doc(
        "---\ndoc_id: d-x\nreview_mode: off\n---\n\n# Title\n\nBody.\n"
    )
    assert _read_review_mode(fm_off) is False
    fm_off.unlink()
    print("review_mode (explicit off): off")

    for truthy in ("pending", "Pending", '"pending"', "on", "true", "yes", "1"):
        fm_on = write_doc(
            f"---\ndoc_id: d-x\nreview_mode: {truthy}\n---\n\nBody.\n"
        )
        assert _read_review_mode(fm_on) is True, f"truthy={truthy}"
        fm_on.unlink()
    print("review_mode (truthy variants): all on")

    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
