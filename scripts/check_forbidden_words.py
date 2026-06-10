#!/usr/bin/env python3
"""Pre-commit guard: reject commits whose staged additions contain any
word from FORBIDDEN_WORDS (case-insensitive, word-boundary match).

Purpose
-------

This script is the mechanical gate against proprietary-product names
leaking into AM's public source. The agent (and the human typing in
a hurry) have both failed at the discipline-based version of this
rule — memory entries and verbal reminders aren't enough. This is the
fallback that doesn't depend on either of us remembering.

Adding / removing words
-----------------------

Edit FORBIDDEN_WORDS below. This script's own path is in
EXCLUDED_PATHS, so the word-list update itself doesn't trigger the
check on the commit that installs it.

Scoping
-------

The check only looks at staged ADDITIONS (lines starting with `+` in
the unified diff, minus the `+++` file-header lines). Lines being
REMOVED are fine — those are content already in history that we're
cleaning up. Only NEW additions are gated.

Bypass
------

`git commit --no-verify` would skip this. AM's policy: don't.
"""
from __future__ import annotations

import re
import subprocess
import sys

# Names that must NOT appear in AM's tracked source. Each entry is
# matched case-insensitively with word boundaries — "morfr" catches
# "Morfr", "MORFR", "the morfr build", but not "morfrology" (not that
# any of those exist).
FORBIDDEN_WORDS = [
    "morfr",
    "prism",
]

# Paths where the words may legitimately appear (this script lists
# them by definition; can't have the script reject its own existence).
# Use repo-relative POSIX paths exactly as they appear in `git diff`.
EXCLUDED_PATHS = {
    "scripts/check_forbidden_words.py",
}


def main() -> int:
    try:
        diff = subprocess.run(
            ["git", "diff", "--cached", "--no-color", "-U0"],
            capture_output=True,
            text=True,
            # git emits UTF-8; without this Windows decodes as cp1252 and
            # crashes on any non-Latin-1 byte (e.g. a U+25CF bullet in the
            # diff), which would both abort the commit AND silently skip the
            # guard. errors='replace' keeps the (ASCII) forbidden words matchable.
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout
    except FileNotFoundError:
        print("[forbidden-words] git not on PATH; skipping check",
              file=sys.stderr)
        return 0
    except subprocess.CalledProcessError as e:
        print(f"[forbidden-words] git diff failed: {e}", file=sys.stderr)
        return 1

    pattern = re.compile(
        r"\b(" + "|".join(re.escape(w) for w in FORBIDDEN_WORDS) + r")\b",
        re.IGNORECASE,
    )

    current_file: str | None = None
    hits: list[tuple[str, str, str]] = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/"):]
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if current_file in EXCLUDED_PATHS:
                continue
            content = line[1:]
            for m in pattern.finditer(content):
                hits.append((current_file or "?", m.group(1), content))

    if not hits:
        return 0

    print(
        "[forbidden-words] commit BLOCKED — staged additions contain "
        "forbidden product names:",
        file=sys.stderr,
    )
    for path, word, content in hits:
        snippet = content.strip()
        if len(snippet) > 100:
            snippet = snippet[:97] + "..."
        print(f"  {path}: matched '{word}' in: {snippet}", file=sys.stderr)
    print("", file=sys.stderr)
    print("This repo is public-MIT. Proprietary product names must not",
          file=sys.stderr)
    print("appear in tracked source. Rephrase generically — e.g. say",
          file=sys.stderr)
    print("'a desktop shell' or 'an installed shell' instead of naming",
          file=sys.stderr)
    print("a commercial product. See feedback_no_morfr_in_am_repo.md",
          file=sys.stderr)
    print("in the auto-memory for the full rule.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
