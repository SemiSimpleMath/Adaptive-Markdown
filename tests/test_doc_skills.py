"""Unit tests for the sidecar per-doc skill helpers (ADR-002):
am_docs.parse_skill_file / free_skill_slug / render_skill_file and
am_preamble.build_skills_context.

Runs as plain pytest or standalone (`python tests/test_doc_skills.py`)."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from am_docs import parse_skill_file, render_skill_file
from am_preamble import build_skills_context


FILE_WITH_FM = (
    "---\n"
    "name: Time Entry Workflow\n"
    "description: How to maintain this notes doc.\n"
    "---\n"
    "\n"
    "## Two Modes\n\nNote collection vs finalization.\n"
)


def test_parse_name_from_frontmatter():
    got = parse_skill_file(FILE_WITH_FM, "time-entry-workflow")
    assert got["name"] == "Time Entry Workflow"
    assert got["body"].startswith("## Two Modes")
    assert "---" not in got["body"]


def test_parse_name_falls_back_to_skill_heading():
    raw = "## SKILL: Drafting Rules\n\nbody text\n"
    got = parse_skill_file(raw, "stem-name")
    assert got["name"] == "Drafting Rules"
    # H1 form too — real docs write `# SKILL:` (this bit the time-notes
    # migration: the old regex required ##+ and produced "untitled #1").
    got_h1 = parse_skill_file("# SKILL: Time Entry\n\nbody\n", "stem")
    assert got_h1["name"] == "Time Entry"


def test_parse_name_falls_back_to_stem():
    got = parse_skill_file("just instructions, no name anywhere\n", "my-skill")
    assert got["name"] == "my-skill"


def test_render_round_trips_through_parse():
    raw = render_skill_file("My Skill", "Do the thing.\n\nCarefully.", "desc")
    got = parse_skill_file(raw, "my-skill")
    assert got["name"] == "My Skill"
    assert got["body"] == "Do the thing.\n\nCarefully."


def test_free_skill_slug_slugifies_and_avoids_collisions(monkeypatch=None):
    import am_docs
    with tempfile.TemporaryDirectory() as td:
        orig = am_docs.DOCS_ROOT
        am_docs.DOCS_ROOT = Path(td)
        try:
            d = Path(td) / "mydoc" / "skills"
            d.mkdir(parents=True)
            s1 = am_docs.free_skill_slug("mydoc", "Time Entry: Workflow!")
            assert s1 == "time-entry-workflow", s1
            (d / "time-entry-workflow.md").write_text("x", encoding="utf-8")
            s2 = am_docs.free_skill_slug("mydoc", "Time Entry: Workflow!")
            assert s2 == "time-entry-workflow-2", s2
            s3 = am_docs.free_skill_slug("mydoc", "!!!")
            assert s3 == "skill", s3
        finally:
            am_docs.DOCS_ROOT = orig


SKILLS = [
    {"rel": "docs/d/skills/a.md", "name": "A", "raw": "---\nname: A\n---\n\nbody-a\n"},
    {"rel": "docs/d/skills/b.md", "name": "B", "raw": "---\nname: B\n---\n\nbody-b\n"},
]


def test_skills_context_inlines_on_first_sight():
    lines, sig = build_skills_context("d", SKILLS, None)
    assert sig, "expected a signature to store"
    joined = "\n".join(lines)
    assert "body-a" in joined and "body-b" in joined
    assert "docs/d/skills/" in joined
    assert "authoritative" in joined


def test_skills_context_skips_when_unchanged():
    _, sig = build_skills_context("d", SKILLS, None)
    lines, sig2 = build_skills_context("d", SKILLS, sig)
    assert sig2 is None
    joined = "\n".join(lines)
    assert "body-a" not in joined          # pointer, not a re-send
    assert "unchanged" in joined


def test_skills_context_reinlines_on_change():
    _, sig = build_skills_context("d", SKILLS, None)
    changed = [dict(SKILLS[0]), dict(SKILLS[1])]
    changed[1]["raw"] = "---\nname: B\n---\n\nbody-b EDITED\n"
    lines, sig2 = build_skills_context("d", changed, sig)
    assert sig2 and sig2 != sig
    assert "body-b EDITED" in "\n".join(lines)


def test_skills_context_announces_removal_once():
    _, sig = build_skills_context("d", SKILLS, None)
    lines, sig2 = build_skills_context("d", [], sig)
    assert "disregard" in "\n".join(lines)
    assert sig2 == ""                       # stored marker: announced
    lines3, sig3 = build_skills_context("d", [], sig2)
    assert lines3 == [] and sig3 is None    # silent thereafter


def test_skills_context_silent_when_no_skills_ever():
    lines, sig = build_skills_context("d", [], None)
    assert lines == [] and sig is None


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok: {t.__name__}")
    print(f"\n{len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
