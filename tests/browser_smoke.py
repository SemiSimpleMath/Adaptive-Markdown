"""Real-browser smoke test for adaptive-markdown.

This is the gap that curl-based testing misses: actual browser behavior on
same-origin GETs (no Origin header), iframe null-origin enforcement, drop-
to-convert preview, etc. Playwright drives a real headless Chromium so the
tests see what the user sees.

Setup (one-time):
    pip install playwright
    playwright install chromium

Run:
    python tests/browser_smoke.py
    python tests/browser_smoke.py --port 8093  # override default port

The script starts the backend in a subprocess on the chosen port, runs the
scenarios, and tears down. Exit code 0 if every assertion passes; 1 if any
fails. Each scenario prints a single PASS/FAIL line so the output is easy
to scan.
"""
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8093


# ---- subprocess + readiness ---------------------------------------------

def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def wait_for_backend(port: int, timeout: float = 20.0) -> bool:
    """Poll the backend until it responds, returning True on success."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


@contextmanager
def backend_running(port: int):
    """Start backend.py as a subprocess on the given port, yield, then kill."""
    if not port_is_free(port):
        raise RuntimeError(
            f"port {port} is already in use — pick another with --port"
        )
    # Force markitdown for PDF conversion under the smoke test so we don't
    # burn API tokens on every run. Production default is Claude vision.
    env = dict(__import__("os").environ)
    env["AM_PDF_BACKEND"] = "markitdown"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "backend.py"), str(port)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    try:
        if not wait_for_backend(port):
            # Drain output for diagnostics, then bail.
            proc.terminate()
            out, _ = proc.communicate(timeout=2)
            raise RuntimeError(f"backend never became ready:\n{out}")
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---- test bookkeeping ---------------------------------------------------

class Results:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def assert_(self, label: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {label}")
            self.passed.append(label)
        else:
            print(f"  FAIL  {label}  ({detail})")
            self.failed.append((label, detail))


# ---- scenarios ----------------------------------------------------------

def scenario_http_routes(page, base: str, r: Results) -> None:
    """Direct fetches from the (real, in-browser) viewer context. Same-origin
    GETs in Chromium do not send Origin, so this is the exact case that
    broke history before the lax-Origin fix."""
    print("\n[scenario] HTTP routes via real-browser fetch (same-origin)")

    page.goto(base + "/")

    def fetch_status(path: str) -> int:
        return page.evaluate(
            "async (p) => (await fetch(p)).status", path
        )

    r.assert_("GET / loads", page.url.startswith(base + "/"))
    r.assert_("GET /favicon.svg = 200", fetch_status("/favicon.svg") == 200)
    r.assert_("GET /docs/intro/current.md = 200",
              fetch_status("/docs/intro/current.md") == 200)
    r.assert_("GET /docs/intro/baseline.md = 200",
              fetch_status("/docs/intro/baseline.md") == 200)
    r.assert_("GET /docs/intro/snaps/foo.md = 404 (not viewer-facing)",
              fetch_status("/docs/intro/snaps/foo.md") == 404)
    r.assert_("GET /.env = 404 (allowlist)", fetch_status("/.env") == 404)
    r.assert_("GET /backend.py = 404 (allowlist)",
              fetch_status("/backend.py") == 404)
    r.assert_("GET /LICENSE = 404 (allowlist)",
              fetch_status("/LICENSE") == 404)
    r.assert_("GET /docs/../backend.py = 404 (traversal)",
              fetch_status("/docs/../backend.py") == 404)
    # The history endpoint is the one the user just hit a 403 on. Catch
    # any regression of that exact case.
    r.assert_(
        "GET /history (same-origin, browser omits Origin) = 200",
        fetch_status("/history?doc=intro") == 200,
    )


def _wait_for_doc_iframe(page, timeout_ms: int = 10000):
    """Return the Frame object for the doc iframe once it has loaded.

    The parent-side iframe.contentDocument is null because the iframe is
    loaded from a different origin (the whole point), so we can't wait
    from the parent. Playwright's CDP-level frame API works regardless of
    SOP — enumerate page.frames and pick the iframe child once it appears."""
    page.wait_for_selector("#doc-frame", timeout=timeout_ms)
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        for f in page.frames:
            if f.parent_frame is page.main_frame:
                # Frame exists; make sure DOM is parsed.
                try:
                    f.wait_for_load_state("domcontentloaded", timeout=2000)
                    return f
                except Exception:
                    continue
        time.sleep(0.2)
    raise TimeoutError("doc iframe never loaded")


def scenario_iframe_isolation(page, base: str, r: Results) -> None:
    """The iframe is loaded from a DIFFERENT origin than the viewer. The
    sandbox grants `allow-same-origin` so the iframe can store its own
    state (needed for nested embeds like YouTube), but the cross-origin
    gap means iframe scripts still cannot reach the viewer's DOM, storage,
    or read same-origin fetch bodies."""
    print("\n[scenario] iframe cross-origin enforcement")

    page.goto(base + "/")
    frame = _wait_for_doc_iframe(page)

    # The iframe should be loaded from a different origin than the parent.
    iframe_origin = frame.evaluate("() => location.origin")
    parent_origin = page.evaluate("() => location.origin")
    r.assert_(
        "iframe origin differs from viewer origin (cross-origin split)",
        iframe_origin != parent_origin and bool(iframe_origin),
        detail=f"viewer={parent_origin} iframe={iframe_origin}",
    )

    # Parent.document access from inside the iframe should throw SecurityError
    # because the iframe is at a different origin than the parent.
    parent_access = frame.evaluate(
        "() => { try { return { ok: true, val: parent.document.title }; } "
        "catch (e) { return { ok: false, err: e.name + ': ' + e.message }; } }"
    )
    r.assert_(
        "iframe parent.document throws SecurityError",
        not parent_access.get("ok"),
        detail=str(parent_access),
    )

    # parent.localStorage — same expectation, cross-origin SecurityError.
    parent_storage = frame.evaluate(
        "() => { try { parent.localStorage.getItem('x'); return { ok: true }; } "
        "catch (e) { return { ok: false, err: e.name }; } }"
    )
    r.assert_(
        "iframe parent.localStorage throws SecurityError",
        not parent_storage.get("ok"),
        detail=str(parent_storage),
    )

    # parent.location.href: also cross-origin SecurityError.
    parent_href = frame.evaluate(
        "() => { try { void parent.location.href; return { ok: true }; } "
        "catch (e) { return { ok: false, err: e.name }; } }"
    )
    r.assert_(
        "iframe parent.location.href throws SecurityError",
        not parent_href.get("ok"),
        detail=str(parent_href),
    )

    # iframe HAS its own localStorage now — that's intentional. The whole
    # point of the cross-origin split is that the iframe gets `allow-same-
    # origin` (so nested YouTube etc. can use Cache Storage), without that
    # storage being the viewer's storage. Confirm it works AND is scoped
    # to the iframe's origin, not the parent's.
    own_storage = frame.evaluate(
        "() => { try { "
        "localStorage.setItem('am-isolation-probe','iframe-stored'); "
        "return { ok: true, val: localStorage.getItem('am-isolation-probe') }; "
        "} catch (e) { return { ok: false, err: e.name }; } }"
    )
    r.assert_(
        "iframe HAS its own localStorage (allow-same-origin in effect)",
        own_storage.get("ok") and own_storage.get("val") == "iframe-stored",
        detail=str(own_storage),
    )
    # And the parent does NOT see that key in its own localStorage.
    parent_probe = page.evaluate(
        "() => localStorage.getItem('am-isolation-probe')"
    )
    r.assert_(
        "iframe's localStorage is isolated from viewer's localStorage",
        parent_probe is None,
        detail=f"parent localStorage probe key = {parent_probe!r}",
    )

    # iframe-side fetch to the *viewer's* origin endpoints: cross-origin
    # without CORS headers, body must not be readable.
    fetch_attempt = frame.evaluate(
        "async () => {\n"
        "  try {\n"
        "    const r = await fetch('" + base + "/.env', { mode: 'no-cors' });\n"
        "    let body = '';\n"
        "    try { body = await r.text(); } catch (e) {}\n"
        "    return { ok: true, type: r.type, status: r.status, body_len: body.length };\n"
        "  } catch (e) { return { ok: false, err: e.name + ': ' + e.message }; }\n"
        "}"
    )
    fetch_safe = (
        not fetch_attempt.get("ok")
        or fetch_attempt.get("type") == "opaque"
        or fetch_attempt.get("body_len", 0) == 0
    )
    r.assert_(
        "iframe cross-origin fetch to viewer's /.env cannot read body",
        fetch_safe,
        detail=str(fetch_attempt),
    )


def scenario_history_undo_reset(page, base: str, r: Results) -> None:
    """Switch to the History tab — it populates from /history without a JSON
    parse error. Direct POSTs to /undo and /reset must respond cleanly."""
    print("\n[scenario] History tab / Undo / Reset")

    page.goto(base + "/")
    page.wait_for_function(
        "() => document.getElementById('doc-frame') && "
        "Array.from(document.querySelectorAll('.view-tab')).some(b => b.textContent.trim() === 'History') && "
        "document.getElementById('doc-select')?.value",
        timeout=10000,
    )

    # Switch to History tab and wait for the snap list to populate.
    page.evaluate(
        "() => Array.from(document.querySelectorAll('.view-tab'))"
        ".find(b => b.textContent.trim() === 'History').click()"
    )
    page.wait_for_selector(".view-history .history-list", timeout=5000)
    # Either rows appear OR an empty state — both are valid; what matters is
    # no JSON parse error.
    page.wait_for_function(
        "() => { const l = document.querySelector('.view-history .history-list');"
        "       return l && !l.textContent.includes('loading…'); }",
        timeout=5000,
    )
    panel_text = page.evaluate(
        "() => document.querySelector('.view-history').textContent"
    )
    no_json_error = (
        "Unexpected token" not in panel_text
        and "not valid JSON" not in panel_text
    )
    r.assert_(
        "History tab renders without JSON parse error",
        no_json_error,
        detail=panel_text[:200],
    )
    # Action buttons must exist on the History tab.
    actions = page.evaluate(
        "() => Array.from(document.querySelectorAll('.view-history .history-actions button'))"
        ".map(b => b.textContent.trim())"
    )
    r.assert_(
        "History tab exposes Undo / Reset / Save as baseline actions",
        any('Undo' in a for a in actions) and any('Reset' in a for a in actions)
        and any('baseline' in a.lower() for a in actions),
        detail=str(actions),
    )

    # Click Undo. Either it succeeds, or it returns "no snapshots yet" — both
    # are acceptable. What's NOT acceptable is "origin not allowed" / 403.
    undo_resp_status = page.evaluate(
        "async () => { const r = await fetch("
        "'/undo?doc=' + encodeURIComponent('intro'), "
        "{ method: 'POST' }); return r.status; }"
    )
    r.assert_(
        "POST /undo returns 200 or 404 (not 403 origin-rejected)",
        undo_resp_status in (200, 404),
        detail=f"status={undo_resp_status}",
    )

    # Click Reset — same logic. Reset has a baseline so should succeed.
    reset_resp_status = page.evaluate(
        "async () => { const r = await fetch("
        "'/reset?doc=' + encodeURIComponent('intro'), "
        "{ method: 'POST' }); return r.status; }"
    )
    r.assert_(
        "POST /reset returns 200 (baseline exists)",
        reset_resp_status == 200,
        detail=f"status={reset_resp_status}",
    )


def scenario_cross_origin_rejected(page, base: str, r: Results) -> None:
    """A page loaded from a foreign origin trying to drive our backend must
    get rejected by the Origin check. Use a data: URL — those run at a null
    origin and any fetch they make sends Origin: 'null'."""
    print("\n[scenario] cross-origin attacker page is rejected")

    # data:text/html loads at a null/opaque origin in Chromium. A fetch from
    # there to our backend should carry Origin: null (or 'null' string),
    # which our Origin check should reject.
    page.goto("data:text/html,<!doctype html><body>foreign-page-test</body>")
    cross_result = page.evaluate(
        "async (base) => {\n"
        "  try {\n"
        "    const r = await fetch(base + '/history?doc=intro');\n"
        "    return { ok: true, status: r.status };\n"
        "  } catch (e) { return { ok: false, err: e.name + ': ' + e.message }; }\n"
        "}",
        base,
    )
    # Either CORS blocks it (browser-side), or our 403 fires. Both are valid
    # defenses. What we don't want: status 200.
    rejected = (
        not cross_result.get("ok")
        or cross_result.get("status") == 403
        or cross_result.get("status") == 0
    )
    r.assert_(
        "cross-origin GET /history is blocked (CORS or 403)",
        rejected,
        detail=str(cross_result),
    )


def scenario_iframe_runtime(page, base: str, r: Results) -> None:
    """Verify the iframe runtime: morphdom is loaded, the cleanup registry
    is exposed, and cleanups fire ONLY when the registering script is
    removed/replaced — not when unrelated blocks change.

    The last assertion is the critical one: AMV3 drains all cleanups on
    every setContent, which silently kills animations whenever a sibling
    block edits. AM's per-script registry fixes that."""
    print("\n[scenario] iframe runtime: morphdom + cleanup registry")

    page.goto(base + "/")
    frame = _wait_for_doc_iframe(page)

    # Wait until morphdom has actually loaded into the iframe.
    try:
        frame.wait_for_function(
            "() => typeof window.morphdom === 'function' && "
            "typeof window.__doc === 'object' && "
            "typeof window.__doc.cleanup === 'function'",
            timeout=8000,
        )
        runtime_ready = True
    except Exception as e:
        runtime_ready = False
        r.assert_(
            "iframe runtime exposes morphdom + __doc.cleanup",
            False, detail=str(e),
        )
        return
    r.assert_("iframe runtime exposes morphdom + __doc.cleanup", runtime_ready)

    def push(html: str) -> None:
        """Send a synthetic setContent into the iframe (bypassing markdown)."""
        page.evaluate(
            "(html) => { document.getElementById('doc-frame').contentWindow"
            ".postMessage({ type: 'setContent', html, meta: '' }, '*'); }",
            html,
        )

    # ---- DOM stability: morphdom keeps unchanged nodes in place ----------
    push('<p id="keep">unchanged</p><p id="change">first</p>')
    frame.wait_for_function(
        "() => document.getElementById('keep') && "
        "document.getElementById('change')",
        timeout=2000,
    )
    # Tag the keep paragraph with a JS-only marker (not in the HTML source).
    # If morphdom preserves it across the next patch, the marker survives.
    frame.evaluate(
        "() => { document.getElementById('keep')._marker = 'sentinel'; }"
    )

    push('<p id="keep">unchanged</p><p id="change">second</p>')
    frame.wait_for_function(
        "() => document.getElementById('change') && "
        "document.getElementById('change').textContent === 'second'",
        timeout=2000,
    )
    marker = frame.evaluate(
        "() => document.getElementById('keep') && "
        "document.getElementById('keep')._marker || null"
    )
    r.assert_(
        "morphdom preserves unchanged DOM node identity across setContent",
        marker == "sentinel",
        detail=f"marker={marker!r}",
    )

    # ---- Cleanup fires when its script is removed ------------------------
    # Use a single counter on `window` so we can compare per-edit.
    push(
        '<p id="a">first</p>'
        '<p id="b">second</p>'
        '<script>'
        'window.__counter = (window.__counter || 0) + 1;'
        'window.__doc.cleanup(() => { '
        'window.__cleanupRan = (window.__cleanupRan || 0) + 1; '
        '});'
        '</script>'
    )
    frame.wait_for_function(
        "() => window.__counter === 1", timeout=2000,
    )
    state1 = frame.evaluate(
        "() => ({ counter: window.__counter, "
        "ran: window.__cleanupRan || 0 })"
    )
    r.assert_(
        "script body executes on first setContent",
        state1["counter"] == 1, detail=str(state1),
    )
    r.assert_(
        "cleanup does NOT run while its script is alive",
        state1["ran"] == 0, detail=str(state1),
    )

    # Now ship a setContent WITHOUT the script — cleanup must fire.
    push('<p id="a">first</p><p id="b">replaced</p>')
    try:
        frame.wait_for_function(
            "() => window.__cleanupRan === 1", timeout=2000,
        )
        ran_after_removal = True
    except Exception:
        ran_after_removal = False
    r.assert_(
        "cleanup runs when its script is removed",
        ran_after_removal,
        detail=str(frame.evaluate(
            "() => ({ counter: window.__counter, "
            "ran: window.__cleanupRan || 0 })"
        )),
    )

    # ---- Cleanup does NOT fire for an unchanged script -------------------
    # The AMV3-regression test: send a setContent with a fresh script (under
    # a new counter name to avoid leftover state from earlier asserts), then
    # send another setContent where ONLY a sibling block changes — the
    # script's textContent is identical, so its cleanup must stay registered
    # (counter unchanged, cleanup counter still 0).
    push(
        '<p id="a">alpha</p>'
        '<p id="b">beta</p>'
        '<script>'
        'window.__c2 = (window.__c2 || 0) + 1;'
        'window.__doc.cleanup(() => { '
        'window.__r2 = (window.__r2 || 0) + 1; '
        '});'
        '</script>'
    )
    frame.wait_for_function("() => window.__c2 === 1", timeout=2000)

    # Change ONLY the sibling — script body byte-identical.
    push(
        '<p id="a">alpha</p>'
        '<p id="b">BETA-EDITED</p>'
        '<script>'
        'window.__c2 = (window.__c2 || 0) + 1;'
        'window.__doc.cleanup(() => { '
        'window.__r2 = (window.__r2 || 0) + 1; '
        '});'
        '</script>'
    )
    frame.wait_for_function(
        "() => document.getElementById('b').textContent === 'BETA-EDITED'",
        timeout=2000,
    )
    # Give the message loop a tick to drain any pending cleanup before reading.
    page.wait_for_timeout(150)
    state3 = frame.evaluate(
        "() => ({ c2: window.__c2 || 0, r2: window.__r2 || 0 })"
    )
    r.assert_(
        "unchanged script is not re-executed (counter stays at 1)",
        state3["c2"] == 1, detail=str(state3),
    )
    r.assert_(
        "unchanged script's cleanup is NOT drained when sibling edits "
        "(AMV3-regression fix)",
        state3["r2"] == 0, detail=str(state3),
    )


