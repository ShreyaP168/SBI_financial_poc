"""
engine/verdict.py — final UW verdict from gating + flags + reconciler.

Decision rules (deterministic):
  DECLINE             → confirmed fraud HIGH, OR an unresolvable/terminal
                        CRITICAL flag (see _RESOLVABLE_CRITICAL_PATTERNS),
                        OR medical UW terminal decision = Decline
  REFER               → ≥ 2 gating FAILs, OR ≥ 4 HIGH flags, OR proposed_sa
                        > hlv_base × 1.05 (counter-offer at max_sa_eligible
                        recommended instead of an outright Decline — per
                        explicit instruction 2026-06-26: never auto-Decline
                        purely for an SA/HLV mismatch when a reduced
                        counter-offer is viable)
  CONDITIONAL         → 1 gating FAIL, OR ≥ 1 HIGH flag, OR reconciler.stp_recommendation = STP_BLOCK
  STANDARD_ACCEPTANCE → no FAILs, no HIGH/CRITICAL flags, reconciler stp_ok

SBI financial-underwriting cases (case["sbi_bureau"] present) only ever
carry financial/IIB/documentation data — no case["medical"] block — so
every fraud_risk/medical_risk-gated branch above is scoped to
`not is_sbi_case` and never fires for them; medical UW is explicitly out
of scope for this product's case sheet. An SBI case instead resolves to
STANDARD_ISSUANCE / FURTHER_REQUIREMENT — see the `elif is_sbi_case:`
branch below.
"""

from __future__ import annotations
from typing import Any

from .calculator import _safe_int, _safe_float
from .types import Verdict, Flag, GatingItem, Calculation
from .routing import is_sbi_case as _is_sbi_case


def _flag_counts(flags: list[Flag]) -> dict[str, int]:
    counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for f in flags:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts


def _gating_counts(items: list[GatingItem]) -> dict[str, int]:
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "NA": 0}
    for it in items:
        counts[it.status] = counts.get(it.status, 0) + 1
    return counts


_RISK_RANK = {"LOW": 0, "MODERATE": 1, "HIGH": 2, "N/A": -1, "—": -1, "": -1}


def _risk_band(calc: Calculation | None) -> str:
    if calc is None:
        return "N/A"
    return str(calc.value or "N/A")


def _corroborated_income_floor(case: dict) -> int:
    """Reliable, independent corroboration of annual income for the
    income-divergence guard. Uses ONLY:
      - system-assessed income (policy_data.insured_income, the xlsx)
      - annualised SALARY-LIKE recurring bank credits

    Deliberately EXCLUDES self-declared CAF income (the figure UW exists to
    verify) and raw bank-credit totals (which include transfers / business
    turnover) — neither is income verification, and using them would
    down-grade every over-declared application.
    """
    floor = 0
    pd = case.get("policy_data") or {}
    xi = _safe_int(pd.get("insured_income"))
    if xi > 0:
        floor = max(floor, xi)
    ba = case.get("bank_analysis") or {}
    for r in ba.get("recurring_credits") or []:
        if r.get("is_salary_like"):
            amt = _safe_int(r.get("avg_amount"))
            if amt > 0:
                floor = max(floor, amt * 12)
    return floor


# Criteria excluded from financial_risk/verdict scoring for an SBI
# Financial Underwriting case — medical review is explicitly out of scope
# for this case-sheet type (see renderer.triage_sheet._NON_PROFILE_CRITERIA,
# which already hides these from the dashboard's own FAIL/WARN badge).
# medical_compliance in particular FAILs on essentially every financial-only
# case, since no such case ever has real medical test documents by design.
# Scoped to SBI cases only (case.sbi_bureau present) — never touches the
# generic full-medical-UW flow, where these checks are the real, intended
# gate.
_SBI_OUT_OF_SCOPE_CRITERIA = {"medical_compliance", "fraud_signal", "min_income_for_sa"}


