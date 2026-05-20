"""Backend assertions for the music-upload path.

Lives outside the playwright smoke harness because uploading a music
doc triggers a docs broadcast that the long-lived smoke page reacts to
— the page switches to the new doc, the iframe lazy-loads abcjs from
CDN, and that race interferes with the next scenario's WebSocket
handshake on Windows. This script spawns its own short-lived backend
and verifies the upload contract via urllib only; no browser involved.

Run: python tests/test_music_upload.py
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
PORT = 8295


def wait_for_backend(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def post_upload(base: str, filename: str, body: bytes, content_type: str):
    boundary = "----testmusicboundary"
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


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "backend.py"), str(PORT)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if not wait_for_backend(PORT):
        proc.kill()
        print("backend never came up")
        return 1

    base = f"http://127.0.0.1:{PORT}"
    fails: list[str] = []

    def check(label, ok, detail=""):
        marker = "PASS" if ok else "FAIL"
        print(f"  {marker}  {label}" + (f"  ({detail[:200]})" if detail else ""))
        if not ok:
            fails.append(label)

    try:
        # ABC
        abc_source = (
            "X:1\nT:Smoke Test\nM:4/4\nL:1/4\nK:C\n"
            "C D E F | G A B c |\n"
        )
        status, body = post_upload(
            base, "smoke-abc.abc",
            abc_source.encode("utf-8"), "text/vnd.abc",
        )
        check("POST /upload accepts .abc and returns 200",
              status == 200, str(body))
        abc_slug = (body or {}).get("slug", "")
        check("ABC response carries converter='music' + slug",
              body.get("converter") == "music" and bool(abc_slug),
              str(body))
        abc_dir = ROOT / "docs" / abc_slug
        abc_md = (abc_dir / "current.md").read_text(encoding="utf-8")
        check(
            "ABC doc wraps source in figure.music > div.abc",
            '<figure class="music">' in abc_md
            and '<div class="abc">' in abc_md
            and "T:Smoke Test" in abc_md,
            abc_md[:300],
        )
        shutil.rmtree(abc_dir, ignore_errors=True)

        # MusicXML
        musicxml_source = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<score-partwise version="3.1">\n'
            '  <part-list><score-part id="P1"><part-name>M</part-name></score-part></part-list>\n'
            '  <part id="P1"><measure number="1">\n'
            '    <note><pitch><step>C</step><octave>4</octave></pitch>'
            '<duration>4</duration><type>whole</type></note>\n'
            '  </measure></part>\n'
            '</score-partwise>\n'
        )
        status, body = post_upload(
            base, "smoke-xml.musicxml",
            musicxml_source.encode("utf-8"),
            "application/vnd.recordare.musicxml+xml",
        )
        check("POST /upload accepts .musicxml and returns 200",
              status == 200, str(body))
        xml_slug = (body or {}).get("slug", "")
        xml_dir = ROOT / "docs" / xml_slug
        xml_md = (xml_dir / "current.md").read_text(encoding="utf-8")
        check(
            "MusicXML doc wraps source in figure.music > "
            "script[type='application/vnd.recordare.musicxml+xml']",
            'type="application/vnd.recordare.musicxml+xml"' in xml_md
            and "score-partwise" in xml_md,
            xml_md[:300],
        )
        shutil.rmtree(xml_dir, ignore_errors=True)

        # MIDI
        midi_bytes = (
            b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60"
            b"MTrk\x00\x00\x00\x0b"
            b"\x00\xc0\x00"
            b"\x00\x90\x3c\x40"
            b"\x60\x80\x3c\x40"
            b"\x00\xff\x2f\x00"
        )
        status, body = post_upload(
            base, "smoke-mid.mid", midi_bytes, "audio/midi",
        )
        check("POST /upload accepts .mid and returns 200",
              status == 200, str(body))
        mid_slug = (body or {}).get("slug", "")
        mid_dir = ROOT / "docs" / mid_slug
        mid_md = (mid_dir / "current.md").read_text(encoding="utf-8")
        check(
            "MIDI doc carries <midi-player src='assets/...'>",
            "<midi-player" in mid_md and "assets/" in mid_md,
            mid_md[:300],
        )
        check(
            "MIDI bytes landed in docs/<slug>/assets/",
            (mid_dir / "assets").exists()
            and any((mid_dir / "assets").iterdir()),
        )
        shutil.rmtree(mid_dir, ignore_errors=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"\n{len(fails)} fail / done")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
