"""Drive haiku through a single chat turn against the live backend, capture
all WebSocket events to a transcript, and let the post-tool-use debug
capture record every attempted Edit body.

Usage:
    python tests/drive_haiku.py "Add a dark-mode toggle to the top-right of the page."

It spawns the backend with AM_DEBUG_CAPTURE=1, opens a WS, sends the chat,
streams events to stdout until turn_done, and dumps a JSON transcript
to tests/_haiku_capture/transcript-{ts}.json. The Edit bodies and any
validator errors land alongside it under tests/_haiku_capture/."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8198
DEFAULT_DOC = "intro"  # slug


def pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
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


async def drive(port: int, prompt: str, doc: str,
                insertion_before: str | None = None,
                insertion_after: str | None = None) -> dict:
    import websockets
    url = f"ws://127.0.0.1:{port}/ws"
    transcript: list[dict] = []
    started = time.time()

    async with websockets.connect(url) as ws:
        # Wait for ready
        ready = json.loads(await ws.recv())
        transcript.append({"ts": time.time() - started, "in": ready})
        if ready.get("type") != "ready":
            return {"transcript": transcript, "error": "no ready"}

        context = {"doc": doc, "selections": []}
        if insertion_before or insertion_after:
            ins: dict = {}
            if insertion_before:
                ins["before"] = {
                    "label": insertion_before[:60],
                    "excerpt": insertion_before,
                }
            else:
                ins["before"] = None
            if insertion_after:
                ins["after"] = {
                    "label": insertion_after[:60],
                    "excerpt": insertion_after,
                }
            else:
                ins["after"] = None
            context["insertion"] = ins
        payload = {"type": "chat", "text": prompt, "context": context}
        await ws.send(json.dumps(payload))
        transcript.append({
            "ts": time.time() - started, "out": payload,
        })

        # Stream until turn_done or hard timeout
        hard_deadline = time.time() + 180  # 3 min cap
        while time.time() < hard_deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=45.0)
            except asyncio.TimeoutError:
                transcript.append({
                    "ts": time.time() - started,
                    "note": "recv timeout — no event in 10s; bailing",
                })
                break
            data = json.loads(raw)
            transcript.append({"ts": time.time() - started, "in": data})
            # Echo a compact summary to stdout so we see live progress
            kind = data.get("type", "?")
            role = data.get("role", "")
            tag = f"{role}/{kind}" if role else kind
            if kind == "tool_use":
                name = data.get("name", "?")
                inp = data.get("input", {}) or {}
                fp = inp.get("file_path") or inp.get("pattern") or ""
                print(f"  [{tag}] {name} {fp}", flush=True)
            elif kind == "text":
                t = (data.get("text") or "").strip().replace("\n", "  ")
                print(f"  [{tag}] {t[:160]}", flush=True)
            elif kind == "error":
                t = data.get("text", "")
                print(f"  [{tag}] {t[:200]}", flush=True)
            elif kind == "turn_done":
                print(f"  [turn_done]", flush=True)
                break
            else:
                print(f"  [{tag}]", flush=True)

    return {"transcript": transcript, "elapsed": time.time() - started}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "prompt", nargs="?",
        default="Add a dark-mode toggle to the top-right of the page.",
    )
    ap.add_argument("--doc", default=DEFAULT_DOC)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument(
        "--insertion-before",
        help="Inject an insertion-point with this excerpt as the block before",
    )
    ap.add_argument(
        "--insertion-after",
        help="Inject an insertion-point with this excerpt as the block after",
    )
    ap.add_argument(
        "--keep-running", action="store_true",
        help="leave backend running after the turn (for follow-up debugging)",
    )
    args = ap.parse_args()
    port = args.port or pick_free_port()

    cap_dir = ROOT / "tests" / "_haiku_capture"
    cap_dir.mkdir(parents=True, exist_ok=True)
    # Wipe stale captures so this run is the only thing in the dir.
    for f in cap_dir.iterdir():
        if f.is_file():
            f.unlink()

    env = os.environ.copy()
    env["AM_DEBUG_CAPTURE"] = "1"
    env["PORT"] = str(port)
    # Force the same UTF-8 respawn path start.py would use.
    env["PYTHONUTF8"] = "1"

    print(f"[drive] starting backend on :{port} with capture", flush=True)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "start.py"), "--claude", f"--port={port}"],
        env=env, cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    # Background-print backend output so we see [validate]/[tool] lines live
    import threading

    def pump():
        for line in proc.stdout:
            print(f"[backend] {line.rstrip()}", flush=True)
    threading.Thread(target=pump, daemon=True).start()

    try:
        if not wait_for_backend(port):
            print("[drive] backend never came up; aborting", flush=True)
            return 1
        print(f"[drive] sending: {args.prompt!r}", flush=True)
        result = asyncio.run(drive(
            port, args.prompt, args.doc,
            insertion_before=args.insertion_before,
            insertion_after=args.insertion_after,
        ))
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out = cap_dir / f"transcript-{ts}.json"
        out.write_text(
            json.dumps(result, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"[drive] transcript written to {out}", flush=True)
        # Summarise captures
        edits = sorted(cap_dir.glob("*.md"))
        fails = [p for p in edits if p.stem.endswith("FAIL")]
        oks = [p for p in edits if p.stem.endswith("OK")]
        print(
            f"[drive] captured {len(edits)} edit attempts "
            f"({len(fails)} failed, {len(oks)} passed validation)",
            flush=True,
        )
        for p in fails:
            err_p = p.with_suffix(".errors.txt")
            err_text = err_p.read_text("utf-8") if err_p.exists() else "?"
            print(f"  FAIL {p.name} :: {err_text.splitlines()[0] if err_text else ''}", flush=True)
        return 0
    finally:
        if not args.keep_running:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