def scenario_data_figure_renders(page, base: str, r: Results) -> None:
    """Push a synthetic <figure class="data"><script type="text/csv">…
    block into the iframe and verify the runtime parses + Tabulator renders.

    Critical regression-guard: if anything truncates the iframe script
    body (e.g., a stray `</script>` token in a renderer comment — has
    bitten us twice now in renderMusicBlocks and renderDataBlocks), the
    runtime won't load Tabulator and the grid won't render. This catches
    that class of bug in 1 second, no upload race, no CDN broadcast
    side effects."""
    print("\n[scenario] data figure: CSV → Tabulator grid renders")

    page.goto(base + "/")
    frame = _wait_for_doc_iframe(page)

    # The runtime must have loaded enough to expose renderDataBlocks
    # (indirectly, via __doc.rerender — that function clears data-source
    # markers and re-runs renderDataBlocks). If the script body was
    # truncated, __doc would either be undefined or missing rerender.
    try:
        frame.wait_for_function(
            "() => window.__doc && typeof window.__doc.rerender === 'function'",
            timeout=8000,
        )
    except Exception as e:
        r.assert_(
            "iframe runtime exposes __doc.rerender (script body intact)",
            False, detail=str(e),
        )
        return
    r.assert_("iframe runtime exposes __doc.rerender (script body intact)", True)

    def push(html: str) -> None:
        page.evaluate(
            "(html) => { document.getElementById('doc-frame').contentWindow"
            ".postMessage({ type: 'setContent', html, meta: '' }, '*'); }",
            html,
        )

    csv = "name,age,city\nAlice,30,NYC\nBob,25,Berlin\nCarol,40,Buenos Aires"
    push(
        '<figure class="data">'
        f'<script type="text/csv">\n{csv}\n</script>'
        '</figure>'
    )

    # Tabulator lazy-loads from CDN — needs a moment. Wait up to 10s.
    try:
        frame.wait_for_function(
            "() => !!document.querySelector('figure.data .tabulator')",
            timeout=10000,
        )
        rendered = True
    except Exception:
        rendered = False
    r.assert_(
        "Tabulator grid renders inside figure.data",
        rendered,
        detail="no .tabulator element found inside figure.data after 10s",
    )

    # Specifically NOT showing the "CSV is empty" error banner — that
    # would mean script.textContent was empty when renderDataBlocks ran
    # (the exact symptom of a stale/truncated runtime).
    error_visible = frame.evaluate(
        "() => !!document.querySelector('figure.data .error-banner')"
    )
    r.assert_(
        "no .error-banner inside figure.data (CSV parsed cleanly)",
        not error_visible,
        detail="error banner is present — likely 'CSV is empty' or render fail",
    )

    # Row count: 3 data rows (excluding header). Tabulator renders rows
    # as .tabulator-row elements in the .tabulator-tableholder.
    row_count = frame.evaluate(
        "() => document.querySelectorAll('figure.data "
        ".tabulator-tableholder .tabulator-row').length"
    )
    r.assert_(
        "Tabulator renders 3 data rows (Alice / Bob / Carol)",
        row_count == 3,
        detail=f"row count = {row_count}",
    )

    # Renderer registration: __doc.getRenderer(fig) should return
    # {kind: 'csv', instance: <tabulator>}. Widgets rely on this.
    kind = frame.evaluate(
        "() => { const fig = document.querySelector('figure.data'); "
        "const r = window.__doc.getRenderer(fig); "
        "return r ? r.kind : null; }"
    )
    r.assert_(
        "FIGURE_RENDERERS registers the data figure with kind='csv'",
        kind == "csv",
        detail=f"kind={kind!r}",
    )


def scenario_diagram_renders(page, base: str, r: Results) -> None:
    """Push a synthetic <figure class="diagram"><script type="text/x-mermaid">
    block into the iframe and verify the runtime parses + Mermaid renders an
    SVG. Same regression-guard shape as scenario_data_figure_renders — if
    a </script>-in-comment ever truncates the runtime, this catches it."""
    print("\n[scenario] diagram figure: Mermaid → SVG renders")

    page.goto(base + "/")
    frame = _wait_for_doc_iframe(page)

    try:
        frame.wait_for_function(
            "() => window.__doc && typeof window.__doc.rerender === 'function'",
            timeout=8000,
        )
    except Exception as e:
        r.assert_(
            "iframe runtime exposes __doc.rerender (script body intact)",
            False, detail=str(e),
        )
        return
    r.assert_("iframe runtime exposes __doc.rerender (script body intact)", True)

    def push(html: str) -> None:
        page.evaluate(
            "(html) => { document.getElementById('doc-frame').contentWindow"
            ".postMessage({ type: 'setContent', html, meta: '' }, '*'); }",
            html,
        )

    mermaid = (
        "flowchart TD\n"
        "  A[Start] --> B{Choice?}\n"
        "  B -->|yes| C[End]\n"
        "  B -->|no| A\n"
    )
    push(
        '<figure class="diagram">'
        f'<script type="text/x-mermaid">\n{mermaid}\n</script>'
        '</figure>'
    )

    # Mermaid lazy-loads from CDN (~1.5MB) — give it up to 15s.
    try:
        frame.wait_for_function(
            "() => !!document.querySelector('figure.diagram .mermaid-render svg')",
            timeout=15000,
        )
        rendered = True
    except Exception:
        rendered = False
    r.assert_(
        "Mermaid SVG renders inside figure.diagram .mermaid-render",
        rendered,
        detail="no SVG found inside .mermaid-render after 15s",
    )

    error_visible = frame.evaluate(
        "() => !!document.querySelector('figure.diagram .error-banner')"
    )
    r.assert_(
        "no .error-banner inside figure.diagram (Mermaid parsed cleanly)",
        not error_visible,
        detail="error banner is present — likely a Mermaid parse error",
    )

    # The SVG should contain at least one <g> element (Mermaid wraps
    # every diagram element in a group). A near-empty SVG would mean
    # parsing succeeded but rendering produced nothing.
    g_count = frame.evaluate(
        "() => document.querySelectorAll('figure.diagram .mermaid-render svg g').length"
    )
    r.assert_(
        "Mermaid SVG has at least one <g> element (real diagram content)",
        g_count >= 1,
        detail=f"g element count = {g_count}",
    )

    kind = frame.evaluate(
        "() => { const fig = document.querySelector('figure.diagram'); "
        "const r = window.__doc.getRenderer(fig); "
        "return r ? r.kind : null; }"
    )
    r.assert_(
        "FIGURE_RENDERERS registers the diagram figure with kind='mermaid'",
        kind == "mermaid",
        detail=f"kind={kind!r}",
    )


def scenario_structural_block_robustness(page, base: str, r: Results) -> None:
    """Test the structural-block path (now the default):
    blank lines inside <figure> / <section> / etc. don't terminate the
    block, AND inner markdown renders.

    Drives the renderer directly with synthetic input via __amRenderDoc."""
    print("\n[scenario] structural-block robustness (blank lines + inner markdown)")

    page.goto(base + "/")
    page.wait_for_function("() => !!window.__amMd && !!window.__amRenderDoc",
                            timeout=8000)

    def render_with_flag(text: str) -> str:
        """Render via __amRenderDoc on the default (structural-blocks-on) path."""
        return page.evaluate("(t) => window.__amRenderDoc(t)", text)

    # ---- Test 1: blank lines inside <figure> ---------------------
    src1 = (
        '<figure>\n'
        '\n'
        '## A figure with markdown inside\n'
        '\n'
        'A paragraph with **bold** and *italic*.\n'
        '\n'
        '- list item 1\n'
        '- list item 2\n'
        '\n'
        '<figcaption>caption text</figcaption>\n'
        '\n'
        '</figure>\n'
    )
    html1 = render_with_flag(src1)
    r.assert_(
        "figure with blank lines: <figure> wraps the content",
        html1.lstrip().startswith("<figure>") and "</figure>" in html1,
        detail=f"got: {html1[:300]!r}",
    )
    r.assert_(
        "figure body: <h2> renders for the heading",
        "<h2" in html1 and "A figure with markdown inside" in html1,
        detail=f"got: {html1[:500]!r}",
    )
    r.assert_(
        "figure body: <strong>bold</strong> renders",
        "<strong>bold</strong>" in html1,
        detail=f"got: {html1[:500]!r}",
    )
    r.assert_(
        "figure body: <ul> renders for the list",
        "<ul>" in html1 and html1.count("<li>") == 2,
        detail=f"got: {html1[:500]!r}",
    )
    r.assert_(
        "figure body: <figcaption> preserved",
        "<figcaption>caption text</figcaption>" in html1,
        detail=f"got: {html1[:500]!r}",
    )
    r.assert_(
        "no leftover sentinel in output",
        "@@AM_STRUCT_BLOCK_" not in html1,
        detail=f"sentinel leaked: {html1[:500]!r}",
    )

    # ---- Test 2: nested structural blocks ------------------------
    src2 = (
        '<section class="theorem">\n'
        '\n'
        '## Theorem statement\n'
        '\n'
        '<figure>\n'
        '\n'
        '### Figure heading\n'
        '\n'
        'Inner content.\n'
        '\n'
        '</figure>\n'
        '\n'
        'Closing paragraph.\n'
        '\n'
        '</section>\n'
    )
    html2 = render_with_flag(src2)
    r.assert_(
        "nested: outer <section> wraps",
        '<section class="theorem">' in html2 and "</section>" in html2,
        detail=f"got: {html2[:600]!r}",
    )
    r.assert_(
        "nested: <h2> for outer heading",
        "<h2" in html2 and "Theorem statement" in html2,
        detail=f"got: {html2[:600]!r}",
    )
    r.assert_(
        "nested: <figure> inside <section> in DOM order",
        html2.index("<figure>") < html2.index("</section>"),
        detail=f"figure not nested inside section",
    )
    r.assert_(
        "nested: <h3> for inner heading",
        "<h3" in html2 and "Figure heading" in html2,
        detail=f"got: {html2[:600]!r}",
    )
    r.assert_(
        "nested: closing paragraph rendered inside section",
        "Closing paragraph" in html2
        and html2.index("Closing paragraph") < html2.index("</section>"),
        detail="closing paragraph not inside section",
    )
    r.assert_(
        "nested: no sentinel leak",
        "@@AM_STRUCT_BLOCK_" not in html2,
        detail=f"sentinel leaked: {html2[:600]!r}",
    )

    # ---- Test 3: structural block coexists with <script> ----------
    src3 = (
        '<figure class="data">\n'
        '\n'
        'A description with **markdown**.\n'
        '\n'
        '<script type="text/csv">\n'
        'name,age\n'
        'A,1\n'
        '</script>\n'
        '\n'
        '</figure>\n'
    )
    html3 = render_with_flag(src3)
    r.assert_(
        "figure+script: outer <figure class=\"data\"> wraps",
        '<figure class="data">' in html3,
        detail=f"got: {html3[:500]!r}",
    )
    r.assert_(
        "figure+script: description renders with <strong>",
        "<strong>markdown</strong>" in html3,
        detail=f"got: {html3[:500]!r}",
    )
    r.assert_(
        "figure+script: script body preserved (CSV content present)",
        "name,age" in html3 and "A,1" in html3
        and 'type="text/csv"' in html3,
        detail=f"script lost: {html3[:600]!r}",
    )
    r.assert_(
        "figure+script: no sentinel leak",
        "@@AM_STRUCT_BLOCK_" not in html3,
        detail=f"sentinel leaked: {html3[:500]!r}",
    )


