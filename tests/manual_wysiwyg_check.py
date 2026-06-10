"""On-demand end-to-end check for the WYSIWYG (Milkdown) editor behind ?edit.

NOT part of browser_smoke (it loads Milkdown + KaTeX from CDN, which would add
network flakiness to the smoke suite). Run it by hand when touching the editor:

    python tests/manual_wysiwyg_check.py

Any python works as the launcher: if it can't import the dep set, the harness
re-execs itself under one that can — see tests/harness_env.py (AM_PYTHON pins).

Spawns its own backend on a free port, then verifies, against the shipped docs:
  - ?edit is flag-only: fresh navigation lands on the Doc view, the editor
    mounts via the View dropdown, and a mid-edit reload returns to Edit
    (per-tab sessionStorage),
  - KaTeX math renders (intro),
  - heading markers ({#id}) are lifted out of the editable text into
    data-am-marker pills, survive heading edits, and round-trip byte-exact,
  - a whole-doc save round-trips (math preserved on disk),
  - structural wrappers pair into editable containers (structure-test):
    nesting, summary labels, wrapper lines byte-exact through save,
    normalize-once idempotence, typed text lands inside the wrapper,
  - embed figures render as cards, not raw <figure> source (plot-test),
  - editing a card's source (Edit source -> Apply -> Save) lands in current.md.

Resets intro + plot-test to baseline on the way out. Exit code 0 = all passed.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "tests"))
import harness_env  # noqa: E402  (needs the sys.path tweak above)


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
    # ?edit is flag-only (fresh navigation lands on the Doc view); enter the
    # editor through the View ▾ dropdown the way a user does.
    page.click("button.view-tab-dropdown:has-text('View')")
    page.click(".view-dropdown-menu .overflow-item:has-text('Edit (beta)')")
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
        # ?edit is a feature flag, not a view switch: a fresh navigation must
        # land on the Doc view (regression: time-notes bookmark booted into
        # the editor, losing collapsible details + hidden skill sections).
        page.goto(f"{base}/?edit")
        page.wait_for_function("() => document.getElementById('doc-select')?.value", timeout=15000)
        page.wait_for_timeout(800)
        r.check("fresh ?edit navigation lands on the Doc view",
                page.evaluate("() => !document.querySelector('#milkdown-mount .ProseMirror')"
                              " && document.getElementById('doc-frame') !== null"))
        _open_editor(page, base, "intro")
        r.check("Edit entry in View dropdown mounts the editor",
                page.evaluate("() => !!document.querySelector('#milkdown-mount .ProseMirror')"))
        # Exactly one editor: a superseded mount racing the CDN load used to
        # mount a SECOND ProseMirror into the new mount div.
        n_editors = page.evaluate(
            "() => document.querySelectorAll('#milkdown-mount .ProseMirror').length")
        r.check("exactly one editor instance after doc switch + reset",
                n_editors == 1, f"editors={n_editors}")
        r.check("KaTeX math renders",
                page.evaluate("() => document.querySelectorAll('#milkdown-mount .katex').length") > 0)
        r.check("caret CSS (white-space: pre-wrap) applied",
                page.evaluate("() => getComputedStyle(document.querySelector('#milkdown-mount .ProseMirror')).whiteSpace")
                in ("pre-wrap", "break-spaces"))
        # Heading markers ({#id}) live in node attrs, not the editable text:
        # invisible to the caret, rendered as a CSS pill, byte-exact on save.
        hdr_re = re.compile(r"^#{1,6} .*\{#[\w-]+\}\s*$")
        cur0 = page.evaluate(
            "async () => (await fetch('/docs/intro/current.md', {cache:'no-store'})).text()")
        marked0 = [l for l in cur0.splitlines() if hdr_re.match(l)]
        r.check("intro has marked headings (precondition)", len(marked0) > 0)
        editor_text = page.evaluate(
            "() => document.querySelector('#milkdown-mount .ProseMirror').textContent") or ""
        r.check("markers absent from editable text", "{#" not in editor_text)
        n_pills = page.evaluate(
            "() => document.querySelectorAll('#milkdown-mount .ProseMirror"
            " :is(h1,h2,h3,h4,h5,h6)[data-am-marker]').length")
        r.check("each marked heading carries its data-am-marker pill",
                n_pills == len(marked0), f"pills={n_pills} expected={len(marked0)}")
        # Type into a marked heading: the anchor must survive the edit (i.e.
        # the amMarker attr must survive other plugins' setNodeMarkup calls).
        mk = page.evaluate(
            "() => { const h = document.querySelector('#milkdown-mount [data-am-marker]');"
            "if (!h) return null; const r = h.getBoundingClientRect();"
            "return { v: h.getAttribute('data-am-marker'),"
            "  x: r.left + 30, y: r.top + r.height / 2 }; }")
        r.check("found a marked heading to edit", bool(mk))
        if mk:
            page.mouse.click(mk["x"], mk["y"])
            page.wait_for_timeout(150)
            page.keyboard.type("Zq")
            page.wait_for_timeout(150)
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
            # Ctrl+click = agent-focus selection (chip + outline); repeat toggles off.
            page.keyboard.down("Control")
            page.mouse.click(pt["x"], pt["y"])
            page.keyboard.up("Control")
            page.wait_for_timeout(200)
            bar = page.evaluate(
                "() => ({ vis: document.getElementById('selection-bar')"
                ".classList.contains('visible'),"
                " label: document.getElementById('selection-label').textContent })")
            r.check("ctrl+click selects block for agent focus",
                    bar and bar["vis"] and bar["label"], f"bar={bar}")
            outlined = page.evaluate(
                "() => document.querySelectorAll('#milkdown-mount .selected-block').length")
            r.check("selected block outlined in editor", outlined == 1, f"outlined={outlined}")
            page.keyboard.down("Control")
            page.mouse.click(pt["x"], pt["y"])
            page.keyboard.up("Control")
            page.wait_for_timeout(200)
            r.check("second ctrl+click deselects",
                    not page.evaluate("() => document.getElementById('selection-bar')"
                                      ".classList.contains('visible')"))
        else:
            r.check("mid-text click places the caret", False, "no long paragraph found")
        page.click("#edit-save")
        page.wait_for_function("() => /saved|failed|error/.test(document.getElementById('edit-status')?.textContent||'')", timeout=15000)
        status = page.eval_on_selector("#edit-status", "el => el.textContent")
        r.check("whole-doc save succeeds", "saved" in status, status)
        cur = page.evaluate("async () => (await fetch('/docs/intro/current.md', {cache:'no-store'})).text()")
        r.check("math round-trips on disk", bool(re.search(r"\$[^$\n]+\$", cur)))
        r.check("re-stamped tracking ids on save", "<!-- id:b-" in cur)
        marked1 = [l for l in cur.splitlines() if hdr_re.match(l)]
        zq = [l for l in marked1 if "Zq" in l]
        r.check("edited heading keeps its anchor",
                bool(mk) and len(zq) == 1 and zq[0].rstrip().endswith(mk["v"]),
                f"zq={zq[:1]}")
        untouched0 = sorted(l for l in marked0
                            if not (mk and l.rstrip().endswith(mk["v"])))
        untouched1 = sorted(l for l in marked1 if "Zq" not in l)
        r.check("untouched heading markers byte-exact on disk",
                untouched0 == untouched1,
                f"before={len(untouched0)} after={len(untouched1)}")
        _reset(page, "intro")
        # Mid-edit refresh returns to the editor (per-tab sessionStorage),
        # even though a fresh navigation lands on Doc.
        page.reload()
        page.wait_for_selector("#milkdown-mount .ProseMirror", timeout=45000)
        r.check("reload while editing returns to the Edit view",
                page.evaluate("() => !!document.querySelector('#milkdown-mount .ProseMirror')"))

        print("[scenario] structural containers (structure-test)")
        _open_editor(page, base, "structure-test")
        ncont = page.evaluate(
            "() => document.querySelectorAll('#milkdown-mount [data-am-container]').length")
        r.check("wrapper tags pair into container nodes", ncont == 5,
                f"containers={ncont} expected=5")
        r.check("nested wrapper pairs inside its parent container",
                page.evaluate("() => !!document.querySelector("
                              "'#milkdown-mount [data-am-container] [data-am-container]')"))
        r.check("no dangling close-tag chips for paired wrappers",
                page.evaluate("() => ![...document.querySelectorAll("
                              "'#milkdown-mount .am-embed-chip')]"
                              ".some(c => /^<\\//.test((c.textContent||'').trim()))"))
        r.check("details header shows its summary text",
                page.evaluate("() => ((document.querySelector('#milkdown-mount"
                              " [data-am-container=\"details\"] .am-container-title')"
                              "||{}).textContent||'').includes('Week One')"))
        r.check("heading-marker pill still works inside a container",
                page.evaluate("() => !!document.querySelector("
                              "'#milkdown-mount [data-am-container] [data-am-marker]')"))
        # Collapse state: seeded from the verbatim open attr, display-only.
        states = page.evaluate(
            "() => [...document.querySelectorAll('#milkdown-mount"
            " [data-am-container=\"details\"]')]"
            ".map(d => d.getAttribute('data-am-collapsed'))")
        r.check("open week expanded, attr-less week collapsed",
                states == ["0", "1"], f"states={states}")
        page.click("#milkdown-mount [data-am-container='details']"
                   " .am-container-header >> nth=1")
        page.wait_for_timeout(150)
        n_open = page.evaluate(
            "() => document.querySelectorAll('#milkdown-mount"
            " [data-am-container=\"details\"][data-am-collapsed=\"0\"]').length")
        r.check("clicking a collapsed week header expands it", n_open == 2,
                f"open={n_open}")
        page.click("#milkdown-mount [data-am-container='details']"
                   " .am-container-header >> nth=1")
        page.wait_for_timeout(150)
        # Agent-skill: opaque card, collapsed by default, content hidden.
        skill = page.evaluate(
            "() => { const s = document.querySelector('#milkdown-mount"
            " [data-am-class~=\"agent-skill\"]'); if (!s) return null;"
            "const t = document.querySelector('#milkdown-mount .ProseMirror').innerText || '';"
            "return { collapsed: s.getAttribute('data-am-collapsed'),"
            "  title: (s.querySelector('.am-container-title')||{}).textContent || '',"
            "  visible: t.includes('never see') }; }")
        r.check("agent-skill card collapsed by default, instructions hidden",
                bool(skill) and skill["collapsed"] == "1" and not skill["visible"],
                f"skill={skill}")
        r.check("agent-skill card titled from its first heading",
                bool(skill) and "Test Skill" in skill["title"],
                f"title={skill and skill['title']}")
        page.click("#milkdown-mount [data-am-class~='agent-skill'] .am-container-header")
        page.wait_for_timeout(150)
        r.check("clicking the skill card reveals its content for editing",
                page.evaluate("() => (document.querySelector('#milkdown-mount"
                              " .ProseMirror').innerText||'').includes('never see')"))
        page.click("#milkdown-mount [data-am-class~='agent-skill'] .am-container-header")
        page.wait_for_timeout(150)
        r.check("collapse toggles are display-only (editor not dirty)",
                page.evaluate("() => document.getElementById('edit-status')"
                              ".textContent") == "ready")
        # Self-contained content chunks render as FULL-text cards (shape-keyed,
        # not class-keyed) with in-place text editing.
        card_text = page.evaluate(
            "() => { const c = [...document.querySelectorAll('#milkdown-mount"
            " .am-text-card')].find(x => (x.textContent||'').includes('Self-contained'));"
            "return c ? c.textContent : null; }")
        r.check("self-contained chunk renders as a full-text card",
                bool(card_text) and "not break its enclosing week" in card_text,
                f"card={str(card_text)[:80]}")
        page.evaluate(
            "() => { const c = [...document.querySelectorAll('#milkdown-mount"
            " .am-text-card')].find(x => (x.textContent||'').includes('Self-contained'));"
            "[...c.querySelectorAll('.am-embed-btn')]"
            ".find(b => b.textContent === 'Edit text').click(); }")
        page.wait_for_selector(".am-text-card textarea", timeout=5000)
        ta_val = page.evaluate("() => document.querySelector('.am-text-card textarea').value")
        r.check("text editor shows inner text without wrapper tags",
                "Self-contained" in ta_val and "<div" not in ta_val,
                f"ta={ta_val[:60]}")
        page.evaluate("() => { const t = document.querySelector('.am-text-card textarea');"
                      " t.value = t.value + ' NARR9'; }")
        page.evaluate("() => { const c = document.querySelector('.am-text-card textarea')"
                      ".closest('.am-text-card');"
                      "[...c.querySelectorAll('.am-embed-btn')]"
                      ".find(b => b.textContent === 'Apply').click(); }")
        page.wait_for_timeout(400)
        # Wrapper/summary lines must survive the first (normalizing) save
        # byte-exact — this is the corruption guard for the serialize path.
        wrap_re = re.compile(r"^[ \t]*</?(?:details|div|aside|section|summary)\b.*$", re.M)
        base0 = page.evaluate(
            "async () => (await fetch('/docs/structure-test/baseline.md', {cache:'no-store'})).text()")
        page.click("#edit-save")
        page.wait_for_function("() => /saved|failed|error/.test(document.getElementById('edit-status')?.textContent||'')", timeout=15000)
        cur_a = page.evaluate(
            "async () => (await fetch('/docs/structure-test/current.md', {cache:'no-store'})).text()")
        r.check("wrapper/summary lines byte-exact through first save",
                wrap_re.findall(base0) == wrap_re.findall(cur_a),
                f"base={wrap_re.findall(base0)} cur={wrap_re.findall(cur_a)}")
        r.check("text-card edit lands inside its wrapper on disk",
                bool(re.search(
                    r'<div class="narrative">\r?\nSelf-contained'
                    r'[\s\S]*?NARR9\r?\n</div>', cur_a)))
        # Normalize-once idempotence: remount on the saved doc, save again,
        # bytes must be identical (modulo re-stamped tracking ids).
        page.click(".view-tab:has-text('Doc')")
        page.wait_for_timeout(600)
        page.click("button.view-tab-dropdown:has-text('View')")
        page.click(".view-dropdown-menu .overflow-item:has-text('Edit (beta)')")
        page.wait_for_selector("#milkdown-mount .ProseMirror", timeout=45000)
        page.wait_for_function(
            "() => document.getElementById('edit-status')?.textContent === 'ready'", timeout=20000)
        page.wait_for_timeout(600)
        page.click("#edit-save")
        page.wait_for_function("() => /saved|failed|error/.test(document.getElementById('edit-status')?.textContent||'')", timeout=15000)
        cur_b = page.evaluate(
            "async () => (await fetch('/docs/structure-test/current.md', {cache:'no-store'})).text()")
        strip_ids = lambda t: re.sub(
            r"^[ \t]*<!-- id:b-[A-Z0-9-]+ -->[ \t]*\r?\n", "", t, flags=re.M)
        r.check("second save is byte-identical (modulo tracking ids)",
                strip_ids(cur_a) == strip_ids(cur_b))
        # Typing into a block inside a details container must land BETWEEN
        # the wrapper tags on disk, not outside them.
        pt = page.evaluate(
            "() => { const p = document.querySelector("
            "'#milkdown-mount [data-am-container=\"details\"] p');"
            "if (!p) return null; const r = p.getBoundingClientRect();"
            "return { x: r.left + Math.min(60, r.width / 2), y: r.top + r.height / 2 }; }")
        r.check("found an editable paragraph inside a details container", bool(pt))
        if pt:
            page.mouse.click(pt["x"], pt["y"])
            page.wait_for_timeout(150)
            page.keyboard.type("Qz7")
            page.wait_for_timeout(150)
            page.click("#edit-save")
            page.wait_for_function("() => /saved|failed|error/.test(document.getElementById('edit-status')?.textContent||'')", timeout=15000)
            cur_c = page.evaluate(
                "async () => (await fetch('/docs/structure-test/current.md', {cache:'no-store'})).text()")
            i_open = cur_c.find("<details")
            i_text = cur_c.find("Qz7")
            i_close = cur_c.find("</details>")
            r.check("typed text lands inside the details block",
                    0 <= i_open < i_text < i_close,
                    f"open={i_open} text={i_text} close={i_close}")
        _reset(page, "structure-test")

        print("[scenario] embed cards + card-source edit (plot-test)")
        _open_editor(page, base, "plot-test")
        ncards = page.evaluate("() => document.querySelectorAll('#milkdown-mount .am-embed-card').length")
        r.check("figures render as embed cards", ncards > 0, f"cards={ncards}")
        r.check("no raw <figure> source shown",
                not page.evaluate("() => (document.querySelector('#milkdown-mount').textContent||'').includes('</figure>')"))
        # Ctrl+click on an embed card selects it with a SOURCE excerpt
        # (data-am-src), not the card's chrome text.
        cpt = page.evaluate(
            "() => { const c = document.querySelector('#milkdown-mount .am-embed-card');"
            "if (!c) return null; const r = c.getBoundingClientRect();"
            "return { x: r.left + r.width / 2, y: r.top + 8 }; }")
        if cpt:
            page.keyboard.down("Control")
            page.mouse.click(cpt["x"], cpt["y"])
            page.keyboard.up("Control")
            page.wait_for_timeout(200)
            csel = page.evaluate(
                "() => ({ vis: document.getElementById('selection-bar')"
                ".classList.contains('visible'),"
                " src: document.querySelector('#milkdown-mount .am-embed-card')"
                ".getAttribute('data-am-src') || '' })")
            r.check("ctrl+click selects embed card (source excerpt available)",
                    csel and csel["vis"] and csel["src"].startswith("<figure"),
                    f"csel={csel}")
            page.evaluate("() => document.getElementById('selection-clear').click()")
            page.wait_for_timeout(200)
        else:
            r.check("ctrl+click selects embed card (source excerpt available)",
                    False, "no embed card found")
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
    harness_env.bootstrap()  # right interpreter + UTF-8 console, or re-exec
    port = pick_free_port()
    env = os.environ.copy()  # bootstrap set PYTHONUTF8, scrubbed PYTHONPATH
    env["PORT"] = str(port)
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
