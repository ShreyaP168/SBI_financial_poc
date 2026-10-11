"""
engine/medical_dual_vendor.py — field-level reconciliation between two
medical-agent extractions (e.g. Claude vs Gemini).

The dual-vendor architecture runs the medical agent twice in parallel
on the same MER + lab inputs, with two different vendor backends.
Wherever the two readings disagree on a high-signal field, this module
produces a structured diff and a severity rank. The engine flag stage
turns those diffs into `MEDICAL_DUAL_VENDOR_DISAGREEMENT` flags so the
underwriter sees both reads side-by-side and can resolve.

Fields compared (high-signal only — we do NOT diff every lab row):

  vitals     — height_cm, weight_kg, bp_systolic_mmhg, bp_diastolic_mmhg,
                pulse_bpm, build
  habits     — smoker (Yes/No), alcohol, tobacco_other_forms
  fraud      — declared_smoker, cotinine_smoker_mismatch
  ecg        — performed, status
  tmt        — performed, recommendation
  conditions — set of declared `personal_medical_history.condition` names
                (active status). This is what catches Q3 checkbox misreads.

Returns a dict shaped:
    {
      "agreed":    [...],
      "disagreed": [
        {"field": "vitals.weight_kg",
         "primary": 64, "secondary": 67,
         "severity": "MEDIUM"},
        ...
      ],
      "missing_in_primary":   [...],
      "missing_in_secondary": [...],
      "primary_vendor":   "claude",
      "secondary_vendor": "gemini",
      "summary":          "3/12 high-signal fields disagreed."
    }
"""

from __future__ import annotations
from typing import Any

# Field paths to compare. Each entry: (dotted_path, comparator_kind).
# comparator_kind:
#   "numeric_pct" — equal if abs(a-b)/max(a,b) < 0.02
#   "exact"        — string equality (case-insensitive)
#   "bool"         — boolish equality
_FIELD_SPECS: list[tuple[str, str]] = [
    ("vitals.height_cm", "numeric_pct"),
    ("vitals.weight_kg", "numeric_pct"),
    ("vitals.bp_systolic_mmhg", "numeric_pct"),
    ("vitals.bp_diastolic_mmhg", "numeric_pct"),
    ("vitals.pulse_bpm", "numeric_pct"),
    ("vitals.build", "exact"),
    ("habits_declared.smoker", "exact"),
    ("habits_declared.alcohol", "exact"),
    ("habits_declared.tobacco_other_forms", "exact"),
    ("fraud_signals.declared_smoker", "exact"),
    ("fraud_signals.cotinine_smoker_mismatch", "bool"),
    ("ecg.performed", "bool"),
    ("ecg.status", "exact"),
    ("tmt.performed", "bool"),
    ("tmt.recommendation", "exact"),
]

# Severity per disagreement kind. We weight a smoker mismatch heavier
# than a 5% BP delta because the underwriting impact is much larger.
_SEVERITY_BY_FIELD = {
    "vitals.height_cm": "MEDIUM",
    "vitals.weight_kg": "MEDIUM",
    "vitals.bp_systolic_mmhg": "MEDIUM",
    "vitals.bp_diastolic_mmhg": "MEDIUM",
    "vitals.pulse_bpm": "LOW",
    "vitals.build": "LOW",
    "habits_declared.smoker": "HIGH",
    "habits_declared.alcohol": "MEDIUM",
    "habits_declared.tobacco_other_forms": "MEDIUM",
    "fraud_signals.declared_smoker": "HIGH",
    "fraud_signals.cotinine_smoker_mismatch": "HIGH",
    "ecg.performed": "MEDIUM",
    "ecg.status": "HIGH",
    "tmt.performed": "MEDIUM",
    "tmt.recommendation": "HIGH",
    "personal_medical_history.condition_set": "HIGH",
    "family_history.condition_set": "MEDIUM",
}


def _walk(d: dict | None, path: str) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _eq_numeric(a: Any, b: Any, tol_pct: float = 2.0) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        af, bf = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if af == 0 and bf == 0:
        return True
    base = max(abs(af), abs(bf), 1.0)
    return abs(af - bf) / base * 100.0 < tol_pct


