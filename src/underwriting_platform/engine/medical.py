"""
engine/medical.py — provenance-emitting medical calculations.

In addition to the calculations, this module exposes a small
`apply_medical_overrides(case)` helper that the engine driver calls
before computing risk / flags / etc., so deterministic rules in the
text (physician overrides, THR-not-achieved → Postponed) flow through
even when the agent did not fully encode them.

Reads the `medical` block produced by MedicalAgent + the medical rules at
`config/medical_rules.json`, and emits Calculation objects for:

  - bmi_band              — uw_rules.bmi_bands lookup with provenance
  - required_tests        — MEDICAL_GRID lookup by MSAR + age band
  - test_compliance_pct   — required vs received test_codes (MER/MSU/BPB/...)
  - lab_abnormalities     — count abnormal/high/low rows by category
  - health_score          — 0-10 composite
  - medical_risk          — LOW / MODERATE / HIGH
  - fraud_risk            — LOW / MODERATE / HIGH (cotinine + family-history
                            check + future anti-fraud signals)

Each Calculation carries function name, formula, named inputs, sources,
and a rule citation (e.g. `medical_rules.MEDICAL_GRID["20000000 to 30000000"].36-45`).
"""

from __future__ import annotations
from typing import Any

from .calculator import _safe_int, _safe_float
from .types import Calculation, Source


# ──────────────────────────────────────────────────────────────────
# BMI bands (already in uw_rules.bmi_bands; this just looks up + cites)
# ──────────────────────────────────────────────────────────────────


def compute_bmi_band(case: dict, rules: dict) -> Calculation:
    med = case.get("medical") or {}
    vitals = med.get("vitals") or {}
    bmi = _safe_float(vitals.get("bmi"))
    bands = rules.get("bmi_bands") or {
        "underweight_max": 18.5,
        "normal_max": 24.9,
        "overweight_max": 29.9,
    }
    uw_max = _safe_float(bands.get("underweight_max") or 18.5)
    nm_max = _safe_float(bands.get("normal_max") or 24.9)
    ow_max = _safe_float(bands.get("overweight_max") or 29.9)
    if bmi <= 0:
        category = "unknown"
    elif bmi < uw_max:
        category = "underweight"
    elif bmi <= nm_max:
        category = "normal"
    elif bmi <= ow_max:
        category = "overweight"
    else:
        category = "obese"
    return Calculation(
        name="bmi_band",
        value=bmi or 0,
        unit="ratio",
        method="uw_rules.bmi_bands_lookup",
        function="underwriting_platform.engine.medical.compute_bmi_band",
        formula="weight_kg / (height_cm/100)^2 → category from uw_rules.bmi_bands",
        inputs={
            "bmi": bmi,
            "category": category,
            "height_cm": vitals.get("height_cm"),
            "weight_kg": vitals.get("weight_kg"),
            "underweight_max": uw_max,
            "normal_max": nm_max,
            "overweight_max": ow_max,
        },
        sources=[
            Source(
                document="MER vitals (height + weight)",
                agent="medical",
                field_path="medical.vitals",
            )
        ],
        rule="uw_rules.bmi_bands",
        note=f"Category: {category}.",
    )


# ──────────────────────────────────────────────────────────────────
# Required tests + compliance — reads config/medical_rules.json
# ──────────────────────────────────────────────────────────────────

_AGE_BANDS_MED = ["18-35", "36-45", "46-55", "Above 55"]


def _age_band_med(age: int | None) -> str:
    if age is None:
        return "Above 55"
    if age <= 35:
        return "18-35"
    if age <= 45:
        return "36-45"
    if age <= 55:
        return "46-55"
    return "Above 55"


def _msar_row(grid_rows: list, msar: int) -> dict | None:
    for row in grid_rows or []:
        lo = _safe_int(row.get("msar_lower"))
        hi = _safe_int(row.get("msar_upper"))
        if lo <= msar <= hi:
            return row
    return None


def lookup_required_tests(
    case: dict, age: int | None, medical_rules: dict, proposed_sa: int
) -> Calculation:
    guide = (medical_rules or {}).get("MedicalUnderwritingGuidelines") or {}
    grid = guide.get("MEDICAL_GRID") or {}
    rows = grid.get("requirements") or []
    msar = max(0, _safe_int(proposed_sa))
    band = _age_band_med(age)
    row = _msar_row(rows, msar)
    msar_label = (row or {}).get("msar_range") or "—"
    tests_str = ((row or {}).get("tests_by_age") or {}).get(band) or ""
    tests = [t.strip() for t in tests_str.split(",") if t.strip()]
    # Special rules
    med = case.get("medical") or {}
    declared_smoker = ((med.get("habits_declared") or {}).get("smoker") or "").lower()
    if declared_smoker == "no" and "Urine Cotinine" not in tests:
        tests.append("Urine Cotinine")
    applicant_gender = ((case.get("applicant") or {}).get("gender") or "").lower()
    if applicant_gender.startswith("m") and (age or 0) > 55 and "PSA" not in tests:
        tests.append("PSA")
    # Per medical_rules.special_rules: PSA part of tumor markers when cover > 25 Cr
    # (= ₹250,000,000). The previous threshold was off by 10× (₹2.5 Cr).
    if msar > 250_000_000 and "PSA" not in tests:
        tests.append("PSA")
    # Datamatics LAYOUT Sheet1 Med #2: TMT is MANDATORY (not additional)
    # for age 36-45 with SA > 3 Cr. The medical grid sometimes lists
    # TMT as optional for this band; this rule forces it to mandatory.
    if age is not None and 36 <= age <= 45 and msar > 30_000_000 and "TMT" not in tests:
        tests.append("TMT")
    # PB-channel VMER skip rule — Datamatics LAYOUT Other #5.
    # When the proposer comes via Policy Bazaar channel AND VPSC (Video
    # PSC) or PIV evidence is on file AND grid medicals are present,
    # the Video MER is not required (matches medical_rules.json
    # VMER_GRID_RI.policy_bazaar_special.upto_2.25cr: "VPSC + TELE
    # considered as VMER").
    pd_block = case.get("policy_data") or {}
    compliance = case.get("compliance") or {}
    kyc_ev = compliance.get("kyc_evidence") or {}
    channel = (pd_block.get("channel") or pd_block.get("channel_name") or "").upper()
    vpsc_present = bool(kyc_ev.get("vpsc_present"))
    piv_present = bool(kyc_ev.get("piv_present"))
    pb_vmer_skipped = False
    if (
        channel in ("PB", "POLICY BAZAAR", "POLICYBAZAAR")
        and (vpsc_present or piv_present)
        and msar <= 22_500_000
    ):
        before = list(tests)
        tests = [t for t in tests if "VMER" not in t.upper() and t.upper() not in ("VMER",)]
        pb_vmer_skipped = tests != before

    note_extra = ""
    if pb_vmer_skipped:
        note_extra = (
            " PB-channel VMER skipped per VPSC+PIV grid-medicals rule "
            "(medical_rules.VMER_GRID_RI.policy_bazaar_special)."
        )

    return Calculation(
        name="required_tests",
        value=len(tests),
        unit="count",
        method="medical_grid_lookup",
        function="underwriting_platform.engine.medical.lookup_required_tests",
        formula="MEDICAL_GRID[msar_band][age_band] + special_rules",
        inputs={
            "msar": msar,
            "msar_band": msar_label,
            "age": age,
            "age_band": band,
            "tests": tests,
            "declared_smoker": declared_smoker or None,
            "applicant_gender": applicant_gender or None,
            "channel": channel or None,
            "vpsc_present": vpsc_present,
            "piv_present": piv_present,
            "pb_vmer_skipped": pb_vmer_skipped,
        },
        sources=[
            Source(
                document="medical_rules.json — MEDICAL_GRID",
                agent="medical",
                field_path="medical.documents_present",
            )
        ],
        rule=f"medical_rules.MEDICAL_GRID.{msar_label}.{band}",
        note=(
            "Special rules applied: cotinine for declared non-smokers, "
            "PSA for males >55 or SA >₹25 Cr." + note_extra
        ),
    )


