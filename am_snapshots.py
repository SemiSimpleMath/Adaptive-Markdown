"""Snapshot store.

Every doc folder owns its history: docs/<slug>/baseline.md is the immutable
history-0 (tracked in git for ship-with docs), docs/<slug>/snaps/snap-*.md
holds pre-edit captures. The structured "patches" sidecar that earlier
versions wrote has been retired — diffs can be recomputed from snapshots
if a review pipeline ever needs them.
"""
from __future__ import annotations

from pathlib import Path

from am_ids import gen_id


def _history_dir_for(doc_path: Path) -> Path:
    """Per-doc snap directory: docs/<slug>/snaps/.

    Caller passes the .../current.md (or .../baseline.md) — we use parent."""
    return doc_path.parent / "snaps"


def _history_zero_path(doc_path: Path) -> Path | None:
    """Locate the history-0 snapshot — always docs/<slug>/baseline.md.

    No more fallback to oldest snap: baseline.md is the canonical Reset
    target. If it's missing on a user-created doc, the user needs to
    explicitly establish a baseline (future "save as baseline" action)."""
    baseline = doc_path.parent / "baseline.md"
    return baseline if baseline.exists() else None


def save_snapshot(doc_path: Path, text: str) -> str:
    """Save text as a snapshot under docs/<slug>/snaps/, returning the id."""
    d = _history_dir_for(doc_path)
    d.mkdir(parents=True, exist_ok=True)
    # gen_id("snap") already prefixes "snap-" — don't double-prefix.
    snap_id = gen_id("snap")
    (d / f"{snap_id}.md").write_text(text, encoding="utf-8")
    return snap_id