def make_verdict(
    case: dict,
    calcs: dict[str, Calculation],
    gating: list[GatingItem],
    flags: list[Flag],
    proposed_sa: int,
    requirements_to_raise: list[dict] | None = None,
) -> Verdict:
    is_sbi_case = _is_sbi_case(case)
    exclude_criteria: set[str] = set(_SBI_OUT_OF_SCOPE_CRITERIA) if is_sbi_case else set()
    if is_sbi_case:
        # min_income_for_sa is a generic SA-tier income cross-check (see
        # engine.gating.check_min_income_for_sa's own docstring: "not an
        # SBI rule"). When the SBI-specific qualification gate — the
        # authoritative income floor for this product — already PASSED,
        # the generic cross-check's FAIL shouldn't independently block an
        # SBI verdict.
        sbi_qual = next((g for g in gating if g.criterion == "sbi_qualification"), None)
        if sbi_qual is not None and sbi_qual.status == "PASS":
            exclude_criteria.add("min_income_for_sa")
    scoped_gating = [g for g in gating if g.criterion not in exclude_criteria]

    fc = _flag_counts(flags)
    gc = _gating_counts(scoped_gating)
    recon = case.get("reconciliation") or {}
    stp = recon.get("stp_recommendation") or "STP_OK"

    hlv_base = _safe_int((calcs.get("hlv_base") or Calculation(name="hlv", value=0)).value)
    # Business insurance (Keyman / Partnership / EE): its insurable value is the
    # eligibility ceiling, superseding the individual income HLV for the
    # SA-vs-HLV decline rule and the cover-level classification.
    _biz = calcs.get("business_insurance_value")
    if _biz is not None and _safe_int(_biz.value) > 0:
        hlv_base = _safe_int(_biz.value)

    # Three risk dials (Phase 8b — overall = max of financial, medical, fraud)
    medical_risk = _risk_band(calcs.get("medical_risk"))
    fraud_risk = _risk_band(calcs.get("fraud_risk"))
    # Financial risk derived from gating + flag counts:
    if gc["FAIL"] >= 1 or fc["CRITICAL"] >= 1 or fc["HIGH"] >= 4:
        financial_risk = "HIGH"
    elif fc["HIGH"] >= 1 or fc["MEDIUM"] >= 3:
        financial_risk = "MODERATE"
    else:
        financial_risk = "LOW"

    overall_risk = max(
        (financial_risk, medical_risk, fraud_risk), key=lambda b: _RISK_RANK.get(b, -1)
    )

    # Material income-divergence guard. The HLV ceiling is built on the
    # VERIFIED earned income (selected_income). For cross-border / NRI filers
    # (the India ITR captures only part-year income), presumptive (44AD), or
    # passive-heavy returns, that figure understates true earning capacity.
    # When independent corroboration (system-assessed income / recurring
    # salary credits) implies an HLV that WOULD clear the proposed SA, we
    # REFER for manual income reconciliation instead of a hard DECLINE.
    _verified_income = _safe_int(
        (calcs.get("selected_income") or Calculation(name="x", value=0)).value
    )
    _corrob_income = _corroborated_income_floor(case)
    _income_diverges = (
        proposed_sa > 0
        and hlv_base > 0
        and _verified_income > 0
        and _corrob_income > _verified_income
        and int(hlv_base * (_corrob_income / _verified_income)) >= proposed_sa
    )

    label = "STANDARD_ACCEPTANCE"
    drivers: list[str] = []

    # A CRITICAL flag is "resolvable" (→ at most REFER, never an automatic
    # Decline) when it describes something the applicant/underwriter could
    # clear up with a document or explanation — an undisclosed prior
    # decline/cover, a missing-information gap, a conflict-of-interest flag
    # pending branch review. Reserve hard DECLINE for confirmed/terminal
    # findings (fraud — handled separately via fraud_risk below — or a
    # disallowed category). A Decline is permanently recorded against the
    # applicant in IIB on every future application; it must not be issued
    # for something that was never actually adjudicated.
    _RESOLVABLE_CRITICAL_PATTERNS = (
        "non-disclosure",
        "non disclosure",
        "undisclosed",
        "not disclosed",
        "pending clarification",
        "must be established",
        "requires clarification",
        "conflict of interest",
        "related-party",
        "related party",
        # Absent/missing documentation is resolvable by obtaining the
        # document — distinct from a CONFIRMED bad finding (e.g. an
        # actual abnormal lab result, or a confirmed cotinine mismatch,
        # which are NOT in this list and still hard-Decline).
        "document missing",
        "doc missing",
        "documents missing",
        "no medical examination",
        "medical examination document",
        "medical documents are present",
        "medical document",
        "absence of medical",
        "missing medical",
        # A single recent-year business loss (RECON_BUSINESS_LOSS_LATEST_YEAR)
        # is a confirmed fact, but it is not the same category as fraud or a
        # disallowed-category finding — it calls for an explanation of the
        # loss and corroborating bank statements, same as any other
        # income-quality concern. The income-averaging methodology already
        # reflects the loss in the eligibility ceiling (a lower HLV), so a
        # separate hard-Decline on top of that double-counts the same risk
        # signal.
        "business loss",
        # An income DISCREPANCY (declared vs verified, no hard evidence of
        # deliberate misrepresentation) is the same finding already handled
        # as a non-blocking WARN by the declared-vs-verified-income gating
        # item — it should not ALSO separately hard-Decline via a
        # differently-worded reconciler flag for the identical underlying
        # fact. Reserve DECLINE for a CONFIRMED lie (e.g. a fabricated
        # document), not an unverified figure that disagrees with another
        # unverified figure.
        "income source mismatch",
        "income mismatch",
    )
    critical_flags = [f for f in flags if f.severity == "CRITICAL"]
    unresolvable_critical = [
        f
        for f in critical_flags
        if not any(
            p in (f.title + " " + f.description).lower() for p in _RESOLVABLE_CRITICAL_PATTERNS
        )
    ]

    # A qualification/education category on the product's own disallowed
    # list (as opposed to merely falling short of an income floor for an
    # otherwise-acceptable category) is a terminal product-ineligibility
    # fact — no document the applicant could produce changes which
    # category they're in, so this isn't a resolvable REFER item the way
    # an income-floor shortfall is.
    sbi_qual_gate = next((g for g in gating if g.criterion == "sbi_qualification"), None)
    # Covers both the flat disallowed-qualification list (e.g. "Below
    # SSC") and an occupation-specific qualification floor (e.g.
    # Agriculturist requiring Graduate+) — both are fixed facts about who
    # the applicant is, not a documentation gap. "not an acceptable
    # combination" is engine.sbi_financial.check_qualification_gate's own
    # wording for the occupation-specific case.
    _qual_ev = (sbi_qual_gate.evidence or "").lower() if sbi_qual_gate else ""
    disallowed_qualification = (
        sbi_qual_gate is not None
        and sbi_qual_gate.status == "FAIL"
        and ("disallowed list" in _qual_ev or "not an acceptable combination" in _qual_ev)
    )

    # Same terminal-fixed-fact principle as disallowed_qualification above,
    # applied to engine.sbi_financial.check_occupation_income_proof's own
    # disallowed_petty_business_occupations list (config:
    # sbi_smart_shield_rules.occupation_income_proof_gate — Pan shop,
    # Rickshaw driver, Scrap merchant, Kirana shop/store, Small merchant,
    # etc.) — being in a disallowed occupation category is not a
    # documentation gap any requirement can resolve, the same reasoning as
    # the qualification branch.
    occ_gate = next((g for g in gating if g.criterion == "sbi_occupation_income_proof"), None)
    _occ_ev = (occ_gate.evidence or "").lower() if occ_gate else ""
    disallowed_occupation = (
        occ_gate is not None
        and occ_gate.status == "FAIL"
        and "disallowed petty-business profile" in _occ_ev
    )

    # Same terminal-vs-resolvable principle, applied to the DRC High-Risk
    # ₹10L income floor: when income is still undocumented (declared-only
    # or bank-credits-only), REFER with "obtain standard income proof" is
    # correct — more documentation could still change the outcome. But
    # once income_documented is PASS (ITR/salary-slip/Form-16/CA
    # computation already on file) and the VERIFIED figure itself falls
    # short of the ₹10L floor, no further document changes that number —
    # it's confirmed and terminal, the same category as a disallowed
    # qualification.
    drc_gate = next((g for g in gating if g.criterion == "sbi_drc_income_floor"), None)
    income_doc_gate = next((g for g in gating if g.criterion == "income_documented"), None)
    drc_verified_insufficient = (
        drc_gate is not None
        and drc_gate.status == "FAIL"
        and income_doc_gate is not None
        and income_doc_gate.status == "PASS"
    )

    failed_gates = [g for g in scoped_gating if g.status == "FAIL"]
    unresolved_mandatory = [
        g
        for g in scoped_gating
        if g.status == "WARN"
        and g.criterion
        in {
            "sbi_qualification",
            "sbi_occupation_income_proof",
            "sbi_medical_grid",
            "sbi_piwc_requirement",
            "kyc",
            "income_documented",
        }
    ]
    terminal_gates = [
        g
        for g in failed_gates
        if g.criterion in {"sbi_student_agriculturist_riders", "sbi_keyman_partnership_ee_age_cap"}
    ]
    processing_failed = bool(case.get("processing_errors")) or any(
        g.criterion == "mandatory_processing" for g in failed_gates
    )
    # Hard DECLINE rules first
    #
    # fraud_risk / medical_risk are generic-engine concepts
    # (engine.medical.compute_fraud_risk / compute_medical_risk) derived
    # entirely from case["medical"] — urine cotinine vs declared-smoker
    # mismatch, MER/VMER contradictions, lab abnormalities, BMI, active
    # conditions. SBI financial-underwriting case.json carries no
    # "medical" block at all (out of scope for this product's case
    # sheet — see _SBI_OUT_OF_SCOPE_CRITERIA above), so both calcs
    # always default to LOW for every SBI case and these branches could
    # never fire on real data. Scoped to `not is_sbi_case` below so
    # that's structural (SBI's decision surface is financial/IIB/
    # documentation only) rather than an accident of absent input data.
    if processing_failed:
        label = "INCOMPLETE"
        drivers.append("Mandatory processing incomplete; issuance blocked pending manual review.")
    elif terminal_gates:
        label = "DECLINE"
        drivers.extend(g.evidence or g.label for g in terminal_gates)
    elif (not is_sbi_case) and fraud_risk == "HIGH":
        label = "DECLINE"
        drivers.append("Fraud risk HIGH — cotinine mismatch or material non-disclosure.")
    elif disallowed_qualification:
        label = "DECLINE"
        drivers.append(
            f"Qualification is in the product's disallowed list (terminal — not resolvable by documentation): {sbi_qual_gate.evidence}"
        )
    elif disallowed_occupation:
        label = "DECLINE"
        drivers.append(
            f"Occupation is in the product's disallowed petty-business list (terminal — not resolvable by documentation): {occ_gate.evidence}"
        )
    elif drc_verified_insufficient:
        label = "DECLINE"
        drivers.append(
            f"Verified income confirmed below the DRC High-Risk floor (terminal — not resolvable by documentation): {drc_gate.evidence}"
        )
    elif unresolvable_critical:
        label = "DECLINE"
        drivers.append(
            f"{len(unresolvable_critical)} CRITICAL flag(s) raised by engine (terminal — not resolvable by documentation)."
        )
    elif (
        (not is_sbi_case)
        and proposed_sa > 0
        and hlv_base > 0
        and proposed_sa > int(hlv_base * 1.05)
        and not _income_diverges
    ):
        # Never auto-Decline purely because the proposed SA exceeds the
        # HLV ceiling when a reduced counter-offer is viable — that's a
        # routine, resolvable underwriting action (offer the eligible SA
        # instead), not a confirmed/terminal adverse finding. Same
        # resolvable-vs-terminal principle as the CRITICAL-flag branch
        # above. Reserve DECLINE for confirmed fraud / unresolvable-
        # CRITICAL findings (those branches already run first).
        #
        # Scoped away from SBI cases: an SA/HLV mismatch there is resolved
        # by raising a requirement (SA reduction or surrogate income
        # proof), the same as every other SBI resolvable finding, rather
        # than skipping straight to REFER. SBI cases fall through to the
        # `elif is_sbi_case` branch below instead, which reads
        # requirements_to_raise (Rule 4 in engine.requirements already
        # raises the appropriate income-proof requirement for this exact
        # scenario).
        label = "REFER"
        _max_sa = _safe_int((calcs.get("max_sa_eligible") or Calculation(name="x", value=0)).value)
        if _max_sa > 0:
            drivers.append(
                f"Proposed SA ₹{proposed_sa:,} exceeds HLV-base × 1.05 "
                f"(₹{int(hlv_base * 1.05):,}) — counter-offer at the eligible "
                f"SA of ₹{_max_sa:,} is viable; refer to Manual UW with this "
                f"requirement rather than declining outright."
            )
        else:
            drivers.append(
                f"Proposed SA ₹{proposed_sa:,} exceeds HLV-base × 1.05 "
                f"(₹{int(hlv_base * 1.05):,}) and no positive eligible SA could "
                f"be computed — refer to Manual UW for income/cover "
                f"clarification before a reduced offer can be quantified."
            )
    elif (
        (not is_sbi_case)
        and medical_risk == "HIGH"
        and gc["FAIL"] >= 1
        and str((calcs.get("medical_uw_terminal") or Calculation(name="x", value="")).value)
        .strip()
        .lower()
        == "decline"
    ):
        # Same resolvable-vs-terminal principle as the CRITICAL-flag rule
        # above, applied on the medical axis: medical_risk == HIGH can mean
        # either "investigated and confirmed bad" (terminal) or simply "no
        # medical investigation has been done yet" (unknown, not bad) — the
        # medical UW terminal decision (Medically Std / Rateable / Additional
        # Test / Repeat Test / Postpone / Decline) already distinguishes
        # these explicitly. Only auto-Decline when THAT axis itself says
        # Decline; "Postpone" / "Additional Test" defer to REFER below so
        # missing-paperwork cases aren't permanently recorded in IIB.
        label = "DECLINE"
        drivers.append(
            "Medical risk HIGH combined with gating failure; medical UW terminal decision is Decline."
        )
    elif _income_diverges and proposed_sa > int(hlv_base * 1.05):
        label = "REFER"
        _corrob_hlv = int(hlv_base * (_corrob_income / _verified_income))
        drivers.append(
            f"Proposed SA ₹{proposed_sa:,} exceeds HLV on verified income "
            f"₹{_verified_income:,}, but corroborating income ₹{_corrob_income:,} "
            f"(system-assessed / recurring salary credits) implies HLV "
            f"₹{_corrob_hlv:,} ≥ proposed SA — refer for manual income "
            f"reconciliation (possible NRI / part-year / presumptive filing)."
        )
    elif (not is_sbi_case) and (gc["FAIL"] >= 2 or medical_risk == "HIGH"):
        label = "REFER"
        if gc["FAIL"] >= 2:
            drivers.append(f"{gc['FAIL']} gating FAILs.")
        if medical_risk == "HIGH":
            drivers.append("Medical risk HIGH — refer to medical UW.")
    elif (not is_sbi_case) and fc["HIGH"] >= 4:
        label = "REFER"
        drivers.append(f"{fc['HIGH']} HIGH-severity flags.")
    elif is_sbi_case:
        # The two generic REFER rules just above (≥2 gating FAILs; ≥4 HIGH
        # flags), and the generic FAIL/HIGH-flag thresholds the CONDITIONAL
        # branch below still uses, are scoped away from SBI cases: SBI's
        # own decision vocabulary has no "CONDITIONAL" category — every
        # non-REFER, non-DECLINE case is either CASE OK TO ACCEPT
        # (displayed "STANDARD ISSUANCE") or FURTHER REQUIREMENT, and that
        # split does not track a generic FAIL/flag count. A gating FAIL
        # here is resolvable by a requirement (surrogate income proof, SA
        # reduction, etc.) rather than automatically pushing to REFER/
        # CONDITIONAL.
        #
        # The "Requirements to Raise" box and the "Further Requirement"
        # verdict share the same name for a reason: ANY open item in that
        # box — not just ones flagged critical — means the case isn't yet
        # a clean Standard Issuance.
        if requirements_to_raise:
            label = "FURTHER_REQUIREMENT"
            drivers.append(
                "Requirement(s) outstanding: "
                + "; ".join(r.get("text", "") for r in requirements_to_raise if isinstance(r, dict))
            )
        elif failed_gates or unresolved_mandatory:
            label = "REFER"
            drivers.extend(
                f"Eligibility unresolved: {g.label}: {g.evidence}"
                for g in failed_gates + unresolved_mandatory
            )
        elif stp == "STP_BLOCK":
            label = "REFER"
            drivers.append("Reconciliation requires manual review.")
        else:
            label = "STANDARD_ISSUANCE"
            drivers.append("No requirements outstanding.")
    elif (
        gc["FAIL"] == 1
        or fc["HIGH"] >= 1
        or stp == "STP_BLOCK"
        or medical_risk == "MODERATE"
        or fraud_risk == "MODERATE"
    ):
        label = "CONDITIONAL"
        if gc["FAIL"] == 1:
            drivers.append("1 gating FAIL.")
        if fc["HIGH"] >= 1:
            drivers.append(f"{fc['HIGH']} HIGH-severity flag(s).")
        if stp == "STP_BLOCK":
            drivers.append("Reconciler recommends STP_BLOCK.")
        if medical_risk == "MODERATE":
            drivers.append("Medical risk MODERATE.")
        if fraud_risk == "MODERATE":
            drivers.append("Fraud risk MODERATE — borderline cotinine or contradictions.")
    else:
        label = "STANDARD_ACCEPTANCE"
        drivers.append("No FAILs / HIGH / CRITICAL; medical + fraud LOW.")

    # Confidence: weighted by reconciler conf and gating PASS share.
    recon_conf = _safe_float(recon.get("overall_confidence") or 0.0)
    pass_share = gc["PASS"] / max(1, sum(gc.values()))
    if label == "INCOMPLETE":
        conf = 0.0
    elif label in ("STANDARD_ACCEPTANCE", "STANDARD_ISSUANCE"):
        conf = round(0.5 * recon_conf + 0.5 * pass_share, 2)
    elif label in ("CONDITIONAL", "FURTHER_REQUIREMENT"):
        conf = round(0.4 * recon_conf + 0.4 * pass_share + 0.2 * 0.7, 2)
    elif label == "REFER":
        conf = round(0.3 * recon_conf + 0.3 * pass_share + 0.4 * 0.6, 2)
    else:  # DECLINE
        conf = round(0.6 + 0.4 * (1 - recon_conf), 2)
    conf = max(0.0, min(1.0, conf))

    rationale_parts: list[str] = []
    rationale_parts.append(
        f"Risk dials — Overall: {overall_risk}, Financial: {financial_risk}, "
        f"Medical: {medical_risk}, Fraud: {fraud_risk}."
    )
    rationale_parts.append(f"Gating: {gc['PASS']} PASS, {gc['WARN']} WARN, {gc['FAIL']} FAIL.")
    rationale_parts.append(
        f"Flags: {fc['CRITICAL']} CRITICAL, {fc['HIGH']} HIGH, {fc['MEDIUM']} MEDIUM, "
        f"{fc['LOW']} LOW."
    )
    rationale_parts.append(f"Reconciler: stp={stp}, conf={recon_conf:.2f}.")
    rationale_parts.append("Decision drivers: " + " | ".join(drivers))

    # Final Decision matrix cell — Datamatics LAYOUT rows 71-77.
    # One of 14 outcomes:
    #   {Std Smoker, Std NSR, Sub-Std-RU Smoker, Sub-Std-RU NSR} ×
    #   {full / restricted / stretched}  + Postpone + Decline
    final_cell = _classify_final_decision_cell(
        label=label,
        case=case,
        calcs=calcs,
        proposed_sa=proposed_sa,
        hlv_base=hlv_base,
    )

    return Verdict(
        label=label,
        confidence=conf,
        rationale=" ".join(rationale_parts),
        function="underwriting_platform.engine.verdict.make_verdict",
        decision_drivers=drivers
        + [
            f"financial_risk={financial_risk}",
            f"medical_risk={medical_risk}",
            f"fraud_risk={fraud_risk}",
            f"overall_risk={overall_risk}",
            f"final_decision_cell={final_cell}",
        ],
        final_decision_cell=final_cell,
    )