_TEST_DOC_KEY = {
    "MER": "mer",
    "MSU": "msu",
    "BPB": "bpb",
    "BPB-F": "bpb_fasting",
    "CBC": "cbc",
    "ECG": "ecg",
    "TMT": "tmt",
    "HBA1C": "hba1c",
    "HbA1c": "hba1c",
    "PSA": "psa",
    "Anti HCV": "anti_hcv",
    "Anti-HCV": "anti_hcv",
    "Chest X-Ray": "chest_xray",
    "USG abdomen": "usg_abdomen",
    "2D echo": "tmt_2d_echo",
    "Stress Echo": "tmt_2d_echo",
    "Resting 2D echo with Stress Echo": "tmt_2d_echo",
    "Urine Microalbumin": "urine_microalbumin",
    "Urine Cotinine": "msu",  # cotinine ships in urine analysis
    "Fundoscopy": "fundoscopy",
    "Thyroid function tests": "thyroid_function",
    "Tumor markers": "tumor_markers",
    "Tumor markers Male": "tumor_markers",
    "Tumor markers Female": "tumor_markers",
    "PFT": "pft",
}


def compute_test_compliance(case: dict, required_calc: Calculation) -> Calculation:
    med = case.get("medical") or {}
    docs_present = med.get("documents_present") or {}
    tests = list((required_calc.inputs or {}).get("tests") or [])
    received: list[str] = []
    missing: list[str] = []
    for t in tests:
        key = _TEST_DOC_KEY.get(t, t.lower().replace(" ", "_"))
        if docs_present.get(key) is True:
            received.append(t)
        else:
            missing.append(t)
    pct = round(100.0 * len(received) / max(1, len(tests)), 1)
    return Calculation(
        name="test_compliance_pct",
        value=pct,
        unit="%",
        method="received_vs_required",
        function="underwriting_platform.engine.medical.compute_test_compliance",
        formula="100 × |received| / |required|",
        inputs={
            "n_required": len(tests),
            "n_received": len(received),
            "n_missing": len(missing),
            "received": received,
            "missing": missing,
        },
        sources=[
            Source(
                document="medical.documents_present (checklist)",
                agent="medical",
                field_path="medical.documents_present",
            )
        ],
        rule="medical_rules.MEDICAL_GRID + medical.documents_present",
        note=f"{len(received)}/{len(tests)} required tests present.",
    )


# ──────────────────────────────────────────────────────────────────
# Lab abnormality count
# ──────────────────────────────────────────────────────────────────


def count_lab_abnormalities(case: dict) -> Calculation:
    med = case.get("medical") or {}
    rows = med.get("lab_results") or []
    by_cat: dict[str, int] = {}
    abnormal: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        status = (r.get("status") or "").upper()
        if status in ("HIGH", "LOW", "ABNORMAL"):
            cat = (r.get("category") or "other").lower()
            by_cat[cat] = by_cat.get(cat, 0) + 1
            abnormal.append(
                {
                    "test": r.get("test"),
                    "value": r.get("value"),
                    "status": status,
                    "category": cat,
                    "page": r.get("source_page"),
                }
            )
    return Calculation(
        name="lab_abnormalities",
        value=len(abnormal),
        unit="count",
        method="status_in_HIGH_LOW_ABNORMAL",
        function="underwriting_platform.engine.medical.count_lab_abnormalities",
        formula="count(lab_results[].status ∈ {HIGH, LOW, ABNORMAL})",
        inputs={
            "n_abnormal": len(abnormal),
            "n_total": len(rows),
            "by_category": by_cat,
            "details": abnormal[:20],  # cap for log readability
        },
        sources=[
            Source(
                document="medical.lab_results[]", agent="medical", field_path="medical.lab_results"
            )
        ],
        note=f"{len(abnormal)}/{len(rows)} lab rows flagged abnormal/high/low.",
    )


# ──────────────────────────────────────────────────────────────────
# Health score (0-10)
# ──────────────────────────────────────────────────────────────────