def scenario_html_block_structured_content(page, base: str, r: Results) -> None:
    """Structured content (callouts, figures, locked blocks, theorems) is
    written as raw HTML blocks in the source. CommonMark passes those
    blocks through verbatim. CSS targets the class names. Verify the
    representative patterns from the skill all render as expected."""
    print("\n[scenario] HTML-block structured content")

    page.goto(base + "/")
    page.wait_for_function("() => !!window.__amMd", timeout=8000)

    def render(src: str) -> str:
        return page.evaluate("(s) => window.__amMd.render(s)", src)

    # 1. <aside class="note"> — the callout pattern
    callout = render(
        '<aside class="note">\n\nThis is a **note**.\n\n</aside>\n'
    )
    r.assert_(
        "aside.note passes through; inner markdown is rendered",
        '<aside class="note">' in callout
        and '<strong>note</strong>' in callout
        and ":::" not in callout,
        detail=callout[:200],
    )

    # 2. <div class="pinned"> — author-locked
    pinned = render(
        '<div class="pinned">\n\nLocked content.\n\n</div>\n'
    )
    r.assert_(
        "div.pinned passes through with markdown inside",
        '<div class="pinned">' in pinned
        and "Locked content." in pinned,
        detail=pinned[:200],
    )

    # 3. <figure> with figcaption
    figure = render(
        '<figure>\n<canvas id="x"></canvas>\n<figcaption>A circle.</figcaption>\n</figure>\n'
    )
    r.assert_(
        "<figure> + <canvas> + <figcaption> all pass through",
        "<figure>" in figure
        and "<canvas" in figure
        and "<figcaption>A circle." in figure,
        detail=figure[:200],
    )

    # 4. <section class="theorem" id="..."> — explicit-boundary kind-block
    theorem = render(
        '<section class="theorem" id="rolle">\n\n## Theorem (Rolle\'s)\n\nStatement.\n\n</section>\n'
    )
    r.assert_(
        "section.theorem with id renders, heading inside processes as markdown",
        '<section class="theorem" id="rolle">' in theorem
        and "Rolle" in theorem
        and "<h2" in theorem,
        detail=theorem[:300],
    )

    # 5. Inline style on a raw HTML block — passes through unchanged
    styled = render(
        '<aside class="note" style="background:#fffacd">\n\nYellow note.\n\n</aside>\n'
    )
    r.assert_(
        "inline style attribute survives on HTML blocks",
        'style="background:#fffacd"' in styled,
        detail=styled[:200],
    )

    # 6. Heading-form kind-block (implicit boundary)
    heading_form = render('## Theorem (Pythagoras) {#pyth}\n\nBody.\n')
    r.assert_(
        "heading-form kind-block: anchor + content render normally",
        'id="pyth"' in heading_form and "Theorem (Pythagoras)" in heading_form,
        detail=heading_form[:200],
    )

    # 7. ::: directive syntax is no longer recognized — falls through as
    # paragraph text (sanity check that the deprecation took effect).
    deprecated = render('::: note\nbody\n:::\n')
    r.assert_(
        "::: directive syntax is no longer a special block (deprecated)",
        '<div class="directive' not in deprecated,
        detail=deprecated[:200],
    )

    # 8. Blank lines inside <figure>/<aside>/<section>/<details> must not
    # terminate the type-6 HTML block. Without the preserveStructuralBlocks
    # preprocessor, the CommonMark spec ends the block at the first blank
    # line — agents who write blank lines inside a figure for readability
    # then end up with their <canvas>/<script>/<figcaption> rendered as
    # separate top-level chunks, and any text after the script's blank
    # line gets parsed as a paragraph and ends up *inside the unclosed
    # script tag* in the assembled stream. The script then has invalid
    # JS (literal <p> tags interleaved) and the animation never runs.
    figure_with_blanks = page.evaluate(
        "() => window.__amPreserve("
        " '<figure>\\n<canvas></canvas>\\n<script>\\nlet x = 1;\\n\\nlet y = 2;\\n</script>\\n<figcaption>cap</figcaption>\\n</figure>\\n'"
        ")"
    )
    r.assert_(
        "preserveStructuralBlocks: blank line inside <figure> replaced",
        "<!--am:keep-->" in figure_with_blanks,
        detail=figure_with_blanks[:300],
    )

    # Full pipeline: preserve then render (mirrors what renderCurrentView does).
    rendered_figure = page.evaluate(
        "(s) => window.__amMd.render(window.__amPreserve(s))",
        "<figure>\n<canvas id=\"x\"></canvas>\n<script>\nlet a = 1;\n\nlet b = 2;\n</script>\n<figcaption>cap</figcaption>\n</figure>\n",
    )
    r.assert_(
        "figure renders as one block when its script body has a blank line "
        "(was: broke into separate chunks)",
        "<figure>" in rendered_figure
        and "<canvas" in rendered_figure
        and "<figcaption>cap</figcaption>" in rendered_figure
        and "</figure>" in rendered_figure
        and "<p>let " not in rendered_figure,  # the regression: blank-line-split would emit <p>let b...
        detail=rendered_figure[:400],
    )

    # 9. Standalone <script> blank lines get the am:keep treatment too —
    # harmless (am:keep is a legacy JS line comment) and removes a "what
    # parents counts as structural?" edge case from the preprocessor.
    standalone_script = page.evaluate(
        "(s) => window.__amPreserve(s)",
        "<script>\nlet a = 1;\n\nlet b = 2;\n</script>\n",
    )
    r.assert_(
        "standalone <script> blank lines also normalized (harmless JS-comment)",
        "<!--am:keep-->" in standalone_script,
        detail=standalone_script[:300],
    )

    # 9b. Regression: a script body with NO interior blank lines must
    # come out unchanged. The old regex matched the empty "line" before
    # the first \n (and the trailing one), prepending a sentinel to the
    # script body. That broke data scripts like MusicXML where OSMD
    # requires `<?xml` at position 0 of textContent.
    no_blank_script = page.evaluate(
        "(s) => window.__amPreserve(s)",
        '<script type="application/vnd.recordare.musicxml+xml">\n'
        '<?xml version="1.0"?>\n'
        '<score-partwise>\n'
        '  <part/>\n'
        '</score-partwise>\n'
        '</script>\n',
    )
    r.assert_(
        "script with no interior blank lines is left untouched",
        "<!--am:keep-->" not in no_blank_script
        and '<?xml version="1.0"?>' in no_blank_script,
        detail=no_blank_script[:300],
    )

    # 10. Section with markdown inside: full pipeline must produce a
    # rendered <h3>, not a literal "###". Regression guard against the
    # over-aggressive preserveStructuralBlocks that was eating section
    # blank lines and breaking inner markdown.
    section_full = page.evaluate(
        "(s) => window.__amMd.render(window.__amPreserve(s))",
        '<section class="theorem">\n\n### Fundamental Theorem\n\nLet $K/F$ be a Galois extension.\n\n</section>\n',
    )
    r.assert_(
        "section with inner markdown: heading renders as <h3>, not literal '###'",
        '<section class="theorem">' in section_full
        and "<h3" in section_full
        and "Fundamental Theorem" in section_full
        and "###" not in section_full,
        detail=section_full[:400],
    )


def scenario_pending_review(page, base: str, r: Results) -> None:
    """Phase 4: Review tab surfaces pending entries; Accept removes the
    entry leaving current.md unchanged; Reject restores old_text and
    removes the entry."""
    print("\n[scenario] pending Review tab (accept + reject)")

    import json
    import shutil
    slug = "smoke-pending-review"
    doc_dir = ROOT / "docs" / slug
    if doc_dir.exists():
        shutil.rmtree(doc_dir)
    doc_dir.mkdir(parents=True)
    current_body = (
        "---\ndoc_id: d-pending\ntitle: Pending review\n"
        "review_mode: pending\n---\n\n"
        "# Title after proposal\n\nProposed paragraph body.\n"
    )
    old_body = (
        "---\ndoc_id: d-pending\ntitle: Pending review\n"
        "review_mode: pending\n---\n\n"
        "# Title before\n\nOriginal paragraph body.\n"
    )
    (doc_dir / "current.md").write_text(current_body, encoding="utf-8", newline="")
    (doc_dir / "baseline.md").write_text(old_body, encoding="utf-8", newline="")

    # Pre-populate pending.json with one whole-doc entry as if the agent
    # had just landed an edit under review_mode.
    pending_data = {
        "version": 1,
        "doc": slug,
        "edits": [{
            "id": "pe-TEST-ACCEPT",
            "tool_use_id": "tu-test",
            "block": {"kind": "doc"},
            "old_text": old_body,
            "new_text": current_body,
            "agent_label": "claude:sonnet-4-6",
            "created_at": "2026-05-18T12:00:00+00:00",
        }],
    }
    (doc_dir / "pending.json").write_text(
        json.dumps(pending_data, indent=2), encoding="utf-8", newline="",
    )

    # GET /pending returns the entry.
    pend = page.evaluate(
        """async (slug) => {
            const r = await fetch('/pending?doc=' + encodeURIComponent(slug));
            return { status: r.status, body: await r.json() };
        }""",
        slug,
    )
    r.assert_(
        "GET /pending returns the pre-populated entry",
        pend.get("status") == 200
        and len((pend.get("body") or {}).get("edits") or []) == 1,
        detail=str(pend)[:200],
    )

    # Load the doc; Review tab should appear.
    page.goto(base + f"/?doc={slug}")
    page.wait_for_function(
        f"() => document.getElementById('doc-select')?.value === '{slug}'",
        timeout=10000,
    )
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.view-tab'))"
        ".some(b => /Review \\(1\\)/.test(b.textContent || ''))",
        timeout=5000,
    )
    r.assert_("Review tab appears with count (1) when pending exists", True)

    # Click the Review tab, verify the before/after panes are present.
    page.evaluate(
        "() => Array.from(document.querySelectorAll('.view-tab'))"
        ".find(b => /Review \\(/.test(b.textContent)).click()"
    )
    page.wait_for_function(
        "() => document.querySelector('.view-review .review-card') !== null",
        timeout=3000,
    )
    panes = page.evaluate(
        "() => ({"
        "  before: document.querySelector('.review-before pre')?.textContent || '',"
        "  after: document.querySelector('.review-after pre')?.textContent || '',"
        "})"
    )
    r.assert_(
        "Before pane shows old_text",
        "Original paragraph body." in (panes.get("before") or ""),
        detail=str(panes)[:200],
    )
    r.assert_(
        "Proposed pane shows new_text",
        "Proposed paragraph body." in (panes.get("after") or ""),
        detail=str(panes)[:200],
    )

    # Accept the entry — current.md should NOT change; pending.json drops.
    page.evaluate(
        "() => document.querySelector('.review-card .btn-accept').click()"
    )
    page.wait_for_function(
        "() => !Array.from(document.querySelectorAll('.view-tab'))"
        ".some(b => /Review/.test(b.textContent || ''))",
        timeout=3000,
    )
    # current.md carries serve-stamped block ids; compare content modulo them.
    import re as _re
    def _no_ids(t):
        return "\n".join(ln for ln in t.split("\n")
                         if not _re.match(r"^<!-- id:b-[A-Z0-9-]+ -->$", ln))
    after_accept_current = (doc_dir / "current.md").read_text(encoding="utf-8")
    r.assert_(
        "current.md content unchanged after Accept (modulo block ids)",
        _no_ids(after_accept_current) == _no_ids(current_body),
        detail=after_accept_current[:200],
    )
    r.assert_(
        "pending.json is gone after the last accept",
        not (doc_dir / "pending.json").exists(),
    )
    r.assert_(
        "Review tab disappears when no pending entries remain",
        not page.evaluate(
            "() => Array.from(document.querySelectorAll('.view-tab'))"
            ".some(b => /Review/.test(b.textContent || ''))"
        ),
    )

    # Re-create a pending entry and test the Reject path.
    pending_data["edits"][0]["id"] = "pe-TEST-REJECT"
    (doc_dir / "pending.json").write_text(
        json.dumps(pending_data, indent=2), encoding="utf-8", newline="",
    )
    # Manually trigger a pending_changed broadcast equivalent by reloading
    # the page (the test framework doesn't easily fire WS broadcasts).
    page.goto(base + f"/?doc={slug}")
    page.wait_for_function(
        f"() => document.getElementById('doc-select')?.value === '{slug}'"
        " && Array.from(document.querySelectorAll('.view-tab'))"
        ".some(b => /Review/.test(b.textContent || ''))",
        timeout=5000,
    )
    page.evaluate(
        "() => Array.from(document.querySelectorAll('.view-tab'))"
        ".find(b => /Review/.test(b.textContent)).click()"
    )
    page.wait_for_function(
        "() => document.querySelector('.view-review .review-card') !== null",
        timeout=3000,
    )
    page.evaluate(
        "() => document.querySelector('.review-card .btn-reject').click()"
    )
    page.wait_for_function(
        "() => !Array.from(document.querySelectorAll('.view-tab'))"
        ".some(b => /Review/.test(b.textContent || ''))",
        timeout=3000,
    )
    after_reject_current = (doc_dir / "current.md").read_text(encoding="utf-8")
    r.assert_(
        "current.md restored to old_text after Reject (modulo block ids)",
        _no_ids(after_reject_current) == _no_ids(old_body),
        detail=after_reject_current[:200],
    )
    r.assert_(
        "pending.json is gone after reject",
        not (doc_dir / "pending.json").exists(),
    )

    shutil.rmtree(doc_dir, ignore_errors=True)


