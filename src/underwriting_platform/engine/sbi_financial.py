"""
engine/sbi_financial.py — SBI Life "Smart Shield Plus" financial UW gating.

Additive module for the SBI Financial Underwriting POC. Every function
returns a GatingItem (engine.types) carrying function/rule/evidence/sources
so the renderer's existing eligibility table (which iterates
`decision["gating"]` generically) surfaces these with full provenance,
with zero renderer changes required.

Only invoked from engine/run.py when `case_json["sbi_bureau"]` is present.

All thresholds are read from config/sbi_smart_shield_rules.json — no
numbers are hardcoded here.
"""

from __future__ import annotations
from typing import Any

from .types import GatingItem, Source, Calculation
from .calculator import _safe_int, _safe_float


def _calc_value(calcs: dict, name: str):
    """calcs holds engine.types.Calculation dataclass instances (not yet
    .to_dict()'d at the point engine/run.py calls into this module) —
    attribute access, not dict .get()."""
    c = (calcs or {}).get(name)
    return getattr(c, "value", None) if c is not None else None


def _g(rules: dict, *path, default=None):
    cur = rules
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default


def compute_sbi_hlv(income_calc: Calculation, age: int, sbi_rules: dict) -> Calculation | None:
    """SBI Smart Shield Plus's own Verified Income multiplicative factor —
    a nested table inside the guidelines docx (age-at-entry band x income
    tier: >=INR 5L vs <INR 5L). Returns None when age/income/table data is
    unavailable, letting the caller fall back to the generic engine grid.

    Confirmed source 2026-06-25 (table is nested inside a table cell — a
    naive top-level read misses it; see config/sbi_smart_shield_rules.json
    income_multiplier_table._source)."""
    table = sbi_rules.get("income_multiplier_table") or {}
    bands = table.get("bands") or []
    income = _safe_int(income_calc.value)
    if not bands or age is None or income <= 0:
        return None
    threshold = _safe_int(table.get("income_band_threshold_inr", 500000))
    band = next(
        (b for b in bands if _safe_int(b.get("age_min")) <= age <= _safe_int(b.get("age_max"))),
        None,
    )
    if band is None:
        return None
    is_high_income = income >= threshold
    multiplier = (
        band.get("multiplier_income_gte_5l")
        if is_high_income
        else band.get("multiplier_income_lt_5l")
    )
    income_tier_label = f"Income ≥ ₹{threshold:,}" if is_high_income else f"Income < ₹{threshold:,}"
    age_label = (
        f"{band.get('age_min')}-{band.get('age_max')}"
        if _safe_int(band.get("age_max")) < 900
        else f"Above {_safe_int(band.get('age_min')) - 1}"
    )
    if multiplier is None:
        # "Individual Consideration" cell (age > 60, income < 5L) — not a
        # numeric multiplier in the source table; no HLV can be computed.
        note = (
            band.get("income_lt_5l_note")
            or "Individual Consideration — not a numeric multiplier; refer for manual UW."
        )
        return Calculation(
            name="hlv_base",
            value=None,
            unit="INR",
            method="sbi_individual_consideration",
            function="underwriting_platform.engine.sbi_financial.compute_sbi_hlv",
            formula="Individual Consideration (no numeric multiplier for this age/income tier)",
            inputs={
                "age": age,
                "age_band": age_label,
                "income": income,
                "income_tier": income_tier_label,
            },
            sources=[
                Source(
                    document=table.get("_source", "Smart Shield Plus guidelines"),
                    agent="underwriting_platform.engine.sbi_financial",
                    field_path="sbi_smart_shield_rules.income_multiplier_table",
                )
            ],
            rule="sbi_smart_shield_rules.income_multiplier_table — Individual Consideration cell",
            note=note,
        )
    hlv_value = income * _safe_int(multiplier)
    return Calculation(
        name="hlv_base",
        value=hlv_value,
        unit="INR",
        method="sbi_income_multiplier_table",
        function="underwriting_platform.engine.sbi_financial.compute_sbi_hlv",
        formula="verified_income × multiplier, per SBI Smart Shield Plus's own age/income-tier table",
        inputs={
            "age": age,
            "age_band": age_label,
            "multiplier": multiplier,
            "income": income,
            "income_tier": income_tier_label,
        },
        sources=income_calc.sources
        + [
            Source(
                document=table.get("_source", "Smart Shield Plus guidelines"),
                agent="underwriting_platform.engine.sbi_financial",
                field_path="sbi_smart_shield_rules.income_multiplier_table",
            )
        ],
        rule=f"sbi_smart_shield_rules.income_multiplier_table — age {age_label}, {income_tier_label} → {multiplier}×",
    )


def check_cibil_gate(
    case: dict,
    sbi_rules: dict,
    calcs: dict | None = None,
    prior_gating: list[GatingItem] | None = None,
) -> GatingItem:
    bureau = case.get("sbi_bureau") or {}
    cfg = sbi_rules.get("cibil_gate") or {}
    score = bureau.get("cibil_score")
    min_score = _safe_int(_g(cfg, "min_cibil_score", default=650))
    max_sa = _safe_int(_g(cfg, "max_sa_inr", default=20000000))
    src = [
        Source(
            document=(bureau.get("_bureau_source_refs") or {}).get(
                "cibil_drc_profile", "Scores.xlsx"
            ),
            agent="sbi_bureau_loader",
            field_path="sbi_bureau.cibil_score",
        )
    ]

    # Header preconditions apply to EVERY surrogate-income type (credit
    # score included), not just the CIBIL-specific rule_text — per
    # "Protection Plans and Surrogate Income guidelines.docx" §13
    # "SURROGATES: Underwriting Consideration – An Overview" (image table,
    # points b/d/e): max age 55, DRC Preferred/Standard only, not
    # applicable to non-earning-life profiles (Retired/Housewife/Student).
    surr_cfg = sbi_rules.get("surrogate_income") or {}
    header = surr_cfg.get("header_conditions") or {}
    app = case.get("applicant") or {}
    age = _safe_int(app.get("age"))
    occ = (app.get("occupation") or "").lower()
    drc = (bureau.get("drc_category") or "").strip()
    max_age = _safe_int(header.get("max_age") or 55)
    allowed_drc = [d.lower() for d in (header.get("drc_category_in") or ["Preferred", "Standard"])]
    not_applicable_profiles = [p.lower() for p in (header.get("not_applicable_profiles") or [])]
    header_violations = []
    if age and age > max_age:
        header_violations.append(f"age {age} exceeds the surrogate-route maximum of {max_age}")
    if drc and drc.lower() not in allowed_drc:
        header_violations.append(f"DRC category '{drc}' is outside Preferred/Standard")
    if occ and any(p in occ for p in not_applicable_profiles):
        header_violations.append(
            f"occupation '{app.get('occupation')}' is a non-earning-life profile"
        )
    if header_violations:
        return GatingItem(
            criterion="sbi_cibil_score",
            label="CIBIL score eligibility (Smart Shield Plus surrogate route)",
            status="NA",
            function="underwriting_platform.engine.sbi_financial.check_cibil_gate",
            rule="sbi_smart_shield_rules.surrogate_income.header_conditions",
            evidence=(
                "CIBIL/surrogate-income route not applicable to this case — "
                + "; ".join(header_violations)
                + " — standard income proof required instead."
            ),
            sources=src,
        )

    # The CIBIL surrogate route is a FALLBACK income-verification method for
    # cases lacking hard documentation — it's irrelevant to a case that
    # already has verified hard proof (ITR/salary-slip/Form-16/CA
    # computation), REGARDLESS of whether a CIBIL score exists at all. Only
    # check loan-details mandatoriness (or raise a "no record" review) when
    # hard proof is actually absent/insufficient.
    #
    # "Insufficient" matters as much as "absent": hard proof existing but
    # falling short of the required SA-band/DRC floor should NOT suppress
    # the CIBIL route either — it's a genuine alternative path in that
    # case. Checked via prior_gating's sbi_qualification result (the
    # earlier generic pass already computed whether hard-proof income
    # clears the floor) rather than re-deriving it here.
    income_calc = (calcs or {}).get("selected_income")
    method = (getattr(income_calc, "method", "") or "").lower()
    base = method
    for suf in ("_fx_inr", "_modal_x_12", "_mean_x_12", "_x_12"):
        if base.endswith(suf):
            base = base[: -len(suf)]
    hard_methods = {"salary_slip", "form_16", "ca_computation", "audited_financials"}
    has_hard_proof = base in hard_methods or base.startswith("itr")
    # Uses sbi_qualification — SBI's own authoritative income-floor check
    # (education-tier floor, per the guideline's qualification table) — as
    # the signal for "does hard-proof income clear the applicable floor."
    # sbi_qualification is already computed and threaded through
    # `prior_gating` for this purpose (see run_sbi_gating below).
    sq_gate = next((g for g in (prior_gating or []) if g.criterion == "sbi_qualification"), None)
    hard_proof_sufficient = has_hard_proof and (sq_gate is None or sq_gate.status != "FAIL")

    if bureau.get("cibil_score_status") == "no_record_found" or score is None:
        # Same hard-proof-sufficiency guard as the score>=650 branch below —
        # a missing CIBIL record is only a review item if this case might
        # actually need the surrogate route; a case with income already
        # fully verified via hard proof was never relying on it.
        if hard_proof_sufficient:
            return GatingItem(
                criterion="sbi_cibil_score",
                label="CIBIL score eligibility (Smart Shield Plus surrogate route)",
                status="NA",
                function="underwriting_platform.engine.sbi_financial.check_cibil_gate",
                rule="sbi_smart_shield_rules.cibil_gate",
                evidence="No CIBIL record on file, but not needed — income is already "
                "documented via hard proof, so the CIBIL surrogate route is "
                "not being relied upon for this case.",
                sources=src,
            )
        return GatingItem(
            criterion="sbi_cibil_score",
            label="CIBIL score eligibility (Smart Shield Plus surrogate route)",
            status="WARN",
            function="underwriting_platform.engine.sbi_financial.check_cibil_gate",
            rule="sbi_smart_shield_rules.cibil_gate",
            evidence="No CIBIL record found for applicant — CIBIL-based surrogate "
            "income route is not available; standard income proof required instead.",
            sources=src,
        )
    score_i = _safe_int(score)
    if score_i >= min_score:
        if hard_proof_sufficient:
            status = "NA"
            evidence = (
                f"CIBIL score {score_i} ≥ {min_score}; CIBIL surrogate route not "
                f"needed — income is already documented via hard proof."
            )
        else:
            # "Loan details mandatory" per the rule text is a real
            # precondition, not just a parenthetical mention — PASS
            # requires loan data to actually be on file, not just a
            # passing CIBIL score.
            has_loan_details = bool((case.get("assets_liabilities") or {}).get("loans"))
            loan_mandatory = bool(cfg.get("loan_details_mandatory"))
            if loan_mandatory and not has_loan_details:
                status = "WARN"
                evidence = (
                    f"CIBIL score {score_i} ≥ {min_score}, so the CIBIL surrogate "
                    f"route (usable up to ₹{max_sa:,}) is conditionally available, "
                    f"but loan details are mandatory for this route and none are on "
                    f"file — must be obtained before relying on this route."
                )
            else:
                status = "PASS"
                evidence = (
                    f"CIBIL score {score_i} ≥ {min_score}. CIBIL surrogate route usable "
                    f"up to ₹{max_sa:,} subject to loan details + income-band check."
                )
    else:
        status = "FAIL"
        evidence = (
            f"CIBIL score {score_i} < {min_score} — CIBIL surrogate income "
            f"route NOT available per SBI Life guideline."
        )
    return GatingItem(
        criterion="sbi_cibil_score",
        label="CIBIL score eligibility (Smart Shield Plus surrogate route)",
        status=status,
        function="underwriting_platform.engine.sbi_financial.check_cibil_gate",
        rule="sbi_smart_shield_rules.cibil_gate — " + cfg.get("rule_text", ""),
        evidence=evidence,
        sources=src,
    )