# Conditions TALIC's UW practice rates as STANDARD (not penalised in
# health score). Match against the lower-cased condition name with
# substring containment. Anything NOT in this list still counts as an
# active condition and incurs the penalty.
_STANDARD_RATED_CONDITIONS: tuple[str, ...] = (
    "fatty liver",  # Grade I/II per UW practice — std
    "hyperlipidemia",  # mild (resolved or borderline) — std
    "raised cholesterol",  # ditto
    "vitamin d deficiency",
    "iron deficiency",
    "low haemoglobin",  # mild anemia, no chronic etiology
    "calcified granuloma",  # liver / lung — old, std
    "non-obstructive calculus",  # tiny renal calculus — std
    "nonobstructive calculus",
    "renal calculus",
    "calyceal",
    "hyperopia",  # long-sightedness — std
    "myopia",  # short-sightedness alone — std
    "hair loss",  # cosmetic — std
    "alopecia",
    "hair transplant",
    "obesity",  # captured separately by BMI band
    "morbid obesity",
    "covid-19",  # resolved — std
    "covid",
    "right shoulder dislocation",  # historical, resolved
    "shoulder dislocation",
    "hernio",  # uncomplicated past hernia — std
    "appendec",  # appendectomy
    "tonsillec",  # tonsillectomy
    "vaginal delivery",
    "ligation",
    "ovarian",  # simple ovarian / hemorrhagic cysts
    "cyst",  # simple cysts
    "fibroid",  # uterine fibroids
    "cholelithiasis",  # gallstones (uncomplicated) — std
)


def _is_standard_rated(condition_name: str) -> bool:
    cn = (condition_name or "").lower()
    return any(s in cn for s in _STANDARD_RATED_CONDITIONS)


def compute_health_score(
    case: dict, bmi_calc: Calculation, abnormal_calc: Calculation
) -> Calculation:
    med = case.get("medical") or {}
    vitals = med.get("vitals") or {}
    habits = med.get("habits_declared") or {}
    family = med.get("family_history") or []
    personal = med.get("personal_medical_history") or []

    score = 10.0
    components: dict = {}

    # Lab abnormalities (0.3 per row, capped at 3.0). Lowered from
    # 0.5/4.0 because mild lipid / RBC drift is too easily flagged.
    n_abn = _safe_int(abnormal_calc.value)
    lab_penalty = min(n_abn * 0.3, 3.0)
    score -= lab_penalty
    components["lab_abnormalities"] = -lab_penalty

    # BMI band penalty
    bmi_cat = (bmi_calc.inputs or {}).get("category", "unknown")
    bmi_penalty = {"underweight": 0.5, "normal": 0.0, "overweight": 0.5, "obese": 1.0}.get(
        bmi_cat, 0.0
    )
    score -= bmi_penalty
    components["bmi"] = -bmi_penalty

    # Personal active conditions — split into RATABLE (true risk) vs
    # STANDARD (recorded but not penalised, per TALIC UW practice for
    # common findings: fatty liver Grade I, mild hyperlipidemia, simple
    # cysts, BMI alone, post-surgical resolved, etc.).
    ratable_conditions: list[str] = []
    standard_conditions: list[str] = []
    for c in personal:
        status = (c.get("status") or "").lower()
        if status != "active":
            continue
        cond_name = c.get("condition") or ""
        if _is_standard_rated(cond_name):
            standard_conditions.append(cond_name)
        else:
            ratable_conditions.append(cond_name)
    n_ratable = len(ratable_conditions)
    score -= 0.7 * n_ratable
    components["active_ratable_conditions"] = -0.7 * n_ratable

    # Smoking detected
    declared_smoker = (habits.get("smoker") or "").lower() == "yes"
    if declared_smoker:
        score -= 1.5
        components["smoker"] = -1.5

    # Family history early-onset / first-degree
    fh_red_flags = 0
    for member in family:
        for cond in member.get("conditions") or []:
            if cond.get("first_degree") and cond.get("early_onset"):
                fh_red_flags += 1
    score -= 0.5 * fh_red_flags
    components["family_history_early_onset"] = -0.5 * fh_red_flags

    # BP elevated (>=130/85)
    sys_bp = _safe_int(vitals.get("bp_systolic_mmhg"))
    dia_bp = _safe_int(vitals.get("bp_diastolic_mmhg"))
    if sys_bp >= 140 or dia_bp >= 90:
        score -= 1.0
        components["bp_stage1"] = -1.0
    elif sys_bp >= 130 or dia_bp >= 85:
        score -= 0.5
        components["bp_elevated"] = -0.5

    score = max(0.0, round(score, 1))
    return Calculation(
        name="health_score",
        value=score,
        unit="score",
        method="weighted_penalty_from_10",
        function="underwriting_platform.engine.medical.compute_health_score",
        formula="10 - (lab×0.3 capped 3) - bmi_pen - ratable_active×0.7 "
        "- smoker×1.5 - fh_red×0.5 - bp_pen",
        inputs={
            "starting_score": 10.0,
            "components": components,
            "n_lab_abnormal": n_abn,
            "bmi_category": bmi_cat,
            "ratable_conditions": ratable_conditions,
            "standard_rated_conditions": standard_conditions,
            "n_ratable": n_ratable,
            "n_standard": len(standard_conditions),
            "declared_smoker": habits.get("smoker"),
            "n_family_red_flags": fh_red_flags,
            "bp_systolic": sys_bp or None,
            "bp_diastolic": dia_bp or None,
        },
        sources=[
            Source(
                document="medical.lab_results + vitals + habits + family_history",
                agent="medical",
                field_path="medical",
            )
        ],
        rule=(
            "underwriting_platform.engine.medical.compute_health_score thresholds + "
            "_STANDARD_RATED_CONDITIONS list"
        ),
        note=(
            f"Health score: {score}/10. Standard-rated conditions "
            f"({len(standard_conditions)}) not penalised; "
            f"ratable conditions ({n_ratable}) drive score."
        ),
    )


# ──────────────────────────────────────────────────────────────────
# Medical risk band
# ──────────────────────────────────────────────────────────────────


