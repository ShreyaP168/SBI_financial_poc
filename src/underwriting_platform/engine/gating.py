"""
engine/gating.py — pass/warn/fail criteria for the v9 engine.

Each check returns a `GatingItem` (engine.types.GatingItem). The engine
driver collects them into a list; the verdict layer reads the count of
FAILs / WARNs to grade the case.
"""

from __future__ import annotations
from .routing import is_sbi_case as _is_sbi_case
import re
from typing import Any

from .calculator import _safe_int, _safe_float
from .types import GatingItem, Source, Calculation

# Matches internal field-path notation like "existing_cover_declared.
# total_life_cover_other = 0" or "income.annual_income = 450000" — the
# reconciler agent's risk_signal descriptions are written for an
# audit trail (precise, schema-referencing) and sometimes include a
# leading sentence of this kind. The Case sheet format guideline is
# explicit: "Any technical words... to be avoided" in client-facing text.
_FIELD_PATH_SENTENCE_RE = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,}\s*=")


def _strip_technical_jargon(text: str | None) -> str:
    """Drop any sentence containing raw internal field-path notation
    (e.g. "a.b.c = 0") from an otherwise natural-language evidence string,
    so reconciler/agent-authored text is safe to show an SBI underwriter.
    Falls back to the original text if stripping would empty it out."""
    if not text:
        return text or ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    clean = [s for s in sentences if not _FIELD_PATH_SENTENCE_RE.search(s)]
    result = " ".join(clean).strip()
    if not result:
        return text
    # A leading contrast word ("However, ...") only makes sense set
    # against a preceding sentence — if that sentence was the one
    # stripped, the orphaned contrast word reads oddly as the new opener.
    result = re.sub(r"^(However|But|Nevertheless|Nonetheless),?\s+", "", result)
    result = result[0].upper() + result[1:] if result else text
    # Reconciler/agent-authored text (not written by this codebase) often
    # uses an em-dash as a clause separator — one consistent style, no
    # em-dashes, throughout the sheet. A semicolon is a safe universal
    # substitute here: it needs no
    # capitalisation fix on the following clause (unlike a period) and
    # reads naturally wherever the source text used the dash as "furthermore"
    # or "specifically" rather than a true parenthetical aside.
    return result.replace(" — ", "; ").replace("—", "-")


# ──────────────────────────────────────────────────────────────────────────
# Individual checks
# ──────────────────────────────────────────────────────────────────────────


def check_income_documented(income_calc: Calculation, rules: dict) -> GatingItem:
    annual = _safe_int(income_calc.value)
    method = (income_calc.method or "").lower()
    # Strip engine-side suffixes ('_x_12', '_fx_inr', '_modal_x_12', etc.)
    # so the bucket detection still works after deterministic recompute or
    # FX conversion.
    base = method
    for suf in ("_fx_inr", "_modal_x_12", "_mean_x_12", "_x_12"):
        if base.endswith(suf):
            base = base[: -len(suf)]
    hard_methods = {"salary_slip", "form_16", "ca_computation", "audited_financials", "itr"}
    # ITR-derived methods carry descriptive suffixes (itr_avg_latest_2_years,
    # itr_latest_year, etc.) — any itr-prefixed method is still hard proof.
    is_itr_based = base in hard_methods or base.startswith("itr")
    # This gate is about whether hard documentation EXISTS, not whether
    # the figure it proves is adequate — a confirmed ₹0 earned income via
    # a full 3-year ITR filing (an applicant with passive income only, no
    # salary/business head) is still fully documented; the separate
    # income-floor/qualification gate is where an inadequate figure
    # correctly fails. Distinguishing "nothing was extracted" (FAIL) from
    # "a hard-proof method confirmed zero" (still PASS) matters — a plain
    # `annual <= 0` check would conflate the two.
    if is_itr_based or base in ("salary_slip", "form_16", "ca_computation", "audited_financials"):
        method_label = "ITR" if base.startswith("itr") else base.replace("_", " ")
        if annual <= 0:
            status, evidence = (
                "PASS",
                (
                    f"Income verified via {method_label} — confirmed ₹0 earned "
                    f"income (no salary or business/profession head on file; "
                    f"only passive income)."
                ),
            )
        else:
            status, evidence = "PASS", f"Income ₹{annual:,} documented via {method_label}."
    elif annual <= 0:
        status, evidence = "FAIL", "No income value extracted by income agent."
    elif base == "bank_credits":
        status, evidence = (
            "WARN",
            f"Income ₹{annual:,} from bank salary credits only — no slip/Form-16/ITR.",
        )
    elif base in ("declared", "declared_only"):
        status, evidence = "FAIL", f"Income ₹{annual:,} is declared-only (CAF) — no proof on file."
    else:
        status, evidence = (
            "WARN",
            f"Income method '{method.replace('_', ' ')}' is not a recognised hard-proof source.",
        )
    return GatingItem(
        criterion="income_documented",
        label="Income documented with hard proof",
        status=status,
        function="underwriting_platform.engine.gating.check_income_documented",
        rule="cascade rungs 1-5 = PASS, rung 6 = WARN, rung 7 = FAIL",
        evidence=evidence,
        sources=income_calc.sources,
    )


