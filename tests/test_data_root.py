"""Regression guards for AM_DATA_DIR override.

The desktop shell (Prism) routes user content into %APPDATA%/Prism/ by
setting AM_DATA_DIR, since its install dir is read-only. The override
needs to:
  - default to ROOT when unset (preserves repo / dev behavior)
  - route DOCS_ROOT and COMPONENTS_ROOT under the override
  - leave ROOT (code root) untouched — index.html and friends still
    live next to the .py files
  - mkdir the override dir on import (first launch)

We use subprocess so each case gets a clean module cache; importing
am_docs once snapshots the env var, and importlib.reload wouldn't
re-trigger backend.py's load_dotenv-from-DATA_ROOT side-effect.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run(env_overrides: dict[str, str], code: str) -> str:
    env = os.environ.copy()
    # Clean any inherited override so tests are deterministic.
    env.pop("AM_DATA_DIR", None)
    env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"subprocess failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return result.stdout.strip()


def test_default_data_root_equals_code_root():
    """Without AM_DATA_DIR, DATA_ROOT collapses onto ROOT — the legacy
    behavior every existing user (and every test fixture) relies on."""
    out = _run(
        {},
        "import am_docs; "
        "print(am_docs.ROOT == am_docs.DATA_ROOT, "
        "am_docs.DOCS_ROOT, am_docs.COMPONENTS_ROOT)",
    )
    eq, docs, comps = out.split(" ", 2)
    assert eq == "True"
    assert docs == str(REPO / "docs")
    assert comps == str(REPO / "components")


def test_override_routes_docs_and_components():
    with tempfile.TemporaryDirectory() as tmp:
        out = _run(
            {"AM_DATA_DIR": tmp},
            "import am_docs; "
            "print(am_docs.ROOT, '||', am_docs.DATA_ROOT, '||', "
            "am_docs.DOCS_ROOT, '||', am_docs.COMPONENTS_ROOT)",
        )
        root, data_root, docs, comps = [p.strip() for p in out.split("||")]
        # ROOT (code root) is unchanged: index.html still lives there.
        assert Path(root) == REPO
        # DATA_ROOT picks up the override; resolve() normalises slashes.
        assert Path(data_root) == Path(tmp).resolve()
        # docs/ and components/ derive from DATA_ROOT, not ROOT.
        assert Path(docs) == Path(tmp).resolve() / "docs"
        assert Path(comps) == Path(tmp).resolve() / "components"


def test_backend_creates_missing_data_dir_on_import():
    """First launch under an installed shell: %APPDATA%/Prism/ won't
    exist yet. backend.py mkdir's it so the .env loader and downstream
    file writes have a place to land."""
    with tempfile.TemporaryDirectory() as parent:
        target = Path(parent) / "fresh-data-dir"
        assert not target.exists()
        out = _run(
            {"AM_DATA_DIR": str(target)},
            "import backend; "
            "import os; print(os.path.isdir(backend.DATA_ROOT))",
        )
        assert out == "True"
        assert target.is_dir()


def test_blank_am_data_dir_falls_back_to_root():
    """Empty-string env var (common Windows footgun: `set AM_DATA_DIR=`)
    must NOT route data into the CWD or an empty path — fall back to ROOT."""
    out = _run(
        {"AM_DATA_DIR": "   "},
        "import am_docs; print(am_docs.ROOT == am_docs.DATA_ROOT)",
    )
    assert out == "True"
