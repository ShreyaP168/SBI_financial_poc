"""
engine/run.py — driver that turns a v9 case_json into a final decision_json.

Phase-6 financial calculations + Phase-8b medical calculations are emitted
side-by-side as Calculation objects.

Decision JSON structure:

    {
      "case_id": "...",
      "proposed_sa": 30000000,
      "applicant": {... from case_json ...},
      "calculations": {
        # financial
        "age", "selected_income", "monthly_gross", "monthly_emi_total",
        "foir", "hlv_base", "hlv_adb_atpd", "hlv_ci_hc", "max_sa_eligible",
        "cover_to_loan_ratio" (optional, Credit Protect),
        # medical (when medical block present)
        "bmi_band", "required_tests", "test_compliance_pct",
        "lab_abnormalities", "health_score",
        "medical_risk", "fraud_risk"
      },
      "gating":  [...],
      "flags":   [...],
      "verdict": {...},
      "reconciliation": {...},
      "extraction_blocks": {... per-agent blocks ...}
    }
"""

from __future__ import annotations
from underwriting_platform.configuration.paths import CONFIG_ROOT
import json
import logging
from pathlib import Path

from .calculator import (
    compute_age,
    compute_selected_income,
    compute_monthly_gross,
    compute_monthly_emi,
    compute_foir,
    compute_hlv_base,
    compute_hlv_adb_atpd,
    compute_hlv_ci_hc,
    compute_max_sa,
    compute_cover_to_loan,
    compute_business_insurance_value,
    _safe_int,
    normalize_primary_source,
)
from .medical import (
    compute_bmi_band,
    lookup_required_tests,
    compute_test_compliance,
    count_lab_abnormalities,
    compute_health_score,
    compute_medical_risk,
    compute_fraud_risk,
    apply_medical_overrides,
)
from .gating import run_gating
from .flags import derive_all_flags
from .verdict import make_verdict
from .acm import compute_acm
from .loading import compute_loading
from .routing import is_sbi_case as _is_sbi_case
from .types import GatingItem
from .rule_catalog import load_sbi_rules, validate_sbi_rules

logger = logging.getLogger(__name__)


_MEDICAL_RULES_CACHE: dict | None = None


def _load_medical_rules() -> dict:
    global _MEDICAL_RULES_CACHE
    if _MEDICAL_RULES_CACHE is not None:
        return _MEDICAL_RULES_CACHE
    p = CONFIG_ROOT / "medical_rules.json"
    if not p.exists():
        logger.warning(
            "medical_rules.json not found at %s; medical engine will fall back to defaults.", p
        )
        _MEDICAL_RULES_CACHE = {}
        return _MEDICAL_RULES_CACHE
    try:
        _MEDICAL_RULES_CACHE = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Failed to read medical_rules.json: %s", e)
        _MEDICAL_RULES_CACHE = {}
    return _MEDICAL_RULES_CACHE


