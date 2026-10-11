"""One place for packaged resources and workspace-owned data paths."""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_default_project = PACKAGE_ROOT.parents[1] if PACKAGE_ROOT.parent.name == "src" else Path.cwd()
PROJECT_ROOT = Path(os.getenv("UW_PROJECT_ROOT", str(_default_project))).resolve()
DATA_ROOT = Path(os.getenv("UW_DATA_ROOT", str(PROJECT_ROOT.parent / "data"))).resolve()
RUNTIME_ROOT = Path(os.getenv("UW_RUNTIME_ROOT", str(DATA_ROOT / "runtime"))).resolve()
CASE_ROOT = Path(
    os.getenv("UW_CASE_ROOT", str(DATA_ROOT / "cases" / "Financial Evaluation"))
).resolve()
CONFIG_ROOT = Path(
    os.getenv("UW_CONFIG_ROOT", str(PACKAGE_ROOT / "resources" / "config"))
).resolve()
REFERENCE_ROOT = Path(os.getenv("UW_REFERENCE_ROOT", str(DATA_ROOT / "references"))).resolve()
CACHE_ROOT = Path(os.getenv("UW_CACHE_ROOT", str(RUNTIME_ROOT / "cache"))).resolve()
