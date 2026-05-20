"""One-shot: same test PDF, but converted via Claude vision (direct
Anthropic API call) instead of markitdown. Renders the result in the AM
viewer and screenshots it so we can compare against pdf-demo.png.

Output:
    screenshots/pdf-demo-claude.png
    screenshots/pdf-demo-claude.md
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCREENSHOT_DIR = ROOT / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)
PORT = 8225
SLUG = "cellular-claude"


def make_test_pdf() -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem,
    )
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=1 * inch, rightMargin=1 * inch,
        topMargin=1 * inch, bottomMargin=1 * inch,
    )
    s = getSampleStyleSheet()
    story = [
        Paragraph("On the Quiet Math of Cellular Automata", s["Title"]),
        Spacer(1, 12),
        Paragraph(
            "This is a synthetic test document used to verify the PDF "
            "import pipeline in adaptive-markdown. The content is fake, "
            "but it has structure: a title, two-level headings, a bulleted "
            "list, and a concluding paragraph with inline math-ish notation.",
            s["BodyText"],
        ),
        Spacer(1, 12),
        Paragraph("1. Introduction", s["Heading1"]),
        Paragraph(
            "Cellular automata are deterministic systems on a discrete grid. "
            "Each cell holds a state from a small alphabet, and the global "
            "state evolves under a local update rule. The classical example "
            "is Conway's Game of Life, with alphabet {0, 1} and a rule that "
            "depends only on a cell's eight neighbors.",
            s["BodyText"],
        ),
        Spacer(1, 8),
        Paragraph("1.1 Why this matters", s["Heading2"]),
        Paragraph(
            "Despite being trivial to specify, cellular automata can simulate "
            "Turing machines. This places the long-run behavior of even very "
            "small rule sets beyond decidable analysis. We are stuck observing "
            "individual trajectories.",
            s["BodyText"],
        ),
        Spacer(1, 12),
        Paragraph("2. Three rules worth knowing", s["Heading1"]),
        ListFlowable(
            [
                ListItem(Paragraph("<b>Rule 30</b>: chaotic from a single live cell; used as a pseudo-random source in early Mathematica.", s["BodyText"])),
                ListItem(Paragraph("<b>Rule 90</b>: produces Sierpinski-triangle patterns; linear over GF(2).", s["BodyText"])),
                ListItem(Paragraph("<b>Rule 110</b>: proven Turing-complete by Cook in 2004.", s["BodyText"])),
            ],
            bulletType="bullet",
        ),
        Spacer(1, 12),
        Paragraph("3. A closing note", s["Heading1"]),
        Paragraph(
            "If the state at time t is x(t) in {0,1}^n, the next state is "
            "x(t+1) = f(x(t)) where f is determined by an 8-bit rule number. "
            "The map f is local, time-invariant, and parallel. That is the "
            "entire model.",
            s["BodyText"],
        ),
    ]
    doc.build(story)
    return buf.getvalue()


CONVERSION_PROMPT = """\
Convert this PDF to adaptive-markdown. Strict rules:

- Use `#` for the document title, `##` for top-level sections, `###` for subsections.
- Use proper markdown bullet lists (`- `) for any list of items in the source.
- Preserve inline emphasis: bold becomes `**`, italic becomes `_`.
- Preserve math: inline as `$...$`, display as `$$...$$`. If the source has
  expressions like `x(t+1) = f(x(t))`, render them as math.
- Do NOT add commentary, summary, or any preamble. Output ONLY the markdown body.
- Drop nothing from the source; preserve all paragraphs verbatim where possible.
"""


def call_claude(pdf_bytes: bytes) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY not set")

    body = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 8192,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.b64encode(pdf_bytes).decode("ascii"),
                    },
                },
                {"type": "text", "text": CONVERSION_PROMPT},
            ],
        }],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Claude API error {e.code}: {err_body}")
    parts = payload.get("content", [])
    text_parts = [p.get("text", "") for p in parts if p.get("type") == "text"]
    return "\n".join(text_parts).strip() + "\n"


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
    pdf_bytes = make_test_pdf()
    print(f"[demo] generated test PDF ({len(pdf_bytes)} bytes)")

    print("[demo] sending to Claude (sonnet)…")
    t0 = time.time()
    md = call_claude(pdf_bytes)
    print(f"[demo] got {len(md)} chars in {time.time()-t0:.1f}s")

    doc_dir = ROOT / "docs" / SLUG
    if doc_dir.exists():
        shutil.rmtree(doc_dir)
    doc_dir.mkdir(parents=True)
    (doc_dir / "current.md").write_text(md, encoding="utf-8", newline="")
    (doc_dir / "baseline.md").write_text(md, encoding="utf-8", newline="")
    (SCREENSHOT_DIR / "pdf-demo-claude.md").write_text(md, encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "backend.py"), str(PORT)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if not wait_for_backend(PORT):
        print("[demo] backend never came up")
        proc.kill()
        return 1

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
            page.wait_for_timeout(1200)
            shot = SCREENSHOT_DIR / "pdf-demo-claude.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"[demo] screenshot -> {shot}")
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    # Cleanup the synthetic doc so docs/ stays clean
    shutil.rmtree(doc_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