def _run_engine(
    case_json: dict, rules: dict, proposed_sa: int = 0, sbi_rules: dict | None = None
) -> dict:
    """Run the full deterministic engine over an extraction+reconciliation case_json."""

    # Established once, used throughout — client requirement: "Do not use
    # TATA-specific thresholds or logic for SBI cases unless independently
    # supported by an SBI reference." config/uw_rules.json is explicitly
    # TATA AIA's own file ("insurer": "TATA AIA Life Insurance") — every
    # use of it below is guarded against SBI cases where no SBI-sourced
    # equivalent exists.
    is_sbi_case = _is_sbi_case(case_json)
    income_block = case_json.get("income") or {}
    if income_block.get("primary_source"):
        reported_source = income_block["primary_source"]
        income_block["primary_source"] = normalize_primary_source(reported_source)
        if reported_source != income_block["primary_source"]:
            income_block["primary_source_reported"] = reported_source
    # A decision letter about this proposal is current-case history, not proof
    # that the applicant concealed another declined proposal at application time.
    case_id = str(case_json.get("case_id") or "").lower()
    recon = case_json.get("reconciliation") or {}
    for signal in recon.get("risk_signals") or []:
        description = str(signal.get("description") or "")
        lower = description.lower()
        if (
            case_id
            and case_id in lower
            and any(t in lower for t in ("prior-decline", "prior decline", "previously declined"))
        ):
            signal["original_interpretation"] = description
            signal["code"] = "CURRENT_PROPOSAL_HISTORY"
            signal["severity"] = "medium"
            signal["description"] = (
                "Correspondence records a decision on this current proposal; verify its chronology before drawing any conclusion about prior-proposal disclosure."
            )
            recon["history_review_note"] = (
                "Current-proposal correspondence alone does not establish concealment of a prior proposal."
            )

    # ── Financial layer ───────────────────────────────────────────
    age_calc = compute_age(case_json)
    extracted_age = (case_json.get("applicant") or {}).get("age")
    # Normalise the displayed age to the authoritative entry age. The renderer
    # prints applicant.age (the identity agent's value, which drifts +1 once a
    # birthday passes the run date); overwrite it with compute_age's SI-anchored
    # result so the case sheet, the HLV age-band, and the SI all agree.
    if age_calc.value is not None:
        case_json.setdefault("applicant", {})["age"] = age_calc.value
    income_calc = compute_selected_income(case_json)
    monthly_gross = compute_monthly_gross(income_calc)
    monthly_emi = compute_monthly_emi(case_json)
    foir = compute_foir(monthly_gross, monthly_emi, rules, is_sbi_case=is_sbi_case)
    hlv_base = None if is_sbi_case else compute_hlv_base(income_calc, age_calc.value, rules)
    # hlv_adb_atpd / hlv_ci_hc use uw_rules.hlv_grids (TATA's own age-band
    # multiplier tables) and are never rendered on the SBI case sheet —
    # skipped entirely for SBI cases rather than computed-but-unused, so
    # a TATA guideline is never consulted at all while processing one.
    hlv_adb = (
        None
        if is_sbi_case
        else compute_hlv_adb_atpd(income_calc, age_calc.value, rules, proposed_sa)
    )
    hlv_ci = None if is_sbi_case else compute_hlv_ci_hc(income_calc, age_calc.value, rules)
    # SBI Smart Shield Plus has its OWN Verified Income multiplicative
    # factor table (age-band x income-tier), present in the guidelines
    # docx as a nested table. When available, this REPLACES the generic
    # engine's hlv_base so every downstream consumer
    # (max_sa_eligible, the SA-vs-HLV gate, the SBI-specific HLV-remaining
    # and rider-stacked-exposure checks, the Part E display row) agrees on
    # one correctly-sourced figure instead of two conflicting ones.
    if is_sbi_case:
        try:
            from .sbi_financial import compute_sbi_hlv, apply_govt_psu_income_multiple

            sbi_rules_for_hlv = (
                load_sbi_rules() if sbi_rules is None else validate_sbi_rules(sbi_rules)
            )
            # Government/Semi-Government/PSU employees get a 1.2x derived-
            # income multiple for HLV purposes (guideline's Salaried-class-
            # only row, item 3) — apply before the multiplier-table lookup
            # so the boosted figure flows through HLV consistently. Returns
            # income_calc unchanged when not applicable.
            income_calc_for_hlv = apply_govt_psu_income_multiple(
                income_calc, case_json, sbi_rules_for_hlv
            )
            sbi_hlv = compute_sbi_hlv(income_calc_for_hlv, age_calc.value, sbi_rules_for_hlv)
            if sbi_hlv is not None:
                # "Individual Consideration" cell (age > 60, income < 5L):
                # sbi_hlv.value is None — the SBI source table itself has no
                # numeric multiplier here ("refer for manual UW"), so this IS
                # the correct, fully-SBI-sourced result for that scenario,
                # not a missing computation to patch over. hlv_base is
                # replaced with it either way (not just when a numeric value
                # exists) — previously this case silently kept the generic
                # TATA-grid hlv_base (config/uw_rules.json, not an SBI
                # document) as a "cross-check", which could present a
                # TATA-derived number as if it were an SBI-sourced HLV.
                # Per-case: not currently hit by any case on file (none are
                # age > 60), but the fallback was a live dependency on a
                # non-SBI rule, not dead code.
                hlv_base = sbi_hlv
            else:
                from .types import Calculation

                hlv_base = Calculation(
                    "hlv_base",
                    None,
                    method="sbi_evidence_incomplete",
                    note="SBI HLV cannot be established from available age/income evidence.",
                )
        except Exception as e:
            raise RuntimeError("SBI HLV computation failed") from e
    # Existing cover (Datamatics LAYOUT r45-r46) — subtract declared
    # cross-insurer total from HLV before producing max_sa_eligible.
    existing_cover_total = 0
    try:
        compliance_blk = case_json.get("compliance") or {}
        ecd = compliance_blk.get("existing_cover_declared") or {}
        existing_cover_total = int(ecd.get("total_life_cover_other") or 0)
    except (TypeError, ValueError) as e:
        raise ValueError("Invalid declared existing cover") from e
    # SBI bureau feed (IIB findings) has landed for SBI Smart Shield Plus
    # cases — use the bureau-verified in-force total instead of the
    # declared figure when it's higher (the declared figure is ₹0 for any
    # case where the applicant didn't disclose existing cover, which
    # understates true exposure rather than overstating it).
    if is_sbi_case:
        try:
            from .sbi_financial import _aggregate_iib_sa

            bureau_inforce = _aggregate_iib_sa((case_json.get("sbi_bureau") or {})).get(
                "combined_total_sa", 0
            )
            existing_cover_total = max(existing_cover_total, _safe_int(bureau_inforce))
        except Exception as e:
            raise RuntimeError("SBI existing-cover aggregation failed") from e
    # Business insurance (Keyman / Partnership / Employer-Employee) — when
    # applicable, its deterministic insurable value (Term UW V5 p42) is the
    # eligibility ceiling, superseding the individual income HLV. The
    # formula itself (director-remuneration/gross-profit/NPAT multiples,
    # and the keyman_cap_crore cap) is entirely TATA-sourced (Term UW V5,
    # not an SBI document) — skipped for SBI cases rather than
    # computed-but-unused, same as hlv_adb_atpd/hlv_ci_hc above. The SBI
    # extraction schema never populates case_json["business_insurance"]
    # today (confirmed across all cases on file), so this is currently a
    # structural guard rather than a change to any live result; SBI's own
    # Keyman/Partnership/Employer-Employee provision (the age-65 ceiling —
    # see engine.sbi_financial.check_keyman_partnership_ee_age_cap) has no
    # insurable-value FORMULA in the SBI reference documents reviewed, so
    # there is no SBI-sourced rule to substitute here.
    biz_calc = None if is_sbi_case else compute_business_insurance_value(case_json, hlv_base, rules)
    eff_hlv = biz_calc if biz_calc is not None and _safe_int(biz_calc.value) > 0 else hlv_base
    max_sa = compute_max_sa(
        eff_hlv, proposed_sa, rules, existing_cover=existing_cover_total, is_sbi_case=is_sbi_case
    )

    # Bank statement Perfios-style analysis — Datamatics LAYOUT Fin
    # #3/#4/#5. Pure-Python deterministic layer on top of the income
    # agent's transactions[] extraction. Attaches case_json["bank_analysis"]
    # with monthly aggregates, detected EMIs, bounces, recurring credits.
    try:
        from .bank_statement_analyzer import attach_to_case as _attach_bank

        _attach_bank(case_json)
    except Exception as e:
        raise RuntimeError("Bank evidence processing failed") from e

    calcs: dict = {
        "age": age_calc,
        "selected_income": income_calc,
        "monthly_gross": monthly_gross,
        "monthly_emi_total": monthly_emi,
        "foir": foir,
        "hlv_base": hlv_base,
        "max_sa_eligible": max_sa,
    }
    # Omitted for SBI cases (see hlv_adb/hlv_ci above) — None, not a
    # Calculation, so left out of the dict entirely rather than stored.
    if hlv_adb is not None:
        calcs["hlv_adb_atpd"] = hlv_adb
    if hlv_ci is not None:
        calcs["hlv_ci_hc"] = hlv_ci
    if biz_calc is not None:
        calcs["business_insurance_value"] = biz_calc
    cl = compute_cover_to_loan(case_json)
    if cl is not None:
        calcs["cover_to_loan_ratio"] = cl

    # ── Medical layer (only if a medical block exists) ────────────
    # Apply deterministic overrides (physician ECG override, THR<85% →
    # Postponed) BEFORE we compute BMI / risk so the calcs see the
    # corrected values.
    if not is_sbi_case:
        case_json = apply_medical_overrides(case_json)
    # Authoritative lab-range overlay — TALIC master from
    # Medical Data ranges.xlsx. Re-classifies medical.lab_results[]
    # statuses (NORMAL/HIGH/LOW) against gender-specific reference
    # bands, overriding the agent's per-report guess.
    try:
        from .lab_ranges import reclassify_labs

        if not is_sbi_case:
            case_json = reclassify_labs(case_json)
    except Exception as e:
        raise RuntimeError("Mandatory medical evidence processing failed") from e
    medical_block = case_json.get("medical") or {}
    # bmi_bands (uw_rules.json, TATA's own bands) and the rest of this
    # layer are already a no-op for every real SBI financial case today
    # (no medical block exists) — guarded explicitly on is_sbi_case too,
    # not just the empty-dict check, so a future SBI case that happened
    # to carry medical data still wouldn't pull in TATA's medical rules.
    if medical_block and not is_sbi_case:
        med_rules = _load_medical_rules()
        bmi_calc = compute_bmi_band(case_json, rules)
        req_calc = lookup_required_tests(case_json, age_calc.value, med_rules, proposed_sa)
        comp_calc = compute_test_compliance(case_json, req_calc)
        abn_calc = count_lab_abnormalities(case_json)
        health_calc = compute_health_score(case_json, bmi_calc, abn_calc)
        med_risk = compute_medical_risk(case_json, age_calc.value, health_calc, abn_calc, bmi_calc)
        fraud = compute_fraud_risk(case_json)
        # Medical UW Terminal Decision — Datamatics LAYOUT row 70.
        from .medical import compute_medical_uw_terminal_decision as _med_term

        med_terminal = _med_term(case_json, med_risk, req_calc, comp_calc, abn_calc)
        calcs.update(
            {
                "bmi_band": bmi_calc,
                "required_tests": req_calc,
                "test_compliance_pct": comp_calc,
                "lab_abnormalities": abn_calc,
                "health_score": health_calc,
                "medical_risk": med_risk,
                "medical_uw_terminal": med_terminal,
                "fraud_risk": fraud,
            }
        )

    gating = run_gating(case_json, calcs, rules, proposed_sa)
    if is_sbi_case:
        bureau = case_json.get("sbi_bureau") or {}
        missing = [
            key
            for key in ("drc_category", "profile_score", "iib_policies")
            if bureau.get(key) is None or (key != "iib_policies" and not bureau.get(key))
        ]
        # An explicit empty IIB list is a completed no-match search; an absent list is not.
        if missing:
            names = {
                "drc_category": "DRC risk category",
                "profile_score": "Profile score",
                "iib_policies": "IIB policy search results",
            }
            gating.append(
                GatingItem(
                    "sbi_bureau_evidence",
                    "Mandatory bureau evidence",
                    "FAIL",
                    rule="sbi_smart_shield_rules._meta",
                    evidence="Missing bureau evidence: " + ", ".join(names[k] for k in missing),
                )
            )
        if proposed_sa <= 0 or age_calc.value is None or not case_json.get("applicant"):
            gating.append(
                GatingItem(
                    "sbi_core_evidence",
                    "Required application evidence",
                    "FAIL",
                    rule="sbi_smart_shield_rules.entry_age / basic_sum_assured",
                    evidence="Applicant, entry age and positive sum assured must be established.",
                )
            )
        if hlv_base.value is None:
            gating.append(
                GatingItem(
                    "sbi_hlv_evidence",
                    "HLV requires manual review",
                    "FAIL",
                    rule="sbi_smart_shield_rules.income_multiplier_table",
                    evidence=hlv_base.note,
                )
            )

    # SBI Life "Smart Shield Plus" financial UW gates (Financial UW POC).
    # Additive only — runs strictly when a case carries SBI bureau data.
    requirements_to_raise: list[dict] = []
    if is_sbi_case:
        try:
            from .sbi_financial import run_sbi_gating

            sbi_gates = run_sbi_gating(
                case_json, calcs, sbi_rules_for_hlv, proposed_sa, prior_gating=gating
            )
            from underwriting_platform.domain.policy import SBI_GATES

            required = SBI_GATES
            if not required.issubset({g.criterion for g in sbi_gates}):
                raise ValueError("Mandatory SBI eligibility evaluations missing")
            if any(g.status not in ("PASS", "WARN", "FAIL", "NA") for g in sbi_gates):
                raise ValueError("Invalid eligibility status")
            gating += sbi_gates
        except Exception as e:
            raise RuntimeError("SBI eligibility processing failed") from e
        try:
            from .requirements import compute_requirements_to_raise

            requirements_to_raise = compute_requirements_to_raise(case_json, calcs, gating)
        except Exception as e:
            raise RuntimeError("Mandatory requirement processing failed") from e

    flags = derive_all_flags(case_json, calcs, gating, rules)
    verdict = make_verdict(
        case_json, calcs, gating, flags, proposed_sa, requirements_to_raise=requirements_to_raise
    )
    from .requirements import affordability_advisory

    advisory_findings = affordability_advisory(case_json, calcs)
    if age_calc.value is not None and extracted_age is not None and age_calc.value != extracted_age:
        advisory_findings.append(
            {
                "code": "ENTRY_AGE_DIFFERS_FROM_EXTRACTED",
                "advisory_only": True,
                "extracted_age": extracted_age,
                "entry_age": age_calc.value,
                "method": age_calc.method,
                "rule": age_calc.rule,
                "note": "Eligibility and HLV use the documented entry age; extracted age retained in normalized input.",
            }
        )
    advisory_findings.extend(
        {"code": "RIDER_EXPOSURE", "advisory_only": True, "note": g.evidence}
        for g in gating
        if g.criterion == "sbi_rider_stacked_exposure" and g.status == "WARN"
    )
    acm = compute_acm(case_json)
    loading = compute_loading(case_json)

    return {
        "case_id": case_json.get("case_id"),
        "routing": {"rule_set": "sbi_smart_shield_plus" if is_sbi_case else "generic"},
        "proposed_sa": proposed_sa,
        "applicant": case_json.get("applicant") or {},
        "nominee": case_json.get("nominee") or {},
        "policy_data": case_json.get("policy_data") or {},
        "iib": case_json.get("iib") or {},
        "alpha_policy": case_json.get("alpha_policy") or {},
        "sbi_bureau": case_json.get("sbi_bureau") or {},
        "bank_analysis": case_json.get("bank_analysis") or {},
        "calculations": {k: v.to_dict() for k, v in calcs.items()},
        "gating": [g.to_dict() for g in gating],
        "flags": [f.to_dict() for f in flags],
        "verdict": verdict.to_dict(),
        "acm": acm.to_dict(),
        "loading": loading.to_dict(),
        "requirements_to_raise": requirements_to_raise,
        "advisory_findings": advisory_findings,
        "reconciliation": case_json.get("reconciliation") or {},
        "extraction_blocks": {
            "identity": {
                "applicant": case_json.get("applicant") or {},
                "anti_fraud_markers": case_json.get("anti_fraud_markers") or {},
            },
            "income": case_json.get("income") or {},
            "assets_liabilities": case_json.get("assets_liabilities") or {},
            "compliance": case_json.get("compliance") or {},
            "business_insurance": case_json.get("business_insurance") or {},
            "credit_protect": case_json.get("credit_protect") or {},
            "surrogate": case_json.get("surrogate") or {},
            "medical": medical_block,
        },
    }


