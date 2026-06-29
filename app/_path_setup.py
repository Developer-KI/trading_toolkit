"""
Add src/ and project root to sys.path.
Import this at the top of every app module before any internal imports.
"""
import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
_ROOT = _APP_DIR.parent
_SRC = _ROOT / "src"

for _p in [str(_SRC), str(_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

ROOT = _ROOT
DATA_DIR = _ROOT / "data"