def scenario_music_upload(page, base: str, r: Results) -> None:
    """Drop a .abc / .musicxml / .mid file onto +Doc → the backend wraps
    it in `<figure class="music">` and saves as a new doc. The iframe
    runtime renders via abcjs / OSMD / html-midi-player when viewed.

    All uploads go through urllib (not page.evaluate) so we don't
    accidentally drive the viewer's WS into a music-doc state with
    iframe lazy-loading abcjs in the background — that interacts
    poorly with the test's tight per-scenario timing and starves the
    next scenario's WS handshake on its first wait."""
    print("\n[scenario] music file imports (.abc / .musicxml / .mid)")

    import base64
    import json as _json
    import shutil
    import urllib.request

    def post_upload(filename: str, body: bytes, content_type: str):
        boundary = "----smoketest-music-boundary"
        wire = (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\n'
            f'Content-Type: {content_type}\r\n\r\n'
        ).encode("utf-8") + body + f"\r\n--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(
            base + "/upload",
            data=wire,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Origin": base,
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, _json.loads(resp.read().decode("utf-8"))

    # ---- ABC ----
    abc_source = (
        "X:1\nT:Smoke Test\nM:4/4\nL:1/4\nK:C\n"
        "C D E F | G A B c |\n"
    )
    status, body = post_upload(
        "smoke-abc.abc", abc_source.encode("utf-8"), "text/vnd.abc",
    )
    r.assert_(
        "POST /upload accepts an .abc file and returns 200",
        status == 200,
        detail=f"status={status} body={body}",
    )
    abc_slug = (body or {}).get("slug", "")
    r.assert_(
        "ABC upload response carries converter='music' and a slug",
        body.get("converter") == "music" and bool(abc_slug),
        detail=str(body)[:300],
    )
    abc_dir = ROOT / "docs" / abc_slug
    abc_md = (abc_dir / "current.md").read_text(encoding="utf-8")
    r.assert_(
        "ABC doc wraps the source in <figure class=\"music\"><div class=\"abc\">",
        '<figure class="music">' in abc_md
        and '<div class="abc">' in abc_md
        and "T:Smoke Test" in abc_md,
        detail=abc_md[:400],
    )
    shutil.rmtree(abc_dir, ignore_errors=True)

    # ---- MusicXML ----
    musicxml_source = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<score-partwise version="3.1">\n'
        '  <part-list><score-part id="P1"><part-name>Music</part-name></score-part></part-list>\n'
        '  <part id="P1"><measure number="1">\n'
        '    <attributes><divisions>1</divisions><key><fifths>0</fifths></key>'
        '<time><beats>4</beats><beat-type>4</beat-type></time>'
        '<clef><sign>G</sign><line>2</line></clef></attributes>\n'
        '    <note><pitch><step>C</step><octave>4</octave></pitch>'
        '<duration>4</duration><type>whole</type></note>\n'
        '  </measure></part>\n'
        '</score-partwise>\n'
    )
    status, body = post_upload(
        "smoke-xml.musicxml",
        musicxml_source.encode("utf-8"),
        "application/vnd.recordare.musicxml+xml",
    )
    r.assert_(
        "POST /upload accepts a .musicxml file and returns 200",
        status == 200,
        detail=f"status={status} body={body}",
    )
    xml_slug = (body or {}).get("slug", "")
    xml_dir = ROOT / "docs" / xml_slug
    xml_md = (xml_dir / "current.md").read_text(encoding="utf-8")
    r.assert_(
        "MusicXML doc wraps the XML in <figure class=\"music\"><div class=\"musicxml\">",
        '<div class="musicxml">' in xml_md and "score-partwise" in xml_md,
        detail=xml_md[:400],
    )
    shutil.rmtree(xml_dir, ignore_errors=True)

    # ---- MIDI ----
    midi_bytes = (
        b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60"
        b"MTrk\x00\x00\x00\x0b"
        b"\x00\xc0\x00"
        b"\x00\x90\x3c\x40"
        b"\x60\x80\x3c\x40"
        b"\x00\xff\x2f\x00"
    )
    status, body = post_upload(
        "smoke-mid.mid", midi_bytes, "audio/midi",
    )
    r.assert_(
        "POST /upload accepts a .mid file and returns 200",
        status == 200,
        detail=f"status={status} body={body}",
    )
    mid_slug = (body or {}).get("slug", "")
    mid_dir = ROOT / "docs" / mid_slug
    mid_md = (mid_dir / "current.md").read_text(encoding="utf-8")
    r.assert_(
        "MIDI doc references a <midi-player src='assets/...'>",
        "<midi-player" in mid_md and "assets/" in mid_md,
        detail=mid_md[:400],
    )
    r.assert_(
        "MIDI bytes landed in docs/<slug>/assets/",
        any((mid_dir / "assets").iterdir())
        if (mid_dir / "assets").exists() else False,
    )
    shutil.rmtree(mid_dir, ignore_errors=True)


def scenario_doc_skill_crud(page, base: str, r: Results) -> None:
    """Skills manager backend: GET /doc-skills lists; POST /update-doc-skill
    rewrites the Nth section's inner content; POST /delete-doc-skill
    removes it. Together with /add-doc-skill these power the Skills
    manager modal — author can view, edit, delete, and add skills."""
    print("\n[scenario] doc-skill CRUD (list / update / delete)")

    import shutil
    slug = "smoke-skill-crud"
    doc_dir = ROOT / "docs" / slug
    if doc_dir.exists():
        shutil.rmtree(doc_dir)
    doc_dir.mkdir(parents=True)
    initial = (
        "---\ndoc_id: d-sc\ntitle: Skill CRUD\n---\n\n"
        '<section class="agent-skill">\n\n'
        "## SKILL: original voice\n\nKeep it terse.\n\n"
        "</section>\n\n"
        "# Body\n\nContent here.\n"
    )
    (doc_dir / "current.md").write_text(initial, encoding="utf-8", newline="")
    (doc_dir / "baseline.md").write_text(initial, encoding="utf-8", newline="")

    page.goto(base + "/")
    page.wait_for_function(
        "() => document.getElementById('doc-select')",
        timeout=10000,
    )

    # GET /doc-skills lists the pre-existing entry.
    listed = page.evaluate(
        """async (slug) => {
            const r = await fetch('/doc-skills?doc=' + encodeURIComponent(slug));
            return { status: r.status, body: await r.json() };
        }""",
        slug,
    )
    skills = (listed.get("body") or {}).get("skills", [])
    r.assert_(
        "GET /doc-skills returns the existing skill",
        listed.get("status") == 200 and len(skills) == 1
        and skills[0].get("name") == "original voice",
        detail=str(listed)[:300],
    )
    r.assert_(
        "skill content includes the inner body",
        "Keep it terse." in (skills[0].get("content") or ""),
    )

    # POST /update-doc-skill rewrites the section's inner content.
    new_content = "## SKILL: refined voice\n\nKeep it terse AND specific."
    updated = page.evaluate(
        """async ({ slug, content }) => {
            const r = await fetch('/update-doc-skill?doc=' + encodeURIComponent(slug) + '&index=0', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content }),
            });
            return r.status;
        }""",
        {"slug": slug, "content": new_content},
    )
    r.assert_(
        "POST /update-doc-skill returns 200",
        updated == 200,
        detail=f"status={updated}",
    )
    disk = (doc_dir / "current.md").read_text(encoding="utf-8")
    r.assert_(
        "update wrote the new content into the source",
        "Keep it terse AND specific." in disk
        and "refined voice" in disk,
        detail=disk[:400],
    )
    r.assert_(
        "update preserved the wrapping <section class='agent-skill'>",
        '<section class="agent-skill">' in disk
        and "</section>" in disk,
    )
    r.assert_(
        "update preserved the body content outside the section",
        "Content here." in disk and "# Body" in disk,
    )

    # POST /add-doc-skill with custom content lands a second skill.
    added = page.evaluate(
        """async (slug) => {
            const r = await fetch('/add-doc-skill?doc=' + encodeURIComponent(slug), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: 'math conventions',
                    content: '## SKILL: math conventions\\n\\nUse $...$ for inline math.',
                }),
            });
            return r.status;
        }""",
        slug,
    )
    r.assert_(
        "POST /add-doc-skill with content adds a section",
        added == 200,
    )
    disk2 = (doc_dir / "current.md").read_text(encoding="utf-8")
    r.assert_(
        "second skill carries the custom content",
        "Use $...$ for inline math." in disk2,
        detail=disk2[:600],
    )

    # GET /doc-skills now returns two entries.
    listed2 = page.evaluate(
        """async (slug) => {
            const r = await fetch('/doc-skills?doc=' + encodeURIComponent(slug));
            return await r.json();
        }""",
        slug,
    )
    r.assert_(
        "GET /doc-skills returns both entries after add",
        len((listed2 or {}).get("skills") or []) == 2,
        detail=str(listed2)[:300],
    )

    # POST /delete-doc-skill removes the newly-added skill (which landed
    # at index=0 because /add-doc-skill inserts after the frontmatter,
    # pushing the older skill down to index=1).
    deleted = page.evaluate(
        """async (slug) => {
            const r = await fetch('/delete-doc-skill?doc=' + encodeURIComponent(slug) + '&index=0', {
                method: 'POST',
            });
            return r.status;
        }""",
        slug,
    )
    r.assert_(
        "POST /delete-doc-skill returns 200",
        deleted == 200,
    )
    disk3 = (doc_dir / "current.md").read_text(encoding="utf-8")
    r.assert_(
        "deleted skill no longer in source",
        "Use $...$ for inline math." not in disk3,
        detail=disk3[:400],
    )
    r.assert_(
        "remaining skill still in source",
        "Keep it terse AND specific." in disk3,
    )

    shutil.rmtree(doc_dir, ignore_errors=True)


def scenario_pending_cleared_on_restore(page, base: str, r: Results) -> None:
    """Phase 3: destructive UI actions (/reset, /undo, /restore_snapshot)
    overwrite current.md to a previous state — any pending entries still
    in the sidecar reference the just-discarded text and would corrupt
    source on Reject. Each restore handler must clear pending.json."""
    print("\n[scenario] pending cleared on /reset, /undo, /restore_snapshot")

    import json
    import shutil
    slug = "smoke-pending-cleared"
    doc_dir = ROOT / "docs" / slug
    if doc_dir.exists():
        shutil.rmtree(doc_dir)
    doc_dir.mkdir(parents=True)
    baseline = "---\ndoc_id: d-pc\ntitle: T\n---\n\n# Base\n\nBaseline body.\n"
    current = "---\ndoc_id: d-pc\ntitle: T\n---\n\n# Current\n\nAgent edit.\n"
    (doc_dir / "baseline.md").write_text(baseline, encoding="utf-8", newline="")
    (doc_dir / "current.md").write_text(current, encoding="utf-8", newline="")

    def write_pending():
        (doc_dir / "pending.json").write_text(
            json.dumps({
                "version": 1, "doc": slug,
                "edits": [{
                    "id": "pe-orphan",
                    "tool_use_id": "tu-x",
                    "block": {"kind": "doc"},
                    "old_text": baseline,
                    "new_text": current,
                    "agent_label": "claude:sonnet",
                    "created_at": "2026-05-18T12:00:00+00:00",
                }],
            }, indent=2),
            encoding="utf-8", newline="",
        )

    page.goto(base + "/")
    page.wait_for_function(
        "() => document.getElementById('doc-select')",
        timeout=10000,
    )

    # /reset clears pending
    write_pending()
    assert (doc_dir / "pending.json").exists()
    result = page.evaluate(
        """async (slug) => {
            const r = await fetch('/reset?doc=' + encodeURIComponent(slug), {
                method: 'POST',
            });
            return r.status;
        }""",
        slug,
    )
    r.assert_(
        "POST /reset returns 200",
        result == 200,
        detail=f"status={result}",
    )
    r.assert_(
        "pending.json cleared after /reset",
        not (doc_dir / "pending.json").exists(),
    )

    # /undo clears pending (need a snapshot to restore from — _snapshot_if_changed
    # ran during the reset above and produced one, so undo has a target).
    write_pending()
    # current.md is now baseline (from the reset); make a fake change so
    # the snapshot/undo target makes sense.
    (doc_dir / "current.md").write_text(current, encoding="utf-8", newline="")
    result_undo = page.evaluate(
        """async (slug) => {
            const r = await fetch('/undo?doc=' + encodeURIComponent(slug), {
                method: 'POST',
            });
            return r.status;
        }""",
        slug,
    )
    r.assert_(
        "POST /undo returns 200 when a snapshot exists",
        result_undo == 200,
        detail=f"status={result_undo}",
    )
    r.assert_(
        "pending.json cleared after /undo",
        not (doc_dir / "pending.json").exists(),
    )

    shutil.rmtree(doc_dir, ignore_errors=True)


def scenario_add_doc_skill(page, base: str, r: Results) -> None:
    """+ Skill button appends an agent-skill section to current.md. The
    rendered Doc view hides it; the source ledger has it. Backend
    endpoint validates input and runs the same snapshot + validator
    pipeline as any other edit."""
    print("\n[scenario] + Skill button / /add-doc-skill")

    import shutil
    slug = "smoke-add-skill"
    doc_dir = ROOT / "docs" / slug
    if doc_dir.exists():
        shutil.rmtree(doc_dir)
    doc_dir.mkdir(parents=True)
    initial = (
        "---\ndoc_id: d-add-skill\ntitle: Add skill smoke\n---\n\n"
        "# Sample doc\n\nFirst paragraph.\n"
    )
    (doc_dir / "current.md").write_text(initial, encoding="utf-8", newline="")
    (doc_dir / "baseline.md").write_text(initial, encoding="utf-8", newline="")

    page.goto(base + f"/?doc={slug}")
    page.wait_for_function(
        f"() => document.getElementById('doc-select')?.value === '{slug}'",
        timeout=10000,
    )

    # POST /add-doc-skill directly — same call the button makes (avoids
    # driving the native prompt() dialog in Playwright).
    add = page.evaluate(
        """async (slug) => {
            const r = await fetch('/add-doc-skill?doc=' + encodeURIComponent(slug), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: 'voice rules' }),
            });
            return { status: r.status, body: await r.json() };
        }""",
        slug,
    )
    r.assert_(
        "POST /add-doc-skill returns 200",
        add.get("status") == 200,
        detail=str(add),
    )
    r.assert_(
        "response carries the skill name back",
        (add.get("body") or {}).get("name") == "voice rules",
        detail=str(add),
    )

    # On-disk: source has the new section.
    disk = (doc_dir / "current.md").read_text(encoding="utf-8")
    r.assert_(
        "current.md gained the agent-skill section",
        '<section class="agent-skill">' in disk and "voice rules" in disk,
        detail=disk[-400:],
    )
    r.assert_(
        "original content is preserved",
        "First paragraph." in disk and "# Sample doc" in disk,
        detail=disk[:200],
    )
    # Placement: the skill section should be inserted ABOVE the body
    # heading so the agent absorbs the contract before processing
    # content. Source-view discoverability also benefits.
    skill_pos = disk.find('<section class="agent-skill">')
    body_pos = disk.find("# Sample doc")
    r.assert_(
        "agent-skill section is inserted before the body content",
        skill_pos >= 0 and body_pos >= 0 and skill_pos < body_pos,
        detail=f"skill_pos={skill_pos} body_pos={body_pos}",
    )

    # In the iframe, the new section should be in the DOM AND hidden.
    frame = _wait_for_doc_iframe(page)
    page.wait_for_timeout(800)
    info = frame.evaluate(
        "() => {"
        "  const sec = document.querySelector('article#body section.agent-skill');"
        "  if (!sec) return { present: false };"
        "  return { present: true, display: getComputedStyle(sec).display };"
        "}"
    )
    r.assert_(
        "appended agent-skill section reaches the iframe DOM",
        info.get("present") is True,
        detail=str(info),
    )
    r.assert_(
        "appended agent-skill section is hidden (display: none)",
        info.get("display") == "none",
        detail=str(info),
    )

    # Skills tab is present in the view chrome (replaces the legacy + Skill
    # button under the overflow menu).
    skills_tab_present = page.evaluate(
        "() => Array.from(document.querySelectorAll('.view-tab'))"
        ".some(b => b.textContent.trim() === 'Skills')"
    )
    r.assert_("Skills tab is in the view chrome", skills_tab_present)

    shutil.rmtree(doc_dir, ignore_errors=True)