def check_drc_income_floor(case: dict, calcs: dict, sbi_rules: dict) -> GatingItem:
    bureau = case.get("sbi_bureau") or {}
    cfg = sbi_rules.get("drc_income_floor_gate") or {}
    drc = (bureau.get("drc_category") or "").strip()
    floor = _safe_int(_g(cfg, "high_risk_min_verified_income_inr", default=1000000))
    # Prefer the engine's authoritative selected_income (e.g. 2-yr ITR
    # average for self-employed) over the income agent's raw annual_income
    # field, so every gate on the sheet agrees on one income figure.
    income_blk = case.get("income") or {}
    annual_income = _safe_int(_calc_value(calcs, "selected_income")) or _safe_int(
        income_blk.get("annual_income")
    )
    drc_source = (bureau.get("_bureau_source_refs") or {}).get("cibil_drc_profile", "Scores.xlsx")
    src = [
        Source(
            document=drc_source, agent="sbi_bureau_loader", field_path="sbi_bureau.drc_category"
        ),
        Source(
            document="Income extraction", agent="income", field_path="calculations.selected_income"
        ),
    ]
    if not drc:
        return GatingItem(
            criterion="sbi_drc_income_floor",
            label="DRC-driven minimum income requirement",
            status="NA",
            function="underwriting_platform.engine.sbi_financial.check_drc_income_floor",
            rule="sbi_smart_shield_rules.drc_income_floor_gate",
            evidence="No DRC category on file for this case.",
            sources=src,
        )
    # The GatingItem.sources field (above) already carries the source
    # document reference for the audit trail / decision.json — the
    # evidence string itself stays free of internal file/sheet names and
    # avoids the word "floor" (client-facing wording); the criterion name
    # is an internal identifier only, never displayed.
    if drc.lower() != "high risk":
        return GatingItem(
            criterion="sbi_drc_income_floor",
            label="DRC-driven minimum income requirement",
            status="PASS",
            function="underwriting_platform.engine.sbi_financial.check_drc_income_floor",
            rule="sbi_smart_shield_rules.drc_income_floor_gate",
            evidence=f"DRC category = '{drc}' — ₹10L High-Risk income minimum not applicable.",
            sources=src,
        )
    if annual_income <= 0:
        status, evidence = (
            "FAIL",
            (f"DRC = High Risk and no verified income on file (minimum ₹{floor:,} required)."),
        )
    elif annual_income >= floor:
        status, evidence = (
            "PASS",
            (f"DRC = High Risk; verified income ₹{annual_income:,} ≥ minimum ₹{floor:,}."),
        )
    else:
        status, evidence = (
            "FAIL",
            (f"DRC = High Risk; verified income ₹{annual_income:,} < required minimum ₹{floor:,}."),
        )
    return GatingItem(
        criterion="sbi_drc_income_floor",
        label="DRC-driven minimum income requirement",
        status=status,
        function="underwriting_platform.engine.sbi_financial.check_drc_income_floor",
        rule=f"sbi_smart_shield_rules.drc_income_floor_gate.high_risk_min_verified_income_inr = {floor}",
        evidence=evidence,
        sources=src,
    )


def check_qualification_gate(case: dict, calcs: dict, sbi_rules: dict) -> GatingItem:
    cfg = sbi_rules.get("qualification_gate") or {}
    app = case.get("applicant") or {}
    qual = (app.get("education_level") or "").strip()
    income_blk = case.get("income") or {}
    annual_income = _safe_int(_calc_value(calcs, "selected_income")) or _safe_int(
        income_blk.get("annual_income")
    )
    src = [
        Source(
            document="Applicant profile", agent="identity", field_path="applicant.education_level"
        ),
        Source(
            document="Income extraction", agent="income", field_path="calculations.selected_income"
        ),
    ]
    if not qual:
        return GatingItem(
            criterion="sbi_qualification",
            label="Qualification / minimum-income gate",
            status="WARN",
            function="underwriting_platform.engine.sbi_financial.check_qualification_gate",
            rule="sbi_smart_shield_rules.qualification_gate",
            evidence="Education/qualification not extracted — cannot verify gate.",
            sources=src,
        )
    disallowed = [d.lower() for d in (cfg.get("disallowed") or [])]
    if any(d in qual.lower() for d in disallowed):
        return GatingItem(
            criterion="sbi_qualification",
            label="Qualification / minimum-income gate",
            status="FAIL",
            function="underwriting_platform.engine.sbi_financial.check_qualification_gate",
            rule=f"sbi_smart_shield_rules.qualification_gate.disallowed = {cfg.get('disallowed')}",
            evidence=f"Qualification '{qual}' is in the disallowed list for this product.",
            sources=src,
        )
    # Occupation-specific qualification floor (special_categories) —
    # separate from and stricter than the generic income-tier table.
    # V02.docx's Agriculturist row requires "Education: Minimum Graduate"
    # regardless of income, which the generic tier table has no way to
    # express. Only Agriculturist is wired in here (the only special
    # category present in this batch's occupations); Student/Housewife
    # would need the same treatment if/when they appear.
    occ = (app.get("occupation") or "").strip().lower()
    special = sbi_rules.get("special_categories") or {}
    if occ == "agriculturist":
        agri_cfg = special.get("agriculturist") or {}
        min_qual = (agri_cfg.get("min_qualification") or "").strip()
        _QUAL_RANK = {
            "illiterate": 0,
            "below ssc": 0,
            "ssc": 1,
            "hsc": 2,
            "graduate": 3,
            "post-graduate": 4,
            "postgraduate": 4,
        }

        def _rank(q: str) -> int | None:
            ql = q.lower()
            for key in sorted(_QUAL_RANK, key=len, reverse=True):
                if key in ql:
                    return _QUAL_RANK[key]
            return None

        applicant_rank, required_rank = _rank(qual), _rank(min_qual)
        if (
            applicant_rank is not None
            and required_rank is not None
            and applicant_rank < required_rank
        ):
            return GatingItem(
                criterion="sbi_qualification",
                label="Qualification / minimum-income gate",
                status="FAIL",
                function="underwriting_platform.engine.sbi_financial.check_qualification_gate",
                rule=f"sbi_smart_shield_rules.special_categories.agriculturist.min_qualification = {min_qual}",
                evidence=(
                    f"Qualification '{qual}' is below the required '{min_qual}' for "
                    f"Agriculturist occupation — not an acceptable combination for this product."
                ),
                sources=src,
            )
    tiers = cfg.get("tiers") or []

    # Exact match first (after stripping hyphens/spaces, since
    # "Post-Graduate" vs "Postgraduate" wouldn't equal even after
    # normalization otherwise), falling back to longest-qualification-first
    # substring matching only if no exact match exists. Naive bidirectional
    # substring matching checked in the config list's own order would
    # misclassify "Postgraduate" as "Graduate" — "graduate" is a substring
    # of "postgraduate" and sorts earlier in the tier list — same
    # length-ordering principle as the Agriculturist _rank() helper above.
    def _norm_qual(s: str) -> str:
        return (s or "").strip().lower().replace("-", "").replace(" ", "")

    qual_n = _norm_qual(qual)
    matched = next((t for t in tiers if _norm_qual(t.get("qualification")) == qual_n), None)
    if matched is None:
        for t in sorted(tiers, key=lambda t: len(t.get("qualification") or ""), reverse=True):
            tn = _norm_qual(t.get("qualification"))
            if tn and (tn in qual_n or qual_n in tn):
                matched = t
                break
    if matched is None:
        return GatingItem(
            criterion="sbi_qualification",
            label="Qualification / minimum-income gate",
            status="WARN",
            function="underwriting_platform.engine.sbi_financial.check_qualification_gate",
            rule="sbi_smart_shield_rules.qualification_gate.tiers",
            evidence=f"Qualification '{qual}' not matched against known tiers ({[t['qualification'] for t in tiers]}).",
            sources=src,
        )
    floor = _safe_int(matched.get("min_income_inr"))
    if annual_income <= 0:
        # Same confirmed-zero-vs-data-gap distinction as engine.gating.
        # check_income_documented / check_min_income_for_sa: a hard-proof
        # method (ITR etc.) that confirms ₹0 earned income means the
        # qualification floor is definitively FAILED (₹0 never meets a
        # positive floor), not merely WARN-pending-verification.
        _income_calc = calcs.get("selected_income")
        _method = ((_income_calc.method if _income_calc else "") or "").lower()
        _hard = _method.startswith("itr") or _method.split("_fx_inr")[0] in (
            "salary_slip",
            "form_16",
            "ca_computation",
            "audited_financials",
        )
        if _hard:
            status, evidence = (
                "FAIL",
                (
                    f"Qualification '{qual}': verified income confirmed as ₹0 "
                    f"(hard-proof documentation on file) < required ₹{floor:,}."
                ),
            )
        else:
            status, evidence = (
                "WARN",
                f"Qualification '{qual}' requires min income ₹{floor:,}; income not yet verified.",
            )
    elif annual_income >= floor:
        status, evidence = (
            "PASS",
            f"Qualification '{qual}': verified income ₹{annual_income:,} ≥ required ₹{floor:,}.",
        )
    else:
        status, evidence = (
            "FAIL",
            f"Qualification '{qual}': verified income ₹{annual_income:,} < required ₹{floor:,}.",
        )
    return GatingItem(
        criterion="sbi_qualification",
        label="Qualification / minimum-income gate",
        status=status,
        function="underwriting_platform.engine.sbi_financial.check_qualification_gate",
        rule=f"sbi_smart_shield_rules.qualification_gate.tiers[{matched.get('qualification')}] = ₹{floor:,}",
        evidence=evidence,
        sources=src,
    )


_GOVT_PSU_TERMS = (
    "government",
    "govt",
    "semi government",
    "semi-government",
    "public sector",
    "psu",
    "defence",
    "defense",
    "army",
    "navy",
    "air force",
    "indian army",
    "indian navy",
    "indian air force",
    "police",
    "railway",
    "para military",
    "paramilitary",
    "csir",
    "isro",
    "drdo",
)
_REPUTED_PRIVATE_TERMS = (
    "reputed",
    "mnc",
    "fortune 500",
    "fortune-500",
    "public limited",
)


def _govt_psu_classification(case: dict) -> tuple[bool, bool, str]:
    """Classify employment per the Smart Shield Plus guideline's
    "Salaried class only" row. Returns (qualifies_for_lower_floor_or_cover_tier,
    qualifies_for_1_2x_income_multiple, matched_term).

    The guideline text distinguishes two overlapping but NOT identical
    categories:
      - "Government/Semi Government/Public Sector Undertakings and
        REPUTED CONCERNS" → qualifies for the lower income floor (₹2.5L)
        and the extended cover-ceiling tiers (items 1-2).
      - "Government/Semi Government/Public Sector Undertakings" (NOT
        "reputed concerns") → ADDITIONALLY qualifies for the 1.2× derived-
        income multiple (item 3) — reputed-private employees do not get
        the income boost, only the lower floor / cover ceiling.

    Conservative by design: only matches on explicit employer/occupation
    text; does not infer from company size or any external master list (no
    such list exists in the SBI reference documents). An applicant who is
    self-employed/business is never classified
    into this salaried-only provision regardless of employer wording.
    """
    app = case.get("applicant") or {}
    occ = (app.get("occupation") or "").strip().lower()
    if "self" in occ or "business" in occ or "professional" in occ and "salaried" not in occ:
        return False, False, ""
    employer = (app.get("employer_name") or "").strip().lower()
    haystack = f"{occ} {employer}"
    if "salaried" in occ:
        # Official payslip employer text may name Government/PSU explicitly
        # while the proposal names only the local workplace/battalion.
        haystack += " " + " ".join(
            str(s.get("employer") or "").lower()
            for s in ((case.get("income") or {}).get("salary_slips") or [])
            if isinstance(s, dict)
        )
    govt_psu_term = next((t for t in _GOVT_PSU_TERMS if t in haystack), None)
    if govt_psu_term:
        return True, True, govt_psu_term
    reputed_term = next((t for t in _REPUTED_PRIVATE_TERMS if t in haystack), None)
    if reputed_term:
        return True, False, reputed_term
    return False, False, ""