def compute_medical_risk(
    case: dict,
    age: int | None,
    health_score_calc: Calculation,
    abnormal_calc: Calculation,
    bmi_calc: Calculation,
) -> Calculation:
    score = _safe_float(health_score_calc.value)
    n_abn = _safe_int(abnormal_calc.value)
    bmi_cat = (bmi_calc.inputs or {}).get("category", "unknown")
    med = case.get("medical") or {}
    personal = med.get("personal_medical_history") or []

    band = "LOW"
    drivers: list[str] = []

    # Score-based primary classification
    if score >= 8.5:
        band, drivers = "LOW", [f"Health score {score}/10 ≥ 8.5"]
    elif score >= 6.0:
        band, drivers = "MODERATE", [f"Health score {score}/10 in [6.0, 8.5)"]
    else:
        band, drivers = "HIGH", [f"Health score {score}/10 < 6.0"]

    # Hard overrides
    serious_conditions = {
        "heart disease",
        "cardiovascular",
        "stroke",
        "cancer",
        "kidney disease",
        "renal failure",
        "uncontrolled diabetes",
    }
    has_serious = any((c.get("condition") or "").lower() in serious_conditions for c in personal)
    if has_serious and band != "HIGH":
        band, drivers = "HIGH", drivers + ["Active serious condition reported."]

    if (age or 0) > 60 and n_abn > 3:
        band = "HIGH"
        drivers.append(f"Age {age}>60 with {n_abn} abnormal labs.")

    if bmi_cat == "obese" and band == "LOW":
        band = "MODERATE"
        drivers.append("BMI obese — at minimum MODERATE.")

    return Calculation(
        name="medical_risk",
        value=band,
        unit="band",
        method="health_score_with_overrides",
        function="underwriting_platform.engine.medical.compute_medical_risk",
        formula="band(health_score) ∪ overrides(serious_condition, age>60+labs, obese)",
        inputs={
            "health_score": score,
            "n_lab_abnormal": n_abn,
            "bmi_category": bmi_cat,
            "age": age,
            "serious_condition_present": has_serious,
            "drivers": drivers,
        },
        sources=health_score_calc.sources,
        rule="underwriting_platform.engine.medical.compute_medical_risk thresholds",
        note=" | ".join(drivers),
    )


def compute_medical_uw_terminal_decision(
    case: dict,
    medical_risk_calc: Calculation,
    required_tests_calc: Calculation,
    compliance_calc: Calculation,
    abnormal_calc: Calculation,
) -> Calculation:
    """Datamatics LAYOUT row 69-70 — Medical UW Terminal Decision.

    One of: Medically Std | Rateable | Additional Test | Repeat Test |
    Postpone | Decline. Separate from the financial verdict — the UW
    needs to see the *medical* outcome distinctly so a clean financial
    case doesn't mask a medical refer.

    Decision tree:
      • Required-tests partly missing (compliance < 100%)  → Additional Test
      • Required-tests on file with adverse findings        → Rateable
      • All tests received, all normal/borderline          → Medically Std
      • Active serious condition reported                  → Postpone (or Decline)
      • Lab-abnormalities count high + medical_risk HIGH    → Postpone
    """
    med = case.get("medical") or {}
    personal = med.get("personal_medical_history") or []
    abn_count = _safe_int(abnormal_calc.value)
    risk_band = (medical_risk_calc.value or "").upper()
    comp_pct = _safe_float(compliance_calc.value)

    serious_active = {
        "heart disease",
        "cardiovascular",
        "stroke",
        "cancer",
        "kidney disease",
        "renal failure",
        "uncontrolled diabetes",
    }
    has_serious_active = any(
        (c.get("condition") or "").lower() in serious_active
        and (c.get("status") or "").lower() not in ("resolved", "history", "controlled")
        for c in personal
    )

    if has_serious_active and abn_count >= 4:
        label, reason = "Decline", "Active serious condition + multiple abnormal labs."
    elif has_serious_active:
        label, reason = "Postpone", "Active serious condition reported."
    elif comp_pct < 100:
        # Any missing required test → Additional Test
        missing = (required_tests_calc.inputs or {}).get("tests") or []
        recd = (compliance_calc.inputs or {}).get("received") or []
        n_missing = max(0, len(missing) - len(recd))
        label = "Additional Test"
        reason = f"Test compliance {comp_pct:.0f}%; {n_missing} required test(s) outstanding."
    elif risk_band == "HIGH":
        label, reason = "Postpone", "Medical risk HIGH; refer to medical UW."
    elif abn_count >= 3:
        label = "Rateable"
        reason = f"{abn_count} lab abnormalities — rate up per medical grid."
    elif risk_band == "MODERATE":
        label = "Repeat Test"
        reason = "Medical risk MODERATE — recheck pending tests / borderline values."
    else:
        label, reason = "Medically Std", "All tests received and within range; no adverse findings."

    return Calculation(
        name="medical_uw_terminal_decision",
        value=label,
        unit="label",
        method="rule_cascade",
        function="underwriting_platform.engine.medical.compute_medical_uw_terminal_decision",
        formula=(
            "decide(serious_active, comp_pct, risk_band, abn_count) "
            "-> {Medically Std | Rateable | Additional Test | "
            "Repeat Test | Postpone | Decline}"
        ),
        inputs={
            "medical_risk_band": risk_band,
            "test_compliance": comp_pct,
            "lab_abnormalities": abn_count,
            "serious_active": has_serious_active,
        },
        sources=medical_risk_calc.sources,
        rule="datamatics_layout.FORMAT.row_70",
        note=reason,
    )


# ──────────────────────────────────────────────────────────────────
# Fraud risk
# ──────────────────────────────────────────────────────────────────


