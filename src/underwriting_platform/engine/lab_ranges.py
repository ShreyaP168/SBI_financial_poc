"""
engine/lab_ranges.py — authoritative lab-reference-range overlay.

Per Datamatics LAYOUT Sheet1 Med #11 (C305285193 regression) the medical
agent's per-report reference range and HIGH/LOW classification can be
wrong (e.g. flags LFTs as elevated but recommends standard rates). This
module supplies the canonical TALIC ranges from
`config/medical_lab_ranges.json` (sourced from the Master-File
"Medical Data ranges.xlsx") and re-classifies each `lab_results[]`
status against the master.

Strategy:
  • Match the agent-extracted `test` name against the master "field"
    (case-insensitive, normalised punctuation).
  • Pick the male / female band based on applicant gender.
  • Compute status by comparing `value` to the band: < min = LOW,
    > max = HIGH, in-range = NORMAL. Qualitative tests (HIV / HBsAg /
    Cotinine) compare the reported text against keywords.

The agent's original reads remain in `lab_results[i]` under
`agent_status` / `agent_reference_range` for audit; the master-derived
classification overwrites the live `status` + `reference_range` fields.
"""

from __future__ import annotations
from underwriting_platform.configuration.paths import CONFIG_ROOT
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_CACHE: dict | None = None
_CACHE_REVISION = None
from .reference_cache import source_revision


def _load() -> dict:
    global _CACHE, _CACHE_REVISION
    p = CONFIG_ROOT / "medical_lab_ranges.json"
    revision = source_revision(p)
    if _CACHE is not None and _CACHE_REVISION == revision:
        return _CACHE
    _CACHE_REVISION = revision
    p = CONFIG_ROOT / "medical_lab_ranges.json"
    if not p.exists():
        logger.warning("medical_lab_ranges.json not found; lab range overlay disabled")
        _CACHE = {}
        return _CACHE
    try:
        _CACHE = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("failed to load medical_lab_ranges.json: %s", e)
        _CACHE = {}
    return _CACHE


# ---------------------------------------------------------------------------
# Name normalisation + matching
# ---------------------------------------------------------------------------

# Map common alias variants to the master "field" key (case-insensitive
# substring match against the master). Keep aliases short + canonical.
_ALIASES: dict[str, str] = {
    "alt/sgpt": "ALT",
    "alt": "ALT",
    "sgpt": "ALT",
    "ast/sgot": "AST",
    "ast": "AST",
    "sgot": "AST",
    "alkaline phosphatase": "ALKALINE PHOSPHATE",
    "alp": "ALKALINE PHOSPHATE",
    "gamma-gt": "GGTP",
    "gamma gt": "GGTP",
    "ggt": "GGTP",
    "hba1c (glycosylated hb)": "HBA1C",
    "glucose (fbs)": "FBS",
    "glucose fasting": "FBS",
    "total cholesterol": "CHOLESTROL",
    "cholesterol total": "CHOLESTROL",
    "hdl-c": "HDL CHOLESTROL",
    "hdl": "HDL CHOLESTROL",
    "ldl-cholesterol": "LDL CHOLESTROL",
    "ldl cholesterol": "LDL CHOLESTROL",
    "hemoglobin (hgb)": "HAEMOGLOBIN",
    "hgb": "HAEMOGLOBIN",
    "hb": "HAEMOGLOBIN",
    "hepatitis b surface antigen (hbsag)": "HBSAG",
    "hiv ag/ab": "HIV-1",
    "creatinine": "CREATININE",
    "urea": "BUN",
    "lymphocytes %": "LYMPHOCYTES",
    "monocytes %": "MONOCYTES",
    "basophils %": "BASOPHILS",
    "eosinophils %": "EISNOPHILS",
}


def _normalise(s: str) -> str:
    """Lowercase + strip non-alphanumeric for fuzzy matching."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _master_index() -> dict[str, dict]:
    """Build a normalised lookup of master entries by normalised field name."""
    data = _load()
    rows = data.get("cbc_lft_others") or []
    idx: dict[str, dict] = {}
    for row in rows:
        f = _normalise(row.get("field") or "")
        if f:
            idx[f] = row
    return idx


def _find_master(test_name: str) -> dict | None:
    if not test_name:
        return None
    norm = _normalise(test_name)
    if not norm:
        return None
    # Alias match first (after normalisation)
    for alias, target in _ALIASES.items():
        if _normalise(alias) == norm:
            target_norm = _normalise(target)
            idx = _master_index()
            if target_norm in idx:
                return idx[target_norm]
    # Direct lookup
    idx = _master_index()
    if norm in idx:
        return idx[norm]
    # Substring fallback — master field is contained in the test name
    for key, row in idx.items():
        if key in norm or norm in key:
            return row
    return None


# ---------------------------------------------------------------------------
# Status re-classification
# ---------------------------------------------------------------------------


def _classify(value, min_v, max_v) -> str:
    """Compare a numeric value against min/max band. Returns NORMAL / LOW /
    HIGH / UNKNOWN."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if isinstance(min_v, (int, float)) and v < min_v:
        return "LOW"
    if isinstance(max_v, (int, float)) and v > max_v:
        return "HIGH"
    if isinstance(min_v, (int, float)) or isinstance(max_v, (int, float)):
        return "NORMAL"
    return "UNKNOWN"