def check_residential_status(case: dict, rules: dict) -> GatingItem:
    applicant = case.get("applicant") or {}
    status_text = (applicant.get("residential_status") or "").strip()
    valid = (rules.get("gating") or {}).get("valid_residential_status") or []
    if not status_text:
        status = "WARN"
        evidence = "Residential status missing on applicant profile."
    elif status_text in valid or "resident" in status_text.lower():
        status = "PASS"
        evidence = f"Residential status = '{status_text}'."
    else:
        status = "WARN"
        evidence = f"Residential status = '{status_text}' (NRI/non-resident — additional checks)."
    return GatingItem(
        criterion="residential_status",
        label="Residential status acceptable",
        status=status,
        function="underwriting_platform.engine.gating.check_residential_status",
        rule=f"uw_rules.gating.valid_residential_status = {valid}",
        evidence=evidence,
        sources=[
            Source(
                document="Applicant profile (CAF / Identity agent)",
                agent="identity",
                field_path="applicant.residential_status",
            )
        ],
    )


def check_kyc(case: dict, rules: dict) -> GatingItem:
    comp = case.get("compliance") or {}
    kyc = (comp.get("kyc_status") or "").strip()
    if kyc.lower() == "verified":
        status, evidence = "PASS", "KYC verified by compliance agent."
    elif kyc.lower() in ("partial", "pending"):
        status, evidence = "WARN", f"KYC status = '{kyc}'. CAF/ID gaps possible."
    elif kyc.lower() == "failed":
        status, evidence = "FAIL", "KYC failed — identity proof inadequate."
    else:
        status, evidence = "WARN", f"KYC status = '{kyc or 'unknown'}'."
    return GatingItem(
        criterion="kyc",
        label="KYC complete",
        status=status,
        function="underwriting_platform.engine.gating.check_kyc",
        rule="compliance.kyc_status ∈ {Verified, Partial, Pending, Failed}",
        evidence=evidence,
        sources=[
            Source(
                document="ID proofs + CAF", agent="compliance", field_path="compliance.kyc_status"
            )
        ],
    )