def compute_fraud_risk(case: dict) -> Calculation:
    med = case.get("medical") or {}
    fraud = med.get("fraud_signals") or {}
    cotinine = _safe_float(fraud.get("urine_cotinine_ng_ml"))
    threshold = _safe_float(fraud.get("cotinine_threshold") or 10)
    declared = (fraud.get("declared_smoker") or "").lower()
    mismatch = bool(fraud.get("cotinine_smoker_mismatch"))

    band = "LOW"
    drivers: list[str] = []
    if mismatch:
        band = "HIGH"
        drivers.append(f"Cotinine {cotinine} ng/mL ≥ {threshold} but declared smoker = {declared}.")
    elif cotinine >= (threshold * 0.5) and declared == "no":
        band = "MODERATE"
        drivers.append(f"Cotinine {cotinine} ng/mL borderline against non-smoker decl.")

    # Personal vs Video MER contradictions
    personal = med.get("personal_medical_history") or []
    contras = [c for c in personal if c.get("contradiction")]
    if contras:
        band = max(band, "MODERATE", key=_RISK_RANK.__getitem__)
        drivers.append(f"{len(contras)} MER↔VMER contradictions.")

    return Calculation(
        name="fraud_risk",
        value=band,
        unit="band",
        method="cotinine_mismatch_plus_mer_vmer",
        function="underwriting_platform.engine.medical.compute_fraud_risk",
        formula="HIGH if cotinine_smoker_mismatch; MODERATE on borderline / contradictions",
        inputs={
            "cotinine_ng_ml": cotinine,
            "cotinine_threshold": threshold,
            "declared_smoker": declared or None,
            "mismatch": mismatch,
            "n_mer_vmer_contradictions": len(contras),
            "drivers": drivers,
        },
        sources=[
            Source(
                document="medical.fraud_signals + personal_medical_history",
                agent="medical",
                field_path="medical.fraud_signals",
            )
        ],
        rule="medical_rules.MEDICAL_GRID.special_rules — Cotinine threshold 10 ng/mL",
        note=" | ".join(drivers) or "No fraud signals.",
    )


_RISK_RANK = {"LOW": 0, "MODERATE": 1, "HIGH": 2, "N/A": -1}


# ──────────────────────────────────────────────────────────────────────
# Deterministic medical-block overrides (run BEFORE risk calculations)
# ──────────────────────────────────────────────────────────────────────

import re as _re

# Strings in `interpretation` that tell us the lab's auto-impression has
# been corrected by the examining physician. When any of these phrases
# is present, the agent's `status` is forced to "normal".
_PHYSICIAN_NORMAL_OVERRIDE_PATTERNS = [
    _re.compile(r"overwritten\s+as\s+normal", _re.IGNORECASE),
    _re.compile(r"overrid(?:den|ed)\s+to\s+normal", _re.IGNORECASE),
    _re.compile(r"reclassified\s+as\s+normal", _re.IGNORECASE),
    _re.compile(r"final[:\s]+normal\s+ecg", _re.IGNORECASE),
    _re.compile(r"classified\s+as\s+normal\s+ecg\s+by\s+dr", _re.IGNORECASE),
    _re.compile(r"normal\s+ecg\s+by\s+(?:examining\s+)?phys", _re.IGNORECASE),
]


def apply_medical_overrides(case: dict) -> dict:
    """In-place adjustments to `case['medical']` that codify standard
    underwriter conventions:

      ECG  — if the interpretation text says the lab's Borderline /
             Abnormal call was overwritten / overridden as Normal by an
             examining physician, force ECG status to 'normal'.
      TMT  — if THR achieved < 85 % and recommendation is missing or
             still 'Negative', force recommendation to 'Postponed'
             with a structured reason.

    Returns the same case dict for chaining. Audit notes are appended
    to a private list under `medical._engine_overrides[]` so the PDF /
    DB record both the original agent value and the override.
    """
    med = case.get("medical")
    if not isinstance(med, dict):
        return case
    overrides: list[dict] = []

    # ── ECG ──
    ecg = med.get("ecg") or {}
    interp = ecg.get("interpretation") or ""
    status = (ecg.get("status") or "").lower()
    if status in ("abnormal", "borderline") and any(
        p.search(interp) for p in _PHYSICIAN_NORMAL_OVERRIDE_PATTERNS
    ):
        overrides.append(
            {
                "field": "ecg.status",
                "from": ecg.get("status"),
                "to": "normal",
                "reason": "Physician override detected in interpretation text.",
                "function": "underwriting_platform.engine.medical.apply_medical_overrides",
            }
        )
        ecg["status"] = "normal"
        med["ecg"] = ecg

    # ── TMT ──
    tmt = med.get("tmt") or {}
    if tmt.get("performed"):
        thr_pct = tmt.get("thr_achieved_pct")
        try:
            thr_val = float(thr_pct) if thr_pct is not None else None
        except (TypeError, ValueError):
            thr_val = None
        rec_now = (tmt.get("recommendation") or "").strip().lower()
        if thr_val is not None and thr_val < 85 and rec_now in ("", "negative", "null", "none"):
            overrides.append(
                {
                    "field": "tmt.recommendation",
                    "from": tmt.get("recommendation"),
                    "to": "Postponed",
                    "reason": f"THR achieved {thr_val}% < 85% (study not diagnostic).",
                    "function": "underwriting_platform.engine.medical.apply_medical_overrides",
                }
            )
            tmt["recommendation"] = "Postponed"
            tmt["recommendation_reason"] = (
                (tmt.get("recommendation_reason") or "")
                + f"  [engine-applied: THR achieved {thr_val}% < 85%]"
            ).strip()
            med["tmt"] = tmt

    # ── BP — apply "mean of 2nd + 3rd reading" convention ──
    # The medical agent's per-case BP-selection has been observed to
    # drift (mean of three / first / lowest / silent). Codify the UW
    # convention deterministically here so all 99 cases on the current
    # batch align without re-extraction. The agent prompt is also
    # updated for the next batch.
    bp_override = _apply_bp_convention(med)
    if bp_override:
        overrides.append(bp_override)

    # ── HABITS VOLUME BACKFILL ──
    # The 2026-05-26 schema extension added structured fields for
    # cigarettes_per_day, years_smoking, alcohol_type, alcohol_units_per_week.
    # For the current batch (extracted before the schema change), the
    # values are usually present in PMH free-text ("Tobacco use — active
    # cigarette smoker (4/day)" / "Alcohol — hard liquor, 2 pegs/day").
    # Backfill them deterministically so EMR-by-volume bands fire.
    habits_backfilled = _backfill_habits_volume(med)
    if habits_backfilled:
        overrides.extend(habits_backfilled)

    # ── documents_present BACKFILL FROM lab_results[] ──
    # The medical agent sometimes extracts a test row into lab_results[]
    # but leaves the corresponding documents_present.<flag> = false/null.
    # Test-compliance then reports the test as missing even though the
    # report IS on file. Bridge: any test name appearing in lab_results
    # sets the corresponding documents_present flag to True (preserving
    # the original under documents_present._agent_<flag> for audit).
    docs_backfilled = _backfill_docs_present_from_labs(med)
    if docs_backfilled:
        overrides.extend(docs_backfilled)

    # ── MER subject_name / subject_dob INFERENCE ──
    # The current medical agent schema doesn't extract MER subject_name /
    # subject_dob (next-batch schema adds them). Best-effort inference
    # from medical free-text vs applicant.applicant_name so the renderer's
    # Name / DOB match cells show "Yes — inferred" instead of "Not
    # verifiable" on the existing 99 cases.
    applicant_block = case.get("applicant") or {}
    subj_inferred = _infer_mer_subject_from_text(med, applicant_block)
    if subj_inferred:
        overrides.extend(subj_inferred)

    if overrides:
        med.setdefault("_engine_overrides", []).extend(overrides)
        case["medical"] = med
    return case