def scenario_agent_skill_hidden(page, base: str, r: Results) -> None:
    """`<section class="agent-skill">` is the per-doc agent contract. It
    lives in the doc body (so it ships with the file, survives Reset and
    export) but is hidden in the rendered Doc view via CSS so the reader
    sees clean content. Source view shows it as-is."""
    print("\n[scenario] agent-skill section hidden in Doc view")

    import shutil
    slug = "smoke-agent-skill"
    doc_dir = ROOT / "docs" / slug
    if doc_dir.exists():
        shutil.rmtree(doc_dir)
    doc_dir.mkdir(parents=True)
    initial = (
        "---\ndoc_id: d-skill-smoke\ntitle: Skill smoke\n---\n\n"
        "# Reader-facing title\n\n"
        "Visible reader paragraph.\n\n"
        '<section class="agent-skill">\n\n'
        "## SKILL: voice rules\n\n"
        "Never use the word 'comprehensive'. Always use the Oxford comma.\n\n"
        "</section>\n\n"
        "After the skill — also visible.\n"
    )
    (doc_dir / "current.md").write_text(initial, encoding="utf-8", newline="")
    (doc_dir / "baseline.md").write_text(initial, encoding="utf-8", newline="")

    page.goto(base + f"/?doc={slug}")
    frame = _wait_for_doc_iframe(page)
    frame.wait_for_selector("article#body p", timeout=10000)
    page.wait_for_timeout(300)

    # The agent-skill section element should be in the DOM (so Source view
    # has access to it and export bundles it) but computed display is none.
    info = frame.evaluate(
        "() => {"
        "  const sec = document.querySelector('article#body section.agent-skill');"
        "  if (!sec) return { present: false };"
        "  const cs = getComputedStyle(sec);"
        "  return { present: true, display: cs.display, "
        "    rect: sec.getBoundingClientRect().height };"
        "}"
    )
    r.assert_(
        "agent-skill section is in the iframe DOM",
        info.get("present") is True,
        detail=str(info),
    )
    r.assert_(
        "agent-skill section computed display is none (hidden from reader)",
        info.get("display") == "none",
        detail=str(info),
    )
    r.assert_(
        "agent-skill section takes zero height in layout",
        (info.get("rect") or 0) == 0,
        detail=str(info),
    )

    # Reader-facing content around the skill stays visible.
    visible = frame.evaluate(
        "() => Array.from(document.querySelectorAll('article#body p'))"
        ".map(p => p.textContent.trim())"
    )
    r.assert_(
        "reader paragraph before the skill is rendered",
        any("Visible reader paragraph" in t for t in (visible or [])),
        detail=str(visible),
    )
    r.assert_(
        "reader paragraph after the skill is rendered",
        any("also visible" in t for t in (visible or [])),
        detail=str(visible),
    )

    # Source view shows the raw markdown including the skill section.
    # Source lives under View ▾ now — open the dropdown then click Source.
    page.evaluate(
        "() => Array.from(document.querySelectorAll('.view-tab-dropdown'))"
        ".find(b => b.textContent.startsWith('View')).click()"
    )
    page.wait_for_timeout(100)
    page.evaluate(
        "() => { const m = Array.from(document.querySelectorAll('.view-dropdown-menu'))"
        ".find(m => !m.hidden);"
        "Array.from(m.querySelectorAll('.overflow-item'))"
        ".find(b => b.textContent.trim() === 'Source').click(); }"
    )
    page.wait_for_timeout(300)
    source_text = page.evaluate(
        "() => document.getElementById('body').textContent"
    )
    r.assert_(
        "Source view shows the agent-skill section as raw markdown",
        '<section class="agent-skill">' in (source_text or "")
        and "Never use the word" in (source_text or ""),
        detail=(source_text or "")[:300],
    )

    # Cleanup
    shutil.rmtree(doc_dir, ignore_errors=True)


def scenario_text_selection(page, base: str, r: Results) -> None:
    """Native text selection in the doc iframe must survive a click.

    Previously the iframe's click handler called removeAllRanges() on every
    click to support block-focus, which killed selection — readers could
    not copy text from the doc. The fix: if there's already a non-empty
    selection at click time, the click is part of a text-select gesture
    and block-focus is skipped."""
    print("\n[scenario] text selection survives click")

    page.goto(base + "/")
    frame = _wait_for_doc_iframe(page)
    frame.wait_for_selector("article#body p", timeout=10000)

    # Case 1: programmatic Selection on a paragraph, then dispatch click.
    # Selection should still be present after.
    result = frame.evaluate(
        """() => {
            const body = document.getElementById('body');
            const p = body.querySelector('p');
            if (!p) return { error: 'no <p>' };
            const range = document.createRange();
            range.selectNodeContents(p);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            const before = sel.toString().trim();
            const rect = p.getBoundingClientRect();
            p.dispatchEvent(new MouseEvent('click', {
                bubbles: true, cancelable: true, button: 0,
                clientX: rect.x + 10, clientY: rect.y + 10,
            }));
            const after = (window.getSelection().toString() || '').trim();
            return { before, after, preserved: before.length > 0 && after === before };
        }"""
    )
    r.assert_(
        "selection on a <p> survives a click (drag-select gesture preserved)",
        bool(result.get("preserved")),
        detail=str(result)[:300],
    )

    # Case 2: collapse the selection (no text selected), click → block focus
    # still works as before. We listen for the iframe→parent 'selection'
    # postMessage to confirm.
    page.evaluate(
        "() => { window.__lastSelMsg = null; window.addEventListener('message', e => "
        "{ if (e.data && e.data.type === 'selection') window.__lastSelMsg = e.data; }); }"
    )
    frame.evaluate(
        """() => {
            window.getSelection().removeAllRanges();
            const p = document.querySelector('article#body p');
            const rect = p.getBoundingClientRect();
            p.dispatchEvent(new MouseEvent('click', {
                bubbles: true, cancelable: true, button: 0,
                clientX: rect.x + 10, clientY: rect.y + 10,
            }));
        }"""
    )
    page.wait_for_function("() => window.__lastSelMsg !== null", timeout=2000)
    sel_msg = page.evaluate("() => window.__lastSelMsg")
    r.assert_(
        "click without selection still triggers block-focus (existing behavior)",
        bool(sel_msg) and sel_msg.get("block") is not None,
        detail=str(sel_msg)[:200],
    )


def scenario_insertion_point(page, base: str, r: Results) -> None:
    """Click in a gap between blocks → iframe posts `insert_click` with
    before/after block info AND draws a horizontal marker at the gap.
    Clicking a real block clears the marker. This is the "insert here"
    affordance — it lets the agent place new content precisely instead
    of guessing position from prose like 'above this' / 'at the top'."""
    print("\n[scenario] insertion-point gap clicks")

    page.goto(base + "/")
    frame = _wait_for_doc_iframe(page)
    frame.wait_for_function(
        "() => typeof window.morphdom === 'function' && "
        "typeof window.__doc === 'object'",
        timeout=8000,
    )

    # Push synthetic content with three blocks of known on-screen positions.
    # Each block gets a fixed height via inline style so we can pick a
    # gap y-coordinate deterministically.
    page.evaluate(
        "() => { document.getElementById('doc-frame').contentWindow"
        ".postMessage({ type: 'setContent', meta: '', html: ["
        "  '<p id=\"a\" style=\"height:80px;margin:0;\">first</p>',"
        "  '<p id=\"b\" style=\"height:80px;margin:0;\">second</p>',"
        "  '<p id=\"c\" style=\"height:80px;margin:0;\">third</p>',"
        "].join('') }, '*'); }"
    )
    frame.wait_for_function(
        "() => document.getElementById('a') "
        "&& document.getElementById('b') "
        "&& document.getElementById('c')",
        timeout=2000,
    )

    # Listen for the insert_click message the iframe posts. We need to
    # capture it from the PARENT since the iframe is null-origin.
    page.evaluate("""() => {
        window.__lastInsert = null;
        window.addEventListener('message', (e) => {
            if (e.data && e.data.type === 'insert_click') {
                window.__lastInsert = JSON.parse(JSON.stringify(e.data));
            }
        });
    }""")

    # Compute a Y-coordinate that falls in the gap between blocks a and b.
    # Coordinates are iframe-local (clientY in the iframe's viewport) but
    # frame.dispatch_event delivers events with the right coord system.
    gap_y = frame.evaluate(
        "() => { const a = document.getElementById('a').getBoundingClientRect();"
        " const b = document.getElementById('b').getBoundingClientRect();"
        " return Math.floor((a.bottom + b.top) / 2); }"
    )
    # Need an element inside the iframe to dispatch the synthetic click on.
    # article#body is the parent of the gap; clicks land on it when there's
    # no child at that y.
    frame.dispatch_event(
        "article#body", "click",
        {"clientX": 100, "clientY": gap_y, "bubbles": True},
    )
    # Give the post-message round-trip a tick.
    page.wait_for_function("() => window.__lastInsert !== null", timeout=2000)

    captured = page.evaluate("() => window.__lastInsert")
    r.assert_(
        "iframe posts insert_click on gap-click",
        captured is not None and captured.get("type") == "insert_click",
        detail=str(captured),
    )
    before = (captured or {}).get("before") or {}
    after = (captured or {}).get("after") or {}
    r.assert_(
        "insert_click identifies before-block as 'first'",
        "first" in (before.get("excerpt") or before.get("label") or ""),
        detail=str(before),
    )
    r.assert_(
        "insert_click identifies after-block as 'second'",
        "second" in (after.get("excerpt") or after.get("label") or ""),
        detail=str(after),
    )

    # Visual marker should now sit between #a and #b in the DOM.
    marker_pos = frame.evaluate(
        "() => { const m = document.querySelector('.insertion-marker');"
        " if (!m) return null;"
        " const sibs = Array.from(document.querySelector('article#body').children);"
        " return { idx: sibs.indexOf(m), beforeId: sibs[sibs.indexOf(m)-1]?.id || null,"
        "          afterId: sibs[sibs.indexOf(m)+1]?.id || null }; }"
    )
    r.assert_(
        "insertion marker is in DOM",
        marker_pos is not None,
        detail=str(marker_pos),
    )
    r.assert_(
        "marker is positioned between #a and #b",
        marker_pos and marker_pos.get("beforeId") == "a"
        and marker_pos.get("afterId") == "b",
        detail=str(marker_pos),
    )

    # Click on a real block — marker should disappear, normal selection works.
    frame.dispatch_event(
        "#c", "click",
        {"bubbles": True},
    )
    page.wait_for_timeout(150)
    marker_after_block_click = frame.evaluate(
        "() => !!document.querySelector('.insertion-marker')"
    )
    r.assert_(
        "block click clears the insertion marker",
        marker_after_block_click is False,
    )

    # Gap-click below all three blocks → insertion at end-of-doc
    # (after=null, before='third').
    page.evaluate("() => { window.__lastInsert = null; }")
    bottom_y = frame.evaluate(
        "() => Math.floor(document.getElementById('c').getBoundingClientRect().bottom + 20)"
    )
    frame.dispatch_event(
        "article#body", "click",
        {"clientX": 100, "clientY": bottom_y, "bubbles": True},
    )
    page.wait_for_function("() => window.__lastInsert !== null", timeout=2000)
    bottom_cap = page.evaluate("() => window.__lastInsert")
    r.assert_(
        "gap-click below last block: before='third', after=null",
        (bottom_cap or {}).get("after") is None
        and "third" in (
            ((bottom_cap or {}).get("before") or {}).get("excerpt", "")
            or ((bottom_cap or {}).get("before") or {}).get("label", "")
        ),
        detail=str(bottom_cap),
    )


def scenario_nested_selection_dedup(page, base: str, r: Results) -> None:
    """Regression: clicking a no-id block nested in a wrapper (a
    <div class="narrative"> around a single long paragraph, like the
    time-notes docs) must outline ONLY the clicked block — not the wrapper
    that shares its truncated-text key. Guards applySelection's containment
    de-dup."""
    print("\n[scenario] nested selection de-dup (wrapper + child share a key)")

    page.goto(base + "/")
    frame = _wait_for_doc_iframe(page)
    frame.wait_for_function(
        "() => typeof window.morphdom === 'function' && "
        "typeof window.__doc === 'object'",
        timeout=8000,
    )

    # A wrapper div whose only child is a long paragraph: div.textContent ==
    # p.textContent, so their keyOf (truncated textContent, no ids) collides.
    long_text = (
        "Determine assumptions, variables and specific methodologies for data "
        "analysis and interpretation framework across multiple overlapping "
        "datasets and validation passes, with careful attention to discrepancy "
        "patterns and rounding effects throughout the engagement."
    )
    page.evaluate(
        "(txt) => { document.getElementById('doc-frame').contentWindow"
        ".postMessage({ type: 'setContent', meta: '', html: ["
        "  '<h2>Week heading</h2>',"
        "  '<div class=\"narrative\"><p>' + txt + '</p></div>',"
        "  '<p>an unrelated trailing paragraph</p>',"
        "].join('') }, '*'); }",
        long_text,
    )
    frame.wait_for_function(
        "() => document.querySelector('div.narrative > p')", timeout=2000,
    )

    # Click the inner paragraph; only it should end up outlined.
    frame.dispatch_event("div.narrative > p", "click", {"bubbles": True})
    page.wait_for_timeout(150)

    state = frame.evaluate(
        "() => {"
        " const sel = Array.from(document.querySelectorAll('.selected-block'));"
        " return {"
        "   count: sel.length,"
        "   pSel: document.querySelector('div.narrative > p')"
        "         ?.classList.contains('selected-block') || false,"
        "   divSel: document.querySelector('div.narrative')"
        "           ?.classList.contains('selected-block') || false,"
        "   tags: sel.map(e => e.tagName)"
        " }; }"
    )
    r.assert_(
        "nested click outlines exactly one block",
        bool(state) and state.get("count") == 1,
        detail=str(state),
    )
    r.assert_(
        "the clicked paragraph is the selected block",
        bool(state and state.get("pSel")),
        detail=str(state),
    )
    r.assert_(
        "the wrapping div is NOT also selected (containment de-dup)",
        bool(state) and state.get("divSel") is False,
        detail=str(state),
    )