def apply_govt_psu_income_multiple(
    income_calc: Calculation, case: dict, sbi_rules: dict
) -> Calculation:
    """Item 3 of the guideline's Salaried-class-only row: for Government/
    Semi-Government/PSU employees specifically (not reputed-private), the
    derived income used for financial-eligibility (HLV) purposes may be
    1.2x the declared/verified income. Returns income_calc UNCHANGED when
    not applicable — callers should always call this before compute_sbi_hlv
    rather than skip it conditionally, so the "not applicable" case is
    still explicit and traceable."""
    _, qualifies_1_2x, term = _govt_psu_classification(case)
    cfg = sbi_rules.get("salaried_extended_cover_gate") or {}
    factor = _safe_float(cfg.get("govt_psu_derived_income_multiple") or 1.2)
    if not qualifies_1_2x or factor <= 1.0:
        return income_calc
    base = _safe_int(income_calc.value)
    boosted = int(round(base * factor))
    return Calculation(
        name=income_calc.name,
        value=boosted,
        unit=income_calc.unit,
        method=f"{income_calc.method}_govt_psu_1.2x",
        function="underwriting_platform.engine.sbi_financial.apply_govt_psu_income_multiple",
        formula=f"{income_calc.formula} × {factor} (Government/Semi-Government/PSU derived-income multiple)",
        inputs={
            **(income_calc.inputs or {}),
            "base_income": base,
            "govt_psu_multiple": factor,
            "matched_employment_term": term,
        },
        sources=income_calc.sources
        + [
            Source(
                document=sbi_rules.get("salaried_extended_cover_gate", {}).get(
                    "_source", "Smart Shield Plus guidelines"
                ),
                agent="underwriting_platform.engine.sbi_financial",
                field_path="sbi_smart_shield_rules.salaried_extended_cover_gate",
            )
        ],
        rule=f"sbi_smart_shield_rules.salaried_extended_cover_gate.govt_psu_derived_income_multiple = {factor}",
        note=(income_calc.note + " " if income_calc.note else "")
        + f"Derived income boosted ₹{base:,} × {factor} = ₹{boosted:,} per Government/Semi-Government/PSU "
        f"provision (matched employment term: '{term}').",
    )


def check_salaried_extended_cover(
    case: dict, calcs: dict, sbi_rules: dict, proposed_sa: int
) -> GatingItem:
    """Items 1-2 of the guideline's Salaried-class-only row: cover-ceiling
    tiers for Government/Semi-Government/PSU/reputed-concern employees,
    distinct from (and more generous than) the generic occupation-income-
    proof gate's ₹30L no-proof cap. NA when the applicant doesn't qualify
    for this employment category at all."""
    qualifies, _, term = _govt_psu_classification(case)
    cfg = sbi_rules.get("salaried_extended_cover_gate") or {}
    src = [
        Source(
            document=cfg.get("_source", "Smart Shield Plus guidelines"),
            agent="underwriting_platform.engine.sbi_financial",
            field_path="sbi_smart_shield_rules.salaried_extended_cover_gate",
        )
    ]
    if not qualifies:
        return GatingItem(
            criterion="sbi_salaried_extended_cover",
            label="Govt/PSU/Reputed-concern extended cover ceiling",
            status="NA",
            function="underwriting_platform.engine.sbi_financial.check_salaried_extended_cover",
            rule="sbi_smart_shield_rules.salaried_extended_cover_gate",
            evidence="Applicant is not Salaried at a Government/Semi-Government/PSU/reputed concern — generic occupation-income-proof gate applies instead.",
            sources=src,
        )
    tier1 = cfg.get("tier1") or {}
    tier2 = cfg.get("tier2") or {}
    declared = _safe_int((case.get("applicant") or {}).get("declared_annual_income"))
    tier1_min_income = _safe_int(tier1.get("min_declared_income_inr"))
    tier1_ceiling = (
        _safe_int(tier1.get("max_sa_inr"))
        if declared >= tier1_min_income
        else _safe_int(tier1.get("fallback_max_sa_inr"))
    )
    bureau = case.get("sbi_bureau") or {}
    drc = (bureau.get("drc_category") or "").strip()
    age = _safe_int(_calc_value(calcs, "age"))
    qual = (case.get("applicant") or {}).get("education_level") or ""
    conds = tier2.get("conditions") or {}
    tier2_eligible = (
        drc in (conds.get("drc_category_in") or [])
        and 0 < age <= _safe_int(conds.get("max_age") or 999)
        and (conds.get("min_qualification") or "").lower() in qual.lower()
        and declared >= _safe_int(conds.get("min_declared_income_inr"))
    )
    ceiling = _safe_int(tier2.get("max_sa_inr")) if tier2_eligible else tier1_ceiling
    if proposed_sa <= ceiling:
        status = "PASS"
        evidence = (
            f"Employment term '{term}' qualifies for the Govt/PSU/reputed-concern cover ceiling "
            f"(declared income ₹{declared:,}) — ceiling ₹{ceiling:,}"
            + (" (Tier-2: DRC/age/qualification conditions met)" if tier2_eligible else " (Tier-1)")
            + f". Proposed SA ₹{proposed_sa:,} is within this ceiling."
        )
    else:
        status = "FAIL"
        evidence = (
            f"Employment term '{term}' qualifies for the Govt/PSU/reputed-concern cover ceiling, "
            f"but proposed SA ₹{proposed_sa:,} exceeds the applicable ceiling ₹{ceiling:,}"
            + (
                " (Tier-2 conditions not all met — DRC/age/qualification/income)"
                if not tier2_eligible
                else ""
            )
            + "."
        )
    return GatingItem(
        criterion="sbi_salaried_extended_cover",
        label="Govt/PSU/Reputed-concern extended cover ceiling",
        status=status,
        function="underwriting_platform.engine.sbi_financial.check_salaried_extended_cover",
        rule=f"sbi_smart_shield_rules.salaried_extended_cover_gate — ceiling ₹{ceiling:,}",
        evidence=evidence,
        sources=src,
    )


