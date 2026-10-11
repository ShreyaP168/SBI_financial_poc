"""
engine/loading.py — EMR (Extra Mortality Rating) recommendation engine.

Produces a deterministic premium-loading recommendation per the medical
findings already extracted into case_json.medical. Each rule emits a
LoadingItem with: code / severity / +X EMR / driver / source. The
top-level `recommended_emr` is the SUM of all firing items (capped at
+200 — anything above that is typically a DECLINE per TALIC practice).

Deliberately conservative: rules trigger on findings the medical agent
already flagged (HBsAg+, raised LFTs, BMI obese, BP stage 1/2, HbA1c
diabetic range, etc.). For "edge" findings where the underwriter
should make the call (single-finding fatty liver alone, mild renal
calculus, etc.), the rule emits a 'note' item with severity=info and
+0 — visible on the case sheet for the UW to consider.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict


@dataclass
class LoadingItem:
    code: str  # short token, e.g. 'HBSAG_POSITIVE'
    severity: str  # 'critical' | 'high' | 'medium' | 'low' | 'info'
    emr_delta: int  # +25 / +50 / +100 / 0 (info-only)
    title: str
    driver: str  # 1-line reason
    source: str  # 'medical.personal_medical_history' / 'lab_results.HbA1c' / …
    suggestion: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LoadingRecommendation:
    recommended_emr: int  # sum across items, capped +200
    decline_recommended: bool  # True when sum > 200 OR critical-finding hard rule
    decline_reason: str = ""
    items: list[LoadingItem] = field(default_factory=list)
    function: str = "underwriting_platform.engine.loading.compute_loading"
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "recommended_emr": self.recommended_emr,
            "decline_recommended": self.decline_recommended,
            "decline_reason": self.decline_reason,
            "function": self.function,
            "items": [i.to_dict() for i in self.items],
            "notes": self.notes,
        }


# ─────────────────────────────────────────────────────────────────────
# Rule helpers
# ─────────────────────────────────────────────────────────────────────


def _safe_float(v):
    try:
        return float(v) if v not in (None, "", "—") else None
    except (TypeError, ValueError):
        return None


def _personal_active_substring(personal: list, *needles: str) -> bool:
    for ph in personal or []:
        if (ph.get("status") or "").lower() != "active":
            continue
        cn = (ph.get("condition") or "").lower()
        if any(n in cn for n in needles):
            return True
    return False


def _lab_value(lab_results: list, test_name_substr: str) -> tuple[float | None, str]:
    """Return (numeric value, status) for the first lab row whose test
    name contains test_name_substr."""
    for r in lab_results or []:
        if not isinstance(r, dict):
            continue
        t = (r.get("test") or "").lower()
        if test_name_substr.lower() in t:
            v = _safe_float(r.get("value"))
            return v, (r.get("status") or "").upper()
    return None, ""


# ─────────────────────────────────────────────────────────────────────
# Main entry
# ─────────────────────────────────────────────────────────────────────


def compute_loading(case: dict) -> LoadingRecommendation:
    """Run every loading rule over case.medical and build the recommendation.
    Returns LoadingRecommendation. If no medical block is present, returns
    a 0-EMR recommendation with notes='no medical block on file'."""
    med = case.get("medical") or {}
    if not med:
        return LoadingRecommendation(
            recommended_emr=0,
            decline_recommended=False,
            notes="No medical block — engine cannot recommend EMR.",
        )

    items: list[LoadingItem] = []
    personal = med.get("personal_medical_history") or []
    labs = med.get("lab_results") or []
    vitals = med.get("vitals") or {}
    habits = med.get("habits_declared") or {}
    fraud = med.get("fraud_signals") or {}

    # ── 1. HBsAg+ (Hepatitis B carrier) ──
    hbsag_pos = _personal_active_substring(personal, "hbsag")
    sgot_v, _ = _lab_value(labs, "sgot")
    sgpt_v, _ = _lab_value(labs, "sgpt")
    if hbsag_pos or _personal_active_substring(personal, "hepatitis b"):
        # Look for raised LFTs
        liver_raised = (sgot_v is not None and sgot_v > 40) or (sgpt_v is not None and sgpt_v > 40)
        if liver_raised:
            items.append(
                LoadingItem(
                    code="HBSAG_POSITIVE_WITH_RAISED_LFT",
                    severity="high",
                    emr_delta=100,
                    title="HBsAg+ with raised LFTs",
                    driver=(
                        f"HBsAg Positive AND liver enzymes elevated "
                        f"(SGOT={sgot_v}, SGPT={sgpt_v}). Per TALIC term UW "
                        "convention, EMR +100 with mandatory Hep-B Questionnaire."
                    ),
                    source="medical.personal_medical_history + lab_results.SGOT/SGPT",
                    suggestion="Request Hep-B Questionnaire + HBsAg quantitative + Anti-HCV.",
                )
            )
        else:
            items.append(
                LoadingItem(
                    code="HBSAG_POSITIVE",
                    severity="high",
                    emr_delta=50,
                    title="HBsAg+ (LFTs not raised or not on file)",
                    driver=(
                        "HBsAg Positive declared. LFTs not extractable / not on file. "
                        "EMR +50 with mandatory follow-up tests."
                    ),
                    source="medical.personal_medical_history",
                    suggestion="Request full LFT panel + Hep-B Questionnaire + HBsAg quantitative + Anti-HCV.",
                )
            )

    # ── 2. BMI obese + dyslipidaemia / HBP ──
    bmi = _safe_float(vitals.get("bmi"))
    sys_bp = _safe_float(vitals.get("bp_systolic_mmhg"))
    dia_bp = _safe_float(vitals.get("bp_diastolic_mmhg"))
    chol_v, _ = _lab_value(labs, "total cholesterol")
    ldl_v, _ = _lab_value(labs, "ldl")
    hdl_v, _ = _lab_value(labs, "hdl")
    metabolic_concerns = []
    if bmi and bmi >= 35:
        metabolic_concerns.append(f"BMI {bmi} (obese class II+)")
    if (sys_bp and sys_bp >= 140) or (dia_bp and dia_bp >= 90):
        metabolic_concerns.append(f"BP {int(sys_bp or 0)}/{int(dia_bp or 0)} (Stage 1+)")
    if chol_v and chol_v >= 240:
        metabolic_concerns.append(f"Total chol {chol_v} ≥ 240")
    if ldl_v and ldl_v >= 160:
        metabolic_concerns.append(f"LDL {ldl_v} ≥ 160")
    if hdl_v and hdl_v < 35:
        metabolic_concerns.append(f"HDL {hdl_v} < 35")

    if len(metabolic_concerns) >= 2:
        items.append(
            LoadingItem(
                code="METABOLIC_SYNDROME_RISK",
                severity="medium",
                emr_delta=50,
                title="Metabolic syndrome risk",
                driver=(
                    "Two or more concurrent risk factors: " + "; ".join(metabolic_concerns) + "."
                ),
                source="medical.vitals + lab_results",
                suggestion="EMR +50; consider repeat lipid panel post weight loss; UW review at renewal.",
            )
        )
    elif bmi and bmi >= 35:
        items.append(
            LoadingItem(
                code="BMI_OBESE_CLASS2_PLUS",
                severity="medium",
                emr_delta=25,
                title="Obese class II+",
                driver=f"BMI {bmi} (class II / morbid). Stand-alone — no other RF on file.",
                source="medical.vitals.bmi",
                suggestion="EMR +25; monitor at renewal.",
            )
        )

    # ── 3. HbA1c (diabetic range) ──
    hba1c_v, _ = _lab_value(labs, "hba1c")
    if hba1c_v is not None:
        if hba1c_v >= 7.5:
            items.append(
                LoadingItem(
                    code="HBA1C_UNCONTROLLED",
                    severity="high",
                    emr_delta=100,
                    title="HbA1c ≥ 7.5% (uncontrolled)",
                    driver=f"HbA1c {hba1c_v}% indicates uncontrolled diabetes.",
                    source="medical.lab_results.HbA1c",
                    suggestion="Endocrinologist clearance + repeat HbA1c in 3 mo.",
                )
            )
        elif hba1c_v >= 6.5:
            items.append(
                LoadingItem(
                    code="HBA1C_DIABETIC",
                    severity="medium",
                    emr_delta=50,
                    title="HbA1c diabetic range",
                    driver=f"HbA1c {hba1c_v}% in the diabetic range (≥6.5%).",
                    source="medical.lab_results.HbA1c",
                    suggestion="Confirm with FBS / repeat HbA1c; EMR +50.",
                )
            )

    # ── 3b. Smoking volume bands (cigarettes per day) ──
    # Standard TALIC convention: 1-10/day = +25, 11-20/day = +50, >20/day = +100.
    # Pack-years dimension (cigs/20 × years) also informs the band when
    # available — heavy long-term smoking gets the upper band even at moderate
    # daily count. The cotinine-mismatch fraud rule below is orthogonal.
    smoker_now = (habits.get("smoker") or "").strip().lower() == "yes"
    cigs_per_day = habits.get("cigarettes_per_day")
    pack_years = habits.get("tobacco_pack_years")
    if smoker_now and cigs_per_day is not None:
        try:
            cpd = int(cigs_per_day)
        except (TypeError, ValueError):
            cpd = None
        if cpd is not None and cpd > 0:
            if cpd > 20:
                items.append(
                    LoadingItem(
                        code="SMOKING_HEAVY",
                        severity="high",
                        emr_delta=100,
                        title=f"Heavy smoker — {cpd}/day",
                        driver=f"Declared cigarette consumption {cpd}/day exceeds 20/day band.",
                        source="medical.habits_declared.cigarettes_per_day",
                        suggestion="EMR +100; confirm against cotinine; consider cardiovascular medicals.",
                    )
                )
            elif cpd >= 11:
                items.append(
                    LoadingItem(
                        code="SMOKING_MODERATE",
                        severity="medium",
                        emr_delta=50,
                        title=f"Moderate smoker — {cpd}/day",
                        driver=f"Declared cigarette consumption {cpd}/day in 11-20 band.",
                        source="medical.habits_declared.cigarettes_per_day",
                        suggestion="EMR +50; standard smoker class.",
                    )
                )
            elif cpd >= 1:
                items.append(
                    LoadingItem(
                        code="SMOKING_LIGHT",
                        severity="low",
                        emr_delta=25,
                        title=f"Light smoker — {cpd}/day",
                        driver=f"Declared cigarette consumption {cpd}/day in 1-10 band.",
                        source="medical.habits_declared.cigarettes_per_day",
                        suggestion="EMR +25; baseline smoker class.",
                    )
                )
    elif smoker_now and pack_years is not None:
        # Long-term-burden fallback when cigs/day not available
        try:
            py = float(pack_years)
        except (TypeError, ValueError):
            py = None
        if py is not None and py >= 20:
            items.append(
                LoadingItem(
                    code="SMOKING_PACK_YEARS_HIGH",
                    severity="medium",
                    emr_delta=50,
                    title=f"High pack-year burden — {py:.0f} pack-years",
                    driver=f"Tobacco pack-years {py:.0f} ≥ 20 indicates significant lifetime burden.",
                    source="medical.habits_declared.tobacco_pack_years",
                    suggestion="EMR +50; rate up for long-term exposure.",
                )
            )

    # ── 3c. Alcohol volume bands (units per week) ──
    # Loose convention: ≤14 units/wk = no EMR; 15-28 = +25; 29-42 = +50;
    # >42 = +100 + alcohol questionnaire / LFT panel. "Hard liquor" type
    # alone (without volume) does not add EMR — the type informs UW but
    # the volume drives the rate-up.
    alcohol_now = (habits.get("alcohol") or "").strip().lower() == "yes"
    units_per_week = habits.get("alcohol_units_per_week")
    if alcohol_now and units_per_week is not None:
        try:
            upw = float(units_per_week)
        except (TypeError, ValueError):
            upw = None
        if upw is not None and upw > 0:
            if upw > 42:
                items.append(
                    LoadingItem(
                        code="ALCOHOL_HEAVY",
                        severity="high",
                        emr_delta=100,
                        title=f"Heavy alcohol — {upw:.0f} units/wk",
                        driver=f"Alcohol consumption {upw:.0f} units/wk exceeds 42 (heavy-drinker band).",
                        source="medical.habits_declared.alcohol_units_per_week",
                        suggestion="EMR +100; require alcohol questionnaire + repeat LFT.",
                    )
                )
            elif upw >= 29:
                items.append(
                    LoadingItem(
                        code="ALCOHOL_HIGH",
                        severity="medium",
                        emr_delta=50,
                        title=f"High alcohol — {upw:.0f} units/wk",
                        driver=f"Alcohol consumption {upw:.0f} units/wk in 29-42 band.",
                        source="medical.habits_declared.alcohol_units_per_week",
                        suggestion="EMR +50; confirm LFT panel within range.",
                    )
                )
            elif upw >= 15:
                items.append(
                    LoadingItem(
                        code="ALCOHOL_MODERATE",
                        severity="low",
                        emr_delta=25,
                        title=f"Moderate alcohol — {upw:.0f} units/wk",
                        driver=f"Alcohol consumption {upw:.0f} units/wk in 15-28 band.",
                        source="medical.habits_declared.alcohol_units_per_week",
                        suggestion="EMR +25; note for renewal.",
                    )
                )

    # ── 4. Smoker fraud (cotinine mismatch) ──
    if fraud.get("cotinine_smoker_mismatch"):
        items.append(
            LoadingItem(
                code="COTININE_SMOKER_MISMATCH",
                severity="critical",
                emr_delta=0,  # not a loading item — DECLINE driver
                title="Smoker fraud (cotinine mismatch)",
                driver=(
                    f"Cotinine {fraud.get('urine_cotinine_ng_ml')} ng/mL ≥ "
                    f"{fraud.get('cotinine_threshold')} but declared smoker "
                    f"= {fraud.get('declared_smoker')}."
                ),
                source="medical.fraud_signals",
                suggestion="DECLINE recommended — material non-disclosure.",
            )
        )

    # ── 5. Active CAD / IHD / cardiac history ──
    if _personal_active_substring(
        personal, "ischaemic", "ischemic", "cad", "myocardial", "angina", "stent", "bypass", "cabg"
    ):
        items.append(
            LoadingItem(
                code="CAD_IHD_ACTIVE",
                severity="critical",
                emr_delta=0,
                title="Active CAD / IHD",
                driver="Active coronary artery disease / IHD on personal history.",
                source="medical.personal_medical_history",
                suggestion="Refer to medical UW; cardiac DECLINE / heavy substandard.",
            )
        )

    # ── 6. Active malignancy ──
    if _personal_active_substring(
        personal, "cancer", "malignancy", "carcinoma", "lymphoma", "leukaemia", "leukemia"
    ):
        items.append(
            LoadingItem(
                code="ACTIVE_MALIGNANCY",
                severity="critical",
                emr_delta=0,
                title="Active malignancy",
                driver="Active malignancy / cancer on personal history.",
                source="medical.personal_medical_history",
                suggestion="DECLINE / postpone until in remission ≥ 5 years.",
            )
        )

    # ── 7a. Fatty liver — Grade-aware ──
    # Per TALIC term UW Feb 2026: Grade I + normal LFTs is STD; Grade II
    # warrants EMR +25 (small loading even when LFTs are normal because
    # of the elevated NAFLD progression / metabolic-syndrome correlation);
    # Grade III is significant steatosis → EMR +50 + UW review. When the
    # condition was undisclosed on the MER (material non-disclosure flagged
    # by the agent or reconciler) add an extra +25.
    fatty_liver_phs = [
        ph
        for ph in personal
        if (ph.get("status") or "").lower() == "active"
        and "fatty liver" in (ph.get("condition") or "").lower()
    ]
    if fatty_liver_phs:
        # Skip when LFTs are raised — already covered by Hep-B / metabolic rules.
        lfts_raised = (sgot_v and sgot_v > 40) or (sgpt_v and sgpt_v > 40)
        if not lfts_raised:
            ph = fatty_liver_phs[0]
            cn = (ph.get("condition") or "").lower() + " " + (ph.get("detail") or "").lower()
            grade3 = any(g in cn for g in ("grade iii", "grade 3", "severe steatosis"))
            grade2 = any(g in cn for g in ("grade ii", "grade 2"))
            undisclosed = (
                "not declared" in cn
                or "not disclosed" in cn
                or "non-disclosure" in cn
                or "undisclosed" in cn
            )
            if grade3:
                base_emr, base_label = 50, "Fatty liver Grade III — EMR +50"
            elif grade2:
                base_emr, base_label = 25, "Fatty liver Grade II — EMR +25"
            else:
                base_emr, base_label = 0, "Fatty liver Grade I — STD"
            extra = 25 if (undisclosed and base_emr > 0) else 0
            total_emr = base_emr + extra
            sev = "info" if total_emr == 0 else "low" if total_emr <= 25 else "medium"
            driver = f"{base_label}." + (
                f" Undisclosed in MER → +{extra} extra (material non-disclosure)." if extra else ""
            )
            items.append(
                LoadingItem(
                    code=(
                        "FATTY_LIVER_GRADE3"
                        if grade3
                        else "FATTY_LIVER_GRADE2"
                        if grade2
                        else "FATTY_LIVER_GRADE1"
                    ),
                    severity=sev,
                    emr_delta=total_emr,
                    title=base_label + (f" (+{extra} non-disclosure)" if extra else ""),
                    driver=driver,
                    source="medical.personal_medical_history",
                    suggestion=(
                        "No loading; note for renewal."
                        if total_emr == 0
                        else "Confirm grade on next USG; UW review at renewal."
                    ),
                )
            )

    # ── 7b. Renal cyst ──
    # Simple cortical cyst <3 cm = info; complex / multiple / large or any
    # mention of "size progression" / "grew" / "increased" / "new cyst on
    # comparison" = EMR +25; explicit progression on serial USG = EMR +50.
    renal_cyst_phs = [
        ph
        for ph in personal
        if (ph.get("status") or "").lower() == "active"
        and any(
            t in (ph.get("condition") or "").lower()
            for t in ("renal cyst", "kidney cyst", "renal cortical cyst", "renal simple cyst")
        )
    ]
    if renal_cyst_phs:
        ph = renal_cyst_phs[0]
        cn = (ph.get("condition") or "").lower() + " " + (ph.get("detail") or "").lower()
        # Pull from extraction_notes too — agents often park progression
        # detail there rather than in condition / detail.
        ext_notes = (med.get("extraction_notes") or "").lower()
        progression_mention = any(
            t in cn or t in ext_notes
            for t in (
                "size progression",
                "grew",
                "increased size",
                "increase in size",
                "new cyst",
                "new finding",
                "cysts increased",
                "cyst is larger",
                "size has increased",
            )
        )
        complex_mention = any(t in cn for t in ("complex", "multilocular", "septated", "bosniak"))
        large_mention = any(t in cn for t in ("large", "giant", "polycystic"))
        multiple_mention = "multiple" in cn or "bilateral" in cn or len(renal_cyst_phs) >= 2
        if progression_mention:
            items.append(
                LoadingItem(
                    code="RENAL_CYST_PROGRESSION",
                    severity="medium",
                    emr_delta=50,
                    title="Renal cyst — size/number progression on serial USG",
                    driver=(
                        "Renal cyst documented as growing or new finding on "
                        "comparison USG. Per TALIC convention this is a "
                        "POSTPONE / EMR +50 event pending nephrology review."
                    ),
                    source="medical.personal_medical_history + extraction_notes",
                    suggestion=(
                        "EMR +50; refer to nephrology; postpone until nature of cyst is clarified."
                    ),
                )
            )
        elif complex_mention or large_mention or multiple_mention:
            items.append(
                LoadingItem(
                    code="RENAL_CYST_COMPLEX",
                    severity="low",
                    emr_delta=25,
                    title="Renal cyst — complex / large / multiple",
                    driver=(
                        "Renal cyst with complex / large / multiple character. "
                        "EMR +25 with UW review."
                    ),
                    source="medical.personal_medical_history",
                    suggestion="EMR +25; serial USG at renewal.",
                )
            )
        else:
            items.append(
                LoadingItem(
                    code="RENAL_CYST_SIMPLE",
                    severity="info",
                    emr_delta=0,
                    title="Simple renal cyst — STD",
                    driver="Simple cortical cyst, no complex / large / progression markers.",
                    source="medical.personal_medical_history",
                    suggestion="No loading. Note for renewal review.",
                )
            )

    # ── 7c. Renal calculus / kidney stone ──
    # Single resolved stone = info; multiple OR large OR staghorn = EMR +25;
    # recurrent (≥2 episodes documented) = EMR +50.
    calculus_phs = [
        ph
        for ph in personal
        if any(
            t in (ph.get("condition") or "").lower()
            for t in (
                "renal calculus",
                "renal calculi",
                "kidney stone",
                "nephrolithiasis",
                "ureteric stone",
            )
        )
    ]
    if calculus_phs:
        ph = calculus_phs[0]
        status = (ph.get("status") or "").lower()
        cn = (ph.get("condition") or "").lower() + " " + (ph.get("detail") or "").lower()
        ext_notes = (med.get("extraction_notes") or "").lower()
        staghorn = "staghorn" in cn or "staghorn" in ext_notes
        large = any(t in cn for t in ("large", ">1 cm", ">10 mm", "obstructive"))
        multiple = "multiple" in cn or "calculi" in cn or len(calculus_phs) >= 2
        recurrent = any(
            t in cn or t in ext_notes
            for t in ("recurrent", "recurrence", "again", "second episode", "second stone")
        )
        if recurrent:
            items.append(
                LoadingItem(
                    code="RENAL_CALCULUS_RECURRENT",
                    severity="medium",
                    emr_delta=50,
                    title="Renal calculus — recurrent",
                    driver="Multiple episodes of renal stone disease documented.",
                    source="medical.personal_medical_history + extraction_notes",
                    suggestion="EMR +50; nephrology workup recommended.",
                )
            )
        elif staghorn or large or multiple:
            items.append(
                LoadingItem(
                    code="RENAL_CALCULUS_LARGE_OR_MULTIPLE",
                    severity="low",
                    emr_delta=25,
                    title="Renal calculus — large / multiple / staghorn",
                    driver=(
                        "Stone burden non-trivial: "
                        + (
                            "staghorn calculus"
                            if staghorn
                            else "large stone"
                            if large
                            else "multiple calculi"
                        )
                        + "."
                    ),
                    source="medical.personal_medical_history",
                    suggestion="EMR +25; renal-function follow-up at renewal.",
                )
            )
        elif status != "resolved":
            items.append(
                LoadingItem(
                    code="RENAL_CALCULUS_PRESENT",
                    severity="info",
                    emr_delta=0,
                    title="Renal calculus — present (single, simple)",
                    driver="Single small calculus, no obstruction / recurrence markers.",
                    source="medical.personal_medical_history",
                    suggestion="No loading; note for renewal.",
                )
            )

    # ── 7d. Hypoproteinemia ──
    # Total protein <6.0 g/dl AND/OR albumin <3.5 g/dl. Both low → EMR +25
    # (signal of protein-losing nephropathy, malabsorption, chronic disease);
    # single isolated low → info only.
    tot_prot, _ = _lab_value(labs, "total protein")
    alb_v, _ = _lab_value(labs, "albumin")
    low_prot = tot_prot is not None and tot_prot < 6.0
    low_alb = alb_v is not None and alb_v < 3.5
    if low_prot and low_alb:
        items.append(
            LoadingItem(
                code="HYPOPROTEINEMIA",
                severity="low",
                emr_delta=25,
                title="Hypoproteinemia (low total protein + low albumin)",
                driver=(
                    f"Total protein {tot_prot} g/dl (<6.0) AND albumin "
                    f"{alb_v} g/dl (<3.5). Consider protein-losing "
                    "nephropathy, malabsorption, or chronic disease."
                ),
                source="medical.lab_results.total protein + albumin",
                suggestion=(
                    "EMR +25; request urine total protein, 24-hr urine "
                    "albumin, repeat liver/kidney panel."
                ),
            )
        )
    elif low_prot or low_alb:
        items.append(
            LoadingItem(
                code="HYPOPROTEINEMIA_BORDERLINE",
                severity="info",
                emr_delta=0,
                title="Borderline low protein / albumin",
                driver=(
                    f"Total protein={tot_prot}, albumin={alb_v} — one value "
                    "below reference range, the other normal/missing."
                ),
                source="medical.lab_results",
                suggestion="No loading; note for renewal review.",
            )
        )

    # ── Aggregate ──
    total = sum(i.emr_delta for i in items)
    decline = False
    decline_reason = ""
    critical_items = [i for i in items if i.severity == "critical"]
    if total > 200:
        decline = True
        decline_reason = f"Cumulative EMR +{total} > 200 (TALIC declines beyond +200)."
    elif critical_items:
        decline = True
        decline_reason = "Critical-severity finding(s): " + "; ".join(
            i.code for i in critical_items
        )
    capped = min(total, 200)

    return LoadingRecommendation(
        recommended_emr=capped,
        decline_recommended=decline,
        decline_reason=decline_reason,
        items=items,
        notes=(
            f"{len(items)} loading rule(s) fired; "
            f"cumulative EMR +{total}, capped at +200 = +{capped}."
        ),
    )
