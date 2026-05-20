"""Screenshot a rendered music doc so we can eyeball the abcjs + audio
widget chrome. Builds a doc with a small ABC tune, opens it in the
viewer, waits for abcjs CDN load + render, screenshots."""
from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCREENSHOTS = ROOT / "screenshots"
SCREENSHOTS.mkdir(exist_ok=True)
PORT = 8297
SLUG = "music-preview"


def wait_for_backend(port, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def main() -> int:
    doc_dir = ROOT / "docs" / SLUG
    if doc_dir.exists():
        shutil.rmtree(doc_dir)
    doc_dir.mkdir(parents=True)
    body = (
        "---\ndoc_id: d-music\ntitle: Music preview\n---\n\n"
        "# Twinkle Twinkle\n\nABC notation renders in-browser via abcjs, "
        "loaded lazily from CDN only when the doc has music. The play "
        "button under the staff uses the Web Audio API.\n\n"
        '<figure class="music">\n<div class="abc">\n'
        "X:1\nT:Twinkle Twinkle Little Star\nM:4/4\nL:1/4\nK:C\n"
        "C C G G | A A G2 | F F E E | D D C2 |\n"
        "G G F F | E E D2 | G G F F | E E D2 |\n"
        "C C G G | A A G2 | F F E E | D D C2 |\n"
        "</div>\n</figure>\n"
    )
    (doc_dir / "current.md").write_text(body, encoding="utf-8", newline="")
    (doc_dir / "baseline.md").write_text(body, encoding="utf-8", newline="")

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "backend.py"), str(PORT)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if not wait_for_backend(PORT):
        proc.kill(); return 1
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/?doc={SLUG}")
            page.wait_for_function(
                f"() => document.getElementById('doc-select')?.value === '{SLUG}'",
                timeout=10000,
            )
            # Wait for abcjs to lazy-load + render. The .abc-notation div
            # only appears after the script tag fires onload.
            doc_frame = None
            for f in page.frames:
                if f.parent_frame is page.main_frame:
                    doc_frame = f
                    break
            if doc_frame is None:
                print("(no iframe found)")
                return 1
            try:
                doc_frame.wait_for_selector(".abc-notation svg", timeout=20000)
                doc_frame.wait_for_selector(".abc-audio", timeout=5000)
            except Exception as e:
                print(f"(music renderer never produced output: {e})")
            page.wait_for_timeout(500)
            shot = SCREENSHOTS / "music-rendered.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"screenshot -> {shot}")
            browser.close()
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill()
        shutil.rmtree(doc_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
