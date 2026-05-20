"""Screenshot the toolbar before/after opening the overflow menu so we
can eyeball the new chrome."""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCREENSHOTS = ROOT / "screenshots"
SCREENSHOTS.mkdir(exist_ok=True)
PORT = 8262


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
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if not wait_for_backend(PORT):
        proc.kill(); return 1
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1400, "height": 280})
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/?doc=intro")
            page.wait_for_function(
                "() => document.getElementById('doc-select')?.value",
                timeout=10000,
            )
            page.wait_for_timeout(600)
            page.screenshot(
                path=str(SCREENSHOTS / "overflow-closed.png"),
                clip={"x": 0, "y": 0, "width": 1400, "height": 80},
            )
            page.click("#overflow-btn")
            page.wait_for_selector("#overflow-menu:not([hidden])", timeout=2000)
            page.wait_for_timeout(200)
            page.screenshot(
                path=str(SCREENSHOTS / "overflow-open.png"),
                clip={"x": 0, "y": 0, "width": 1400, "height": 280},
            )
            print(f"screenshots -> {SCREENSHOTS / 'overflow-closed.png'} and overflow-open.png")
            browser.close()
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
