"""One-shot demo: synth a moderately-structured PDF, drop it on a fresh
backend, screenshot the rendered AM view so we can see what markitdown's
output actually looks like in the viewer.

Run:
    python tests/manual_pdf_screenshot.py
Output:
    screenshots/pdf-demo.png (the rendered view)
    screenshots/pdf-demo.md  (the converted markdown, for inspection)
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCREENSHOT_DIR = ROOT / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)
PORT = 8224


def make_test_pdf() -> bytes:
    """Build a small article-style PDF: title, intro, headings, list, math."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
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
    styles = getSampleStyleSheet()
    h1 = styles["Heading1"]
    h2 = styles["Heading2"]
    body = styles["BodyText"]

    story = [
        Paragraph("On the Quiet Math of Cellular Automata", styles["Title"]),
        Spacer(1, 12),
        Paragraph(
            "This is a synthetic test document used to verify the PDF import "
            "pipeline in adaptive-markdown. The content is fake, but it has "
            "structure: a title, two-level headings, a bulleted list, and a "
            "concluding paragraph with inline math-ish notation.",
            body,
        ),
        Spacer(1, 12),
        Paragraph("1. Introduction", h1),
        Paragraph(
            "Cellular automata are deterministic systems on a discrete grid. "
            "Each cell holds a state from a small alphabet, and the global "
            "state evolves under a local update rule. The classical example "
            "is Conway's Game of Life, with alphabet {0, 1} and a rule that "
            "depends only on a cell's eight neighbors.",
            body,
        ),
        Spacer(1, 8),
        Paragraph("1.1 Why this matters", h2),
        Paragraph(
            "Despite being trivial to specify, cellular automata can simulate "
            "Turing machines. This places the long-run behavior of even very "
            "small rule sets beyond decidable analysis. We are stuck observing "
            "individual trajectories.",
            body,
        ),
        Spacer(1, 12),
        Paragraph("2. Three rules worth knowing", h1),
        ListFlowable(
            [
                ListItem(Paragraph(
                    "<b>Rule 30</b>: chaotic from a single live cell; used as "
                    "a pseudo-random source in early Mathematica.", body)),
                ListItem(Paragraph(
                    "<b>Rule 90</b>: produces Sierpinski-triangle patterns; "
                    "linear over GF(2).", body)),
                ListItem(Paragraph(
                    "<b>Rule 110</b>: proven Turing-complete by Cook in 2004.",
                    body)),
            ],
            bulletType="bullet",
        ),
        Spacer(1, 12),
        Paragraph("3. A closing note", h1),
        Paragraph(
            "If the state at time t is x(t) in {0,1}^n, the next state is "
            "x(t+1) = f(x(t)) where f is determined by an 8-bit rule number. "
            "The map f is local, time-invariant, and parallel. That is the "
            "entire model.",
            body,
        ),
    ]
    doc.build(story)
    return buf.getvalue()


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

    # Use the default backend (Claude with markitdown fallback) — same
    # path the real user sees on a PDF drop.
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "backend.py"), str(PORT)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if not wait_for_backend(PORT):
        print("[demo] backend never came up")
        proc.kill()
        return 1
    print(f"[demo] backend up on :{PORT}")

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/")
            page.wait_for_function(
                "() => document.getElementById('doc-select')?.value",
                timeout=10000,
            )

            # Upload via the same path the UI uses.
            import base64
            b64 = base64.b64encode(pdf_bytes).decode("ascii")
            result = page.evaluate(
                """async (b64) => {
                    const bin = atob(b64);
                    const bytes = new Uint8Array(bin.length);
                    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                    const blob = new Blob([bytes], { type: 'application/pdf' });
                    const form = new FormData();
                    form.append('file', blob, 'cellular-automata.pdf');
                    const r = await fetch('/upload', { method: 'POST', body: form });
                    return { status: r.status, body: await r.json() };
                }""",
                b64,
            )
            print(f"[demo] upload result: {result}")
            slug = result.get("body", {}).get("slug")
            if not slug:
                print("[demo] no slug in upload response; aborting")
                return 2

            # Wait for the viewer to switch to the new doc.
            page.wait_for_function(
                f"() => document.getElementById('doc-select')?.value === '{slug}'",
                timeout=10000,
            )
            # Give morphdom + KaTeX a tick to settle.
            page.wait_for_timeout(800)

            shot = SCREENSHOT_DIR / "pdf-demo-integrated.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"[demo] screenshot -> {shot}")
            print(f"[demo] converter used: {result.get('body', {}).get('converter')}")

            # Also save the raw converted markdown so we can read it.
            converted = (ROOT / "docs" / slug / "current.md").read_text(encoding="utf-8")
            (SCREENSHOT_DIR / "pdf-demo.md").write_text(converted, encoding="utf-8")
            print(f"[demo] markdown    -> {SCREENSHOT_DIR / 'pdf-demo.md'}")

            # Cleanup the synthetic doc so it doesn't clutter docs/.
            shutil.rmtree(ROOT / "docs" / slug, ignore_errors=True)

            browser.close()
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
