"""On-demand end-to-end check for the WYSIWYG (Milkdown) editor behind ?edit.

NOT part of browser_smoke (it loads Milkdown + KaTeX from CDN, which would add
network flakiness to the smoke suite). Run it by hand when touching the editor:

    python tests/manual_wysiwyg_check.py

Spawns its own backend on a free port, then verifies, against the shipped docs:
  - the editor mounts on ?edit (auto-starts in the Edit view),
  - KaTeX math renders (intro),
  - a whole-doc save round-trips (math preserved on disk),
  - embed figures render as cards, not raw <figure> source (plot-test),
  - editing a card's source (Edit source -> Apply -> Save) lands in current.md.

Resets intro + plot-test to baseline on the way out. Exit code 0 = all passed.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_for_backend(port: int, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


class R:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, name, cond, detail=""):
        ok = bool(cond)
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
        if ok:
            self.passed += 1
        else:
            self.failed += 1


def _reset(page, slug):
    page.evaluate(
        "async (s) => { await fetch('/reset?doc=' + s, {method:'POST',"
        "headers:{'Content-Type':'application/json'}, body: JSON.stringify({doc:s})}); }",
        slug)
    page.wait_for_timeout(700)


def _open_editor(page, base, slug):
    page.goto(f"{base}/?edit")
    page.wait_for_function("() => document.getElementById('doc-select')?.value", timeout=15000)
    _reset(page, slug)
    page.select_option("#doc-select", slug)
    page.wait_for_timeout(1000)
    # ?edit auto-starts the editor; ensure it (re)mounts for this doc.
    page.wait_for_selector("#milkdown-mount .ProseMirror", timeout=45000)
    page.wait_for_function(
        "() => document.getElementById('edit-status')?.textContent === 'ready'", timeout=20000)
    page.wait_for_timeout(1000)


def run(base: str) -> int:
    from playwright.sync_api import sync_playwright
    r = R()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda e: print(f"    [pageerror] {str(e)[:200]}"))

        print("[scenario] editor mount + math + save round-trip (intro)")
        _open_editor(page, base, "intro")
        r.check("editor mounts on ?edit",
                page.evaluate("() => !!document.querySelector('#milkdown-mount .ProseMirror')"))
        r.check("KaTeX math renders",
                page.evaluate("() => document.querySelectorAll('#milkdown-mount .katex').length") > 0)
        r.check("caret CSS (white-space: pre-wrap) applied",
                page.evaluate("() => getComputedStyle(document.querySelector('#milkdown-mount .ProseMirror')).whiteSpace")
                in ("pre-wrap", "break-spaces"))
        # Regression: the block-focus handlers on #body used to preventDefault +
        # removeAllRanges on editor clicks, so the caret could only be placed at
        # line ends. Click mid-text and require a collapsed selection inside it.
        pt = page.evaluate(
            "() => { const p = [...document.querySelectorAll('#milkdown-mount .ProseMirror p')]"
            ".find(x => (x.textContent||'').length > 80); if (!p) return null;"
            "const r = p.getBoundingClientRect();"
            "return { x: r.left + Math.min(260, r.width * 0.45), y: r.top + 10 }; }")
        if pt:
            page.mouse.click(pt["x"], pt["y"])
            page.wait_for_timeout(200)
            sel = page.evaluate(
                "() => { const s = document.getSelection();"
                "if (!s || !s.anchorNode) return null;"
                "const el = s.anchorNode.nodeType === 3 ? s.anchorNode.parentElement : s.anchorNode;"
                "return { collapsed: s.isCollapsed, offset: s.anchorOffset,"
                "  inEditor: !!(el && el.closest('#milkdown-mount .ProseMirror')) }; }")
            r.check("mid-text click places the caret",
                    sel and sel["inEditor"] and sel["collapsed"] and sel["offset"] > 0,
                    f"sel={sel}")
        else:
            r.check("mid-text click places the caret", False, "no long paragraph found")
        page.click("#edit-save")
        page.wait_for_function("() => /saved|failed|error/.test(document.getElementById('edit-status')?.textContent||'')", timeout=15000)
        status = page.eval_on_selector("#edit-status", "el => el.textContent")
        r.check("whole-doc save succeeds", "saved" in status, status)
        cur = page.evaluate("async () => (await fetch('/docs/intro/current.md', {cache:'no-store'})).text()")
        import re
        r.check("math round-trips on disk", bool(re.search(r"\$[^$\n]+\$", cur)))
        r.check("re-stamped tracking ids on save", "<!-- id:b-" in cur)
        _reset(page, "intro")

        print("[scenario] embed cards + card-source edit (plot-test)")
        _open_editor(page, base, "plot-test")
        ncards = page.evaluate("() => document.querySelectorAll('#milkdown-mount .am-embed-card').length")
        r.check("figures render as embed cards", ncards > 0, f"cards={ncards}")
        r.check("no raw <figure> source shown",
                not page.evaluate("() => (document.querySelector('#milkdown-mount').textContent||'').includes('</figure>')"))
        page.evaluate("() => document.querySelector('.am-embed-card .am-embed-btn').click()")
        page.wait_for_selector(".am-embed-edit textarea", timeout=5000)
        page.evaluate("() => { const t=document.querySelector('.am-embed-edit textarea'); t.value = t.value + '<!--WYSIWYG_CHECK_MARK-->'; }")
        page.evaluate("() => { const a=[...document.querySelectorAll('.am-embed-edit .am-embed-btn')].find(x=>x.textContent==='Apply'); if(a)a.click(); }")
        page.wait_for_timeout(600)
        page.click("#edit-save")
        page.wait_for_function("() => /saved|failed|error/.test(document.getElementById('edit-status')?.textContent||'')", timeout=15000)
        cur2 = page.evaluate("async () => (await fetch('/docs/plot-test/current.md', {cache:'no-store'})).text()")
        r.check("card-source edit lands in current.md", "WYSIWYG_CHECK_MARK" in cur2)
        _reset(page, "plot-test")

        browser.close()

    print(f"\n{r.passed} passed, {r.failed} failed")
    return 1 if r.failed else 0


def main() -> int:
    port = pick_free_port()
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["PYTHONUTF8"] = "1"
    print(f"[harness] starting backend on :{port}")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "start.py"), "--claude", f"--port={port}"],
        env=env, cwd=str(ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        if not wait_for_backend(port):
            print("[harness] backend never came up")
            return 1
        time.sleep(1.0)
        return run(f"http://127.0.0.1:{port}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
