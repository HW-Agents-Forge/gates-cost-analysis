"""Locations: user cache, project state directory."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def user_cache_dir() -> Path:
    override = os.environ.get("GATECOST_CACHE_DIR")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "gatecost"
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "gatecost"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "gatecost"


def state_dir(root: Path) -> Path:
    return root / ".gatecost"