# ── BP convention helper ──────────────────────────────────────────────

# Parser for individual BP readings inside `vitals.bp_text`. Tolerant of:
#   "128/88, 120/84, 118/84"
#   "126/80, 126/60, 120/80"
#   "first 130/90, second 128/86, third 124/84"
#   "(1st) 138/92 (2nd) 128/86 (3rd) 124/84"
# Returns a list of (sys, dia) tuples in document order.
_BP_READING_RE = _re.compile(r"(?<!\d)(\d{2,3})\s*/\s*(\d{2,3})(?!\d)")


def _parse_bp_readings(bp_text: str) -> list[tuple[int, int]]:
    """Extract `<sys>/<dia>` readings from bp_text.

    The agent's bp_text often takes the form:
      "<headline> mmHg (lowest of three readings: A/a, B/b, C/c)"
    where <headline> is the agent's pick. We want the raw readings
    inside the parenthetical, NOT the headline.

    Strategy:
      1. If the text contains a "readings" marker (readings:, readings are,
         three readings:, etc.), parse only the substring AFTER that marker.
      2. Otherwise, parse the whole text and use everything.
      3. De-dupe immediate repeats.
    """
    text = bp_text or ""
    # Find a "readings" marker — use the LAST one so a phrase like
    # "second reading; three readings: A, B, C" lands on the colon.
    marker_re = _re.compile(r"reading[s]?\s*[:are]+\s*", _re.IGNORECASE)
    last = None
    for m in marker_re.finditer(text):
        last = m
    if last:
        text = text[last.end() :]
    out: list[tuple[int, int]] = []
    for m in _BP_READING_RE.finditer(text):
        try:
            s = int(m.group(1))
            d = int(m.group(2))
        except (TypeError, ValueError):
            continue
        if not (60 <= s <= 260 and 30 <= d <= 160):
            continue
        if out and out[-1] == (s, d):
            continue
        out.append((s, d))
    return out


def _apply_bp_convention(med: dict) -> dict | None:
    """Re-pick BP from the readings in `vitals.bp_text` per the canonical
    UW convention: mean of 2nd + 3rd (drop 1st as white-coat).
    Edge cases:
      • 2 readings → mean of both
      • 1 reading  → use it as-is (no override emitted)
      • 0 readings parseable from bp_text → no override
      • Agent already followed convention (bp_text contains "mean of 2nd"
        or "1st ... discarded" markers) → no override
    Returns the override audit dict if applied, else None. Mutates
    `med['vitals']` in place when applied.
    """
    vitals = med.get("vitals") or {}
    bp_text = vitals.get("bp_text") or ""

    # Already-applied guard — agent followed the convention.
    bp_lower = bp_text.lower()
    if (
        "mean of 2nd" in bp_lower
        or "average of 2nd" in bp_lower
        or "1st reading discarded" in bp_lower
        or "1st reading dropped" in bp_lower
        or "1st reading excluded" in bp_lower
    ):
        return None

    readings = _parse_bp_readings(bp_text)
    if len(readings) < 2:
        return None  # single reading — nothing to recompute

    if len(readings) >= 3:
        kept = readings[1:3]  # drop 1st, average 2nd+3rd
        rule = "mean of 2nd + 3rd readings (1st dropped — white-coat)"
    else:  # exactly 2 readings
        kept = readings
        rule = "mean of both readings (only 2 available)"

    new_sys = int(round(sum(r[0] for r in kept) / len(kept)))
    new_dia = int(round(sum(r[1] for r in kept) / len(kept)))

    old_sys = _safe_int(vitals.get("bp_systolic_mmhg"))
    old_dia = _safe_int(vitals.get("bp_diastolic_mmhg"))

    if old_sys == new_sys and old_dia == new_dia:
        return None  # already aligned with the convention

    new_text = (
        f"{new_sys}/{new_dia} mmHg ({rule}; "
        + ", ".join(f"{s}/{d}" for s, d in readings)
        + f"; engine-applied UW convention. Original agent pick: "
        + f"{old_sys}/{old_dia})."
    )
    vitals["bp_systolic_mmhg"] = new_sys
    vitals["bp_diastolic_mmhg"] = new_dia
    # Keep original bp_text under a side-key for audit; overwrite the
    # display text so the rest of the pipeline + renderer pick it up.
    vitals.setdefault("bp_text_original_agent", bp_text)
    vitals["bp_text"] = new_text
    med["vitals"] = vitals
    return {
        "field": "vitals.bp_*",
        "from": f"{old_sys}/{old_dia} ({bp_text[:60]})",
        "to": f"{new_sys}/{new_dia} ({rule})",
        "reason": "Engine BP convention applied: mean of 2nd + 3rd.",
        "function": "underwriting_platform.engine.medical._apply_bp_convention",
    }


# ── Habits-volume backfill (cigs/day, alcohol units/wk, type) ─────────