def run_engine(
    case_json: dict, rules: dict, proposed_sa: int = 0, *, sbi_rules: dict | None = None
) -> dict:
    """No processing failure may produce an issuance recommendation."""
    try:
        if not isinstance(case_json, dict):
            raise ValueError("Expected a case JSON object")
        if case_json.get("processing_errors"):
            # Preserve computations from usable partial facts for human review.
            # This path can never publish the diagnostic underwriting verdict.
            import copy

            partial = copy.deepcopy(case_json)
            errors = partial.pop("processing_errors")
            from .audit import attach_field_evidence

            diagnostic = attach_field_evidence(
                _run_engine(partial, rules, proposed_sa, sbi_rules), partial
            )
            diagnostic["_diagnostic_verdict"] = copy.deepcopy(diagnostic["verdict"])
            diagnostic["verdict"] = {
                "label": "INCOMPLETE",
                "decision_drivers": [
                    "Mandatory processing incomplete; diagnostic facts retained for review"
                ],
            }
            diagnostic["processing_errors"] = errors
            return diagnostic
        from .audit import attach_field_evidence

        return attach_field_evidence(
            _run_engine(case_json, rules, proposed_sa, sbi_rules), case_json
        )
    except Exception as exc:
        logger.exception("Underwriting processing failed closed")
        case_json = case_json if isinstance(case_json, dict) else {}
        return {
            "case_id": case_json.get("case_id"),
            "proposed_sa": proposed_sa,
            "applicant": case_json.get("applicant")
            if isinstance(case_json.get("applicant"), dict)
            else {},
            "routing": {
                "rule_set": "sbi_smart_shield_plus" if _is_sbi_case(case_json) else "generic"
            },
            "calculations": {},
            "flags": [],
            "gating": [
                {
                    "criterion": "mandatory_processing",
                    "label": "Mandatory underwriting processing",
                    "status": "FAIL",
                    "evidence": "Processing could not be completed; manual review required.",
                }
            ],
            "requirements_to_raise": [
                {
                    "text": "Manual review required: underwriting processing incomplete.",
                    "critical": True,
                }
            ],
            "verdict": {
                "label": "INCOMPLETE",
                "confidence": 0.0,
                "decision_drivers": [
                    "Mandatory underwriting processing incomplete; issuance blocked."
                ],
                "rationale": "Manual review required before any issuance decision.",
            },
            "processing_errors": [
                {"stage": "engine", "type": type(exc).__name__, "message": str(exc)}
            ],
        }