def scenario_asset_drop(page, base: str, r: Results) -> None:
    """POST /upload-asset with a fake PNG, verify it lands at
    docs/<slug>/assets/, is served back from /docs/<slug>/assets/<name>,
    and the iframe's <base href> resolves relative `assets/…` URLs."""
    print("\n[scenario] asset drop")

    page.goto(base + "/")
    page.wait_for_function(
        "() => document.getElementById('doc-select')?.value",
        timeout=10000,
    )

    # Minimal 1x1 transparent PNG (valid PNG, ~70 bytes)
    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB"
        "0C8AAAAASUVORK5CYII="
    )
    upload_result = page.evaluate(
        """async (b64) => {
            const bin = atob(b64);
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            const blob = new Blob([bytes], { type: 'image/png' });
            const form = new FormData();
            form.append('file', blob, 'pixel.png');
            const r = await fetch('/upload-asset?doc=intro', {
                method: 'POST', body: form,
            });
            const j = await r.json();
            return { status: r.status, body: j };
        }""",
        png_b64,
    )
    r.assert_(
        "POST /upload-asset?doc=intro accepts a PNG",
        upload_result.get("status") == 200,
        detail=str(upload_result),
    )
    asset_name = upload_result.get("body", {}).get("name", "")
    r.assert_(
        "asset upload response carries the saved filename",
        asset_name.endswith(".png") and "pixel" in asset_name,
        detail=str(upload_result),
    )

    # The static serve allows docs/<slug>/assets/<file>.
    fetched_status = page.evaluate(
        """async (name) => {
            const r = await fetch('/docs/intro/assets/' + name);
            return r.status;
        }""",
        asset_name,
    )
    r.assert_(
        "asset is served back at /docs/intro/assets/<name>",
        fetched_status == 200,
        detail=f"status={fetched_status}",
    )

    # Disallowed asset extensions are rejected.
    bad = page.evaluate(
        """async () => {
            const blob = new Blob(['#!/bin/sh\\necho hi'], { type: 'application/x-sh' });
            const form = new FormData();
            form.append('file', blob, 'evil.sh');
            const r = await fetch('/upload-asset?doc=intro', {
                method: 'POST', body: form,
            });
            return r.status;
        }"""
    )
    r.assert_(
        "blocklist: .sh asset is rejected",
        bad == 415,
        detail=f"status={bad}",
    )

    # snaps/ under a doc still 404s (sanity check the serve allowlist).
    snaps_status = page.evaluate(
        "async () => (await fetch('/docs/intro/snaps/foo.md')).status"
    )
    r.assert_(
        "GET /docs/<slug>/snaps/* still 404 (assets allowlist didn't broaden too much)",
        snaps_status == 404,
        detail=f"status={snaps_status}",
    )


def _make_minimal_pdf(text: str = "Hello AM PDF") -> bytes:
    """Build a tiny but valid single-page PDF whose only content is one
    string. markitdown / pdfminer should extract `text` from this when
    converting. Avoids bundling a binary fixture in git."""
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    add(
        b"<< /Type /Page /Parent 2 0 R "
        b"/Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 612 792] /Contents 5 0 R >>"
    )
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    stream_inner = (
        f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    )
    add(
        f"<< /Length {len(stream_inner)} >>\nstream\n".encode()
        + stream_inner
        + b"\nendstream"
    )

    out = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets = [0]
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF"
    ).encode()
    return out


def scenario_pdf_import(page, base: str, r: Results) -> None:
    """POST a minimal hand-crafted PDF to /upload. Verify the backend
    converts it server-side via markitdown, writes current.md + baseline.md
    + original.pdf under docs/<slug>/, and the new doc is in the docs list."""
    print("\n[scenario] PDF import (server-side markitdown convert)")

    page.goto(base + "/")
    page.wait_for_function(
        "() => document.getElementById('doc-select')?.value",
        timeout=10000,
    )

    pdf_bytes = _make_minimal_pdf("Hello AM PDF smoke")
    pdf_b64 = __import__("base64").b64encode(pdf_bytes).decode("ascii")

    upload = page.evaluate(
        """async (b64) => {
            const bin = atob(b64);
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            const blob = new Blob([bytes], { type: 'application/pdf' });
            const form = new FormData();
            form.append('file', blob, 'pdf-smoke.pdf');
            const r = await fetch('/upload', { method: 'POST', body: form });
            const j = await r.json();
            return { status: r.status, body: j };
        }""",
        pdf_b64,
    )

    if upload.get("status") == 501:
        # markitdown not installed — skip the rest of the scenario but
        # report what happened so the human running tests sees the gap.
        r.assert_(
            "PDF import: markitdown not installed (501) — scenario skipped",
            False,
            detail=str(upload),
        )
        return

    r.assert_(
        "POST /upload accepts a PDF and returns 200",
        upload.get("status") == 200,
        detail=str(upload),
    )
    body = upload.get("body", {}) or {}
    slug = body.get("slug", "")
    r.assert_(
        "PDF upload response carries converted: true",
        body.get("converted") is True,
        detail=str(body),
    )
    r.assert_(
        "PDF upload response carries a slug",
        bool(slug and slug.startswith("pdf-smoke")),
        detail=str(body),
    )

    # Filesystem invariants
    doc_dir = ROOT / "docs" / slug
    try:
        current = (doc_dir / "current.md").read_text(encoding="utf-8")
        baseline = (doc_dir / "baseline.md").read_text(encoding="utf-8")
        original_exists = (doc_dir / "original.pdf").exists()
    except Exception as exc:
        current = ""
        baseline = ""
        original_exists = False
        print(f"  (filesystem read failed: {exc})")

    r.assert_(
        "docs/<slug>/original.pdf is on disk",
        original_exists,
        detail=f"slug={slug}",
    )
    r.assert_(
        "docs/<slug>/current.md was written by the converter",
        bool(current.strip()),
        detail=f"len={len(current)}",
    )
    r.assert_(
        "current.md contains extracted PDF text",
        "Hello AM PDF smoke" in current,
        detail=f"current.md head: {current[:200]!r}",
    )
    # current.md carries eager block-id comments (the system stamps them on
    # every write); baseline.md is the pristine history-0. They match modulo
    # those invisible <!-- id:b-... --> lines.
    import re as _re
    def _strip_ids(t):
        return "\n".join(
            ln for ln in t.split("\n")
            if not _re.match(r"^<!-- id:b-[A-Z0-9-]+ -->$", ln)
        )
    r.assert_(
        "baseline.md matches current.md after conversion (modulo block ids)",
        _strip_ids(baseline) == _strip_ids(current),
        detail=f"baseline_len={len(baseline)}, current_len={len(current)}",
    )

    # Doc list refreshes via the docs broadcast → dropdown should include
    # the new slug shortly after upload lands.
    page.wait_for_function(
        f"() => Array.from(document.getElementById('doc-select').options)"
        f".some(o => o.value === '{slug}')",
        timeout=5000,
    )
    options = page.evaluate(
        "() => Array.from(document.getElementById('doc-select').options).map(o => o.value)"
    )
    r.assert_(
        "new PDF-derived doc is in the doc-select dropdown",
        slug in options,
        detail=str(options),
    )

    # Cleanup: remove the test doc directory so reruns don't accumulate
    # pdf-smoke-2, -3, -4, ... in docs/.
    try:
        import shutil
        if doc_dir.exists():
            shutil.rmtree(doc_dir)
    except Exception as exc:
        print(f"  (cleanup of {doc_dir} failed: {exc})")


def scenario_export(page, base: str, r: Results) -> None:
    """Single-file HTML export. Click the Export tab, intercept the download,
    and verify the bundled HTML has the structure a reader would need to
    open the doc standalone."""
    print("\n[scenario] single-file HTML export")

    page.goto(base + "/")
    page.wait_for_function(
        "() => document.getElementById('doc-select')?.value && window.__amExport",
        timeout=10000,
    )

    # Use the export module directly so we can read the bundled HTML in-test
    # without having to handle a Playwright download. The Export button
    # exercises the same path.
    bundle = page.evaluate(
        """async () => {
            const out = await window.__amExport.previewExport();
            const css = await window.__amExport.fetchExportCss();
            return { out, cssLen: css.length };
        }"""
    )
    out = bundle.get("out", "") or ""
    r.assert_(
        "exported HTML starts with <!DOCTYPE html>",
        out.startswith("<!DOCTYPE html>"),
        detail=out[:80],
    )
    r.assert_(
        "exported HTML carries no system block-id comments (clean artifact)",
        "<!-- id:b-" not in out,
        detail="found a stray <!-- id:b- --> in the export",
    )
    r.assert_(
        "exported HTML includes the KaTeX CDN stylesheet",
        "cdn.jsdelivr.net/npm/katex" in out and "katex.min.css" in out,
    )
    r.assert_(
        "exported HTML includes the KaTeX auto-render bootstrap",
        "renderMathInElement" in out and "DOMContentLoaded" in out,
    )
    r.assert_(
        "exported HTML inlines AM typography CSS (non-empty)",
        bundle.get("cssLen", 0) > 200,
        detail=f"cssLen={bundle.get('cssLen')}",
    )
    r.assert_(
        "exported HTML has an <article id=\"body\"> wrapper",
        '<article id="body">' in out,
    )
    r.assert_(
        "exported HTML stubs window.__doc.cleanup so doc scripts don't crash",
        "window.__doc" in out and "cleanup" in out,
    )
    r.assert_(
        "exported HTML carries doc content (intro doc has 'Welcome')",
        "Welcome" in out,
        detail=out[:1500],
    )

    # Now verify asset inlining works: drop a known PNG via /upload-asset,
    # synthesize a markdown body that references it, run inlineAssets, and
    # check the assets/foo.png ref was rewritten to a data: URI.
    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB"
        "0C8AAAAASUVORK5CYII="
    )
    asset_result = page.evaluate(
        """async (b64) => {
            const bin = atob(b64);
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            const blob = new Blob([bytes], { type: 'image/png' });
            const form = new FormData();
            form.append('file', blob, 'export-probe.png');
            const r = await fetch('/upload-asset?doc=intro', { method: 'POST', body: form });
            const j = await r.json();
            return j;
        }""",
        png_b64,
    )
    asset_name = (asset_result or {}).get("name", "")
    r.assert_(
        "asset upload for export inlining test succeeded",
        asset_name.endswith(".png"),
        detail=str(asset_result),
    )

    inlined = page.evaluate(
        """async (name) => {
            const body = '<img src="assets/' + name + '" alt="probe">';
            return await window.__amExport.inlineAssets(body);
        }""",
        asset_name,
    )
    r.assert_(
        "inlineAssets rewrites assets/<name> to a data: URI",
        "data:image/png;base64," in (inlined or ""),
        detail=(inlined or "")[:200],
    )


def scenario_slash_commands(page, base: str, r: Results) -> None:
    """The slash-command vocabulary beyond /cancel: /help, /reset, /undo,
    /new, /model, and the unknown-command path. Each one is tested in
    isolation against its observable side effect (chat message, dialog,
    POST, WS frame)."""
    print("\n[scenario] slash commands (/help /reset /undo /new /model)")

    page.goto(base + "/")
    page.wait_for_function(
        "() => document.getElementById('input') && "
        "document.getElementById('status')?.textContent === 'connected'",
        timeout=10000,
    )

    def send_command(cmd):
        page.fill("#input", cmd)
        page.locator("#input").press("Enter")

    # /help — assistant message with the help text.
    send_command("/help")
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.msg.assistant .msg-text'))"
        ".some(el => /slash commands/i.test(el.textContent || ''))",
        timeout=3000,
    )
    help_visible = page.evaluate(
        "() => Array.from(document.querySelectorAll('.msg.assistant .msg-text'))"
        ".some(el => /\\/cancel/.test(el.textContent || '') && "
        "/\\/reset/.test(el.textContent || '') && "
        "/\\/undo/.test(el.textContent || '') && "
        "/\\/model/.test(el.textContent || ''))"
    )
    r.assert_(
        "/help lists cancel + reset + undo + model commands",
        help_visible,
    )

    # Unknown command — error message.
    send_command("/notacommand")
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.msg.error .msg-text'))"
        ".some(el => /unknown slash command/i.test(el.textContent || ''))",
        timeout=3000,
    )
    r.assert_(
        "/<bogus> shows an unknown-command error",
        True,
    )

    # /undo — POSTs to /undo for the current doc. Track network.
    undo_calls = []
    page.on(
        "request",
        lambda req: undo_calls.append(req.url)
        if req.method == "POST" and "/undo" in req.url
        else None,
    )
    send_command("/undo")
    page.wait_for_timeout(800)
    r.assert_(
        "/undo POSTs to /undo for the current doc",
        any("/undo" in u for u in undo_calls),
        detail=f"undo calls: {undo_calls}",
    )

    # /reset — opens the reset confirmation dialog.
    send_command("/reset")
    page.wait_for_selector(
        "#reset-confirm-dlg[open]", timeout=3000, state="attached",
    )
    dlg_open = page.evaluate(
        "() => document.getElementById('reset-confirm-dlg').open"
    )
    r.assert_(
        "/reset opens the reset-confirmation dialog",
        dlg_open is True,
    )
    page.click("#reset-cancel")  # don't actually reset
    page.wait_for_timeout(200)

    # /model with no arg — assistant message showing current + available.
    send_command("/model")
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.msg.assistant .msg-text'))"
        ".some(el => /current model/i.test(el.textContent || ''))",
        timeout=3000,
    )
    r.assert_(
        "/model with no arg shows current model + available list",
        True,
    )

    # /model with a bad name — error.
    send_command("/model notamodel")
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.msg.error .msg-text'))"
        ".some(el => /\\/model: unknown/i.test(el.textContent || ''))",
        timeout=3000,
    )
    r.assert_(
        "/model <bogus> shows an unknown-model error",
        True,
    )

    # /new — clears the chat log and sends new_chat. We track WS sends.
    page.evaluate(
        """() => {
            window.__amWsSent = [];
            const orig = WebSocket.prototype.send;
            WebSocket.prototype.send = function (data) {
                try { window.__amWsSent.push(typeof data === 'string' ? data : '[bin]'); }
                catch (e) {}
                return orig.call(this, data);
            };
        }"""
    )
    send_command("/new")
    page.wait_for_timeout(400)
    sent = page.evaluate("() => window.__amWsSent || []")
    new_chat_sent = any('"new_chat"' in s for s in sent)
    r.assert_(
        "/new sends {type:'new_chat'} over the WebSocket",
        new_chat_sent,
        detail=f"sent: {sent[-3:]}",
    )
    log_empty_after_new = page.evaluate(
        "() => document.getElementById('log').children.length === 0"
    )
    r.assert_(
        "/new clears the chat log",
        log_empty_after_new,
    )


