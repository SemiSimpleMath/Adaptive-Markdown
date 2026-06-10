"""Environment bootstrap shared by the browser harnesses.

Two shell hazards (both seen on the dev box) break the harnesses before
they reach a single test:

  1. Wrong interpreter: `python` on PATH can resolve to an unrelated
     project's venv (no playwright / claude_agent_sdk), and PYTHONPATH can
     point at unrelated code that shadows imports. The harnesses spawn the
     backend with sys.executable, so a polluted interpreter fails deep
     inside backend startup instead of at the door.
  2. cp1252 console: scenario names print non-ASCII (arrows), which crashes
     `print` under the default Windows code page.

bootstrap() fixes both: it scrubs PYTHONPATH, forces UTF-8 output for this
process and every child, and — if the running interpreter can't import the
dep set — re-execs the harness under one that can: $AM_PYTHON first, then
every interpreter the Windows `py` launcher knows, then python3/python on
PATH. Set AM_PYTHON to pin an interpreter explicitly.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

# Everything the harness AND the backend it spawns must be able to import.
REQUIRED = ("playwright", "aiohttp", "claude_agent_sdk", "markdown_it")
_PROBE = "import " + ", ".join(REQUIRED)
_REEXEC_FLAG = "_AM_HARNESS_REEXECED"  # guards against a re-exec loop


def _deps_ok(python: str) -> bool:
    try:
        return subprocess.run(
            [python, "-c", _PROBE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=60,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _candidates() -> list[str]:
    cand = []
    if os.environ.get("AM_PYTHON"):
        cand.append(os.environ["AM_PYTHON"])
    if shutil.which("py"):  # interpreters registered with the py launcher
        try:
            out = subprocess.run(["py", "-0p"], capture_output=True,
                                 text=True, timeout=15).stdout or ""
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        for line in out.splitlines():
            # ` -V:3.11 *  C:\Program Files\Python311\python.exe` — the path
            # may contain spaces, so regex it out rather than split().
            m = re.search(r"[A-Za-z]:[\\/].*\.exe\s*$", line)
            if m:
                cand.append(m.group(0).strip())
    for name in ("python3", "python"):
        p = shutil.which(name)
        if p:
            cand.append(p)
    seen: set[str] = set()
    return [c for c in cand
            if not (os.path.normcase(c) in seen or seen.add(os.path.normcase(c)))]


def bootstrap() -> None:
    # UTF-8 out, whatever the console code page; children inherit via env.
    os.environ["PYTHONUTF8"] = "1"
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    # Unrelated PYTHONPATH entries can shadow imports here and in the
    # backend subprocess; the repo never needs it.
    os.environ.pop("PYTHONPATH", None)

    try:
        for mod in REQUIRED:
            __import__(mod)
        return  # current interpreter has the full dep set
    except ImportError as exc:
        missing = exc

    if os.environ.get(_REEXEC_FLAG):
        sys.exit(f"[harness] {sys.executable} still lacks deps after re-exec"
                 f" ({missing}); set AM_PYTHON to one that can: {_PROBE}")

    here = os.path.normcase(os.path.abspath(sys.executable))
    for python in _candidates():
        if os.path.normcase(os.path.abspath(python)) == here:
            continue
        if _deps_ok(python):
            print(f"[harness] {sys.executable} can't import the dep set "
                  f"({missing}); re-running under {python}")
            sys.stdout.flush()
            os.environ[_REEXEC_FLAG] = "1"
            sys.exit(subprocess.run([python, *sys.argv]).returncode)

    sys.exit(f"[harness] no interpreter with the dep set found ({missing}); "
             f"pip-install into {sys.executable} or set AM_PYTHON. "
             f"Required: {_PROBE}")