def check_occupation_income_proof(case: dict, sbi_rules: dict, proposed_sa: int) -> GatingItem:
    cfg = sbi_rules.get("occupation_income_proof_gate") or {}
    app = case.get("applicant") or {}
    occ = (app.get("occupation") or "").strip()
    income_blk = case.get("income") or {}
    has_proof = (
        bool(income_blk.get("primary_source"))
        and income_blk.get("primary_source") != "declared_only"
    )
    src = [
        Source(document="Applicant profile", agent="identity", field_path="applicant.occupation"),
        Source(document="Income extraction", agent="income", field_path="income.primary_source"),
    ]
    petty = [p.lower() for p in (cfg.get("disallowed_petty_business_occupations") or [])]
    # For Business/Self-employed applicants, the extraction schema puts the
    # broad category ("Self-Employed Business") in applicant.occupation and
    # the actual trade/shop name — exactly the level of detail a petty-
    # business label like "Kirana store" or "Pan shop" would appear at — in
    # applicant.employer_name or applicant.business_nature (there being no
    # separate employer for a self-employed person). Scoped to Business/
    # Self-employed occupations only, so a salaried applicant's actual
    # employer name is never matched against this petty-business list.
    employer = (app.get("employer_name") or "").strip()
    business_nature_for_literal = (
        app.get("declared_business_nature") or app.get("business_nature") or ""
    ).strip()
    is_business_occ_for_petty_check = bool(occ) and any(
        k in occ.lower() for k in ("business", "self-employed", "self employed")
    )
    occ_and_employer = occ.lower()
    if is_business_occ_for_petty_check:
        if employer:
            occ_and_employer += f" {employer.lower()}"
            src.append(
                Source(
                    document="Applicant profile",
                    agent="identity",
                    field_path="applicant.employer_name",
                )
            )
        if business_nature_for_literal:
            occ_and_employer += f" {business_nature_for_literal.lower()}"
            src.append(
                Source(
                    document="Applicant profile",
                    agent="identity",
                    field_path="applicant.business_nature",
                )
            )
    if occ and any(p in occ_and_employer for p in petty):
        matched_on = f"Occupation '{occ}'"
        if employer and employer.lower() not in occ.lower():
            matched_on += f", business name '{employer}'"
        if business_nature_for_literal and business_nature_for_literal.lower() not in occ.lower():
            matched_on += f", business nature '{business_nature_for_literal}'"
        return GatingItem(
            criterion="sbi_occupation_income_proof",
            label="Occupation / income-proof requirement",
            status="FAIL",
            function="underwriting_platform.engine.sbi_financial.check_occupation_income_proof",
            rule="sbi_smart_shield_rules.occupation_income_proof_gate.disallowed_petty_business_occupations",
            evidence=f"{matched_on} is a disallowed petty-business profile for this product.",
            sources=src,
        )
    # Catches spelling/wording variants of the same named petty-business
    # types that the literal list above might miss (judgement call made by
    # the identity agent at extraction time — see agents/prompts/identity.txt
    # for the narrow same-trade-only scope). Only "Yes" acts here; "Unsure"/
    # "No"/missing fall through to the normal proof-of-business path.
    business_nature = (app.get("business_nature") or "").strip()
    petty_flag = (app.get("business_petty_trade_flag") or "").strip().lower()
    if is_business_occ_for_petty_check and business_nature and petty_flag == "yes":
        reasoning = (app.get("business_petty_trade_reasoning") or "").strip()
        src.append(
            Source(
                document="Applicant profile",
                agent="identity",
                field_path="applicant.business_nature",
            )
        )
        return GatingItem(
            criterion="sbi_occupation_income_proof",
            label="Occupation / income-proof requirement",
            status="FAIL",
            function="underwriting_platform.engine.sbi_financial.check_occupation_income_proof",
            rule="sbi_smart_shield_rules.occupation_income_proof_gate.disallowed_petty_business_occupations",
            evidence=(
                f"Occupation '{occ}', business nature '{business_nature}' — identified as a disallowed "
                f"petty-business profile for this product"
                + (f" ({reasoning})" if reasoning else "")
                + "."
            ),
            sources=src,
        )
    # Government/Semi-Government/PSU/reputed-concern employees get a LOWER
    # income floor (₹2.5L vs the generic ₹5L) and a HIGHER no-proof cap
    # (₹50L vs the generic ₹30L) per the guideline's "List of Standard
    # Income Proofs" row. Checked before the generic salaried branch so
    # this more favourable tier takes precedence when it applies.
    qualifies_govt_psu, _, govt_term = _govt_psu_classification(case)
    if qualifies_govt_psu:
        govt_floor = _safe_int(_g(cfg, "govt_psu_reputed_min_income_inr", default=250000))
        govt_cap = _safe_int(
            _g(cfg, "govt_psu_reputed_max_sa_with_lower_income_inr", default=5000000)
        )
        income_blk_income = _safe_int(income_blk.get("annual_income"))
        if proposed_sa and proposed_sa <= govt_cap:
            return GatingItem(
                criterion="sbi_occupation_income_proof",
                label="Occupation / income-proof requirement",
                status="PASS",
                function="underwriting_platform.engine.sbi_financial.check_occupation_income_proof",
                rule=f"sbi_smart_shield_rules.occupation_income_proof_gate.govt_psu_reputed_max_sa_with_lower_income_inr = {govt_cap}",
                evidence=(
                    f"Occupation '{occ}', employer matches Government/PSU/reputed-concern term "
                    f"'{govt_term}' — proposed SA ₹{proposed_sa:,} ≤ ₹{govt_cap:,} — no income proof "
                    f"mandated (lower ₹{govt_floor:,} income floor applies for this employment category)."
                ),
                sources=src,
            )
    # V02.docx, same cell as the ₹30L no-proof concession: "(Not applicable
    # for Female Business/Self-employed categories)" — a female applicant
    # in a Business/Self-employed occupation never gets the no-proof
    # concession, regardless of SA.
    gender = (app.get("gender") or "").strip().lower()
    is_female = gender in ("female", "f")
    is_business_or_self_employed = bool(occ) and any(
        k in occ.lower() for k in ("business", "self-employed", "self employed")
    )
    female_carveout = is_female and is_business_or_self_employed

    no_proof_cap = _safe_int(_g(cfg, "salaried_professional_no_proof_max_sa_inr", default=3000000))
    is_salaried = (
        occ.lower() in ("salaried", "professional", "self-employed professional") if occ else False
    )
    if is_salaried and not female_carveout and proposed_sa and proposed_sa <= no_proof_cap:
        return GatingItem(
            criterion="sbi_occupation_income_proof",
            label="Occupation / income-proof requirement",
            status="PASS",
            function="underwriting_platform.engine.sbi_financial.check_occupation_income_proof",
            rule=f"sbi_smart_shield_rules.occupation_income_proof_gate.salaried_professional_no_proof_max_sa_inr = {no_proof_cap}",
            evidence=f"Occupation '{occ}', proposed SA ₹{proposed_sa:,} ≤ ₹{no_proof_cap:,} — no income proof mandated.",
            sources=src,
        )
    # V02.docx has TWO separate "Proof of business" sub-rules:
    #   "Business": Standard Income Proof + Proof of business mandatory
    #     for SA ABOVE ₹30L (config: business_proof_mandatory_above_sa_inr).
    #   "Self employed": Standard Income Proof + Proof of business
    #     mandatory for ANY opted SA (stricter — no ₹30L concession at
    #     all).
    #
    # occ=="self-employed professional" is deliberately excluded — the
    # is_salaried branch above already groups it with Salaried/Professional
    # (professionals like doctors/lawyers), and only falls through to here
    # when its SA exceeds no_proof_cap; it should not also be judged
    # against "Self employed"'s any-SA business-proof rule, which targets a
    # trading/business profile.
    #
    # Deliberately NOT gated on female_carveout here — the guideline's
    # female exception ("Not applicable for Female Business/Self-employed
    # categories") only removes the ≤₹30L NO-INCOME-PROOF concession for
    # "Business"; it says nothing about the separate "Proof of business"
    # requirement, which the source states plainly for "Self employed"
    # (any SA, no gender clause at all) and for "Business" above ₹30L (no
    # gender clause either). The female-specific income-proof requirement
    # is still enforced below, scoped only to the one early-PASS branch it
    # actually modifies.
    is_self_employed_occ = (
        bool(occ)
        and occ.lower() != "self-employed professional"
        and ("self-employed" in occ.lower() or "self employed" in occ.lower())
    )
    is_business_only_occ = bool(occ) and "business" in occ.lower() and not is_self_employed_occ
    business_proof_cap = _safe_int(
        _g(cfg, "business_proof_mandatory_above_sa_inr", default=3000000)
    )
    proof_of_business_required = is_self_employed_occ or (
        is_business_only_occ and proposed_sa and proposed_sa > business_proof_cap
    )
    if is_business_only_occ and not proof_of_business_required and not female_carveout:
        # Pure "Business" (not self-employed) at/under the ₹30L threshold,
        # male applicant — unchanged original concession. A female
        # applicant in this exact scenario falls through instead, to the
        # female_carveout branch further below (income proof still
        # required regardless of the ≤₹30L SA).
        return GatingItem(
            criterion="sbi_occupation_income_proof",
            label="Occupation / income-proof requirement",
            status="PASS",
            function="underwriting_platform.engine.sbi_financial.check_occupation_income_proof",
            rule=f"sbi_smart_shield_rules.occupation_income_proof_gate.business_proof_mandatory_above_sa_inr = {business_proof_cap}",
            evidence=f"Occupation '{occ}', proposed SA ₹{proposed_sa:,} ≤ ₹{business_proof_cap:,} — no income proof mandated.",
            sources=src,
        )
    if proof_of_business_required:
        # V02.docx doesn't enumerate an exact accepted document list for
        # "Proof of business" the way it does for income proof — this is a
        # best-effort content scan for business-registration-type evidence
        # (GST/MSME/trade-licence/shop-act/incorporation/partnership-deed),
        # not a field-level check, and is reported as such rather than
        # silently assumed either way.
        texts = list((income_blk.get("income_sources") or []))
        texts.append(income_blk.get("reconciliation_notes") or "")
        texts.append((case.get("compliance") or {}).get("extraction_notes") or "")
        blob = " ".join(str(t) for t in texts).lower()
        # "proof of business" itself is deliberately NOT a keyword here — a
        # negated sentence like "I don't have any proof of business" would
        # false-match on the phrase as a substring. Kept to specific
        # document-TYPE names, each far less likely to appear in a
        # negation of the general requirement.
        biz_keywords = (
            "gst",
            "udyog aadhaar",
            "udyam",
            "msme",
            "trade licen",
            "shop and establishment",
            "shop & establishment",
            "certificate of incorporation",
            "business registration",
            "partnership deed",
        )
        has_business_proof = any(k in blob for k in biz_keywords)
        if has_proof and has_business_proof:
            return GatingItem(
                criterion="sbi_occupation_income_proof",
                label="Occupation / income-proof requirement",
                status="PASS",
                function="underwriting_platform.engine.sbi_financial.check_occupation_income_proof",
                rule="sbi_smart_shield_rules.occupation_income_proof_gate — 'Standard Income Proof along with Proof of business'",
                evidence=(
                    f"Occupation '{occ}' — income proof on file ({income_blk.get('primary_source')}) "
                    f"and business-registration-type evidence found in the document set."
                ),
                sources=src,
            )
        missing = []
        if not has_proof:
            missing.append("standard income proof")
        if not has_business_proof:
            missing.append(
                "proof of business (e.g. GST/MSME/trade-licence registration — not found in the document set)"
            )
        threshold_txt = (
            "for any opted sum assured"
            if is_self_employed_occ
            else f"for sum assured above ₹{business_proof_cap:,}"
        )
        return GatingItem(
            criterion="sbi_occupation_income_proof",
            label="Occupation / income-proof requirement",
            status="WARN",
            function="underwriting_platform.engine.sbi_financial.check_occupation_income_proof",
            rule="sbi_smart_shield_rules.occupation_income_proof_gate — 'Standard Income Proof along with Proof of business'",
            evidence=(
                f"Occupation '{occ}' — {' and '.join(missing)} required but not confirmed on file "
                f"(V02.docx: 'Standard Income Proof along with Proof of business mandatory {threshold_txt}')."
            ),
            sources=src,
        )
    if female_carveout and not has_proof:
        return GatingItem(
            criterion="sbi_occupation_income_proof",
            label="Occupation / income-proof requirement",
            status="WARN",
            function="underwriting_platform.engine.sbi_financial.check_occupation_income_proof",
            rule="sbi_smart_shield_rules.occupation_income_proof_gate — Female Business/Self-employed carve-out",
            evidence=(
                f"Occupation '{occ}' (Female) — the no-proof concession for Business/Self-employed "
                f"categories does not apply to female applicants per the Smart Shield Plus guideline; "
                f"standard income proof required regardless of proposed SA."
            ),
            sources=src,
        )
    if has_proof:
        status, evidence = (
            "PASS",
            f"Occupation '{occ or 'unknown'}' — income proof on file ({income_blk.get('primary_source')}).",
        )
    else:
        status, evidence = (
            "WARN",
            f"Occupation '{occ or 'unknown'}' — standard income proof required but not confirmed on file.",
        )
    return GatingItem(
        criterion="sbi_occupation_income_proof",
        label="Occupation / income-proof requirement",
        status=status,
        function="underwriting_platform.engine.sbi_financial.check_occupation_income_proof",
        rule="sbi_smart_shield_rules.occupation_income_proof_gate",
        evidence=evidence,
        sources=src,
    )


def check_student_agriculturist_riders(case: dict, sbi_rules: dict) -> GatingItem:
    """V02.docx: both the Student loan-category and Agriculturist
    special-category rows explicitly state 'Riders not allowed'. Only
    checks the rider-SA fields already extracted (adb/appd/bhb_sum_assured)
    — does not depend on any new extraction field."""
    app = case.get("applicant") or {}
    occ = (app.get("occupation") or "").strip().lower()
    src = [
        Source(document="Applicant profile", agent="identity", field_path="applicant.occupation")
    ]
    if occ not in ("student", "agriculturist"):
        return GatingItem(
            criterion="sbi_student_agriculturist_riders",
            label="Riders-not-allowed (Student/Agriculturist)",
            status="NA",
            function="underwriting_platform.engine.sbi_financial.check_student_agriculturist_riders",
            rule="sbi_smart_shield_rules.special_categories",
            evidence="Not applicable — occupation is not Student or Agriculturist.",
            sources=src,
        )
    riders = [
        ("ADB", _safe_int(app.get("adb_sum_assured"))),
        ("APPD", _safe_int(app.get("appd_sum_assured"))),
        ("BHB", _safe_int(app.get("bhb_sum_assured"))),
    ]
    opted = [name for name, sa in riders if sa and sa > 0]
    if opted:
        return GatingItem(
            criterion="sbi_student_agriculturist_riders",
            label="Riders-not-allowed (Student/Agriculturist)",
            status="FAIL",
            function="underwriting_platform.engine.sbi_financial.check_student_agriculturist_riders",
            rule="sbi_smart_shield_rules.special_categories — riders not allowed for Student/Agriculturist",
            evidence=f"Occupation '{app.get('occupation')}' — rider(s) opted ({', '.join(opted)}) are not "
            f"allowed for this category.",
            sources=src,
        )
    return GatingItem(
        criterion="sbi_student_agriculturist_riders",
        label="Riders-not-allowed (Student/Agriculturist)",
        status="PASS",
        function="underwriting_platform.engine.sbi_financial.check_student_agriculturist_riders",
        rule="sbi_smart_shield_rules.special_categories — riders not allowed for Student/Agriculturist",
        evidence=f"Occupation '{app.get('occupation')}' — no riders opted.",
        sources=src,
    )


def check_keyman_partnership_ee_age_cap(case: dict, sbi_rules: dict) -> GatingItem:
    """V02.docx: 'Keyman/Partnership/Employer employee cover (deferred
    assignment): will not be offered beyond age 65 years.' Detects the
    category the same way check_piwc_requirement below does
    (occupation/employer text match), since no dedicated 'proposal
    category' field exists in the extraction schema."""
    app = case.get("applicant") or {}
    occ = (app.get("occupation") or "").strip().lower()
    employer = (app.get("employer_name") or "").strip().lower()
    age = _safe_int(app.get("age"))
    src = [
        Source(document="Applicant profile", agent="identity", field_path="applicant.occupation")
    ]
    terms = ("keyman", "partnership", "employer employee", "employer-employee")
    matches = any(t in occ or t in employer for t in terms)
    if not matches:
        return GatingItem(
            criterion="sbi_keyman_partnership_ee_age_cap",
            label="Keyman/Partnership/EE age ceiling (65)",
            status="NA",
            function="underwriting_platform.engine.sbi_financial.check_keyman_partnership_ee_age_cap",
            rule="sbi_smart_shield_rules — Keyman/Partnership/Employer-Employee deferred assignment",
            evidence="Not applicable — no Keyman/Partnership/Employer-Employee category match on occupation/employer.",
            sources=src,
        )
    if age and age > 65:
        return GatingItem(
            criterion="sbi_keyman_partnership_ee_age_cap",
            label="Keyman/Partnership/EE age ceiling (65)",
            status="FAIL",
            function="underwriting_platform.engine.sbi_financial.check_keyman_partnership_ee_age_cap",
            rule="sbi_smart_shield_rules — Keyman/Partnership/Employer-Employee deferred assignment, max age 65",
            evidence=f"Age {age} exceeds the age-65 ceiling for Keyman/Partnership/Employer-Employee "
            f"(deferred assignment) cover.",
            sources=src,
        )
    return GatingItem(
        criterion="sbi_keyman_partnership_ee_age_cap",
        label="Keyman/Partnership/EE age ceiling (65)",
        status="PASS",
        function="underwriting_platform.engine.sbi_financial.check_keyman_partnership_ee_age_cap",
        rule="sbi_smart_shield_rules — Keyman/Partnership/Employer-Employee deferred assignment, max age 65",
        evidence=f"Age {age or '—'} within the age-65 ceiling for this category.",
        sources=src,
    )


