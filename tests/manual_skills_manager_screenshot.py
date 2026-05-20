"""Screenshot the Skills manager modal so we can eyeball the new chrome."""
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
PORT = 8273
SLUG = "skills-preview"


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
        "---\ndoc_id: d-sm\ntitle: Skills preview\n---\n\n"
        '<section class="agent-skill">\n\n'
        "## SKILL: voice rules\n\n"
        "Use first-person plural throughout. Never the word 'comprehensive'. "
        "Avoid sales-speak.\n\n"
        "</section>\n\n"
        '<section class="agent-skill">\n\n'
        "## SKILL: math conventions\n\n"
        "Inline math as $...$, display math as $$...$$. Theorems wrapped "
        "in <section class=\"theorem\">. Cite by [author year] only.\n\n"
        "</section>\n\n"
        "# Body content\n\nReader-facing material.\n"
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
            page.click("#overflow-btn")
            page.wait_for_selector("#overflow-menu:not([hidden])", timeout=2000)
            page.click("#add-skill-btn")
            page.wait_for_selector("#skills-list .skill-card", timeout=3000)
            page.wait_for_timeout(300)
            shot1 = SCREENSHOTS / "skills-manager-list.png"
            page.screenshot(path=str(shot1), full_page=False)
            print(f"list view -> {shot1}")

            # Click Edit on the first skill to see the editor.
            page.evaluate(
                "() => document.querySelector('#skills-list .skill-card .btn-tiny').click()"
            )
            page.wait_for_selector("#skills-edit-dlg[open]", timeout=2000)
            page.wait_for_timeout(300)
            shot2 = SCREENSHOTS / "skills-manager-editor.png"
            page.screenshot(path=str(shot2), full_page=False)
            print(f"editor view -> {shot2}")

            browser.close()
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill()
        shutil.rmtree(doc_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
