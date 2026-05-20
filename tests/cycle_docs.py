"""Walk through every doc in docs/, capture a screenshot of the rendered
state, and log any console errors. Diagnostic harness for "does each doc
actually render after a runtime change?" — complements browser_smoke.py
which tests features, not full-doc rendering.

Usage:
    python tests/cycle_docs.py            # uses port 8095 (or first free)
    python tests/cycle_docs.py --port N   # explicit port

Writes:
    screenshots/cycle/<slug>.png          one screenshot per doc
    screenshots/cycle/_summary.txt        per-doc PASS/FAIL + first error
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

os.environ.setdefault("PYTHONUTF8", "1")

from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCS_ROOT = ROOT / "docs"
OUT = ROOT / "screenshots" / "cycle"


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def first_free_port(start: int) -> int:
    for p in range(start, start + 50):
        if port_is_free(p):
            return p
    raise RuntimeError("no free port found")


def wait_for_backend(port: int, timeout: float = 20.0) -> bool:
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
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["AM_PDF_BACKEND"] = "markitdown"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "backend.py"), str(port)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=env,
    )
    try:
        if not wait_for_backend(port):
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


def all_slugs() -> list[str]:
    """Every doc folder under docs/ with a current.md (or baseline.md)."""
    slugs: list[str] = []
    for sub in sorted(DOCS_ROOT.iterdir()):
        if not sub.is_dir():
            continue
        if (sub / "current.md").exists() or (sub / "baseline.md").exists():
            slugs.append(sub.name)
    return slugs


def cycle(base: str, slugs: list[str]) -> list[dict]:
    """For each slug: nav to it, wait for iframe ready, screenshot, log."""
    results: list[dict] = []
    OUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1400, "height": 900})

        for slug in slugs:
            page = context.new_page()
            console_msgs: list[dict] = []
            page_errors: list[str] = []
            page.on("console", lambda msg: console_msgs.append(
                {"type": msg.type, "text": msg.text}
            ))
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))

            print(f"\n[cycle] {slug}")
            entry: dict = {
                "slug": slug,
                "console_errors": [],
                "console_warnings": [],
                "page_errors": [],
                "render_ok": False,
                "screenshot": None,
                "notes": [],
            }
            try:
                page.goto(f"{base}/?doc={slug}", wait_until="networkidle",
                          timeout=15000)
                # Wait for iframe to become ready (the runtime sets
                # window.__doc on the iframe contentWindow).
                # Use a poll loop; some scenarios are slow on first run.
                deadline = time.time() + 10
                iframe_ready = False
                while time.time() < deadline:
                    try:
                        frame = page.frame(name=None, url="**/iframe-host")
                        if frame is None:
                            for f in page.frames:
                                if "iframe-host" in (f.url or ""):
                                    frame = f
                                    break
                        if frame and frame.evaluate("() => !!window.__doc"):
                            iframe_ready = True
                            break
                    except Exception:
                        pass
                    time.sleep(0.3)
                entry["render_ok"] = iframe_ready
                if not iframe_ready:
                    entry["notes"].append("iframe runtime never became ready")

                # Give renderers a moment to settle (music/data lazy-load CDN)
                time.sleep(1.5)

                shot = OUT / f"{slug}.png"
                page.screenshot(path=str(shot), full_page=False)
                entry["screenshot"] = str(shot.relative_to(ROOT))
                print(f"  screenshot: {entry['screenshot']}")
            except Exception as e:
                entry["notes"].append(f"navigation failed: {type(e).__name__}: {e}")
                print(f"  ! navigation failed: {e}")
            finally:
                entry["console_errors"] = [m["text"] for m in console_msgs if m["type"] == "error"]
                entry["console_warnings"] = [m["text"] for m in console_msgs if m["type"] == "warning"]
                entry["page_errors"] = page_errors
                for e in entry["console_errors"]:
                    print(f"  ERROR  {e[:200]}")
                for e in entry["page_errors"]:
                    print(f"  PAGEERR  {e[:200]}")
                results.append(entry)
                page.close()

        context.close()
        browser.close()

    return results


def write_summary(results: list[dict]) -> Path:
    p = OUT / "_summary.txt"
    lines = []
    lines.append(f"Cycled {len(results)} docs\n")
    ok = sum(1 for r in results if r["render_ok"] and not r["console_errors"] and not r["page_errors"])
    lines.append(f"{ok}/{len(results)} clean (rendered + no console errors)\n")
    lines.append("")
    for r in results:
        tag = "OK   " if r["render_ok"] and not r["console_errors"] and not r["page_errors"] else "FAIL "
        lines.append(f"{tag} {r['slug']}")
        lines.append(f"       screenshot: {r.get('screenshot') or '(none)'}")
        if r["notes"]:
            for n in r["notes"]:
                lines.append(f"       note:   {n}")
        for e in r["page_errors"]:
            lines.append(f"       pageerr: {e[:300]}")
        for e in r["console_errors"]:
            lines.append(f"       error:  {e[:300]}")
        # Show warnings only when there's no error, otherwise noise dominates
        if not r["console_errors"] and not r["page_errors"]:
            for w in r["console_warnings"][:3]:
                lines.append(f"       warn:   {w[:200]}")
        lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()
    port = args.port or first_free_port(8095)

    slugs = all_slugs()
    print(f"[cycle] {len(slugs)} docs: {', '.join(slugs)}")
    with backend_running(port):
        results = cycle(f"http://127.0.0.1:{port}", slugs)
    summary = write_summary(results)
    print(f"\nSummary written to {summary.relative_to(ROOT)}")
    fails = [r for r in results if not r["render_ok"] or r["console_errors"] or r["page_errors"]]
    print(f"{len(results) - len(fails)}/{len(results)} clean, {len(fails)} need attention")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
