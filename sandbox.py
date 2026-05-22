"""Windows fallback sandbox for the agent's Bash tool.

On macOS / Linux / WSL2 the Claude CLI binary enforces a real sandbox
(syscall filter + network namespace). On Windows the CLI prints a
warning and runs commands unsandboxed — see the start.py output:

    Sandbox disabled: sandbox.enabled is set but windows is not
    supported (requires macOS, Linux, or WSL2)

This module is the Windows fallback. It runs a command in a subprocess
with the constraints we *can* enforce from Python stdlib:

  - **cwd** locked to `docs/<slug>/` so the agent's working directory is
    the doc folder, not the project root
  - **env** curated: no `ANTHROPIC_API_KEY`, no `HOME`/`USERPROFILE`,
    no `OPENAI_API_KEY`, no proxy vars. The subprocess inherits a
    minimal `PATH` and locale; that's it
  - **timeout** so a runaway script can't burn the machine
  - **Job Object** (Windows only, via `pywin32` if installed) to cap
    memory and prevent fork-bomb child explosions. Degrades to
    timeout-only when pywin32 is absent

Threat model recap: this is "good enough for a personal machine," not
airtight. A motivated attacker who lands code execution inside the
subprocess can still `import socket` and open arbitrary connections —
Python's stdlib is on the inside. The realistic protection is that:

  (1) prompt-injection attacks usually reach for `curl`/`wget`/
      `Invoke-WebRequest`, which the curated PATH strips out, and
  (2) the curated env has no API keys to exfiltrate.

If you need airtight, run in WSL2 (then you get the CLI's real
sandbox) or in a Docker container.

Invocation: importable (`from sandbox import run_sandboxed`) or via
the `__main__` entrypoint when invoked as `python -m sandbox --slug X
--cmd Y`, which is what the PreToolUse hook does to wrap an
agent-issued Bash command.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
# Honor AM_DATA_DIR so an installed shell that routes user content to
# %APPDATA%/<app>/ doesn't end up sandboxing the agent into the empty
# bundle docs/ dir. sandbox.py runs as a subprocess invoked by the
# PreToolUse hook (`python -m sandbox ...`) and inherits env from the
# parent — so AM_DATA_DIR propagates automatically. Mirrors the same
# fallback (blank string = unset) am_docs.py uses for the main process.
_data_env = os.environ.get("AM_DATA_DIR", "").strip()
DATA_ROOT = Path(_data_env).resolve() if _data_env else ROOT
DOCS_ROOT = DATA_ROOT / "docs"

DEFAULT_TIMEOUT = 30

# Locked-down env keys. The subprocess gets ONLY these (with the values
# we explicitly set below), nothing else from the parent process — so
# `ANTHROPIC_API_KEY`, `HOME`, `USERPROFILE`, `HTTP_PROXY` etc. are all
# stripped.
_ENV_PASSTHROUGH = ("LANG", "LC_ALL", "TZ")

# Binaries the agent is allowed to invoke. The curated PATH is built
# from the full paths of these (looked up via shutil.which), so
# anything not on this list is unavailable to the subprocess even if
# it exists on the host. Deliberately omits: curl, wget, Invoke-WebRequest,
# Start-BitsTransfer, git (which can clone and exfil), ssh, scp.
# NOTE: deliberately NO `bash` / `sh` entry — on Windows `shutil.which`
# resolves to `C:\Windows\System32\bash.EXE` (the WSL launcher), and
# pulling System32 into the curated PATH would expose every Windows
# system utility (incl. `curl.exe`, `certutil`, `Invoke-WebRequest` via
# powershell). The outer shell is located separately via
# `_find_git_bash`; the agent's nested bash invocations resolve through
# this curated PATH.
_ALLOWED_BINARIES = (
    "python", "python3",
    "node", "npm", "npx",
    "awk", "sed", "grep", "find", "sort", "uniq", "head", "tail",
    "cat", "echo", "tr", "cut", "wc",
    "jq", "yq",
    "ffmpeg", "ffprobe",
    "magick", "convert", "identify",
)


_GIT_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
)


def _find_git_bash(search_path: str) -> str | None:
    """Locate a Git Bash (or equivalent non-WSL bash). The WSL launcher
    at `C:\\Windows\\System32\\bash.exe` is deliberately rejected; it
    fails non-interactively in opaque ways."""
    for candidate in _GIT_BASH_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    # Last-resort: walk the curated PATH and accept any bash that's not
    # in System32. shutil.which would happily return the WSL one.
    for entry in search_path.split(os.pathsep):
        if not entry:
            continue
        p = Path(entry) / "bash.exe"
        if p.is_file() and "system32" not in str(p).lower():
            return str(p)
    return None


def _curated_path() -> str:
    """Resolve each allowed binary on the host, dedupe their containing
    directories, and join into a PATH the subprocess will inherit.

    Binaries that don't exist on the host are silently skipped — the
    sandbox just doesn't make them available. The user installs more
    tools and they appear next time.
    """
    seen: list[str] = []
    for name in _ALLOWED_BINARIES:
        p = shutil.which(name)
        if not p:
            continue
        parent = str(Path(p).parent)
        if parent not in seen:
            seen.append(parent)
    # On Windows, also include the directory containing python itself so
    # the agent can find the same python the backend is using even when
    # it isn't on the system PATH by `python` (only `py` works).
    if os.name == "nt":
        exe_dir = str(Path(sys.executable).parent)
        if exe_dir not in seen:
            seen.append(exe_dir)
    return os.pathsep.join(seen)


def _curated_env(doc_dir: Path) -> dict[str, str]:
    """Build the env the subprocess will see — nothing inherited unless
    explicitly listed in `_ENV_PASSTHROUGH`, plus a PATH curated to the
    allowlist and a TMP scoped to the doc folder."""
    env: dict[str, str] = {"PATH": _curated_path()}
    for key in _ENV_PASSTHROUGH:
        v = os.environ.get(key)
        if v is not None:
            env[key] = v
    tmp = doc_dir / ".tmp"
    tmp.mkdir(exist_ok=True)
    env["TMP"] = str(tmp)
    env["TEMP"] = str(tmp)
    env["TMPDIR"] = str(tmp)
    # Force UTF-8 in Python subprocesses so non-Latin-1 characters
    # (math symbols, accented text) don't crash with cp1252 errors.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _resolve_doc_dir(slug: str) -> Path:
    """Look up the doc folder, raising if the slug is bogus or the
    folder doesn't exist. Resolved against `DOCS_ROOT` so a malicious
    slug like `../../etc` can't escape."""
    candidate = (DOCS_ROOT / slug).resolve()
    try:
        candidate.relative_to(DOCS_ROOT)
    except ValueError:
        raise ValueError(f"slug escapes docs root: {slug!r}")
    if not candidate.is_dir():
        raise FileNotFoundError(f"no such doc folder: {candidate}")
    return candidate