_CIGS_PER_DAY_PATTERNS = [
    _re.compile(r"(\d{1,2})\s*(?:cig|cigarette|cigs|sticks?)\s*(?:/|per)\s*day", _re.IGNORECASE),
    _re.compile(r"(\d{1,2})\s*/\s*day", _re.IGNORECASE),
    _re.compile(r"(\d{1,2})\s*(?:cig|cigarette|cigs|sticks?)\s*a\s*day", _re.IGNORECASE),
    _re.compile(r"smokes?\s*(\d{1,2})\s*(?:cig|cigarette|cigs|sticks?)?\s*daily", _re.IGNORECASE),
]
_YEARS_SMOKING_PATTERNS = [
    _re.compile(r"smoking\s*(?:for\s*)?(\d{1,2})\s*(?:years?|yrs?)", _re.IGNORECASE),
    _re.compile(r"(\d{1,2})\s*(?:years?|yrs?)\s*(?:of\s*)?(?:smoking|smoker)", _re.IGNORECASE),
]
_ALCOHOL_TYPE_PATTERNS = {
    "hard_liquor": _re.compile(
        r"\b(hard\s*liquor|whisky|whiskey|rum|vodka|brandy|gin)\b", _re.IGNORECASE
    ),
    "wine": _re.compile(r"\bwine\b", _re.IGNORECASE),
    "beer": _re.compile(r"\bbeer\b", _re.IGNORECASE),
}
_PEGS_PATTERNS = [
    # pegs / drinks / units per day / per week / per session
    (
        _re.compile(r"(\d{1,2})\s*(?:pegs?|drinks?|units?)\s*(?:/|per)\s*day", _re.IGNORECASE),
        7,
    ),  # ×7 for weekly
    (
        _re.compile(
            r"(\d{1,2})\s*(?:pegs?|drinks?|units?)\s*(?:/|per)\s*(?:wk|week)", _re.IGNORECASE
        ),
        1,
    ),
    (_re.compile(r"(\d{1,2})\s*(?:pegs?|drinks?|units?)\s*a\s*day", _re.IGNORECASE), 7),
    (_re.compile(r"(\d{1,2})\s*(?:pegs?|drinks?|units?)\s*a\s*week", _re.IGNORECASE), 1),
]


def _backfill_habits_volume(med: dict) -> list[dict]:
    """Mine PMH free-text + agent_meta notes for smoking / alcohol
    volume hints when the structured `habits_declared` fields are blank.
    Used for the existing 99-case batch where the schema was extended
    after extraction. Returns a list of override audit dicts.
    """
    overrides: list[dict] = []
    habits = med.get("habits_declared") or {}
    pmh = med.get("personal_medical_history") or []
    notes = med.get("extraction_notes") or ""
    agent_notes = (med.get("agent_meta") or {}).get("notes") or ""

    # Gather all free-text candidates
    haystack = " | ".join(
        [
            notes,
            agent_notes,
            *(
                str(c.get("condition") or "") + " " + str(c.get("detail") or "")
                for c in pmh
                if isinstance(c, dict)
            ),
        ]
    )
    if not haystack.strip():
        return overrides

    # Cigarettes per day
    if habits.get("cigarettes_per_day") in (None, ""):
        for pat in _CIGS_PER_DAY_PATTERNS:
            m = pat.search(haystack)
            if m:
                try:
                    n = int(m.group(1))
                    if 1 <= n <= 80:
                        habits["cigarettes_per_day"] = n
                        overrides.append(
                            {
                                "field": "habits_declared.cigarettes_per_day",
                                "from": None,
                                "to": n,
                                "reason": f"Backfilled from PMH free-text: {m.group(0)!r}.",
                                "function": "underwriting_platform.engine.medical._backfill_habits_volume",
                            }
                        )
                        break
                except (TypeError, ValueError):
                    continue

    # Years smoking
    if habits.get("years_smoking") in (None, ""):
        for pat in _YEARS_SMOKING_PATTERNS:
            m = pat.search(haystack)
            if m:
                try:
                    y = int(m.group(1))
                    if 1 <= y <= 70:
                        habits["years_smoking"] = y
                        overrides.append(
                            {
                                "field": "habits_declared.years_smoking",
                                "from": None,
                                "to": y,
                                "reason": f"Backfilled from PMH free-text: {m.group(0)!r}.",
                                "function": "underwriting_platform.engine.medical._backfill_habits_volume",
                            }
                        )
                        break
                except (TypeError, ValueError):
                    continue

    # Pack-years (derived if both above present)
    if (
        habits.get("tobacco_pack_years") in (None, "")
        and isinstance(habits.get("cigarettes_per_day"), (int, float))
        and isinstance(habits.get("years_smoking"), (int, float))
    ):
        py = round((float(habits["cigarettes_per_day"]) / 20.0) * float(habits["years_smoking"]), 1)
        habits["tobacco_pack_years"] = py
        overrides.append(
            {
                "field": "habits_declared.tobacco_pack_years",
                "from": None,
                "to": py,
                "reason": "Derived = (cigs_per_day / 20) × years_smoking.",
                "function": "underwriting_platform.engine.medical._backfill_habits_volume",
            }
        )

    # Alcohol type
    if habits.get("alcohol_type") in (None, ""):
        for atype, pat in _ALCOHOL_TYPE_PATTERNS.items():
            m = pat.search(haystack)
            if m:
                habits["alcohol_type"] = atype
                overrides.append(
                    {
                        "field": "habits_declared.alcohol_type",
                        "from": None,
                        "to": atype,
                        "reason": f"Backfilled from PMH free-text: {m.group(0)!r}.",
                        "function": "underwriting_platform.engine.medical._backfill_habits_volume",
                    }
                )
                break

    # Alcohol units per week
    if habits.get("alcohol_units_per_week") in (None, ""):
        for pat, factor in _PEGS_PATTERNS:
            m = pat.search(haystack)
            if m:
                try:
                    n = int(m.group(1))
                    if 1 <= n <= 50:
                        upw = n * factor
                        habits["alcohol_units_per_week"] = upw
                        overrides.append(
                            {
                                "field": "habits_declared.alcohol_units_per_week",
                                "from": None,
                                "to": upw,
                                "reason": f"Backfilled from PMH free-text: {m.group(0)!r} (×{factor} → /wk).",
                                "function": "underwriting_platform.engine.medical._backfill_habits_volume",
                            }
                        )
                        break
                except (TypeError, ValueError):
                    continue

    if overrides:
        med["habits_declared"] = habits
    return overrides


# ── documents_present backfill from lab_results[] ─────────────────────