def check_piwc_requirement(case: dict, sbi_rules: dict) -> GatingItem:
    """Insta-PIWC (the pre-issuance verification call) is mandatory per the
    Smart Shield Plus guideline for every case except Keyman, Employer-
    Employee, and Defense-category proposals. No agent currently extracts
    a "PIWC completed" signal from the document set (it's an operational
    call log, not typically a case-pack document) — so this gate can only
    report whether the EXEMPTION applies, not affirmatively confirm
    completion."""
    cfg = sbi_rules.get("piwc_gate") or {}
    app = case.get("applicant") or {}
    occ = (app.get("occupation") or "").strip().lower()
    employer = (app.get("employer_name") or "").strip().lower()
    exempt_terms = [t.lower() for t in (cfg.get("exempt_categories") or [])]
    is_exempt = any(t in occ or t in employer for t in exempt_terms)
    src = [
        Source(
            document="Proposal form — occupation/employer",
            agent="identity",
            field_path="applicant.occupation",
        )
    ]
    if is_exempt:
        return GatingItem(
            criterion="sbi_piwc_requirement",
            label="Insta-PIWC requirement",
            status="NA",
            function="underwriting_platform.engine.sbi_financial.check_piwc_requirement",
            rule="sbi_smart_shield_rules.piwc_gate",
            evidence=f"Occupation/employer '{app.get('occupation') or '—'}' / "
            f"'{app.get('employer_name') or '—'}' falls under an exempt "
            f"category (Keyman / Employer-Employee / Defense) — Insta-PIWC "
            f"not mandatory.",
            sources=src,
        )
    vmer_present = bool((case.get("medical") or {}).get("documents_present", {}).get("vmer"))
    evidence = (
        f"Insta-PIWC is mandatory for this occupation category per the Smart "
        f"Shield Plus guideline; completion is not captured in the document "
        f"set available to this review and must be confirmed operationally "
        f"before issuance. If Insta-PIWC isn't available (combo product / "
        f"physical proposal), Video MER is required instead — "
        f"{'present' if vmer_present else 'NOT currently on file'}."
    )
    return GatingItem(
        criterion="sbi_piwc_requirement",
        label="Insta-PIWC requirement",
        status="WARN",
        function="underwriting_platform.engine.sbi_financial.check_piwc_requirement",
        rule="sbi_smart_shield_rules.piwc_gate",
        evidence=evidence,
        sources=src,
    )


def _medical_grid_lookup(sbi_rules: dict, age: int, proposed_sa: int):
    """Returns (sa_band_label, age_band_label, tests_text) or None if below
    the grid's minimum SA (no medical mandated)."""
    grid = sbi_rules.get("medical_grid") or {}
    min_sa = _safe_int(grid.get("min_sa_for_any_medical_inr") or 2500000)
    if not proposed_sa or proposed_sa < min_sa:
        return None
    age_band = (
        "18-35"
        if age <= 35
        else "36-40"
        if age <= 40
        else "41-50"
        if age <= 50
        else "51-55"
        if age <= 55
        else "Above 55"
    )
    for band in grid.get("sa_bands") or []:
        sa_min = _safe_int(band.get("sa_min_inr"))
        sa_max = band.get("sa_max_inr")
        sa_max_i = _safe_int(sa_max) if sa_max is not None else None
        if proposed_sa >= sa_min and (sa_max_i is None or proposed_sa <= sa_max_i):
            tests = (band.get("tests_by_age") or {}).get(age_band)
            if tests:
                return band.get("label") or "", age_band, tests
    return None


def check_medical_requirement(case: dict, sbi_rules: dict, proposed_sa: int) -> GatingItem:
    """V02.docx: 'Medical | Medically underwritten. Refer attached medical
    grid.' — a SA-band x age-band table of mandatory tests (MER, Cat
    A/B/C/D, ECG/TMT, Nicotine, HBA1C, and more at higher SA bands),
    applicable from SA ₹25L upward. This product's case.json never carries
    a populated 'medical' block (no medical extraction agent runs for this
    SBI financial-underwriting flow), so this gate can only report which
    tests the grid REQUIRES for this applicant's age/SA, the same
    confirm-operationally pattern as check_piwc_requirement above — it
    cannot affirmatively confirm completion, only flag the requirement and
    what's not evidenced on file. The generic engine.medical module's
    medical_compliance/fraud_signal gating is keyed off that same
    'medical' block and stays NA/out-of-scope for SBI cases independently
    of this gate."""
    app = case.get("applicant") or {}
    age = _safe_int(app.get("age"))
    src = [
        Source(document="Applicant profile", agent="identity", field_path="applicant.age"),
        Source(document="Policy data", agent="policy", field_path="proposed_sa"),
    ]
    if not age or not proposed_sa:
        return GatingItem(
            criterion="sbi_medical_grid",
            label="Mandatory medical tests (Smart Shield Plus medical grid)",
            status="NA",
            function="underwriting_platform.engine.sbi_financial.check_medical_requirement",
            rule="sbi_smart_shield_rules.medical_grid",
            evidence="Age or proposed SA not on file — medical-grid applicability could not be determined.",
            sources=src,
        )
    hit = _medical_grid_lookup(sbi_rules, age, proposed_sa)
    if hit is None:
        min_sa = _safe_int(
            (sbi_rules.get("medical_grid") or {}).get("min_sa_for_any_medical_inr") or 2500000
        )
        return GatingItem(
            criterion="sbi_medical_grid",
            label="Mandatory medical tests (Smart Shield Plus medical grid)",
            status="NA",
            function="underwriting_platform.engine.sbi_financial.check_medical_requirement",
            rule="sbi_smart_shield_rules.medical_grid",
            evidence=f"Proposed SA ₹{proposed_sa:,} is below the medical grid's ₹{min_sa:,} threshold — no medical tests mandated by the grid.",
            sources=src,
        )
    sa_band_label, age_band, tests = hit
    return GatingItem(
        criterion="sbi_medical_grid",
        label="Mandatory medical tests (Smart Shield Plus medical grid)",
        status="WARN",
        function="underwriting_platform.engine.sbi_financial.check_medical_requirement",
        rule="sbi_smart_shield_rules.medical_grid",
        evidence=(
            f"Age {age} ({age_band}), proposed SA ₹{proposed_sa:,} ({sa_band_label}) requires: {tests}. "
            f"Completion is not captured in the document set available to this review and must be "
            f"confirmed before issuance."
        ),
        sources=src,
    )


def _aggregate_iib_sa(bureau: dict) -> dict:
    """SA aggregation only — status disqualifiers are band-specific and are
    evaluated separately in check_iib_gate against that band's own
    disallow_* lists from config (not a fixed keyword list).

    "Lapse Paid-Up" counts toward the total at its CURRENT (reduced)
    paid-up sum assured — that policy is still providing live cover, just
    at a reduced amount, distinct from a plain "Lapsed" policy (no
    paid-up value, zero ongoing cover) or a Surrendered/Declined/
    Cooling-off-cancelled one (never had, or no longer has, any
    exposure)."""
    policies = bureau.get("iib_policies") or []
    sbi_inforce_sa = 0
    other_inforce_sa = 0
    unknown_insurer_sa = 0
    for p in policies:
        insurer = p.get("insurer") or ""
        is_sbi = "sbi life" in insurer.lower()
        status = (p.get("status") or "").lower()
        sa = _safe_int(p.get("sum_assured_inr"))
        has_live_cover = (
            "inforce" in status
            or "in force" in status
            or "paid-up" in status
            or "paid up" in status
        )
        if has_live_cover:
            if not insurer.strip() or any(
                t in insurer.lower() for t in ("masked", "not found", "unknown", "not available")
            ):
                unknown_insurer_sa += sa
            elif is_sbi:
                sbi_inforce_sa += sa
            else:
                other_inforce_sa += sa
    return {
        "sbi_life_total_sa": sbi_inforce_sa,
        "iib_other_total_sa": other_inforce_sa,
        "unknown_insurer_total_sa": unknown_insurer_sa,
        "combined_total_sa": sbi_inforce_sa + other_inforce_sa + unknown_insurer_sa,
    }


def _match_any(value: str, terms: list[str]) -> bool:
    """Bidirectional substring match so "DECLINE" (bureau value) matches
    "Declined" (config term) and vice versa — bureau UW-decision/status
    text doesn't reliably agree on tense/inflection with the rule list."""
    v = (value or "").strip().lower()
    if not v:
        return False
    for t in terms or []:
        t = (t or "").strip().lower()
        if not t:
            continue
        stem = t[:-1] if t.endswith("d") and len(t) > 4 else t  # declined -> decline
        if t in v or v in t or stem in v:
            return True
    return False


def _band_disqualifiers(bureau: dict, band: dict) -> dict:
    """Evaluate this band's own disallow_* lists (config-driven, not a
    fixed keyword list) against the raw policy rows."""
    policies = bureau.get("iib_policies") or []
    sbi_bad_uw, sbi_pipv, iib_bad_status = [], [], []
    uw_terms = band.get("disallow_sbi_uw_decision_in") or []
    sub_terms = band.get("disallow_sbi_cancellation_sub_status_in") or []
    status_terms = band.get("disallow_iib_proposal_status_in") or []
    for p in policies:
        is_sbi = "sbi life" in (p.get("insurer") or "").lower()
        if is_sbi and _match_any(p.get("uw_decision"), uw_terms):
            sbi_bad_uw.append(p)
        if is_sbi and _match_any(p.get("cancellation_sub_status"), sub_terms):
            sbi_pipv.append(p)
        if _match_any(p.get("status"), status_terms):
            iib_bad_status.append(p)
    return {"sbi_bad_uw": sbi_bad_uw, "sbi_pipv": sbi_pipv, "iib_bad_status": iib_bad_status}


