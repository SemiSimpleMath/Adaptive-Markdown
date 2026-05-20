"""Screenshot the Review tab so we can eyeball the diff display."""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCREENSHOTS = ROOT / "screenshots"
SCREENSHOTS.mkdir(exist_ok=True)
PORT = 8264
SLUG = "review-preview"


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
    old_body = (
        "---\ndoc_id: d-rv\ntitle: Review preview\nreview_mode: pending\n---\n\n"
        "# Galois extensions\n\n"
        "A Galois extension is a field extension that is normal and "
        "separable.\n"
    )
    new_body = (
        "---\ndoc_id: d-rv\ntitle: Review preview\nreview_mode: pending\n---\n\n"
        "# The Galois correspondence\n\n"
        "A Galois extension $K/F$ is a field extension that is both normal "
        "and separable; equivalently, $|\\mathrm{Gal}(K/F)| = [K:F]$. "
        "The Galois group acts on $K$ fixing $F$.\n"
    )
    (doc_dir / "current.md").write_text(new_body, encoding="utf-8", newline="")
    (doc_dir / "baseline.md").write_text(old_body, encoding="utf-8", newline="")
    (doc_dir / "pending.json").write_text(
        json.dumps({
            "version": 1, "doc": SLUG,
            "edits": [{
                "id": "pe-DEMO",
                "tool_use_id": "tu-1",
                "block": {"kind": "doc"},
                "old_text": old_body,
                "new_text": new_body,
                "agent_label": "claude:sonnet-4-6",
                "created_at": "2026-05-18T12:00:00+00:00",
            }],
        }, indent=2),
        encoding="utf-8", newline="",
    )

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
            ctx = browser.new_context(viewport={"width": 1400, "height": 900})
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/?doc={SLUG}")
            page.wait_for_function(
                "() => Array.from(document.querySelectorAll('.view-tab'))"
                ".some(b => /Review/.test(b.textContent || ''))",
                timeout=10000,
            )
            page.evaluate(
                "() => Array.from(document.querySelectorAll('.view-tab'))"
                ".find(b => /Review/.test(b.textContent)).click()"
            )
            page.wait_for_selector(".view-review .review-card", timeout=3000)
            page.wait_for_timeout(400)
            shot = SCREENSHOTS / "review-tab.png"
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
