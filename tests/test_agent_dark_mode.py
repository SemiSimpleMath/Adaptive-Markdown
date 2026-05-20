"""Real-API regression: drive Haiku to add a dark-mode toggle to intro,
verify the rendered iframe has a working button.

Burns API tokens (~$0.01 per run on Haiku). Skips if ANTHROPIC_API_KEY
is unset.

What this catches:

  - Agent writes `<\\/script>` (escaped) as the closing tag instead of
    `</script>` plain — script never terminates, body extends past EOF,
    invalid JS, button never created. The validator currently lets
    this slip through because am_html.find_blocks returns no block for
    unmatched opens. Without this test, the regression is invisible
    until a user clicks "add dark mode toggle" in the live viewer.

  - Edit lands, validator passes, but the rendered button is hidden /
    default-styled / wrong-positioned (CSS specificity, sentinel
    leakage, etc.). All of those are visible only in the rendered DOM.

Run as:
    python tests/test_agent_dark_mode.py

Exit code 0 on success, 1 on any failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PYTHONUTF8", "1")

PROMPT = (
    "Add a dark-mode toggle button to the top of the page. Use vanilla JS, "
    "no external dependencies. Persist the preference in localStorage. "
    "Make the button fixed-position top-right so it's always visible."
)


def pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close()
    return p


def wait_for_backend(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def restore_intro_baseline() -> None:
    """Reset docs/intro/current.md from baseline so the agent starts clean."""
    baseline = ROOT / "docs" / "intro" / "baseline.md"
    current = ROOT / "docs" / "intro" / "current.md"
    if not baseline.exists():
        raise RuntimeError("docs/intro/baseline.md missing — can't reset")
    shutil.copy(baseline, current)
    # Also wipe any pending sidecar to avoid stale review-mode state
    pending = ROOT / "docs" / "intro" / "pending.json"
    pending.unlink(missing_ok=True)


async def drive_chat(port: int, prompt: str, timeout_s: float = 180.0) -> dict:
    import websockets
    url = f"ws://127.0.0.1:{port}/ws"
    started = time.time()
    transcript: list[dict] = []
    async with websockets.connect(url) as ws:
        ready = json.loads(await ws.recv())
        transcript.append({"ts": 0, "in": ready})
        if ready.get("type") != "ready":
            return {"transcript": transcript, "error": "no ready"}
        payload = {
            "type": "chat", "text": prompt,
            "context": {"doc": "intro", "selections": []},
        }
        await ws.send(json.dumps(payload))
        transcript.append({"ts": time.time() - started, "out": payload})
        hard_deadline = time.time() + timeout_s
        cost = None
        while time.time() < hard_deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=45.0)
            except asyncio.TimeoutError:
                transcript.append({"ts": time.time() - started,
                                   "note": "recv timeout"})
                break
            data = json.loads(raw)
            transcript.append({"ts": time.time() - started, "in": data})
            kind = data.get("type")
            if kind == "tool_use":
                name = data.get("name") or "?"
                print(f"  [tool] {name}", flush=True)
            elif kind == "turn_done":
                cost = data.get("cost_usd")
                break
            elif kind == "error":
                print(f"  [ERR] {data.get('text','')[:200]}", flush=True)
    return {"transcript": transcript, "cost_usd": cost,
            "elapsed_s": time.time() - started}


def inspect_source() -> dict:
    """Inspect docs/intro/current.md for the dark-mode toggle the agent
    added. Returns flags useful for assertions."""
    src = (ROOT / "docs" / "intro" / "current.md").read_text(encoding="utf-8")
    lower = src.lower()
    raw_open = lower.count("<script")
    raw_close = lower.count("</script")
    return {
        "len": len(src),
        "script_open_tokens": raw_open,
        "script_close_tokens": raw_close,
        "has_escaped_close": "<\\/script>" in src,
        "has_plain_close": "</script>" in src,
        "has_dm_toggle_id": "dark-mode-toggle" in src or "dark-mode" in src.lower(),
    }


def inspect_render(port: int) -> dict:
    """Use playwright to load the doc, see if the button rendered."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_context(viewport={"width": 1400, "height": 900}).new_page()
        errs = []
        page.on("console", lambda m: errs.append(f"[{m.type}] {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: errs.append(f"[pageerror] {e}"))
        page.goto(f"http://127.0.0.1:{port}/?doc=intro",
                  wait_until="networkidle", timeout=15000)
        frame = None
        for f in page.frames:
            if "iframe-host" in (f.url or ""):
                frame = f; break
        if not frame:
            browser.close()
            return {"error": "no iframe"}
        # Wait briefly for the agent's script to execute
        time.sleep(2)
        info = frame.evaluate("""() => {
            // Look for ANY button the agent might have added — common
            // ids/classes haiku tends to use
            const candidates = [
                '#dark-mode-toggle', '#darkmode-toggle', '#dm-toggle',
                'button[aria-label*="dark"]', 'button[aria-label*="theme"]',
            ];
            for (const sel of candidates) {
                const el = document.querySelector(sel);
                if (el) {
                    const cs = getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    return {
                        found: sel,
                        position: cs.position,
                        visible: r.width > 0 && r.height > 0,
                        top: r.top, right: window.innerWidth - r.right,
                        display: cs.display, visibility: cs.visibility,
                    };
                }
            }
            // Catchall: any button under <article>?
            const anyBtn = document.querySelector('article button');
            if (anyBtn) {
                const r = anyBtn.getBoundingClientRect();
                return {
                    found: 'article button (fallback)',
                    id: anyBtn.id || null,
                    text: anyBtn.textContent.trim().slice(0, 40),
                    visible: r.width > 0 && r.height > 0,
                };
            }
            return { found: null };
        }""")
        page.screenshot(path=str(ROOT / "screenshots" / "test-dark-mode.png"))
        browser.close()
    return {"button": info, "console_errors": errs}


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("SKIP: ANTHROPIC_API_KEY not set")
        return 0

    print("Resetting docs/intro/current.md from baseline…")
    restore_intro_baseline()

    port = pick_free_port()
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["MODEL"] = env.get("MODEL", "claude-haiku-4-5-20251001")
    print(f"Starting backend on port {port} (model: {env['MODEL']})…")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "backend.py"), str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        if not wait_for_backend(port):
            out, _ = proc.communicate(timeout=2)
            print("backend never became ready:\n" + (out or ""))
            return 1
        print(f"Driving chat: {PROMPT!r}")
        chat = asyncio.run(drive_chat(port, PROMPT))
        cost = chat.get("cost_usd")
        elapsed = chat.get("elapsed_s")
        print(f"chat done in {elapsed:.1f}s, cost ${cost}")

        print("\n--- Source inspection ---")
        src = inspect_source()
        for k, v in src.items():
            print(f"  {k}: {v}")

        print("\n--- Render inspection ---")
        render = inspect_render(port)
        for k, v in render.items():
            if k == "console_errors":
                print(f"  console_errors ({len(v)}):")
                for e in v[:5]: print(f"    {e[:160]}")
            else:
                print(f"  {k}: {v}")

        # Assertions
        failures = []
        if src["script_open_tokens"] != src["script_close_tokens"]:
            failures.append(
                f"script open/close token count mismatch: "
                f"{src['script_open_tokens']} opens vs "
                f"{src['script_close_tokens']} closes "
                f"(escaped close present: {src['has_escaped_close']})"
            )
        if src["has_escaped_close"]:
            failures.append(
                "agent wrote `<\\/script>` (escaped close) — that's "
                "wrong; closing tags should be `</script>` plain"
            )
        btn = render.get("button", {})
        if not btn.get("found"):
            failures.append("no button found in rendered DOM")
        elif not btn.get("visible"):
            failures.append(f"button found ({btn.get('found')}) but not visible")
        elif btn.get("position") != "fixed":
            failures.append(
                f"button position is {btn.get('position')!r} not 'fixed' — "
                "user asked for fixed top-right; CSS may not have applied"
            )

        if failures:
            print(f"\n{len(failures)} FAILURE(s):")
            for f in failures: print(f"  ✗ {f}")
            return 1
        print("\nALL OK")
        return 0
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
