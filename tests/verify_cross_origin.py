"""Verify the proposed cross-origin model works in a real browser.

Spin up AM's backend on one port, then load it via two different hostnames:
  - http://127.0.0.1:<port>/   (parent)
  - http://localhost:<port>/   (iframe)

Confirm the browser treats them as different origins:
  1. The iframe IS visible to the parent (we can reach contentWindow).
  2. parent.document access from inside the iframe THROWS SecurityError.
  3. parent.localStorage access from inside the iframe THROWS.
  4. parent.location.href access THROWS (cross-origin).
  5. postMessage cross-origin DOES work (and we can verify e.origin).

If all assertions pass, the option-2 model (same port, two hostnames) is
viable; we can proceed with the architectural rework.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8228


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
    print(f"backend up on :{PORT}")

    failures: list[str] = []
    def check(label, ok, detail=""):
        marker = "PASS" if ok else "FAIL"
        print(f"  {marker}  {label}" + (f"  ({detail})" if detail else ""))
        if not ok:
            failures.append(label)

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context()
            page = ctx.new_page()

            # Parent loads at 127.0.0.1
            page.goto(f"http://127.0.0.1:{PORT}/")
            page.wait_for_function(
                "() => document.getElementById('doc-select')?.value",
                timeout=10000,
            )

            # Inject an iframe pointing at localhost (same machine, same
            # backend, same port — different origin per the SOP).
            page.evaluate(
                f"""() => {{
                    const f = document.createElement('iframe');
                    f.id = 'cross-origin-probe';
                    f.src = 'http://localhost:{PORT}/';
                    f.style.width = '300px';
                    f.style.height = '200px';
                    document.body.appendChild(f);
                    return new Promise((resolve) => {{
                        f.onload = () => resolve(true);
                    }});
                }}"""
            )

            # 1. We can reach the iframe element from the parent.
            iframe_present = page.evaluate(
                "() => !!document.getElementById('cross-origin-probe')"
            )
            check("iframe element reachable from parent", iframe_present)

            # 2. From parent, trying to read iframe.contentDocument
            #    cross-origin should give null (not throw, but blocked).
            content_doc = page.evaluate(
                "() => { try { return document.getElementById('cross-origin-probe').contentDocument; } catch (e) { return 'THREW:' + e.name; } }"
            )
            check(
                "iframe.contentDocument from parent is null/blocked",
                content_doc is None or str(content_doc).startswith("THREW:"),
                detail=str(content_doc),
            )

            # 3. From inside the iframe, parent.document access throws.
            iframe = page.frame_locator("#cross-origin-probe")
            # We use evaluate via the iframe locator's first frame
            iframe_handle = page.query_selector("#cross-origin-probe")
            inner = iframe_handle.content_frame()

            parent_doc = inner.evaluate(
                "() => { try { void parent.document.title; return 'NO_THROW'; } "
                "catch (e) { return e.name + ':' + (e.message||'').slice(0,80); } }"
            )
            check(
                "parent.document from iframe THROWS SecurityError",
                "SecurityError" in str(parent_doc),
                detail=str(parent_doc),
            )

            # 4. parent.localStorage from inside iframe THROWS
            parent_ls = inner.evaluate(
                "() => { try { void parent.localStorage.length; return 'NO_THROW'; } "
                "catch (e) { return e.name + ':' + (e.message||'').slice(0,80); } }"
            )
            check(
                "parent.localStorage from iframe THROWS SecurityError",
                "SecurityError" in str(parent_ls),
                detail=str(parent_ls),
            )

            # 5. parent.location.href from inside iframe THROWS
            parent_href = inner.evaluate(
                "() => { try { void parent.location.href; return 'NO_THROW'; } "
                "catch (e) { return e.name + ':' + (e.message||'').slice(0,80); } }"
            )
            check(
                "parent.location.href from iframe THROWS SecurityError",
                "SecurityError" in str(parent_href),
                detail=str(parent_href),
            )

            # 6. iframe's OWN location.href is readable (proves it has its own origin)
            own_href = inner.evaluate("() => location.href")
            check(
                "iframe's own location.href is readable (has own origin)",
                str(own_href).startswith(f"http://localhost:{PORT}"),
                detail=str(own_href),
            )

            # 7. iframe can use its OWN localStorage (proves allow-same-origin
            #    semantics work for the iframe's own origin)
            own_ls = inner.evaluate(
                "() => { try { localStorage.setItem('am-test','ok'); return localStorage.getItem('am-test'); } "
                "catch (e) { return e.name + ':' + (e.message||'').slice(0,80); } }"
            )
            check(
                "iframe can read/write its OWN localStorage",
                own_ls == "ok",
                detail=str(own_ls),
            )

            # 8. postMessage from iframe to parent: e.origin should be the
            #    iframe's hostname-based origin.
            page.evaluate("""() => {
                window.__amOriginSeen = null;
                window.addEventListener('message', (e) => {
                    if (e.data && e.data.type === 'xo-probe-ping')
                        window.__amOriginSeen = e.origin;
                });
            }""")
            inner.evaluate(
                "() => parent.postMessage({type:'xo-probe-ping'}, '*')"
            )
            page.wait_for_timeout(200)
            origin_seen = page.evaluate("() => window.__amOriginSeen")
            check(
                "postMessage e.origin from iframe = http://localhost:<port>",
                str(origin_seen) == f"http://localhost:{PORT}",
                detail=str(origin_seen),
            )

            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    if failures:
        print(f"\n{len(failures)} FAIL")
        return 1
    print("\nALL PASS — option 2 (same port, two hostnames) is viable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