def check_iib_gate(case: dict, sbi_rules: dict) -> GatingItem:
    bureau = case.get("sbi_bureau") or {}
    cfg = sbi_rules.get("iib_gate") or {}
    src = [
        Source(
            document=(bureau.get("_bureau_source_refs") or {}).get(
                "iib", "IIB, prev insurance.docx"
            ),
            agent="sbi_bureau_loader",
            field_path="sbi_bureau.iib_policies",
        )
    ]
    policies = bureau.get("iib_policies")
    if policies is None:
        return GatingItem(
            criterion="sbi_iib_gate",
            label="IIB / prior-policy aggregation gate",
            status="NA",
            function="underwriting_platform.engine.sbi_financial.check_iib_gate",
            rule="sbi_smart_shield_rules.iib_gate",
            evidence="No IIB data on file for this case.",
            sources=src,
        )
    agg = _aggregate_iib_sa(bureau)
    sbi_sa = agg["sbi_life_total_sa"]
    unknown_sa = agg["unknown_insurer_total_sa"]
    if unknown_sa:
        return GatingItem(
            criterion="sbi_iib_gate",
            label="IIB / prior-policy aggregation gate",
            status="WARN",
            function="underwriting_platform.engine.sbi_financial.check_iib_gate",
            rule="sbi_smart_shield_rules.iib_gate.bands — requires identified SBI Life exposure",
            evidence=(
                f"Insurer identities are masked or unavailable for in-force SA ₹{unknown_sa:,}. "
                f"Combined in-force SA ₹{agg['combined_total_sa']:,} is retained for HLV. "
                "The SBI Life band and its ₹50L cap cannot be confirmed until insurer identities are established; manual UW review required."
            ),
            sources=src,
        )
    bands = cfg.get("bands") or []
    band = None
    for b in bands:
        lo = _safe_int(b.get("sbi_life_total_sa_min_inr", 0))
        hi = b.get("sbi_life_total_sa_max_inr")
        hi = _safe_int(hi) if hi is not None else None
        if sbi_sa >= lo and (hi is None or sbi_sa <= hi):
            band = b
            break
    if band is None:
        return GatingItem(
            criterion="sbi_iib_gate",
            label="IIB / prior-policy aggregation gate",
            status="WARN",
            function="underwriting_platform.engine.sbi_financial.check_iib_gate",
            rule="sbi_smart_shield_rules.iib_gate.above_30l_note",
            evidence=(
                f"SBI Life existing in-force SA ₹{sbi_sa:,} exceeds the ₹30L band covered "
                f"by the source guideline ({cfg.get('above_30l_note', '')}) — refer for manual UW."
            ),
            sources=src,
        )
    disq = _band_disqualifiers(bureau, band)
    agg = {**agg, **disq}
    cap = _safe_int(band.get("combined_iib_plus_sbi_max_inr"))
    problems = []
    if agg["combined_total_sa"] > cap:
        problems.append(
            f"combined IIB+SBI Life SA ₹{agg['combined_total_sa']:,} exceeds cap ₹{cap:,}"
        )
    if agg["sbi_bad_uw"]:
        problems.append(
            f"{len(agg['sbi_bad_uw'])} SBI Life policy(ies) with disqualifying UW decision"
        )
    if agg["sbi_pipv"]:
        problems.append(
            f"{len(agg['sbi_pipv'])} SBI Life policy(ies) with cancellation sub-status PIPV"
        )
    if agg["iib_bad_status"]:
        problems.append(
            f"{len(agg['iib_bad_status'])} IIB policy(ies) with disqualifying proposal status"
        )
    status = "FAIL" if problems else "PASS"
    evidence = (
        f"Band: {band.get('label')}. SBI Life in-force SA ₹{sbi_sa:,}; other-insurer in-force SA "
        f"₹{agg['iib_other_total_sa']:,}; combined ₹{agg['combined_total_sa']:,} (cap ₹{cap:,}). "
        + ("All sub-checks clear." if not problems else "Issues: " + "; ".join(problems) + ".")
    )
    return GatingItem(
        criterion="sbi_iib_gate",
        label="IIB / prior-policy aggregation gate",
        status=status,
        function="underwriting_platform.engine.sbi_financial.check_iib_gate",
        rule=f"sbi_smart_shield_rules.iib_gate.bands[{band.get('label')}]",
        evidence=evidence,
        sources=src,
    )


def check_material_non_disclosure(case: dict, sbi_rules: dict) -> GatingItem:
    """Gap 2 — a prior proposal shown DECLINED/POSTPONED/RATED-UP in the bureau
    record, contradicted by a 'No' answer on the proposal form's own prior-
    history disclosure question, is a materially different (and more
    serious) finding than generic undisclosed-cover: it's a direct
    misrepresentation on a specific question the applicant answered.
    """
    bureau = case.get("sbi_bureau") or {}
    comp = case.get("compliance") or {}
    hist = comp.get("prior_proposal_history") or {}
    src = [
        Source(
            document=(bureau.get("_bureau_source_refs") or {}).get(
                "iib", "IIB, prev insurance.docx"
            ),
            agent="sbi_bureau_loader",
            field_path="sbi_bureau.iib_policies",
        ),
        Source(
            document="Proposal form disclosure question",
            agent="compliance",
            field_path="compliance.prior_proposal_history",
        ),
    ]
    # Scoped exactly to the client's rule 1/2 wording ("Declined, postponed,
    # ratedup") — deliberately excludes "withdraw" (customer-initiated, not
    # an insurer adverse decision). Must stay in sync with
    # engine.requirements._ADVERSE_TERMS, which already excludes it.
    bad_terms = ("declin", "postpone", "rated up", "rate up")
    flagged_policies = [
        p
        for p in (bureau.get("iib_policies") or [])
        if any(t in (p.get("status") or "").lower() for t in bad_terms)
        or any(t in (p.get("uw_decision") or "").lower() for t in bad_terms)
    ]
    if not flagged_policies:
        return GatingItem(
            criterion="sbi_material_non_disclosure",
            label="Material non-disclosure of prior decline/postponement",
            status="NA",
            function="underwriting_platform.engine.sbi_financial.check_material_non_disclosure",
            rule="proposal form prior-history question vs sbi_bureau.iib_policies",
            evidence="No declined/postponed/rated-up prior policy found in bureau records.",
            sources=src,
        )
    answered = (hist.get("applicant_answered") or "").strip().lower()
    if not hist.get("question_found") or not answered:
        return GatingItem(
            criterion="sbi_material_non_disclosure",
            label="Material non-disclosure of prior decline/postponement",
            status="WARN",
            function="underwriting_platform.engine.sbi_financial.check_material_non_disclosure",
            rule="proposal form prior-history question vs sbi_bureau.iib_policies",
            evidence=(
                f"Bureau shows {len(flagged_policies)} declined/postponed/rated-up prior "
                f"polic(y/ies), but the proposal form's own disclosure question/answer "
                f"was not extracted — cannot confirm whether this was disclosed."
            ),
            sources=src,
        )
    if answered == "no":
        details = "; ".join(
            f"{p.get('insurer', '?')} {p.get('product_type', 'policy')} SA ₹{_safe_int(p.get('sum_assured_inr')):,} "
            f"— status {p.get('status')}"
            for p in flagged_policies
        )
        return GatingItem(
            criterion="sbi_material_non_disclosure",
            label="Material non-disclosure of prior decline/postponement",
            status="FAIL",
            function="underwriting_platform.engine.sbi_financial.check_material_non_disclosure",
            rule="proposal form prior-history question vs sbi_bureau.iib_policies",
            evidence=(
                f"Applicant answered 'No' to the proposal form's prior-decline/postponement "
                f"disclosure question, but bureau records show: {details}. This is a direct "
                f"contradiction on a specific disclosure question (distinct from general "
                f"undisclosed-cover) — applicant must submit the decline letter and an "
                f"explanation before any underwriting decision is issued, per SBI Life's own "
                f"guidance to seek customer details on decline/postponed/medically-nonstandard "
                f"prior policies (Explanation on IIB and CIBIL Score rules.msg)."
            ),
            sources=src,
        )
    return GatingItem(
        criterion="sbi_material_non_disclosure",
        label="Material non-disclosure of prior decline/postponement",
        status="PASS",
        function="underwriting_platform.engine.sbi_financial.check_material_non_disclosure",
        rule="proposal form prior-history question vs sbi_bureau.iib_policies",
        evidence=f"Applicant disclosed prior history ('{hist.get('applicant_answered')}'); consistent with bureau records.",
        sources=src,
    )


def check_pending_proposal_disclosure(case: dict, sbi_rules: dict) -> GatingItem:
    """A prior proposal shown 'Proposal Pending for Underwriting' at another
    insurer in the bureau record is neither adverse (check_material_non_
    disclosure's own scope) nor a confirmed in-force policy
    (existing_cover_disclosed) — its eventual decision is simply unknown.
    Previously surfaced only as a bare Requirements-to-Raise line (engine.
    requirements's own independent scan of sbi_bureau.iib_policies, with no
    backing gating item) — giving it its own gate here means the risk
    summary carries the full finding instead of the requirement box being
    the only place it's explained."""
    bureau = case.get("sbi_bureau") or {}
    policies = bureau.get("iib_policies") or []
    src = [
        Source(
            document=(bureau.get("_bureau_source_refs") or {}).get(
                "iib", "IIB, prev insurance.docx"
            ),
            agent="sbi_bureau_loader",
            field_path="sbi_bureau.iib_policies",
        )
    ]
    pending = [
        p
        for p in policies
        if "pending" in (p.get("status") or "").lower()
        and "sbi life" not in (p.get("insurer") or "").lower()
    ]
    if not pending:
        return GatingItem(
            criterion="sbi_pending_proposal_disclosure",
            label="Pending proposal(s) at other insurer(s)",
            status="NA",
            function="underwriting_platform.engine.sbi_financial.check_pending_proposal_disclosure",
            rule="sbi_bureau.iib_policies — status 'Proposal Pending for Underwriting'",
            evidence="No pending proposal at another insurer found in bureau records.",
            sources=src,
        )
    named = sorted(
        {
            p.get("insurer")
            for p in pending
            if p.get("insurer") and "masked" not in p.get("insurer", "").lower()
        }
    )
    where = (
        f"at {', '.join(named)}" if named else "at other insurer(s) (name masked in bureau export)"
    )
    nos = sorted({p.get("policy_or_wf_no") for p in pending if p.get("policy_or_wf_no")})
    nos_txt = f" ({', '.join(nos)})" if nos else ""
    return GatingItem(
        criterion="sbi_pending_proposal_disclosure",
        label="Pending proposal(s) at other insurer(s)",
        status="WARN",
        function="underwriting_platform.engine.sbi_financial.check_pending_proposal_disclosure",
        rule="sbi_bureau.iib_policies — status 'Proposal Pending for Underwriting'",
        evidence=(
            f"{len(pending)} proposal(s){nos_txt} shown 'Proposal Pending for Underwriting' "
            f"{where} — outcome unknown, could still turn out adverse. Obtain the exact "
            f"current decision status before finalising this case."
        ),
        sources=src,
    )