def check_foir(foir_calc: Calculation, rules: dict, is_sbi_case: bool = False) -> GatingItem:
    foir = _safe_float(foir_calc.value)
    # uw_rules.foir_threshold_pct (55%) is TATA AIA's own affordability
    # threshold (config/uw_rules.json: "insurer": "TATA AIA Life
    # Insurance") — no SBI document defines an equivalent FOIR rule. The
    # computed FOIR % is still genuinely useful information (EMI load vs
    # income), so it's still shown for SBI cases — just never judged
    # PASS/WARN/FAIL against a borrowed TATA number.
    if is_sbi_case:
        return GatingItem(
            criterion="foir",
            label="FOIR (informational — no SBI affordability threshold)",
            status="NA",
            function="underwriting_platform.engine.gating.check_foir",
            rule="No SBI document defines a FOIR threshold for this product",
            evidence=f"FOIR {foir:.1f}% — shown for information only; not evaluated against any SBI rule.",
            sources=foir_calc.sources,
        )
    threshold = _safe_int(rules.get("foir_threshold_pct") or 55)
    coverage_days = _safe_int((foir_calc.inputs or {}).get("bank_statement_coverage_days"))
    short_window = 0 < coverage_days < 30
    emi_unknown_with_liabilities = bool(
        (foir_calc.inputs or {}).get("emi_unknown_with_liabilities")
    )
    liabilities_total = _safe_int((foir_calc.inputs or {}).get("liabilities_total"))
    if foir <= 0:
        status, evidence = "PASS", "No EMIs detected — FOIR effectively 0%."
        if short_window:
            status = "WARN"
            evidence += (
                f" Caveat: bank statement covers only {coverage_days} day(s) "
                f"— too short to reliably confirm no EMIs/recurring obligations "
                f"exist; a fuller statement should be obtained before relying "
                f"on this figure."
            )
        elif emi_unknown_with_liabilities:
            # Declared liabilities exist but no EMI breakdown was provided —
            # "no EMIs detected" reflects absent data, not a confirmed
            # absence of obligations.
            status = "WARN"
            evidence += (
                f" Caveat: declared liabilities total ₹{liabilities_total:,} "
                f"but no EMI breakdown was provided — EMI data unavailable, "
                f"not confirmed absent; obtain loan statements before relying "
                f"on this figure."
            )
    elif foir <= threshold:
        status, evidence = "PASS", f"FOIR {foir:.1f}% within threshold {threshold}%."
    elif foir <= threshold + 10:
        status, evidence = "WARN", f"FOIR {foir:.1f}% above threshold {threshold}% by ≤10pp."
    else:
        status, evidence = "FAIL", f"FOIR {foir:.1f}% exceeds threshold {threshold}% by >10pp."
    return GatingItem(
        criterion="foir",
        label="FOIR within affordability limit",
        status=status,
        function="underwriting_platform.engine.gating.check_foir",
        rule=f"uw_rules.foir_threshold_pct = {threshold}",
        evidence=evidence,
        sources=foir_calc.sources,
    )


def check_sa_within_hlv(max_sa: Calculation, proposed_sa: int) -> GatingItem:
    # max_sa.value is None (not 0) when the underlying HLV itself is None —
    # currently only the SBI "Individual Consideration" scenario (age > 60,
    # income < ₹5L), where SBI's own source table has no numeric multiplier
    # at all. That's a genuinely different state from a confirmed-zero HLV
    # and must not be reported as one.
    if max_sa.value is None:
        return GatingItem(
            criterion="sa_within_hlv",
            label="Proposed SA within HLV ceiling",
            status="NA",
            function="underwriting_platform.engine.gating.check_sa_within_hlv",
            rule="proposed_sa ≤ hlv_base",
            evidence=(
                max_sa.note
                or "HLV base is not specified by SBI guidelines for this "
                "case — refer for manual UW; this comparison cannot be automatically "
                "determined."
            ),
            sources=max_sa.sources,
        )
    chosen = _safe_int(max_sa.value)
    inputs = max_sa.inputs or {}
    hlv = _safe_int(inputs.get("hlv_base"))
    if proposed_sa <= 0:
        status, evidence = "WARN", "Proposed SA = 0 / not provided."
    elif hlv <= 0:
        # Worded as a confirmed inadequacy, not a data gap — HLV = 0 here
        # means verified earned income itself computed to ₹0 (income ×
        # multiplier = 0), which correctly and definitively fails any
        # positive proposed SA; it does not mean the eligibility check
        # could not be performed.
        status, evidence = (
            "FAIL",
            (
                "HLV computed as ₹0 (income-based eligibility is zero) — "
                "proposed SA cannot be justified by income."
            ),
        )
    elif proposed_sa <= hlv:
        status, evidence = (f"PASS", f"Proposed SA ₹{proposed_sa:,} ≤ HLV-base ₹{hlv:,}.")
    else:
        status, evidence = (f"FAIL", f"Proposed SA ₹{proposed_sa:,} exceeds HLV-base ₹{hlv:,}.")
    return GatingItem(
        criterion="sa_within_hlv",
        label="Proposed SA within HLV ceiling",
        status=status,
        function="underwriting_platform.engine.gating.check_sa_within_hlv",
        rule="proposed_sa ≤ hlv_base",
        evidence=evidence,
        sources=max_sa.sources,
    )


