"""Smoke-verify that a YouTube embed actually renders inside the
cross-origin AM iframe — the whole point of this architectural change.

Creates a temporary doc with a YouTube iframe, opens it in the viewer,
waits for the YouTube player to fully load, screenshots, and checks
that the player's controls are present in the nested-iframe DOM.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8230
SLUG = "yt-embed-probe"
DOC_DIR = ROOT / "docs" / SLUG

# Big Buck Bunny — short, public-domain-ish, reliably available
TEST_VIDEO_ID = "aqz-KE-bpKQ"

DOC_BODY = f"""---
title: YouTube embed verification
audience: test
---

# YouTube embed cross-origin check

This doc embeds a YouTube iframe. With the AM iframe sandboxed but loaded
from a different origin than the viewer, `allow-same-origin` lets the
YouTube player function.

<figure>
<iframe id="probe-yt" width="560" height="315" src="https://www.youtube.com/embed/{TEST_VIDEO_ID}" title="YouTube video player" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" allowfullscreen></iframe>
<figcaption>Test video.</figcaption>
</figure>
"""


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
    if DOC_DIR.exists():
        shutil.rmtree(DOC_DIR)
    DOC_DIR.mkdir(parents=True)
    (DOC_DIR / "current.md").write_text(DOC_BODY, encoding="utf-8", newline="")
    (DOC_DIR / "baseline.md").write_text(DOC_BODY, encoding="utf-8", newline="")

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "backend.py"), str(PORT)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if not wait_for_backend(PORT):
        print("backend never came up")
        proc.kill()
        return 1

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
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/?doc={SLUG}")
            page.wait_for_function(
                f"() => document.getElementById('doc-select')?.value === '{SLUG}'",
                timeout=10000,
            )
            # Give the iframe + nested YouTube iframe time to load
            page.wait_for_timeout(5000)

            # Find the doc iframe
            am_iframe = None
            for f in page.frames:
                if f.parent_frame is page.main_frame:
                    am_iframe = f
                    break
            check("AM doc iframe is reachable", am_iframe is not None)
            if am_iframe is None:
                return 1

            # Verify YouTube iframe element exists inside AM iframe
            yt_present = am_iframe.evaluate(
                "() => !!document.getElementById('probe-yt')"
            )
            check("YouTube iframe element is in the doc DOM", yt_present)

            # Find the nested YouTube frame
            yt_frame = None
            for f in page.frames:
                if f.parent_frame is am_iframe and "youtube" in (f.url or "").lower():
                    yt_frame = f
                    break
            check("Nested YouTube frame loaded with youtube URL", yt_frame is not None,
                  detail=(yt_frame.url if yt_frame else "(none)"))

            if yt_frame is not None:
                # Wait for the player chrome to mount. We don't insist on
                # playback (headless Chromium often blocks autoplay) — we
                # just need evidence the player is alive: a <video> element,
                # the html5 player container, and a non-zero body size.
                try:
                    yt_frame.wait_for_selector(
                        "video, #player, #movie_player, .html5-video-player",
                        timeout=15000,
                    )
                    player_present = True
                except Exception:
                    player_present = False
                check(
                    "YouTube player container rendered in nested iframe",
                    player_present,
                )

                player_info = yt_frame.evaluate(
                    "() => { const v = document.querySelector('video'); "
                    "const p = document.querySelector('#movie_player, #player'); "
                    "const r = document.body.getBoundingClientRect(); "
                    "return { hasVideo: !!v, hasPlayer: !!p, bodyW: r.width, bodyH: r.height }; }"
                )
                check(
                    "YouTube nested iframe has a <video> element",
                    bool(player_info.get("hasVideo")),
                    detail=str(player_info),
                )
                check(
                    "YouTube nested iframe has non-zero body size (it rendered)",
                    player_info.get("bodyW", 0) > 100 and player_info.get("bodyH", 0) > 100,
                    detail=str(player_info),
                )

            screenshots = ROOT / "screenshots"
            screenshots.mkdir(exist_ok=True)
            shot = screenshots / "youtube-embed.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"\nScreenshot: {shot}")

            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(DOC_DIR, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} FAIL — YouTube embed not fully working")
        return 1
    print("\nALL PASS — YouTube embed renders correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
