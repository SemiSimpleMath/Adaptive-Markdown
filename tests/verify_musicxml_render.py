"""Drop a realistic MusicXML through the full pipeline (upload → iframe
render → OSMD) and report what actually happens. Logs:
  - what the backend wrote to current.md
  - what the iframe's script tag has as textContent (i.e. what OSMD sees)
  - whether OSMD rendered or threw
  - the actual OSMD error if any
"""
from __future__ import annotations
import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8304

# A small but real-world-shaped MusicXML — multiple measures, two voices,
# notes with attributes, the kind of structure OSMD needs to find.
SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 3.1 Partwise//EN" "http://www.musicxml.org/dtds/partwise.dtd">
<score-partwise version="3.1">
  <work>
    <work-title>Realistic Test</work-title>
  </work>
  <identification>
    <creator type="composer">Test Composer</creator>
    <encoding>
      <software>Test</software>
    </encoding>
  </identification>
  <part-list>
    <score-part id="P1">
      <part-name>Piano</part-name>
    </score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>4</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <clef><sign>G</sign><line>2</line></clef>
      </attributes>
      <note>
        <pitch><step>C</step><octave>4</octave></pitch>
        <duration>4</duration>
        <type>quarter</type>
      </note>
      <note>
        <pitch><step>D</step><octave>4</octave></pitch>
        <duration>4</duration>
        <type>quarter</type>
      </note>
      <note>
        <pitch><step>E</step><octave>4</octave></pitch>
        <duration>4</duration>
        <type>quarter</type>
      </note>
      <note>
        <pitch><step>F</step><octave>4</octave></pitch>
        <duration>4</duration>
        <type>quarter</type>
      </note>
    </measure>
    <measure number="2">
      <note>
        <pitch><step>G</step><octave>4</octave></pitch>
        <duration>8</duration>
        <type>half</type>
      </note>
      <note>
        <pitch><step>G</step><octave>4</octave></pitch>
        <duration>8</duration>
        <type>half</type>
      </note>
    </measure>
  </part>
</score-partwise>
"""


def wait_for_backend(port, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def post_upload(base, filename, body, content_type):
    boundary = "----verifymusicxml"
    wire = (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
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
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main():
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "backend.py"), str(PORT)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if not wait_for_backend(PORT):
        proc.kill()
        return 1
    try:
        base = f"http://127.0.0.1:{PORT}"
        status, body = post_upload(
            base, "verify-xml.musicxml",
            SAMPLE_XML.encode("utf-8"),
            "application/vnd.recordare.musicxml+xml",
        )
        slug = body.get("slug")
        print(f"upload status: {status}, slug: {slug}")

        doc_path = ROOT / "docs" / slug / "current.md"
        on_disk = doc_path.read_text(encoding="utf-8")
        print("\n=== on-disk current.md (first 1500 chars) ===")
        print(on_disk[:1500])
        print("=== end ===\n")

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            iframe_errors = []
            page.on("console", lambda m: iframe_errors.append(
                f"[{m.type}] {m.text}") if m.type in ("error", "warning") else None)

            page.goto(f"http://127.0.0.1:{PORT}/?doc={slug}")
            page.wait_for_function(
                f"() => document.getElementById('doc-select')?.value === '{slug}'",
                timeout=10000,
            )
            page.wait_for_timeout(2000)  # give renderMusicBlocks time

            iframe = None
            for f in page.frames:
                if f.parent_frame is page.main_frame:
                    iframe = f
                    break
            if iframe is None:
                print("no iframe?")
                return 1

            # Probe what OSMD actually sees.
            script_txt = iframe.evaluate(
                "() => {"
                "  const s = document.querySelector('figure.music script[type^=\"application/vnd.recordare\"]');"
                "  return s ? s.textContent : null;"
                "}"
            )
            print(f"\n=== script tag textContent (first 500 chars) ===")
            print((script_txt or "")[:500])
            print(f"=== textContent length: {len(script_txt or '')} ===\n")

            render_status = iframe.evaluate(
                "() => {"
                "  const t = document.querySelector('.musicxml-render');"
                "  if (!t) return { rendered: false, reason: 'no .musicxml-render div' };"
                "  const err = t.querySelector('.error-banner');"
                "  if (err) return { rendered: false, reason: err.textContent };"
                "  const svg = t.querySelector('svg');"
                "  return { rendered: !!svg, reason: svg ? 'rendered svg present' : 'no svg in target' };"
                "}"
            )
            print(f"render status: {render_status}")

            # Try OSMD ourselves on the same string to see what error it gives.
            osmd_test = iframe.evaluate(
                """async (xml) => {
                    if (!window.opensheetmusicdisplay) return 'OSMD not loaded';
                    const div = document.createElement('div');
                    document.body.appendChild(div);
                    try {
                        const osmd = new window.opensheetmusicdisplay.OpenSheetMusicDisplay(div);
                        await osmd.load(xml);
                        osmd.render();
                        return 'success';
                    } catch (e) {
                        return 'failed: ' + (e.message || e);
                    }
                }""",
                SAMPLE_XML,
            )
            print(f"osmd direct test on original XML: {osmd_test}")

            print(f"\nconsole errors/warnings ({len(iframe_errors)}):")
            for m in iframe_errors:
                print(f"  {m}")

            shot = ROOT / "screenshots" / "musicxml-debug.png"
            shot.parent.mkdir(exist_ok=True)
            page.screenshot(path=str(shot), full_page=True)
            print(f"\nscreenshot -> {shot}")

            browser.close()

        # Cleanup
        shutil.rmtree(ROOT / "docs" / slug, ignore_errors=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