def check_bhb_eligibility(case: dict, calcs: dict, sbi_rules: dict) -> GatingItem:
    """Gap 4 — Better Half Benefit's 5 documented sub-conditions, formally
    checked (rule existed in config since the original build; no function
    ever evaluated it)."""
    cfg = sbi_rules.get("better_half_benefit_gate") or {}
    app = case.get("applicant") or {}
    bureau = case.get("sbi_bureau") or {}
    nominees = app.get("nominees") or []
    spouse = next(
        (
            n
            for n in nominees
            if (n.get("relation") or "").strip().lower() in ("wife", "husband", "spouse")
        ),
        None,
    )
    bhb_sa = _safe_int(app.get("bhb_sum_assured"))
    src = [
        Source(
            document="Proposal form — plan/rider section",
            agent="identity",
            field_path="applicant.bhb_sum_assured",
        ),
        Source(
            document="Proposal form — nominee/spouse details",
            agent="identity",
            field_path="applicant.nominees",
        ),
    ]
    if not bhb_sa:
        return GatingItem(
            criterion="sbi_bhb_eligibility",
            label="Better Half Benefit eligibility (5 sub-conditions)",
            status="NA",
            function="underwriting_platform.engine.sbi_financial.check_bhb_eligibility",
            rule="sbi_smart_shield_rules.better_half_benefit_gate",
            evidence="Better Half Benefit not opted / not extracted for this case.",
            sources=src,
        )
    checks: dict[str, bool] = {}
    notes: list[str] = []
    # "Not satisfied" summary — ONLY the condition(s) that failed or
    # couldn't be verified, in plain underwriter language.
    unsatisfied: list[str] = []
    # Tracks sub-conditions whose underlying data is simply missing (spouse
    # DOB/qualification not extracted, HLV unavailable, etc.) — distinct
    # from `checks`, which only ever holds conditions that COULD actually
    # be evaluated. Any undeterminable condition caps the status at WARN
    # (can't confirm full eligibility) unless a checkable condition already
    # FAILs outright, which stays FAIL regardless — otherwise a case with
    # 1+ of BHB's 5 documented conditions never actually verified (e.g.
    # spouse qualification not on file) would silently read as a clean
    # PASS just because the conditions that DID have data happened to pass.
    undeterminable: list[str] = []

    max_sa = _safe_int(_g(cfg, "max_sa_inr", default=2500000))
    checks["sa_within_max"] = bhb_sa <= max_sa
    notes.append(f"BHB SA ₹{bhb_sa:,} {'≤' if checks['sa_within_max'] else '>'} max ₹{max_sa:,}")
    if not checks["sa_within_max"]:
        unsatisfied.append(f"BHB sum assured ₹{bhb_sa:,} exceeds the ₹{max_sa:,} maximum.")

    spouse_age = None
    if spouse and spouse.get("dob"):
        from datetime import date as _date

        try:
            sd = _parse_iso_date(spouse["dob"])
            # Anchor to the proposal's own submission date, same as Gap 3's
            # age-at-entry fix — never anchor to today's processing date.
            anchor = _parse_iso_date(app.get("submission_date")) or _date.today()
            if sd:
                spouse_age = (
                    anchor.year - sd.year - ((anchor.month, anchor.day) < (sd.month, sd.day))
                )
        except Exception:
            spouse_age = None
    max_spouse_age = _safe_int(_g(cfg, "max_entry_age_beneficiary", default=55))
    if spouse_age is not None:
        checks["spouse_age_within_max"] = spouse_age <= max_spouse_age
        notes.append(
            f"spouse age {spouse_age} {'≤' if checks['spouse_age_within_max'] else '>'} max {max_spouse_age}"
        )
        if not checks["spouse_age_within_max"]:
            unsatisfied.append(
                f"Spouse age {spouse_age} exceeds the maximum entry age of {max_spouse_age}."
            )
    else:
        notes.append("spouse age not determinable — spouse DOB missing")
        undeterminable.append("spouse age (DOB missing)")
        # Same root cause (missing spouse DOB) also blocks the age-gap
        # check below — one bullet for both rather than two that repeat
        # the same underlying fact.
        unsatisfied.append("Spouse date of birth not on file (age and age-gap).")

    la_age = _safe_int(_calc_value(calcs, "age")) or _safe_int(app.get("age"))
    max_gap = _safe_int(_g(cfg, "max_age_gap_years", default=10))
    if spouse_age is not None and la_age:
        gap = abs(la_age - spouse_age)
        checks["age_gap_within_max"] = gap <= max_gap
        notes.append(
            f"age gap {gap}yr {'≤' if checks['age_gap_within_max'] else '>'} max {max_gap}yr"
        )
        if not checks["age_gap_within_max"]:
            unsatisfied.append(
                f"Age gap between life assured and spouse ({gap}yr) exceeds the {max_gap}yr maximum."
            )
    else:
        notes.append("age gap not determinable")
        undeterminable.append("LA/spouse age gap")
        if spouse_age is not None and not la_age:
            unsatisfied.append("Life assured's age not on file (age gap).")

    min_qual = (_g(cfg, "min_qualification_beneficiary", default="Graduate") or "").lower()
    spouse_qual = (spouse.get("education_level") or "").lower() if spouse else ""
    if spouse_qual:
        checks["spouse_qualification_ok"] = min_qual in spouse_qual or spouse_qual in min_qual
        if checks["spouse_qualification_ok"]:
            notes.append(f"spouse qualification '{spouse_qual}' vs required '{min_qual}'")
        else:
            # Distinct phrasing from the "not extracted" branch below —
            # engine.requirements keys off this to tell "data missing"
            # apart from "data present and below the acceptable range".
            notes.append(
                f"spouse qualification '{spouse_qual}' is below the required "
                f"'{min_qual}' — BHB qualification sub-condition fails"
            )
            # Keeps the literal phrase "qualification sub-condition fails"
            # — matched verbatim by engine.requirements, to distinguish a
            # genuine below-range finding from the "not on file" case
            # below.
            unsatisfied.append(
                f"Spouse qualification ('{spouse.get('education_level')}') is below "
                f"the required '{min_qual.title()}' minimum: BHB qualification "
                f"sub-condition fails."
            )
    else:
        notes.append(
            "spouse qualification not extracted — cannot verify (proposal form may not capture this)"
        )
        undeterminable.append("spouse qualification (not on file)")
        # Keeps "qualification" + "not on file" together — matched
        # verbatim by engine.requirements to distinguish this from the
        # genuine below-range case above.
        unsatisfied.append("Spouse qualification not on file.")

    base_sa = _safe_int(app.get("sum_assured"))
    hlv_base = _safe_int(_calc_value(calcs, "hlv_base"))
    if hlv_base and base_sa:
        checks["income_covers_total"] = hlv_base >= (base_sa + bhb_sa)
        notes.append(
            f"HLV ₹{hlv_base:,} (see Part E — income × multiplier) "
            f"{'≥' if checks['income_covers_total'] else '<'} base+BHB ₹{base_sa + bhb_sa:,}"
        )
        if not checks["income_covers_total"]:
            unsatisfied.append(
                f"Verified income does not support the combined base + BHB sum "
                f"assured (₹{base_sa + bhb_sa:,})."
            )
    else:
        notes.append("HLV / base SA not available — cannot verify income-covers-total")
        undeterminable.append("income covers base+BHB total")
        unsatisfied.append("Income/HLV not on file (base + BHB sum assured coverage).")

    drc = (bureau.get("drc_category") or "").strip().lower()
    if drc:
        checks["drc_not_high_risk"] = drc != "high risk"
        notes.append(
            f"DRC '{bureau.get('drc_category')}' {'is not' if checks['drc_not_high_risk'] else 'IS'} High Risk"
        )
        if not checks["drc_not_high_risk"]:
            unsatisfied.append("DRC risk category is High Risk; BHB is not applicable.")
    else:
        notes.append("DRC category not on file")
        undeterminable.append("DRC High-Risk exclusion")
        unsatisfied.append("DRC risk category not on file.")

    # Three more BHB exclusions from the same V02.docx cell as the SA/age/
    # gap/qualification/income/DRC conditions above: "Not applicable for
    # Negative location", "...for High Risk as per DRC" (already checked
    # above), "If primary life assured and/or spouse is substandard as per
    # declaration", and "For proposal category 'MWP' - BHB rider will not
    # be applicable". negative_pin_flag reads the CAF's own "Negative PIN
    # Code" field (agents/prompts/compliance.txt rule 6); proposal_category
    # and substandard_declaration are populated by
    # agents/prompts/identity.txt and agents/prompts/compliance.txt. Any
    # of the three falls into the "undeterminable" path when its
    # underlying field isn't populated for a given case.
    comp = case.get("compliance") or {}
    neg_pin = (comp.get("aml_evidence") or {}).get("negative_pin_flag")
    if neg_pin is not None:
        checks["not_negative_location"] = not bool(neg_pin)
        notes.append(f"negative-PIN flag {'not set' if not neg_pin else 'SET'}")
        if neg_pin:
            unsatisfied.append(
                "Applicant's address is flagged as a negative-PIN location; BHB is not applicable."
            )
    else:
        notes.append("negative-PIN flag not on file")
        undeterminable.append("negative location exclusion")
        unsatisfied.append("Negative-location status not on file.")

    proposal_category = (app.get("proposal_category") or "").strip().lower()
    if proposal_category:
        checks["not_mwp"] = proposal_category != "mwp"
        notes.append(f"proposal category '{app.get('proposal_category')}'")
        if proposal_category == "mwp":
            unsatisfied.append("Proposal category is 'MWP'; BHB rider is not applicable.")
    else:
        notes.append("proposal category not on file")
        undeterminable.append("MWP-category exclusion")
        unsatisfied.append("Proposal category not on file (MWP exclusion).")

    substd = comp.get("substandard_declaration") or {}
    la_substd, sp_substd = substd.get("life_assured_substandard"), substd.get("spouse_substandard")
    if la_substd is not None or sp_substd is not None:
        is_substd = bool(la_substd) or bool(sp_substd)
        checks["not_substandard"] = not is_substd
        who = "; ".join(w for w, v in (("life assured", la_substd), ("spouse", sp_substd)) if v)
        notes.append(f"substandard declared: {who or 'none'}")
        if is_substd:
            unsatisfied.append(f"Substandard declared ({who}); BHB is not applicable.")
    else:
        notes.append("substandard declaration not on file")
        undeterminable.append("substandard-declaration exclusion")
        unsatisfied.append("Substandard declaration not on file.")

    if not checks:
        status = "WARN"
    elif not all(checks.values()):
        status = "FAIL"
    elif undeterminable:
        # Every CHECKABLE condition passed, but at least one of BHB's 5
        # documented conditions couldn't be evaluated at all — this is not
        # the same as a clean pass and must not render as one.
        status = "WARN"
    else:
        status = "PASS"
    if status == "PASS":
        evidence = "All 5 BHB eligibility conditions verified and satisfied."
    elif unsatisfied:
        # Only what's NOT satisfied, in plain underwriter language — not
        # the full pass/fail dump (that detail still exists internally in
        # `notes`/`checks` for the audit trail, just no longer forced
        # into the client-facing evidence text).
        evidence = " ".join(unsatisfied)
    else:
        evidence = f"{sum(checks.values())}/{len(checks)} checkable BHB conditions pass."
    return GatingItem(
        criterion="sbi_bhb_eligibility",
        label="Better Half Benefit eligibility (5 sub-conditions)",
        status=status,
        function="underwriting_platform.engine.sbi_financial.check_bhb_eligibility",
        rule="sbi_smart_shield_rules.better_half_benefit_gate",
        evidence=evidence,
        sources=src,
    )


def _parse_iso_date(s):
    from datetime import date as _date

    try:
        y, m, d = str(s).split("-")[:3]
        return _date(int(y), int(m), int(d[:2]))
    except Exception:
        return None


def check_hlv_remaining(case: dict, calcs: dict, sbi_rules: dict, proposed_sa: int) -> GatingItem:
    """Gap 5 — remaining HLV eligibility after deducting BUREAU-VERIFIED
    existing in-force cover (not the self-declared figure, which is ₹0 for
    any non-disclosed case and therefore understates the true exposure)."""
    bureau = case.get("sbi_bureau") or {}
    hlv_base_raw = _calc_value(calcs, "hlv_base")
    hlv_base = _safe_int(hlv_base_raw)
    src = [
        Source(
            document="Engine HLV calculation",
            agent="underwriting_platform.engine.calculator",
            field_path="calculations.hlv_base",
        ),
        Source(
            document=(bureau.get("_bureau_source_refs") or {}).get(
                "iib", "IIB, prev insurance.docx"
            ),
            agent="sbi_bureau_loader",
            field_path="sbi_bureau.iib_policies",
        ),
    ]
    if bureau.get("iib_policies") is None or not hlv_base:
        # hlv_base_raw is None (not 0) for the SBI "Individual Consideration"
        # scenario — HLV genuinely isn't specified by the SBI source table
        # for this age/income combination, distinct from a missing bureau
        # feed. Both land on NA either way (this gate can't resolve
        # eligibility without a real HLV number regardless of cause), but
        # the evidence text says which it is.
        hlv_calc = calcs.get("hlv_base")
        evidence = (
            "HLV not specified by SBI guidelines for this age/income combination "
            "— refer for manual UW."
            if hlv_base_raw is None and hlv_calc is not None
            else "HLV or bureau data not available."
        )
        return GatingItem(
            criterion="sbi_hlv_remaining",
            label="HLV-remaining eligibility (after existing in-force cover)",
            status="NA",
            function="underwriting_platform.engine.sbi_financial.check_hlv_remaining",
            rule="hlv_base - bureau-verified in-force cover",
            evidence=evidence,
            sources=src,
        )
    agg = _aggregate_iib_sa(bureau)
    declared = _safe_int(
        ((case.get("compliance") or {}).get("existing_cover_declared") or {}).get(
            "total_life_cover_other"
        )
    )
    existing_inforce = max(agg["combined_total_sa"], declared)
    hlv_remaining = max(0, hlv_base - existing_inforce)
    if proposed_sa <= 0:
        status, evidence = "NA", "Proposed SA not provided."
    elif proposed_sa <= hlv_remaining:
        status = "PASS"
        evidence = (
            f"HLV ₹{hlv_base:,} − existing cover (higher of declared and bureau) ₹{existing_inforce:,} "
            f"= remaining ₹{hlv_remaining:,}. Proposed SA ₹{proposed_sa:,} ≤ remaining — within limit."
        )
    else:
        status = "FAIL"
        evidence = (
            f"HLV ₹{hlv_base:,} − existing cover (higher of declared and bureau) ₹{existing_inforce:,} "
            f"= remaining ₹{hlv_remaining:,}. Proposed SA ₹{proposed_sa:,} EXCEEDS remaining eligibility."
        )
    return GatingItem(
        criterion="sbi_hlv_remaining",
        label="HLV-remaining eligibility (after existing in-force cover)",
        status=status,
        function="underwriting_platform.engine.sbi_financial.check_hlv_remaining",
        rule="hlv_base - max(declared cover, bureau-verified in-force cover)",
        evidence=evidence,
        sources=src,
    )


