"""Regression guard: syntax-check every inline <script> in the viewer.

Catches the `</script>-in-comment` trap that has bitten this codebase
three times (commits 9337575 / 24cb46e / 2f2d9ea, all the same shape):
a JS comment containing a literal `</script>` makes the HTML parser
terminate the parent script tag at the comment, dumping the rest of
the runtime into the HTML body. Browser symptom is downstream and
hard to diagnose (broken music render, broken CSV figure, broken doc
switching, depending on what got truncated).

The pattern recurs because writing `</script>` in a JS comment FEELS
safe but isn't — the browser's HTML parser ignores JS syntax and
terminates the parent <script> at the first `</script>` token. Escape
with `<\\/script>` (HTML parser doesn't recognize the backslash form,
so it stays as plain text inside the script).

Run before committing any change to `iframe-host.html` or `index.html`:

    python scripts/check_inline_scripts.py

Runs in ~1 second (no playwright, no backend). Returns exit code 0
on success, 1 on any failure — suitable as a pre-commit hook.

Two complementary checks per file:

  1. Each inline script body the BROWSER would parse is syntactically
     valid JS (`node --check`). Uses am_html.find_blocks for the
     extraction, which respects HTML-spec script opacity — same
     semantics as the browser, so a stray `</script>` mid-body cuts
     the extracted body too, and node flags the truncated head.

  2. Each <script> opening has exactly one </script> closing token.
     Catches the rarer case where the truncated body happens to be
     syntactically valid (e.g., a stray </script> after a complete
     statement) — token count would still mismatch.

Requires `node` on PATH for the syntax check.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import am_html

FAILED = 0

# The files with inline JS we care about. Add new ones here as the
# project grows. (iframe-host.html ships its own runtime; index.html
# is the viewer chrome.)
TARGETS = ["index.html", "iframe-host.html"]


def check_inline_scripts(filename: str) -> None:
    global FAILED
    path = ROOT / filename
    if not path.exists():
        print(f"  SKIP  {filename} (not found)")
        return
    src = path.read_text(encoding="utf-8")
    # find_blocks treats <script> as opaque (browser-equivalent) — so
    # if the source has `</script>` in a comment, find_blocks
    # terminates the body at that token, just like the browser would,
    # and node --check will then flag the truncated body as broken.
    blocks = am_html.find_blocks(src, "script")

    # Complementary check: each <script> opening should have exactly
    # one </script> closing token. If a stray `</script>` in a body
    # left the truncated head syntactically valid (e.g., a comment
    # after a complete statement), node --check passes but the bug
    # still ships — that script body would be cut early in the browser.
    # Counting tokens catches it.
    close_token_count = src.lower().count("</script")
    if len(blocks) != close_token_count:
        FAILED += 1
        print(
            f"  FAIL  {filename}: am_html extracted {len(blocks)} "
            f"<script> block(s) but the file has {close_token_count} "
            f"</script> token(s). A stray `</script>` inside one of "
            f"the script bodies is terminating the browser's parse "
            f"early — escape it as `<\\/script>`."
        )

    inline = [b for b in blocks if "src" not in b.attrs and b.body.strip()]
    if not inline:
        print(f"  {filename}: no inline <script> blocks")
        return
    for b in inline:
        with tempfile.NamedTemporaryFile(
            suffix=".js", mode="w", delete=False, encoding="utf-8",
        ) as f:
            f.write(b.body)
            tmp = Path(f.name)
        try:
            result = subprocess.run(
                ["node", "--check", str(tmp)],
                capture_output=True, text=True, timeout=10,
            )
        finally:
            tmp.unlink(missing_ok=True)
        label = f"{filename}:{b.line} (inline <script>, {len(b.body)} chars)"
        if result.returncode == 0:
            print(f"  PASS  {label}")
        else:
            FAILED += 1
            # Surface the node error verbatim — it names the line/col
            # in the script body so the operator can see what broke.
            err = result.stderr.strip().splitlines()
            head = "\n        ".join(err[:8])
            print(f"  FAIL  {label}\n        {head}")


def main() -> int:
    print("Inline <script> syntax check (am_html-extracted, node --check)\n")
    for t in TARGETS:
        check_inline_scripts(t)
    if FAILED:
        print(f"\n{FAILED} FAILURES — likely a </script> token in a JS "
              f"comment terminating the parent tag. Escape with `<\\/script>`.")
        return 1
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
