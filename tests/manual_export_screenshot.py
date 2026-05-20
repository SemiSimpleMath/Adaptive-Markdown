"""End-to-end demo: export a doc via the Export button's path, write the
resulting HTML to disk, open it from a file:// URL, screenshot, and
sanity-check that math + headings + structure all render in the
standalone file with no backend.

Output:
  screenshots/export-viewer.png    (rendered in the AM viewer)
  screenshots/export-standalone.png (rendered from the exported .html alone)
  screenshots/export.html           (the actual exported file)
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCREENSHOTS = ROOT / "screenshots"
SCREENSHOTS.mkdir(exist_ok=True)
PORT = 8237


def wait_for_backend(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "backend.py"), str(PORT)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if not wait_for_backend(PORT):
        print("backend never came up")
        proc.kill()
        return 1

    export_path = SCREENSHOTS / "export.html"
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 1400})
            page = ctx.new_page()

            page.goto(f"http://127.0.0.1:{PORT}/?doc=intro")
            page.wait_for_function(
                "() => document.getElementById('doc-select')?.value === 'intro'"
                " && window.__amExport",
                timeout=10000,
            )
            page.wait_for_timeout(1200)
            page.screenshot(path=str(SCREENSHOTS / "export-viewer.png"),
                            full_page=True)
            print(f"[demo] viewer screenshot -> {SCREENSHOTS / 'export-viewer.png'}")

            # Build the export bundle exactly the same way the Export
            # button does and save the bytes to disk.
            html = page.evaluate(
                "() => window.__amExport.previewExport()"
            )
            export_path.write_text(html, encoding="utf-8")
            print(f"[demo] exported -> {export_path} ({len(html)} chars)")

            # Open the exported file via file:// — no backend in the loop.
            file_url = export_path.as_uri()
            page2 = ctx.new_page()
            page2.goto(file_url)
            # KaTeX is loaded from CDN — wait for math to typeset.
            try:
                page2.wait_for_function(
                    "() => document.querySelector('.katex')",
                    timeout=15000,
                )
            except Exception as e:
                print(f"[demo] WARN: KaTeX never rendered: {e}")
            page2.wait_for_timeout(1500)
            page2.screenshot(
                path=str(SCREENSHOTS / "export-standalone.png"),
                full_page=True,
            )
            print(f"[demo] standalone screenshot -> "
                  f"{SCREENSHOTS / 'export-standalone.png'}")

            # Sanity: confirm the standalone file actually has typeset math
            katex_count = page2.evaluate(
                "() => document.querySelectorAll('.katex').length"
            )
            heading_count = page2.evaluate(
                "() => document.querySelectorAll('h1, h2, h3').length"
            )
            doc_chars = page2.evaluate(
                "() => document.body.textContent.length"
            )
            print(f"[demo] standalone: katex_count={katex_count} "
                  f"heading_count={heading_count} body_chars={doc_chars}")

            if katex_count == 0 or heading_count == 0:
                print("[demo] FAIL — exported doc didn't render properly")
                return 1
            print("[demo] PASS — exported doc renders standalone")

            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    return 0


if __name__ == "__main__":
    sys.exit(main())