def scenario_serve_normalizes(page, base: str, r: Results) -> None:
    """A freshly-written doc with NO ids gets stamped when the viewer fetches
    it — serve_static is the single normalization choke point. Proves the
    wiring + the new block classifier end-to-end in a real browser, including
    that an inline-HTML-led paragraph is stamped (not skipped as a block)."""
    print("\n[scenario] serve_static normalizes current.md on fetch")
    import re as _re
    import shutil
    slug = "smoke-serve-norm"
    doc_dir = ROOT / "docs" / slug
    if doc_dir.exists():
        shutil.rmtree(doc_dir)
    doc_dir.mkdir(parents=True)
    initial = (
        "# Serve norm\n\n"
        "First paragraph with no id yet.\n\n"
        "<em>Inline</em>-led paragraph also has no id.\n\n"
        "Second plain paragraph.\n"
    )
    (doc_dir / "current.md").write_text(initial, encoding="utf-8", newline="")
    (doc_dir / "baseline.md").write_text(initial, encoding="utf-8", newline="")

    page.goto(base + f"/?doc={slug}")
    frame = _wait_for_doc_iframe(page)
    frame.wait_for_selector("article#body p", timeout=10000)
    page.wait_for_timeout(300)

    all_tagged = frame.evaluate(
        "() => Array.from(document.querySelectorAll("
        "'article#body > p, article#body > h1')).every(el => !!el.dataset.trackId)"
    )
    r.assert_(
        "every top-level heading/paragraph has data-track-id after a plain fetch",
        all_tagged, detail="a block was missing data-track-id",
    )
    disk = (doc_dir / "current.md").read_text(encoding="utf-8")
    r.assert_(
        "serve_static wrote id comments back to current.md on disk",
        disk.count("<!-- id:b-") >= 4,
        detail=disk,
    )
    r.assert_(
        "inline-HTML-led paragraph got stamped (classified as paragraph, not HTML block)",
        _re.search(r"<!-- id:b-[A-Z0-9-]+ -->\n<em>Inline</em>-led", disk) is not None,
        detail=disk,
    )
    # baseline.md is left pristine.
    base_txt = (doc_dir / "baseline.md").read_text(encoding="utf-8")
    r.assert_(
        "baseline.md stays pristine (no id comments)",
        "<!-- id:b-" not in base_txt,
        detail=base_txt,
    )
    shutil.rmtree(doc_dir, ignore_errors=True)


def scenario_inline_source_edit(page, base: str, r: Results) -> None:
    """Pencil-on-hover inline SOURCE editor: hover a block -> pencil -> the
    block becomes a <textarea> of its EXACT markdown -> edit -> save -> it
    re-renders. Lossless end to end (a styled <span> + math survive), because
    the reader edits source, never the rendered DOM."""
    print("\n[scenario] inline source edit (pencil -> textarea -> save)")
    import shutil
    slug = "smoke-srcedit"
    doc_dir = ROOT / "docs" / slug
    if doc_dir.exists():
        shutil.rmtree(doc_dir)
    doc_dir.mkdir(parents=True)
    initial = (
        "# Src edit\n\n"
        'Edit me: a <span style="color:red">styled</span> word and math $E=mc^2$.\n\n'
        "Untouched paragraph.\n"
    )
    (doc_dir / "current.md").write_text(initial, encoding="utf-8", newline="")
    (doc_dir / "baseline.md").write_text(initial, encoding="utf-8", newline="")

    page.goto(base + f"/?doc={slug}")
    frame = _wait_for_doc_iframe(page)
    frame.wait_for_selector("article#body p[data-track-id]", timeout=10000)
    page.wait_for_timeout(300)

    tid = frame.evaluate("""() => {
        const p = [...document.querySelectorAll('article#body p[data-track-id]')]
            .find(e => /styled/.test(e.textContent || ''));
        return p ? p.dataset.trackId : null;
    }""")
    r.assert_("styled-span paragraph carries a data-track-id (serve-normalized)",
              bool(tid), detail=str(tid))

    # Hover -> pencil appears -> click it -> request source -> editor opens.
    frame.evaluate("""() => {
        const p = [...document.querySelectorAll('article#body p[data-track-id]')]
            .find(e => /styled/.test(e.textContent || ''));
        p.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
    }""")
    frame.wait_for_selector("#am-block-tools", state="visible", timeout=4000)
    r.assert_("edit toolbar appears on hover", True)
    frame.eval_on_selector("#am-block-tools button", "b => b.click()")  # ✎ is first
    frame.wait_for_selector(".am-src-edit textarea", timeout=5000)

    src = frame.eval_on_selector(".am-src-edit textarea", "t => t.value")
    r.assert_(
        "textarea shows the EXACT block source (styled span + math intact)",
        '<span style="color:red">styled</span>' in src and "$E=mc^2$" in src,
        detail=repr(src),
    )

    # Edit the word inside the span, save with Ctrl+Enter.
    frame.eval_on_selector(".am-src-edit textarea",
                           "t => { t.value = t.value.replace('>styled<', '>restyled<'); }")
    frame.eval_on_selector(
        ".am-src-edit textarea",
        "t => t.dispatchEvent(new KeyboardEvent('keydown', "
        "{key:'Enter', ctrlKey:true, bubbles:true}))")
    page.wait_for_timeout(1600)  # save -> doc_changed -> re-render

    after = frame.evaluate("""() => {
        const p = [...document.querySelectorAll('article#body p')]
            .find(e => /restyled/.test(e.textContent || ''));
        return p ? !!p.querySelector('span[style]') : false;
    }""")
    r.assert_("edited block re-rendered with the <span> styling preserved", after)

    disk = (doc_dir / "current.md").read_text(encoding="utf-8")
    r.assert_(
        "current.md has the edited source byte-exact (span + math, no nbsp)",
        ('<span style="color:red">restyled</span>' in disk
         and "$E=mc^2$" in disk and " " not in disk
         and "Untouched paragraph." in disk),
        detail=disk,
    )
    shutil.rmtree(doc_dir, ignore_errors=True)


def scenario_render_invariant(page, base: str, r: Results) -> None:
    """THE guard against the edge cases we didn't enumerate: stamping ids must
    not change what the viewer renders. For a doc packed with the hard block
    types (loose list, <figure> with a blank caption, GFM table, fenced code
    with a blank line, math, nested <section>), the FULL render pipeline must
    produce identical HTML from the stamped source and from the same source
    with ids stripped — modulo the inserted comment nodes."""
    print("\n[scenario] render(stamped) == render(stripped) invariant")
    import shutil
    slug = "smoke-invariant"
    doc_dir = ROOT / "docs" / slug
    if doc_dir.exists():
        shutil.rmtree(doc_dir)
    doc_dir.mkdir(parents=True)
    body = (
        "# Heading\n\n"
        "Intro paragraph with *emphasis* and math $x^2$.\n\n"
        "- loose one\n\n- loose two\n- loose three\n\n"
        "| a | b |\n| - | - |\n| 1 | 2 |\n\n"
        "```python\nx = 1\n\ny = 2\n```\n\n"
        '<figure class="c">\n\nA **caption** with a blank line.\n\n</figure>\n\n'
        '<section class="theorem">\n\nInner prose and $E=mc^2$.\n\n</section>\n\n'
        "Closing paragraph.\n"
    )
    (doc_dir / "current.md").write_text(body, encoding="utf-8", newline="")
    (doc_dir / "baseline.md").write_text(body, encoding="utf-8", newline="")

    page.goto(base + f"/?doc={slug}")
    _wait_for_doc_iframe(page)
    page.wait_for_timeout(400)

    stamped = (doc_dir / "current.md").read_text(encoding="utf-8")
    r.assert_("doc got stamped on serve (>= 7 ids)",
              stamped.count("<!-- id:b-") >= 7, detail=stamped)
    result = page.evaluate(
        """(body) => {
            const strip = t => t.split('\\n')
                .filter(l => !/^<!-- id:b-[A-Z0-9-]+ -->$/.test(l)).join('\\n');
            const norm = h => h.replace(/<!--[\\s\\S]*?-->/g, '')
                .replace(/\\s+/g, ' ').trim();
            const a = norm(window.__amRenderDoc(body));
            const b = norm(window.__amRenderDoc(strip(body)));
            return { ok: a === b, a: a.slice(0, 280), b: b.slice(0, 280) };
        }""",
        stamped,
    )
    r.assert_(
        "stamping is render-neutral through the full pipeline (lists/figure/math/table)",
        result.get("ok"),
        detail="STAMPED: " + str(result.get("a")) + "\nSTRIPPED: " + str(result.get("b")),
    )
    shutil.rmtree(doc_dir, ignore_errors=True)


def scenario_block_ops_all_types(page, base: str, r: Results) -> None:
    """Edit-any-block + insert + delete via the hover toolbar — the user-
    reported gaps. A loose LIST (not just prose) opens in the editor as one
    unit; ＋ inserts a new block below; 🗑 deletes one."""
    print("\n[scenario] block ops: edit list / insert / delete")
    import shutil
    slug = "smoke-blockops"
    doc_dir = ROOT / "docs" / slug
    if doc_dir.exists():
        shutil.rmtree(doc_dir)
    doc_dir.mkdir(parents=True)
    initial = "# Block ops\n\n- alpha\n- beta\n\nA paragraph to delete.\n"
    (doc_dir / "current.md").write_text(initial, encoding="utf-8", newline="")
    (doc_dir / "baseline.md").write_text(initial, encoding="utf-8", newline="")

    page.goto(base + f"/?doc={slug}")
    frame = _wait_for_doc_iframe(page)
    frame.wait_for_selector("article#body ul[data-track-id]", timeout=10000)
    page.wait_for_timeout(300)

    # (1) EDIT a LIST — a non-prose block is now editable as one unit.
    r.assert_("the list (ul) carries a data-track-id (every block is stamped)",
              frame.evaluate("() => !!document.querySelector('article#body ul[data-track-id]')"))
    frame.evaluate("""() => document.querySelector('article#body ul[data-track-id]')
        .dispatchEvent(new MouseEvent('mouseover', { bubbles: true }))""")
    frame.wait_for_selector("#am-block-tools", state="visible", timeout=4000)
    frame.eval_on_selector("#am-block-tools button:nth-child(1)", "b => b.click()")  # edit
    frame.wait_for_selector(".am-src-edit textarea", timeout=5000)
    src = frame.eval_on_selector(".am-src-edit textarea", "t => t.value")
    r.assert_("textarea shows the WHOLE list source", src == "- alpha\n- beta",
              detail=repr(src))
    frame.eval_on_selector(
        ".am-src-edit textarea",
        "t => { t.value = '- alpha\\n- beta\\n- gamma'; "
        "t.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',ctrlKey:true,bubbles:true})); }")
    page.wait_for_timeout(1500)
    disk = (doc_dir / "current.md").read_text(encoding="utf-8")
    r.assert_("list edit landed (third item added)", "- gamma" in disk, detail=disk)

    # (2) INSERT a new block below the heading.
    frame.wait_for_selector("article#body h1[data-track-id]", timeout=5000)
    frame.evaluate("""() => document.querySelector('article#body h1[data-track-id]')
        .dispatchEvent(new MouseEvent('mouseover', { bubbles: true }))""")
    frame.wait_for_selector("#am-block-tools", state="visible", timeout=4000)
    frame.eval_on_selector("#am-block-tools button:nth-child(2)", "b => b.click()")  # insert
    frame.wait_for_selector(".am-src-edit textarea", timeout=5000)
    frame.eval_on_selector(
        ".am-src-edit textarea",
        "t => { t.value = 'Freshly inserted paragraph.'; "
        "t.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',ctrlKey:true,bubbles:true})); }")
    page.wait_for_timeout(1500)
    disk2 = (doc_dir / "current.md").read_text(encoding="utf-8")
    r.assert_("inserted block landed with its own id",
              "Freshly inserted paragraph." in disk2 and disk2.count("<!-- id:b-") >= 4,
              detail=disk2)

    # (3) DELETE a block via the confirm bar.
    frame.evaluate("""() => {
        const p = [...document.querySelectorAll('article#body p[data-track-id]')]
            .find(e => /paragraph to delete/.test(e.textContent || ''));
        p.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
    }""")
    frame.wait_for_selector("#am-block-tools", state="visible", timeout=4000)
    frame.eval_on_selector("#am-block-tools button:nth-child(3)", "b => b.click()")  # delete
    frame.wait_for_selector(".am-src-confirm", timeout=4000)
    frame.eval_on_selector(".am-src-confirm button.am-del", "b => b.click()")
    page.wait_for_timeout(1500)
    disk3 = (doc_dir / "current.md").read_text(encoding="utf-8")
    r.assert_("deleted block is gone from source",
              "A paragraph to delete." not in disk3, detail=disk3)
    shutil.rmtree(doc_dir, ignore_errors=True)