# Test-name keyword → documents_present flag mapping. Each key is a
# substring; matched case-insensitively against `lab_results[i].test`.
_LAB_TEST_TO_DOC_FLAG: list[tuple[tuple[str, ...], str]] = [
    (("hba1c", "glycated", "glycosylated"), "hba1c"),
    (("psa", "prostate-specific"), "psa"),
    (("anti hcv", "anti-hcv", "hcv antibody"), "anti_hcv"),
    (("hiv",), "hiv"),
    (("hbsag", "hep b surface", "hepatitis b s"), "hbsag"),
    (("cotinine",), "urine_cotinine"),
    (("urine microalbumin",), "urine_microalbumin"),
    (("thyroid", "tsh", "t3", "t4"), "thyroid_function"),
    (("chest x-ray", "chest xray", "cxr"), "chest_xray"),
    (("usg abdomen", "ultrasound abdomen"), "usg_abdomen"),
    (("2d echo", "echocardiogram"), "tmt_2d_echo"),
    (
        (
            "fundoscopy",
            "fundus",
        ),
        "fundoscopy",
    ),
    (("tumor marker", "afp", "cea", "ca 125", "ca 19-9", "ca 15-3"), "tumor_markers"),
    (("pft", "pulmonary function"), "pft"),
]


def _backfill_docs_present_from_labs(med: dict) -> list[dict]:
    """When a test appears in `lab_results[]` but the corresponding
    `documents_present.<flag>` is false/null, set the flag to True.

    Edge case: HIV / HBsAg / Anti-HCV are commonly bundled inside the
    BPB (Basic Pathology Battery) panel — when BPB is present the
    individual flags should naturally be True too. The agent has been
    inconsistent about setting them; this backfill closes the gap.
    """
    labs = med.get("lab_results") or []
    docs = med.get("documents_present") or {}
    if not labs:
        return []
    overrides: list[dict] = []
    for row in labs:
        if not isinstance(row, dict):
            continue
        test_name = (row.get("test") or "").lower()
        if not test_name:
            continue
        for needles, flag in _LAB_TEST_TO_DOC_FLAG:
            if any(n in test_name for n in needles):
                if not docs.get(flag):
                    # Preserve agent's original under a side key for audit
                    docs.setdefault(f"_agent_{flag}", docs.get(flag))
                    docs[flag] = True
                    overrides.append(
                        {
                            "field": f"documents_present.{flag}",
                            "from": docs.get(f"_agent_{flag}"),
                            "to": True,
                            "reason": (
                                f"Backfilled from lab_results[] — test "
                                f"{row.get('test')!r} is present on file."
                            ),
                            "function": "underwriting_platform.engine.medical._backfill_docs_present_from_labs",
                        }
                    )
                break  # one flag per row
    if overrides:
        med["documents_present"] = docs
    return overrides


# ── MER subject_name / subject_dob inference ──────────────────────────


def _infer_mer_subject_from_text(med: dict, applicant: dict) -> list[dict]:
    """Best-effort inference of MER subject identity for the current 99
    cases where the medical schema doesn't carry subject_name / subject_dob.

    Strategy:
      - If subject_name already set on med, no-op.
      - Else: scan medical's free-text fields (extraction_notes,
        agent_meta.notes, lab_results[*].source_doc, ecg.interpretation,
        tmt.interpretation, etc.) for the applicant's surname OR first
        name. If found → set subject_name = applicant_name with origin
        marker '__inferred_from_medical_text'.
      - subject_dob: try to find the applicant's DOB digits in the same
        haystack (DD-MM-YYYY / YYYY-MM-DD / DD/MM/YYYY shapes).
    """
    if med.get("subject_name"):
        return []
    applicant_name = (applicant.get("applicant_name") or "").strip()
    if not applicant_name:
        return []
    tokens = [
        t
        for t in applicant_name.replace(".", " ").split()
        if len(t) >= 3 and t.lower() not in ("mr", "mrs", "ms", "dr", "shri", "smt", "kumari")
    ]
    if not tokens:
        return []

    haystack_parts: list[str] = []
    for k in ("extraction_notes",):
        if med.get(k):
            haystack_parts.append(str(med[k]))
    am = med.get("agent_meta") or {}
    if am.get("notes"):
        haystack_parts.append(str(am["notes"]))
    for r in med.get("lab_results") or []:
        if isinstance(r, dict) and r.get("source_doc"):
            haystack_parts.append(str(r["source_doc"]))
    for k in ("ecg", "tmt"):
        sub = med.get(k) or {}
        if sub.get("interpretation"):
            haystack_parts.append(str(sub["interpretation"]))
        if sub.get("recommendation_reason"):
            haystack_parts.append(str(sub["recommendation_reason"]))
    haystack = " | ".join(haystack_parts).lower()
    if not haystack:
        return []

    hits = sum(1 for t in tokens if t.lower() in haystack)
    if hits == 0:
        return []  # no token match — leave Not verifiable

    overrides: list[dict] = []
    med["subject_name"] = applicant_name
    med["_subject_name_origin"] = "inferred_from_medical_text"
    overrides.append(
        {
            "field": "subject_name",
            "from": None,
            "to": applicant_name,
            "reason": f"Applicant name token(s) found in medical free-text ({hits}/{len(tokens)} tokens matched).",
            "function": "underwriting_platform.engine.medical._infer_mer_subject_from_text",
        }
    )

    # DOB inference — look for the applicant's DOB digits in any order
    app_dob = (applicant.get("dob") or "").strip()
    if app_dob:
        dob_digits = "".join(c for c in app_dob if c.isdigit())[:8]
        if dob_digits:
            # Compare normalized digit-only sub-strings — catches dd-mm-yyyy etc.
            hs_digits = "".join(c for c in haystack if c.isdigit())
            yy = dob_digits[:4] if app_dob[:4].isdigit() else dob_digits[4:8]
            if yy and yy in hs_digits:
                med["subject_dob"] = app_dob
                med["_subject_dob_origin"] = "inferred_from_medical_text"
                overrides.append(
                    {
                        "field": "subject_dob",
                        "from": None,
                        "to": app_dob,
                        "reason": f"DOB year {yy!r} found in medical free-text.",
                        "function": "underwriting_platform.engine.medical._infer_mer_subject_from_text",
                    }
                )

    return overrides
