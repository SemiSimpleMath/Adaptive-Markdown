"""Ensure the project root is on sys.path so tests can `import backend`,
`from agent_runtime.codex_runtime import ...` etc. without installation."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