def scenario_cancel_command(page, base: str, r: Results) -> None:
    """`/cancel` typed into the chat input sends a {type:'cancel'} WS
    message instead of a chat turn. When no turn is running, the backend
    replies with a 'nothing to cancel' assistant note. (Cancelling an
    in-flight turn requires a real agent runtime + a long-running call,
    which we don't have in smoke; manual verify covers that path.)"""
    print("\n[scenario] /cancel slash command")

    page.goto(base + "/")
    page.wait_for_function(
        "() => document.getElementById('input') && "
        "document.getElementById('status')?.textContent === 'connected'",
        timeout=10000,
    )

    # Track WS sends so we can confirm /cancel did NOT go out as a chat.
    # Also tee what the page receives so we can debug if assertions fail.
    page.evaluate(
        """() => {
            window.__amWsSent = [];
            window.__amWsRecv = [];
            const origSend = WebSocket.prototype.send;
            WebSocket.prototype.send = function (data) {
                try { window.__amWsSent.push(typeof data === 'string' ? data : '[binary]'); }
                catch (e) {}
                return origSend.call(this, data);
            };
            const origAdd = EventTarget.prototype.addEventListener;
            // Capture incoming messages on any future ws.
            EventTarget.prototype.addEventListener = function (ev, fn, opts) {
                if (ev === 'message' && this instanceof WebSocket) {
                    const wrapped = function (e) {
                        try { window.__amWsRecv.push(String(e.data).slice(0, 200)); }
                        catch (er) {}
                        return fn.call(this, e);
                    };
                    return origAdd.call(this, ev, wrapped, opts);
                }
                return origAdd.call(this, ev, fn, opts);
            };
        }"""
    )

    # Type /cancel and press Enter.
    page.fill("#input", "/cancel")
    page.locator("#input").press("Enter")

    # Wait for SOME assistant reply — either "Nothing to cancel" (idle path)
    # or "Turn cancelled by reader" (interrupted-an-in-flight-turn path).
    # Earlier scenarios (e.g. tex_skip_preview) may leave an agent turn in
    # flight; both responses prove /cancel is wired correctly end-to-end.
    try:
        page.wait_for_function(
            "() => Array.from(document.querySelectorAll('.msg.assistant .msg-text'))"
            ".some(el => /nothing to cancel|turn cancelled/i.test(el.textContent || ''))",
            timeout=8000,
        )
        replied = True
    except Exception:
        replied = False
    if not replied:
        sent = page.evaluate("() => window.__amWsSent || []")
        chat_html = page.evaluate(
            "() => Array.from(document.querySelectorAll('.msg')).map(m => "
            "(m.className||'') + ': ' + (m.textContent||'').slice(0,120)).join(' | ')"
        )
        print("  ws sent:", sent[-5:])
        print("  chat log:", chat_html)
    r.assert_(
        "/cancel produces an assistant reply (cancelled-in-flight OR nothing-to-cancel)",
        replied,
    )

    sent = page.evaluate("() => window.__amWsSent || []")
    cancel_msgs = [s for s in sent if '"cancel"' in s and '"chat"' not in s]
    chat_with_cancel = [s for s in sent if '"chat"' in s and "/cancel" in s]
    r.assert_(
        "/cancel sent as {type:'cancel'}, not as a chat turn",
        len(cancel_msgs) >= 1 and len(chat_with_cancel) == 0,
        detail=f"cancel_msgs={len(cancel_msgs)} chat_with_cancel={len(chat_with_cancel)}",
    )

    # Input is cleared after the command runs.
    val = page.evaluate("() => document.getElementById('input').value")
    r.assert_(
        "/cancel clears the chat input after send",
        val == "",
        detail=f"input={val!r}",
    )


def scenario_tex_skip_preview(page, base: str, r: Results) -> None:
    """A .tex drop now bypasses the convert-preview dialog and POSTs
    directly to /upload — server-side Claude (or agent fallback under
    AM_PDF_BACKEND=markitdown) handles the conversion. No preview gate,
    no UI confirm step, no agent chat exchange at import time."""
    print("\n[scenario] .tex drop skips preview, server converts")

    page.goto(base + "/")
    page.wait_for_function(
        "() => document.getElementById('new-doc-input')",
        timeout=10000,
    )

    # Mock the /upload response. This test verifies FRONTEND behavior —
    # the .tex drop fires a POST to /upload and doesn't open the
    # convert-preview dialog. Whether the backend's actual Claude
    # conversion succeeds is a different concern; calling the real
    # Anthropic API here makes the test slow + flaky + costs money +
    # ties up the backend for downstream effects (post-scenario WS
    # broadcasts, async agent turns the test then has to clean up).
    #
    # Per-scenario context isolation removes the cross-scenario leak,
    # but the in-process backend would still be busy serving the
    # Claude call — risking page.goto timeouts in the next scenario.
    # Mocking the response cuts the entire backend chain out of the
    # test, keeping the scope tight to what the test actually
    # asserts.
    page.route(
        "**/upload",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=(
                '{"path":"docs/tex-skip-preview/current.md",'
                '"slug":"tex-skip-preview","name":"tex-skip-preview.md",'
                '"kind":".tex","converted":true,"converter":"smoke-mock"}'
            ),
        ),
    )

    upload_urls = []
    page.on(
        "request",
        lambda req: upload_urls.append(req.url)
        if req.method == "POST" and "/upload" in req.url
        else None,
    )

    fake_tex = (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\section{Smoke}\n"
        "Tex import smoke test.\n"
        "\\end{document}\n"
    )
    page.set_input_files(
        "#new-doc-input",
        files=[{
            "name": "tex-skip-preview.tex",
            "mimeType": "text/x-tex",
            "buffer": fake_tex.encode("utf-8"),
        }],
    )

    # Preview dialog must NOT open.
    page.wait_for_timeout(500)
    dialog_open = page.evaluate(
        "() => document.getElementById('convert-preview-dlg').open"
    )
    r.assert_(
        ".tex drop does NOT open the convert-preview dialog",
        dialog_open is False or dialog_open is None,
        detail=f"dialog.open={dialog_open}",
    )

    r.assert_(
        ".tex drop POSTs directly to /upload",
        any("/upload" in u for u in upload_urls),
        detail=f"upload urls observed: {upload_urls}",
    )

    # No cleanup needed — the mock means no file was created.


def scenario_reset_confirm_dialog(page, base: str, r: Results) -> None:
    """Reset (from the History tab) opens a styled <dialog> instead of the
    native confirm(). Cancel must close without resetting; Reset must close
    + POST /reset."""
    print("\n[scenario] reset confirmation modal")

    page.goto(base + "/")
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.view-tab'))"
        ".some(b => b.textContent.trim() === 'History') && "
        "document.getElementById('doc-select')?.value",
        timeout=10000,
    )

    # Track POSTs to /reset so we can confirm Cancel suppresses them.
    reset_posts = []
    page.on(
        "request",
        lambda req: reset_posts.append(req.url)
        if req.method == "POST" and "/reset" in req.url else None,
    )

    def click_reset_in_history_tab():
        page.evaluate(
            "() => Array.from(document.querySelectorAll('.view-tab'))"
            ".find(b => b.textContent.trim() === 'History').click()"
        )
        page.wait_for_selector(".view-history .history-actions", timeout=3000)
        page.evaluate(
            "() => Array.from(document.querySelectorAll('.view-history .history-actions button'))"
            ".find(b => b.textContent.includes('Reset')).click()"
        )

    click_reset_in_history_tab()
    page.wait_for_function(
        "() => document.getElementById('reset-confirm-dlg').open",
        timeout=3000,
    )
    r.assert_(
        "reset dialog opens from History-tab Reset action",
        page.evaluate("() => document.getElementById('reset-confirm-dlg').open")
        is True,
    )

    doc_line = page.evaluate(
        "() => document.getElementById('reset-doc-line').textContent"
    )
    r.assert_(
        "dialog body shows the doc path that will be reset",
        "docs/intro/current.md" in doc_line and "baseline.md" in doc_line,
        detail=doc_line,
    )

    # Cancel closes the dialog and does NOT POST /reset
    page.click("#reset-cancel")
    page.wait_for_function(
        "() => !document.getElementById('reset-confirm-dlg').open",
        timeout=3000,
    )
    page.wait_for_timeout(200)
    r.assert_(
        "Cancel closes the dialog without POSTing /reset",
        len(reset_posts) == 0,
        detail=f"unexpected posts: {reset_posts}",
    )

    # Re-open + click Reset → dialog closes + /reset is POSTed
    click_reset_in_history_tab()
    page.wait_for_function(
        "() => document.getElementById('reset-confirm-dlg').open",
        timeout=3000,
    )
    page.click("#reset-confirm")
    page.wait_for_function(
        "() => !document.getElementById('reset-confirm-dlg').open",
        timeout=3000,
    )
    page.wait_for_timeout(300)
    r.assert_(
        "Reset confirmation closes the dialog and POSTs /reset",
        len(reset_posts) == 1 and "doc=intro" in reset_posts[0],
        detail=f"posts: {reset_posts}",
    )


def scenario_drop_preview_dialog(page, base: str, r: Results) -> None:
    """Drop a fake .rst file via the hidden file input. The convert-preview
    dialog must appear with the file metadata + a preview of the content.
    Click Cancel — no upload should hit /upload.

    (.tex used to live here, but it now goes through server-side Claude
    conversion and bypasses the preview gate — see scenario_tex_skip_preview.)"""
    print("\n[scenario] drop-to-convert preview dialog")

    page.goto(base + "/")
    page.wait_for_function(
        "() => document.getElementById('new-doc-input') && "
        "document.getElementById('convert-preview-dlg')",
        timeout=10000,
    )

    # Track upload requests so we can confirm Cancel suppresses them.
    uploads = []
    page.on(
        "request",
        lambda req: uploads.append(req.url)
        if req.method == "POST" and "/upload" in req.url
        else None,
    )

    fake_rst = (
        "Sample reStructuredText\n"
        "=======================\n"
        "\n"
        "Hello, world.\n"
    )
    page.set_input_files(
        "#new-doc-input",
        files=[{
            "name": "smoke-test.rst",
            "mimeType": "text/x-rst",
            "buffer": fake_rst.encode("utf-8"),
        }],
    )

    # The dialog should open. Wait up to 5s.
    page.wait_for_selector(
        "#convert-preview-dlg[open]", timeout=5000,
        state="attached",
    )
    dialog_visible = page.evaluate(
        "() => document.getElementById('convert-preview-dlg').open"
    )
    r.assert_(
        "convert-preview dialog opens on non-.md drop",
        dialog_visible is True,
    )

    preview_text = page.evaluate(
        "() => document.getElementById('convert-preview-body').textContent"
    )
    r.assert_(
        "preview body shows file contents",
        "Hello, world." in preview_text,
        detail=preview_text[:120],
    )

    # Click Cancel — no upload should fire.
    page.click("#convert-cancel")
    page.wait_for_function(
        "() => !document.getElementById('convert-preview-dlg').open",
        timeout=3000,
    )
    # Give the network a moment to be sure nothing was sent.
    page.wait_for_timeout(300)
    r.assert_(
        "Cancel suppresses the upload",
        len(uploads) == 0,
        detail=f"uploads observed: {uploads}",
    )


# ---- driver -------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--headed", action="store_true",
                        help="show the browser window")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    base = f"http://127.0.0.1:{args.port}"
    results = Results()

    print(f"[harness] starting backend on {base}")
    with backend_running(args.port):
        print("[harness] backend ready")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            try:
                # Each scenario gets its own browser CONTEXT (fresh storage,
                # fresh cookies, fresh WebSocket). State from scenario N —
                # in-flight uploads, agent turns the test moved on without
                # awaiting, accumulated event listeners, pending broadcasts —
                # cannot leak into scenario N+1 because the WS connection
                # the broadcast would have arrived on is closed.
                #
                # Without this isolation, the smoke harness has historically
                # papered over cross-scenario state-bleed (see scenario_music
                # _upload removed at the harness level, scenario_cancel_command
                # accommodating "tex may leave a turn in flight", and the
                # flake that prompted this refactor — late agent-write
                # broadcast from tex_skip_preview navigating
                # inline_edit_roundtrip's page to the wrong doc).
                #
                # Cost: ~0.5-1s context-setup per scenario, ~25 scenarios.
                # Worth it for deterministic CI.
                def run_scenario(fn) -> None:
                    context = browser.new_context()
                    try:
                        page = context.new_page()
                        # Bootstrap navigation. Several scenarios implicitly
                        # assumed the page was already at base (relative-URL
                        # fetch calls etc.) — that assumption was hidden by
                        # the shared-page setup. With fresh contexts, the
                        # page starts at about:blank. Doing the goto here
                        # once removes the assumption without forcing every
                        # scenario to duplicate the boilerplate. Scenarios
                        # that want a different starting URL just call
                        # page.goto themselves (idempotent).
                        page.goto(base + "/")
                        fn(page, base, results)
                    finally:
                        context.close()

                run_scenario(scenario_http_routes)
                run_scenario(scenario_iframe_isolation)
                run_scenario(scenario_history_undo_reset)
                run_scenario(scenario_cross_origin_rejected)
                run_scenario(scenario_iframe_runtime)
                run_scenario(scenario_data_figure_renders)
                run_scenario(scenario_diagram_renders)
                run_scenario(scenario_structural_block_robustness)
                run_scenario(scenario_html_block_structured_content)
                run_scenario(scenario_agent_skill_hidden)
                run_scenario(scenario_add_doc_skill)
                run_scenario(scenario_doc_skill_crud)
                # scenario_music_upload was previously removed at the harness
                # level due to cross-scenario state-bleed. With per-scenario
                # context isolation that root cause is gone — left out for
                # now to avoid CDN load on every smoke run, not because of
                # the original race. Still covered via tests/test_music_upload.py.
                run_scenario(scenario_pending_review)
                run_scenario(scenario_pending_cleared_on_restore)
                run_scenario(scenario_text_selection)
                run_scenario(scenario_insertion_point)
                run_scenario(scenario_nested_selection_dedup)
                run_scenario(scenario_asset_drop)
                run_scenario(scenario_pdf_import)
                run_scenario(scenario_tex_skip_preview)
                run_scenario(scenario_serve_normalizes)
                run_scenario(scenario_inline_source_edit)
                run_scenario(scenario_render_invariant)
                run_scenario(scenario_block_ops_all_types)
                run_scenario(scenario_cancel_command)
                run_scenario(scenario_slash_commands)
                run_scenario(scenario_export)
                run_scenario(scenario_reset_confirm_dialog)
                run_scenario(scenario_drop_preview_dialog)
            finally:
                browser.close()

    print("\n" + "=" * 60)
    print(f"{len(results.passed)} passed, {len(results.failed)} failed")
    if results.failed:
        print("\nFailures:")
        for label, detail in results.failed:
            print(f"  - {label}")
            if detail:
                print(f"      {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