def _eq_exact(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return str(a).strip().lower() == str(b).strip().lower()


def _eq_bool(a: Any, b: Any) -> bool:
    return bool(a) == bool(b)


def _normalise_condition_name(s: str) -> str:
    return (s or "").strip().lower()


def _active_condition_set(med: dict | None) -> set[str]:
    """Set of active personal-medical-history condition names (lowercased)."""
    if not isinstance(med, dict):
        return set()
    out: set[str] = set()
    for c in med.get("personal_medical_history") or []:
        if not isinstance(c, dict):
            continue
        status = (c.get("status") or "").lower()
        if status not in ("active", "resolved", ""):
            # Only include things that ARE recorded; "never" entries don't count.
            if status == "never":
                continue
        # Treat anything not "never" as a declared condition.
        name = _normalise_condition_name(c.get("condition") or "")
        if name and (c.get("status") or "").lower() != "never":
            out.add(name)
    return out


def _family_condition_set(med: dict | None) -> set[str]:
    """Set of family-history condition names (lowercased) regardless of relationship."""
    if not isinstance(med, dict):
        return set()
    out: set[str] = set()
    for member in med.get("family_history") or []:
        if not isinstance(member, dict):
            continue
        for cond in member.get("conditions") or []:
            if isinstance(cond, dict):
                name = _normalise_condition_name(cond.get("name") or "")
                if name:
                    out.add(name)
    return out


def compare_medical_blocks(
    primary: dict | None,
    secondary: dict | None,
    primary_vendor: str = "claude",
    secondary_vendor: str = "gemini",
) -> dict:
    """Field-level diff. Both blocks should be the value at
    `case_json["medical"]` (NOT the wrapping dict).
    """
    primary = primary or {}
    secondary = secondary or {}
    agreed: list[str] = []
    disagreed: list[dict] = []
    missing_p: list[str] = []
    missing_s: list[str] = []

    for path, kind in _FIELD_SPECS:
        a = _walk(primary, path)
        b = _walk(secondary, path)
        if a is None and b is None:
            continue
        if a is None and b is not None:
            missing_p.append(path)
            continue
        if b is None and a is not None:
            missing_s.append(path)
            continue

        if kind == "numeric_pct":
            same = _eq_numeric(a, b)
        elif kind == "exact":
            same = _eq_exact(a, b)
        elif kind == "bool":
            same = _eq_bool(a, b)
        else:
            same = a == b

        if same:
            agreed.append(path)
        else:
            disagreed.append(
                {
                    "field": path,
                    "primary": a,
                    "secondary": b,
                    "severity": _SEVERITY_BY_FIELD.get(path, "MEDIUM"),
                }
            )

    # ── Set-based diff for personal-medical-history conditions ─────
    p_set = _active_condition_set(primary)
    s_set = _active_condition_set(secondary)
    only_p = p_set - s_set
    only_s = s_set - p_set
    if only_p or only_s:
        disagreed.append(
            {
                "field": "personal_medical_history.condition_set",
                "primary": sorted(p_set),
                "secondary": sorted(s_set),
                "only_in_primary": sorted(only_p),
                "only_in_secondary": sorted(only_s),
                "severity": _SEVERITY_BY_FIELD["personal_medical_history.condition_set"],
            }
        )
    else:
        agreed.append("personal_medical_history.condition_set")

    # ── Set-based diff for family-history conditions ───────────────
    fp_set = _family_condition_set(primary)
    fs_set = _family_condition_set(secondary)
    fonly_p = fp_set - fs_set
    fonly_s = fs_set - fp_set
    if fonly_p or fonly_s:
        disagreed.append(
            {
                "field": "family_history.condition_set",
                "primary": sorted(fp_set),
                "secondary": sorted(fs_set),
                "only_in_primary": sorted(fonly_p),
                "only_in_secondary": sorted(fonly_s),
                "severity": _SEVERITY_BY_FIELD["family_history.condition_set"],
            }
        )
    else:
        agreed.append("family_history.condition_set")

    n_total = len(agreed) + len(disagreed)
    summary = (
        f"{len(disagreed)}/{n_total} high-signal fields disagreed "
        f"between {primary_vendor} (primary) and {secondary_vendor} "
        "(secondary)."
    )

    return {
        "primary_vendor": primary_vendor,
        "secondary_vendor": secondary_vendor,
        "agreed": agreed,
        "disagreed": disagreed,
        "missing_in_primary": missing_p,
        "missing_in_secondary": missing_s,
        "summary": summary,
    }