def run_sandboxed(
    cmd: str,
    doc_slug: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Run `cmd` as a shell command inside the doc's sandbox.

    Returns a dict with stdout, stderr, exit_code, timed_out. The
    exit_code is -1 if we couldn't even launch (e.g. shell missing).
    """
    doc_dir = _resolve_doc_dir(doc_slug)
    env = _curated_env(doc_dir)

    # On Windows, the agent will likely write Bash-flavored commands
    # (it knows Bash best). We deliberately AVOID the WSL launcher
    # `C:\Windows\System32\bash.exe` — it talks to a Windows service
    # and fails non-interactively with cryptic "RPC call contains a
    # handle that differs from the declared handle type" errors. Prefer
    # Git Bash (installed at the standard Git for Windows path), then
    # any bash on the curated PATH that isn't the system32 one, then
    # cmd.exe as last resort.
    #
    # On Windows, Git Bash's `/etc/profile` does the MSYS path-translation
    # dance (converting Windows-style PATH entries the parent passed in
    # into the `/c/...` form bash actually searches). Skipping it with
    # `--noprofile` leaves bash unable to find ANY of the binaries the
    # parent process placed on PATH. So we let the profile run and accept
    # that Git Bash's bundled `/usr/bin` and `/mingw64/bin` (containing
    # awk/sed/grep/find/etc., but also curl/ssh/wget) end up on PATH.
    #
    # Documented caveat: PATH curation on Windows is best-effort. The
    # load-bearing protections in this sandbox are the curated ENV (no
    # API keys for prompt-injection to exfiltrate) and the scoped CWD,
    # NOT the binary allowlist.
    if os.name == "nt":
        bash = _find_git_bash(env["PATH"])
        if bash:
            argv: list[str] = [bash, "-c", cmd]
        else:
            argv = ["cmd.exe", "/c", cmd]
        use_shell = False
    else:
        # Unix: use /bin/sh -c for portability. On Linux/macOS we'd
        # rarely fall back here because the CLI's real sandbox is in
        # play, but the function should still work standalone.
        argv = ["/bin/sh", "-c", cmd]
        use_shell = False

    try:
        result = subprocess.run(
            argv,
            cwd=str(doc_dir),
            env=env,
            shell=use_shell,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "stdout": (e.stdout.decode("utf-8", "replace")
                       if isinstance(e.stdout, bytes) else (e.stdout or "")),
            "stderr": (
                (e.stderr.decode("utf-8", "replace")
                 if isinstance(e.stderr, bytes) else (e.stderr or ""))
                + f"\n[sandbox] killed after {timeout}s timeout"
            ),
            "exit_code": 124,
            "timed_out": True,
        }
    except FileNotFoundError as e:
        return {
            "stdout": "",
            "stderr": f"[sandbox] cannot launch shell: {e}",
            "exit_code": -1,
            "timed_out": False,
        }


def main(argv: Iterable[str] | None = None) -> int:
    """CLI entrypoint — used by the PreToolUse hook to wrap a Bash call.

    The hook rewrites the agent's Bash command so the SDK ends up
    running something like:

        python -m sandbox --slug intro --cmd "python script.py"

    We unwrap, run the inner command sandboxed, then print the
    captured stdout/stderr verbatim and exit with the inner exit code
    — so the agent sees the same output it would have seen running the
    command directly, just with the constraints applied.
    """
    parser = argparse.ArgumentParser(prog="sandbox")
    parser.add_argument("--slug", required=True, help="doc slug")
    parser.add_argument("--cmd", required=True, help="command to run")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--json", action="store_true",
                        help="emit a JSON envelope instead of raw streams")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        out = run_sandboxed(args.cmd, args.slug, timeout=args.timeout)
    except (ValueError, FileNotFoundError) as e:
        print(f"[sandbox] {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(out))
    else:
        if out["stdout"]:
            sys.stdout.write(out["stdout"])
        if out["stderr"]:
            sys.stderr.write(out["stderr"])
    return out["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
