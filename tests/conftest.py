"""Test bootstrap: make the repo package and the plugin's vendored deps importable."""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parents[2]
VENDOR = PLUGIN_ROOT / "vendor"

for candidate in (VENDOR, REPO_ROOT):
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))