def check_min_income_for_sa(
    income_calc: Calculation, proposed_sa: int, rules: dict, is_sbi_case: bool = False
) -> GatingItem:
    """SA-tier income floor — minimum verified income required for the
    proposed-SA band. Bands defined in uw_rules.gating.sa_tier_min_income.

    This table is TATA AIA's own SA-tier income floor (config/uw_rules.json:
    "insurer": "TATA AIA Life Insurance"), not an SBI rule — not evaluated
    for SBI cases at all (client requirement: no TATA threshold for an SBI
    case unless independently SBI-sourced). The SBI-specific qualification
    gate is the sole authority for this floor on SBI cases."""
    if is_sbi_case:
        return GatingItem(
            criterion="min_income_for_sa",
            label="Verified income meets minimum for SA band",
            status="NA",
            function="underwriting_platform.engine.gating.check_min_income_for_sa",
            rule="uw_rules.gating.sa_tier_min_income (TATA AIA — not evaluated for SBI cases)",
            evidence=(
                "Not evaluated for SBI cases — superseded by the SBI-specific "
                "qualification gate (see 'Qualification / minimum-income gate')."
            ),
            sources=income_calc.sources,
        )
    annual = _safe_int(income_calc.value)
    _caveat = ""
    tiers = (rules.get("gating") or {}).get("sa_tier_min_income") or []
    chosen = None
    for tier in tiers:
        if proposed_sa <= _safe_int(tier.get("sa_max_inr")):
            chosen = tier
            break
    if not chosen:
        return GatingItem(
            criterion="min_income_for_sa",
            label="Verified income meets minimum for SA band",
            status="NA",
            function="underwriting_platform.engine.gating.check_min_income_for_sa",
            rule="uw_rules.gating.sa_tier_min_income",
            evidence="No SA tier matched (config missing or proposed_sa = 0).",
        )
    floor = _safe_int(chosen.get("min_income_inr"))
    label = chosen.get("label") or f"min income {floor:,} for SA up to {chosen.get('sa_max_inr'):,}"
    if proposed_sa <= 0:
        status, evidence = "NA", "Proposed SA = 0; no SA-tier floor applies."
    elif annual <= 0:
        # Distinguish a hard-proof-confirmed ₹0 from a genuine data gap —
        # same distinction as check_income_documented above (both
        # downstream of the same calculator figure). FAIL is correct
        # either way (₹0 never meets a positive floor); only the evidence
        # text differs between "unknown" and "confirmed zero".
        _method = (income_calc.method or "").lower()
        _hard = _method.startswith("itr") or _method.split("_fx_inr")[0] in (
            "salary_slip",
            "form_16",
            "ca_computation",
            "audited_financials",
        )
        status = "FAIL"
        evidence = (
            "Verified income confirmed as ₹0 (hard-proof documentation on file)."
            if _hard
            else "Verified income could not be established."
        )
    elif annual >= floor:
        status, evidence = (
            "PASS",
            (
                f"Verified income ₹{annual:,} ≥ floor ₹{floor:,} "
                f"for SA up to ₹{chosen.get('sa_max_inr'):,}."
            ),
        )
    else:
        status, evidence = (
            "FAIL",
            (
                f"Verified income ₹{annual:,} < floor ₹{floor:,} "
                f"for proposed SA ₹{proposed_sa:,} ({label})."
            ),
        )
    evidence += _caveat
    return GatingItem(
        criterion="min_income_for_sa",
        label="Verified income meets minimum for SA band",
        status=status,
        function="underwriting_platform.engine.gating.check_min_income_for_sa",
        rule=f"uw_rules.gating.sa_tier_min_income — {label}",
        evidence=evidence,
        sources=income_calc.sources,
    )


