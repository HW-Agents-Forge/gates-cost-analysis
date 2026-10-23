"""gatecost: fast, approximate gate-count estimation for RTL."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Read-only data files (VERSION, schema/). In the repository (skill) layout they sit at the repository
# root; in a pip-installed layout the build copies them to ``gatecost/_data`` (see setup.py).
DATA_DIR = REPO_ROOT if (REPO_ROOT / "VERSION").is_file() else Path(__file__).resolve().parent / "_data"


def version() -> str:
    try:
        return (DATA_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"