def _classify_qualitative(value: str, master_row: dict) -> str:
    """For qualitative tests (HIV/HBsAg/Cotinine etc.): map the reported
    value to NORMAL (Non-Reactive/Negative) or HIGH (Reactive/Positive).
    """
    s = (value or "").strip().lower()
    if not s:
        return "UNKNOWN"
    if any(
        t in s
        for t in (
            "non-reactive",
            "non reactive",
            "nonreactive",
            "negative",
            "not detected",
            "absent",
            "nil",
        )
    ):
        return "NORMAL"
    if any(t in s for t in ("reactive", "positive", "detected", "present")):
        return "HIGH"  # HIGH used as the "abnormal" bucket for serology
    return "UNKNOWN"


def reclassify_labs(case: dict) -> dict:
    """Re-classify medical.lab_results[] against the master ranges.

    Mutates the case dict in-place (and returns it) so the agent's
    original `status` and `reference_range` are preserved under
    `agent_status` and `agent_reference_range` while the live fields
    carry the master-derived values.
    """
    medical = case.get("medical") or {}
    labs = medical.get("lab_results")
    if not labs:
        return case

    applicant = case.get("applicant") or {}
    gender = (applicant.get("gender") or "").strip().lower()
    is_female = gender.startswith("f")

    overrides = 0
    skipped_unit_mismatch = 0
    for L in labs:
        master = _find_master(L.get("test") or "")
        if not master:
            continue
        # Preserve agent originals
        if "agent_status" not in L:
            L["agent_status"] = L.get("status")
        if "agent_reference_range" not in L:
            L["agent_reference_range"] = L.get("reference_range")

        master_units = (master.get("units") or "").strip().lower()
        lab_units = (L.get("unit") or "").strip().lower()

        if master.get("qualitative"):
            # Qualitative tests don't have unit concerns
            new_status = _classify_qualitative(str(L.get("value") or ""), master)
            L["status"] = new_status
            L["reference_range"] = "Reactive/Non Reactive"
            L["range_source"] = "TALIC master (qualitative)"
            if new_status != L.get("agent_status"):
                overrides += 1
            continue

        # For numeric tests: only override when units match. Indian labs
        # report in conventional units (mg/dl, /cumm); international labs
        # often report in SI units (mmol/L, 10^3/uL) — applying the
        # master band to an SI-units value produces FALSE classifications.
        units_match = (
            master_units
            and lab_units
            and (
                master_units == lab_units or master_units in lab_units or lab_units in master_units
            )
        )
        if not units_match:
            skipped_unit_mismatch += 1
            # Agent classification kept; just annotate that the master
            # has a band but units differ.
            L["range_source"] = (
                f"TALIC master unit mismatch "
                f"(master={master_units!r}, "
                f"lab={lab_units!r}) — agent status kept"
            )
            continue

        mn = master["female_min"] if is_female else master["male_min"]
        mx = master["female_max"] if is_female else master["male_max"]
        new_status = _classify(L.get("value"), mn, mx)
        L["status"] = new_status
        band = f"{mn if mn is not None else ''} - {mx if mx is not None else ''}".strip(" -")
        if master_units:
            band = f"{band} {master_units}" if band else f"({master_units})"
        L["reference_range"] = band or L.get("reference_range")
        L["range_source"] = "TALIC master (Medical Data ranges.xlsx)"
        if L["status"] != L.get("agent_status"):
            overrides += 1

    if overrides or skipped_unit_mismatch:
        meta = medical.setdefault("agent_meta", {})
        notes = meta.get("notes") or ""
        meta["notes"] = (
            notes
            + (
                f" Lab-range overlay: {overrides} re-classified "
                f"against TALIC master, {skipped_unit_mismatch} skipped "
                f"due to unit mismatch (agent classification kept)."
            )
        ).strip()
    return case