def check_existing_cover_disclosure(case: dict) -> GatingItem:
    """Caught by the reconciler too, but explicit here for the gating list."""
    recon = case.get("reconciliation") or {}
    risks = recon.get("risk_signals") or []
    undisclosed = [r for r in risks if r.get("code") == "UNDISCLOSED_EXISTING_COVER"]
    if undisclosed:
        sev = (undisclosed[0].get("severity") or "high").lower()
        status = "FAIL" if sev == "critical" else "WARN"
        evidence = (
            _strip_technical_jargon(undisclosed[0].get("description"))
            or "Undisclosed existing cover suspected."
        )
    else:
        status, evidence = "PASS", "No undisclosed existing cover signals."
    return GatingItem(
        criterion="existing_cover_disclosed",
        label="Existing cover declarations consistent with bank evidence",
        status=status,
        function="underwriting_platform.engine.gating.check_existing_cover_disclosure",
        rule="reconciler.risk_signals[UNDISCLOSED_EXISTING_COVER]",
        evidence=evidence,
    )


def check_medical_compliance(calcs: dict) -> GatingItem:
    """Required medical tests received vs grid."""
    comp = calcs.get("test_compliance_pct")
    if comp is None:
        return GatingItem(
            criterion="medical_compliance",
            label="Required medical tests received per grid",
            status="NA",
            function="underwriting_platform.engine.gating.check_medical_compliance",
            rule="medical_rules.MEDICAL_GRID",
            evidence="No medical block on file — gating skipped.",
        )
    pct = _safe_float(comp.value)
    inp = comp.inputs or {}
    received = inp.get("received") or []
    missing = inp.get("missing") or []
    if pct >= 100:
        status, evidence = "PASS", f"All {len(received)} required tests present."
    elif pct >= 75:
        status, evidence = ("WARN", f"{int(pct)}% test compliance — missing: {', '.join(missing)}.")
    else:
        status, evidence = (
            "FAIL",
            f"Only {int(pct)}% test compliance — missing: {', '.join(missing)}.",
        )
    return GatingItem(
        criterion="medical_compliance",
        label="Required medical tests received per grid",
        status=status,
        function="underwriting_platform.engine.gating.check_medical_compliance",
        rule="medical_rules.MEDICAL_GRID + medical.documents_present",
        evidence=evidence,
        sources=comp.sources,
    )


def check_fraud_signal(calcs: dict) -> GatingItem:
    fraud = calcs.get("fraud_risk")
    if fraud is None:
        return GatingItem(
            criterion="fraud_signal",
            label="No fraud red flags",
            status="NA",
            function="underwriting_platform.engine.gating.check_fraud_signal",
            rule="medical.fraud_signals",
            evidence="No medical block on file — fraud check skipped.",
        )
    band = str(fraud.value or "LOW").upper()
    note = fraud.note or ""
    if band == "HIGH":
        status, evidence = "FAIL", f"Fraud HIGH: {note}"
    elif band == "MODERATE":
        status, evidence = "WARN", f"Fraud MODERATE: {note}"
    else:
        status, evidence = "PASS", "No fraud red flags detected."
    return GatingItem(
        criterion="fraud_signal",
        label="No fraud red flags (cotinine + MER↔VMER consistency)",
        status=status,
        function="underwriting_platform.engine.gating.check_fraud_signal",
        rule="medical_rules.MEDICAL_GRID.special_rules — Cotinine ≥10 ng/mL",
        evidence=evidence,
        sources=fraud.sources,
    )


# ──────────────────────────────────────────────────────────────────────────
# Composite runner
# ──────────────────────────────────────────────────────────────────────────


def run_gating(
    case: dict, calcs: dict[str, Calculation], rules: dict, proposed_sa: int
) -> list[GatingItem]:
    """Run every gating check and return the list."""
    items: list[GatingItem] = []
    items.append(check_income_documented(calcs["selected_income"], rules))
    items.append(
        check_min_income_for_sa(
            calcs["selected_income"], proposed_sa, rules, is_sbi_case=_is_sbi_case(case)
        )
    )
    items.append(check_residential_status(case, rules))
    items.append(check_kyc(case, rules))
    items.append(check_foir(calcs["foir"], rules, is_sbi_case=_is_sbi_case(case)))
    items.append(check_sa_within_hlv(calcs["max_sa_eligible"], proposed_sa))
    items.append(check_existing_cover_disclosure(case))
    # Medical gating only runs when the medical block is present.
    if case.get("medical"):
        items.append(check_medical_compliance(calcs))
        items.append(check_fraud_signal(calcs))
    return items