# ---------------------------------------------------------------------------
# 14-cell Final Decision matrix — Datamatics LAYOUT rows 71-77
# ---------------------------------------------------------------------------


def _classify_final_decision_cell(
    *, label: str, case: dict, calcs: dict, proposed_sa: int, hlv_base: int
) -> str:
    """Map the engine's 6-class verdict to one of the 14 Datamatics
    Final Decision cells.

    Cells:
      Std Smoker Rates × {full / restricted / stretched}
      Std NSR        × {full / restricted / stretched}
      Sub-Std Rated Up Smoker × {full / restricted / stretched}
      Sub-Std Rated Up NSR    × {full / restricted / stretched}
      Postpone
      Decline

    Cover-level mapping (cover_class):
      full        — full applied SA grantable
      restricted  — max_sa_eligible < proposed_sa (HLV-remaining / per-life cap binds)
      stretched   — proposed_sa in the 1.00–1.05×HLV tolerance band

    Smoker resolution: medical.habits_declared.smoker / cotinine outcome.
    Sub-Std (rated-up): loading factor > 0 or rate-up flag.
    """
    if label == "POSTPONE":
        return "Postpone"
    if label == "DECLINE":
        return "Decline"
    if label in ("REFER", "FURTHER_REQUIREMENT", "INCOMPLETE", "ERROR"):
        return "Refer for Manual UW"
    if label not in (
        "STANDARD_ACCEPTANCE",
        "STANDARD_ISSUANCE",
        "CONDITIONAL",
        "FURTHER_REQUIREMENT",
        "REFER",
    ):
        return "Refer for Manual UW"

    # Cover-level classification (Datamatics LAYOUT: full / restricted /
    # stretched cover):
    #   • restricted — the insurer must OFFER LESS than applied because
    #     HLV-remaining (after existing cover) or the per-life cap binds below
    #     the proposed SA  →  max_sa_eligible < proposed_sa.
    #   • stretched  — proposed SA sits in the 1.00–1.05×HLV tolerance band we
    #     still grant (top of grid).
    #   • full       — the full applied SA is grantable.
    # A customer *under-buying* (proposed well below HLV) is "full", not
    # "restricted" — restricted means the insurer offers less than applied,
    # not that the applicant applied for less than they could have gotten.
    max_sa = _safe_int((calcs.get("max_sa_eligible") or Calculation(name="x", value=0)).value)
    if proposed_sa <= 0 or hlv_base <= 0:
        cover_class = "full"
    elif proposed_sa > int(hlv_base * 1.00):
        cover_class = "stretched"
    elif max_sa > 0 and proposed_sa > max_sa:
        cover_class = "restricted"
    else:
        cover_class = "full"

    # Smoker / NSR resolution
    med = case.get("medical") or {}
    habits = med.get("habits_declared") or {}
    fraud = med.get("fraud_signals") or {}
    declared_smoker = str(habits.get("smoker") or "").strip().lower()
    cotinine_pos = bool(fraud.get("cotinine_smoker_mismatch")) or (
        _safe_float(fraud.get("urine_cotinine_ng_ml"))
        >= _safe_float(fraud.get("cotinine_threshold") or 10)
    )
    is_smoker = declared_smoker in ("yes", "smoker", "true") or cotinine_pos
    rate_class_smoker = "Smoker" if is_smoker else "NSR"

    # Sub-Std (rated up) resolution
    loading_calc = calcs.get("loading")
    loading_emr = 0
    if loading_calc is not None:
        try:
            loading_emr = int((loading_calc.inputs or {}).get("emr") or 0)
        except (TypeError, ValueError):
            loading_emr = 0
    # Also consider medical risk MODERATE as a rate-up trigger for CONDITIONAL
    med_risk_band = (calcs.get("medical_risk") or {}).value if calcs.get("medical_risk") else None
    med_risk_band = (med_risk_band or "").upper()
    is_rated_up = loading_emr > 0 or (label == "CONDITIONAL" and med_risk_band == "MODERATE")

    rate_class = (
        f"Sub-Std Rated Up {rate_class_smoker}" if is_rated_up else f"Std {rate_class_smoker}"
    )
    return f"{rate_class} · {cover_class}"
