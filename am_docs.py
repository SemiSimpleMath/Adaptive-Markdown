"""Doc-identity, listing, and slug → path resolution.

Every .md ships with a stable `doc_id` in its frontmatter (minted on
first sight) and lives under `docs/<slug>/current.md`. This module owns:

  - listing every doc the viewer should know about (list_all_docs)
  - resolving a slug to its writable current.md (_doc_path_for) and
    the inverse (_doc_slug_from_path)
  - first-run bootstrap (ensure_doc_ids, ensure_working_copies)

Used by backend wiring, am_upload, and anywhere else that needs to
turn a slug into a path or list the workspace.
"""
from __future__ import annotations

import re
from pathlib import Path

from am_ids import gen_id

ROOT = Path(__file__).resolve().parent
DOCS_ROOT = ROOT / "docs"
COMPONENTS_ROOT = ROOT / "components"
DOC_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$", re.IGNORECASE)
# Components share the same slug shape (kebab-case, leading alnum). Reuse
# the same regex so the agent doesn't need to learn two naming rules.
COMPONENT_SLUG_RE = DOC_SLUG_RE

# Frontmatter helpers — used by ensure_doc_ids and shared with am_pending's
# review-mode reader. Re-defining the regex here keeps am_docs free of
# cross-module imports for one regex.
FRONTMATTER_RE = re.compile(r"^---\r?\n([\s\S]*?)\r?\n---\r?\n")
_HAS_DOC_ID_RE = re.compile(r"^doc_id\s*:", re.MULTILINE)


def list_all_docs() -> list[str]:
    """Slugs of every doc — one per docs/<slug>/ folder that has a current.md
    (or a baseline.md awaiting first-run init). Sorted."""
    if not DOCS_ROOT.exists():
        return []
    slugs: list[str] = []
    for sub in DOCS_ROOT.iterdir():
        if not sub.is_dir():
            continue
        if not DOC_SLUG_RE.match(sub.name):
            continue  # ignore weird names; agents never create them
        if (sub / "current.md").exists() or (sub / "baseline.md").exists():
            slugs.append(sub.name)
    return sorted(slugs)


def list_all_components() -> list[str]:
    """Slugs of every saved component — one per components/<slug>.md file.
    Sorted. Returns empty list if components/ doesn't exist yet (first
    run, before any component has been saved or shipped)."""
    if not COMPONENTS_ROOT.exists():
        return []
    slugs: list[str] = []
    for entry in COMPONENTS_ROOT.iterdir():
        if not entry.is_file() or entry.suffix.lower() != ".md":
            continue
        slug = entry.stem
        if not COMPONENT_SLUG_RE.match(slug):
            continue
        slugs.append(slug)
    return sorted(slugs)


def _doc_path_for(doc_param: str) -> Path | None:
    """Resolve a doc slug (e.g. 'intro') to its current.md absolute Path.

    Returns None if the slug is malformed or the doc folder doesn't exist.
    The agent only ever writes to current.md; baseline.md is immutable."""
    if not doc_param or not DOC_SLUG_RE.match(doc_param):
        return None
    p = (DOCS_ROOT / doc_param / "current.md").resolve()
    try:
        p.relative_to(DOCS_ROOT)
    except ValueError:
        return None
    if p.parent.parent != DOCS_ROOT:
        return None
    # current.md may not exist yet on a fresh clone; the boot init creates it
    # from baseline.md. Either way, return the canonical path.
    return p


def _doc_slug_from_path(file_path: str | Path) -> str | None:
    """Inverse of _doc_path_for: given an absolute or ROOT-relative path that
    points at docs/<slug>/current.md, return the slug. Otherwise None."""
    try:
        p = Path(file_path).resolve()
        rel = p.relative_to(DOCS_ROOT)
    except (ValueError, OSError):
        return None
    parts = rel.parts
    if len(parts) != 2 or parts[1] != "current.md":
        return None
    if not DOC_SLUG_RE.match(parts[0]):
        return None
    return parts[0]


def ensure_doc_ids() -> None:
    """Ensure every docs/<slug>/current.md has a `doc_id` in its frontmatter."""
    if not DOCS_ROOT.exists():
        return
    minted = 0
    for sub in sorted(DOCS_ROOT.iterdir()):
        if not sub.is_dir() or not DOC_SLUG_RE.match(sub.name):
            continue
        md = sub / "current.md"
        if not md.exists():
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        m = FRONTMATTER_RE.match(text)
        if not m:
            continue  # no frontmatter — leave alone for now
        fm_body = m.group(1)
        if _HAS_DOC_ID_RE.search(fm_body):
            continue
        new_id = gen_id("d")
        # Insert doc_id at the top of the frontmatter, preserving everything else
        # byte-for-byte (no YAML round-trip — keeps quote styles, comments, order).
        new_fm_body = f"doc_id: {new_id}\n{fm_body}"
        new_text = f"---\n{new_fm_body}\n---\n" + text[m.end():]
        md.write_text(new_text, encoding="utf-8")
        minted += 1
        print(f"  minted doc_id={new_id} for {sub.name}/current.md", flush=True)
    if minted:
        print(f"ensured doc_ids ({minted} new)", flush=True)


def ensure_working_copies() -> None:
    """First-run / fresh-clone init: for every docs/<slug>/baseline.md that
    lacks a sibling current.md, copy baseline.md -> current.md. Ensures the
    viewer always has something to render and the agent always has a file
    to Edit, even on a freshly cloned repo where only baselines are in git."""
    if not DOCS_ROOT.exists():
        return
    created = 0
    for sub in sorted(DOCS_ROOT.iterdir()):
        if not sub.is_dir() or not DOC_SLUG_RE.match(sub.name):
            continue
        baseline = sub / "baseline.md"
        current = sub / "current.md"
        if baseline.exists() and not current.exists():
            current.write_bytes(baseline.read_bytes())
            created += 1
            print(f"  initialised current.md for {sub.name} from baseline",
                  flush=True)
    if created:
        print(f"ensured working copies ({created} new)", flush=True)
