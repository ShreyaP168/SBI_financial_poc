"""
engine/sbi_bureau.py — load SBI Life bureau data (CIBIL / DRC / Profile
Score / IIB findings) for the Financial Underwriting POC.

This data cannot be extracted from applicant PDFs — it's sourced
out-of-band from Scores.xlsx + "IIB, prev insurance.docx" screenshots,
same category as engine/policy_data.py's xlsx join. Lookup is by
case_id (== the SBI Life proposal number, e.g. "SYNTHETIC_CASE").

Path resolution:
  1. Explicit `path=` argument
  2. `SBI_BUREAU_DATA_JSON` environment variable
  3. config/sbi_bureau_data.json (default, ships with the POC sample cases)
"""

from __future__ import annotations
from underwriting_platform.configuration.paths import CONFIG_ROOT
import json
import logging
import os
from pathlib import Path
from typing import Optional

from .reference_cache import source_revision

_loaded_revisions = {}
logger = logging.getLogger(__name__)

_CACHE: Optional[dict] = None
_LOADED_PATH: Optional[Path] = None


def _resolve_path(path: Optional[str | Path] = None) -> Optional[Path]:
    if path:
        p = Path(path)
        return p if p.exists() else None
    env_p = os.environ.get("SBI_BUREAU_DATA_JSON")
    if env_p:
        p = Path(env_p)
        if p.exists():
            return p
    default_p = CONFIG_ROOT / "sbi_bureau_data.json"
    return default_p if default_p.exists() else None


def load_bureau_data(path: Optional[str | Path] = None) -> dict:
    global _CACHE, _LOADED_PATH
    resolved = _resolve_path(path)
    if resolved is None:
        _CACHE = {}
        _LOADED_PATH = None
        return _CACHE
    revision = source_revision(resolved)
    if (
        _CACHE is not None
        and _LOADED_PATH == resolved
        and _loaded_revisions.get("_LOADED_PATH") == revision
    ):
        return _CACHE
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[sbi_bureau] failed to load %s: %s", resolved, e)
        _CACHE = {}
        _LOADED_PATH = None
        return _CACHE
    _CACHE = data.get("cases") or {}
    _LOADED_PATH = resolved
    _loaded_revisions["_LOADED_PATH"] = revision
    logger.info("[sbi_bureau] indexed %d case(s) from %s", len(_CACHE), resolved.name)
    return _CACHE


def lookup_bureau(case_id: str, path: Optional[str | Path] = None) -> Optional[dict]:
    """Return the bureau record for `case_id`, or None if not on file."""
    cases = load_bureau_data(path)
    return cases.get(str(case_id).strip())
