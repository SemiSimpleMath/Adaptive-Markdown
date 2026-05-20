"""Screenshot the refreshed intro doc so we can eyeball it.

Copies docs/intro/baseline.md to a throwaway slug, opens the viewer
against that slug (so the user's own docs/intro/current.md edits are
not disturbed), screenshots, cleans up.
"""
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
PORT = 8249
PROBE_SLUG = "intro-preview-probe"


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
    src = ROOT / "docs" / "intro" / "baseline.md"
    dst = ROOT / "docs" / PROBE_SLUG
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    md = src.read_text(encoding="utf-8")
    (dst / "current.md").write_text(md, encoding="utf-8", newline="")
    (dst / "baseline.md").write_text(md, encoding="utf-8", newline="")

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "backend.py"), str(PORT)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if not wait_for_backend(PORT):
        print("backend never came up")
        proc.kill()
        return 1

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 2200})
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/?doc={PROBE_SLUG}")
            page.wait_for_function(
                f"() => document.getElementById('doc-select')?.value === '{PROBE_SLUG}'",
                timeout=10000,
            )
            page.wait_for_timeout(1500)
            shot = SCREENSHOTS / "intro-refreshed.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"screenshot -> {shot}")
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(dst, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