def check_income_over_declaration(case: dict, calcs: dict, sbi_rules: dict) -> GatingItem:
    """Gap 6 — declared-vs-verified income gap, named and quantified.
    20% threshold is standard UW/AML practice, not a numbered SBI rule —
    evidence text says so explicitly."""
    app = case.get("applicant") or {}
    income_blk = case.get("income") or {}
    declared = _safe_int(app.get("declared_annual_income"))
    verified = _safe_int(_calc_value(calcs, "selected_income")) or _safe_int(
        income_blk.get("annual_income")
    )
    src = [
        Source(
            document="Proposal form — declared income",
            agent="identity",
            field_path="applicant.declared_annual_income",
        ),
        Source(
            document="Income extraction (ITR/Form16/etc.)",
            agent="income",
            field_path="calculations.selected_income",
        ),
    ]
    if not declared or not verified:
        return GatingItem(
            criterion="sbi_income_over_declaration",
            label="Declared vs verified income gap",
            status="NA",
            function="underwriting_platform.engine.sbi_financial.check_income_over_declaration",
            rule="(declared - verified) / verified > 20% (standard UW/AML practice, not an SBI-document rule)",
            evidence="Declared or verified income not available for comparison.",
            sources=src,
        )
    gap_pct = (declared - verified) / verified * 100
    threshold = 20.0
    if gap_pct <= threshold:
        status, evidence = (
            "PASS",
            f"Declared ₹{declared:,} vs verified ₹{verified:,}, gap {gap_pct:.1f}% within {threshold:.0f}% threshold.",
        )
    else:
        status, evidence = (
            "WARN",
            (
                f"Declared ₹{declared:,} vs verified ₹{verified:,}, "
                f"gap {gap_pct:.1f}% exceeds the {threshold:.0f}% standard UW/AML threshold. "
                f"Use the verified figure for all eligibility calculations; underwriter to "
                f"obtain explanation for the over-declaration."
            ),
        )
    return GatingItem(
        criterion="sbi_income_over_declaration",
        label="Declared vs verified income gap",
        status=status,
        function="underwriting_platform.engine.sbi_financial.check_income_over_declaration",
        rule="(declared - verified) / verified > 20% (standard UW/AML practice, not an SBI-document rule)",
        evidence=evidence,
        sources=src,
    )


def check_rider_stacked_exposure(
    case: dict, calcs: dict, sbi_rules: dict, proposed_sa: int
) -> GatingItem:
    """Gap 8 — total payout on accidental death (base + ADB + BHB) vs HLV.
    Guidelines define SUC = Base SA only for Level Cover (Table 0 Row 1),
    so this is NOT a violation of a documented SA/SUC rule — it's a
    supplementary sanity check, stated as such in the evidence."""
    app = case.get("applicant") or {}
    base_sa = _safe_int(proposed_sa) or _safe_int(app.get("sum_assured"))
    adb_sa = _safe_int(app.get("adb_sum_assured"))
    bhb_sa = _safe_int(app.get("bhb_sum_assured"))
    hlv_base = _safe_int(_calc_value(calcs, "hlv_base"))
    src = [
        Source(
            document="Proposal form — plan/rider section",
            agent="identity",
            field_path="applicant.sum_assured / adb_sum_assured / bhb_sum_assured",
        ),
        Source(
            document="Engine HLV calculation",
            agent="underwriting_platform.engine.calculator",
            field_path="calculations.hlv_base",
        ),
    ]
    if not (adb_sa or bhb_sa):
        return GatingItem(
            criterion="sbi_rider_stacked_exposure",
            label="Rider-stacked total exposure vs HLV",
            status="NA",
            function="underwriting_platform.engine.sbi_financial.check_rider_stacked_exposure",
            rule="base_sa + adb_sa + bhb_sa vs hlv_base (supplementary check — SUC itself is Base SA only per guidelines Table 0 Row 1)",
            evidence="No ADB/BHB riders on file for this case.",
            sources=src,
        )
    total_exposure = base_sa + adb_sa + bhb_sa
    if not hlv_base:
        return GatingItem(
            criterion="sbi_rider_stacked_exposure",
            label="Rider-stacked total exposure vs HLV",
            status="WARN",
            function="underwriting_platform.engine.sbi_financial.check_rider_stacked_exposure",
            rule="base_sa + adb_sa + bhb_sa vs hlv_base",
            evidence=f"Total accidental-death payout ₹{total_exposure:,} (base ₹{base_sa:,} + ADB ₹{adb_sa:,} + BHB ₹{bhb_sa:,}); HLV not available to compare.",
            sources=src,
        )
    if total_exposure > hlv_base:
        status = "WARN"
        evidence = (
            f"Total accidental-death payout ₹{total_exposure:,} (base ₹{base_sa:,} + ADB ₹{adb_sa:,} "
            f"+ BHB ₹{bhb_sa:,}) exceeds HLV ₹{hlv_base:,}. Per guidelines, SUC = Base SA only for "
            f"Level Cover (not a documented violation), but the rider-stacked combination warrants "
            f"manual UW sign-off."
        )
    else:
        status = "PASS"
        evidence = f"Total accidental-death payout ₹{total_exposure:,} within HLV ₹{hlv_base:,}."
    return GatingItem(
        criterion="sbi_rider_stacked_exposure",
        label="Rider-stacked total exposure vs HLV",
        status=status,
        function="underwriting_platform.engine.sbi_financial.check_rider_stacked_exposure",
        rule="base_sa + adb_sa + bhb_sa vs hlv_base (supplementary check — SUC itself is Base SA only per guidelines Table 0 Row 1)",
        evidence=evidence,
        sources=src,
    )


def _upgrade_income_documented_for_sbi_bank_statement(
    case: dict, calcs: dict, prior_gating: list[GatingItem] | None
) -> None:
    """SBI Smart Shield Plus's own "List of Standard Income Proofs"
    (Undewriring Guidelines V02.docx) names "Bank statement reflecting
    salary credits for last six months" as an explicitly accepted standard
    proof for Salaried applicants — not a weaker fallback the way the
    generic engine.gating.check_income_documented treats it (WARN, "no
    slip/Form-16/ITR"), which has no knowledge of this product's own proof
    list. Upgrades that WARN to PASS IN PLACE (mutates the existing
    GatingItem already in prior_gating — does not append a second
    "income_documented" criterion) when: the case is Salaried, the
    verified-income method is bank_credits, and at least 6 months of
    credits actually fed that figure."""
    if not prior_gating:
        return
    item = next((g for g in prior_gating if g.criterion == "income_documented"), None)
    if item is None or item.status != "WARN":
        return
    income_calc = (calcs or {}).get("selected_income")
    method = (getattr(income_calc, "method", "") or "").lower()
    base = method
    for suf in ("_fx_inr", "_modal_x_12", "_mean_x_12", "_x_12"):
        if base.endswith(suf):
            base = base[: -len(suf)]
    if base != "bank_credits":
        return
    app = case.get("applicant") or {}
    occ = (app.get("occupation") or "").lower()
    is_salaried = "salar" in occ and not any(
        k in occ for k in ("self", "business", "profession", "propriet")
    )
    if not is_salaried:
        return
    n_credits = _safe_int((getattr(income_calc, "inputs", None) or {}).get("n_bank_credits"))
    if n_credits < 6:
        return
    item.status = "PASS"
    item.rule = (
        "sbi_smart_shield_rules — List of Standard Income Proofs (Salaried): "
        "bank statement reflecting salary credits for last six months"
    )
    item.evidence = (
        f"Income ₹{_safe_int(getattr(income_calc, 'value', 0)):,} documented via bank "
        f"statement showing {n_credits} months of recurring salary credits — an "
        f"explicitly accepted standard income proof for Salaried applicants under "
        f"this product."
    )


def _upgrade_kyc_evidence_with_specifics(case: dict, prior_gating: list[GatingItem] | None) -> None:
    """underwriting_platform.engine.gating.check_kyc's evidence ("KYC status = 'Partial'. CAF/ID
    gaps possible.") is a correct status call but names no actual gap —
    "gaps possible" tells an underwriter nothing they can act on, and the
    compliance agent's own kyc_evidence sub-document already records
    exactly which documents are and aren't on file. Replace the generic
    text with the specific missing items when status isn't PASS. Mutates
    the existing "kyc" GatingItem in place (does not append a
    duplicate)."""
    if not prior_gating:
        return
    item = next((g for g in prior_gating if g.criterion == "kyc"), None)
    if item is None or item.status not in ("WARN", "FAIL"):
        return
    kyc_ev = (case.get("compliance") or {}).get("kyc_evidence") or {}
    if not kyc_ev:
        return
    missing = []
    photo_id_present = any(
        kyc_ev.get(k)
        for k in ("aadhaar_present", "passport_present", "driving_licence", "voter_id_present")
    )
    if not photo_id_present:
        missing.append("Photo ID (Aadhaar / Passport / Driving Licence)")
    if not kyc_ev.get("pan_present"):
        missing.append("PAN")
    if not kyc_ev.get("address_proof_present"):
        missing.append("address proof")
    if not missing:
        return
    item.evidence = f"Missing from the document set: {', '.join(missing)}."
    item.rule = "compliance.kyc_evidence — required KYC documents on file"


def run_sbi_gating(
    case: dict,
    calcs: dict,
    sbi_rules: dict,
    proposed_sa: int,
    prior_gating: list[GatingItem] | None = None,
) -> list[GatingItem]:
    """Composite runner — mirrors engine.gating.run_gating's shape.
    `calcs` is the engine's dict of Calculation dataclass instances (same
    object engine/run.py passes to engine.gating.run_gating). `prior_gating`
    is the already-computed generic gating list (engine.gating.run_gating's
    output, which runs before this) — passed through so check_cibil_gate
    can see sbi_qualification's real PASS/FAIL rather than just whether a
    hard-proof income METHOD was used."""
    _upgrade_income_documented_for_sbi_bank_statement(case, calcs, prior_gating)
    _upgrade_kyc_evidence_with_specifics(case, prior_gating)
    items: list[GatingItem] = []
    # Computed first (ahead of its position in `items`) so check_cibil_gate
    # can see its authoritative PASS/FAIL via prior_gating.
    qualification_item = check_qualification_gate(case, calcs, sbi_rules)
    cibil_prior_gating = list(prior_gating or []) + [qualification_item]
    items.append(check_cibil_gate(case, sbi_rules, calcs, cibil_prior_gating))
    items.append(check_iib_gate(case, sbi_rules))
    items.append(check_drc_income_floor(case, calcs, sbi_rules))
    items.append(qualification_item)
    items.append(check_occupation_income_proof(case, sbi_rules, proposed_sa))
    items.append(check_student_agriculturist_riders(case, sbi_rules))
    items.append(check_keyman_partnership_ee_age_cap(case, sbi_rules))
    items.append(check_salaried_extended_cover(case, calcs, sbi_rules, proposed_sa))
    items.append(check_material_non_disclosure(case, sbi_rules))
    items.append(check_pending_proposal_disclosure(case, sbi_rules))
    items.append(check_bhb_eligibility(case, calcs, sbi_rules))
    items.append(check_hlv_remaining(case, calcs, sbi_rules, proposed_sa))
    items.append(check_income_over_declaration(case, calcs, sbi_rules))
    items.append(check_rider_stacked_exposure(case, calcs, sbi_rules, proposed_sa))
    items.append(check_piwc_requirement(case, sbi_rules))
    items.append(check_medical_requirement(case, sbi_rules, proposed_sa))
    return items
