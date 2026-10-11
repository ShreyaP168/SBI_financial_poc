"""
engine/requirements.py — "Requirements to Raise" business rules for the
SBI Smart Shield Plus triage case sheet.

Source: client review "Case sheet format.docx" (2026-09-30). The 8 rules,
verbatim, and where each is handled:

  1. Adverse IIB policy status (Declined/Postponed/Rated-up) for an
     insurer OTHER than SBI Life ("SBIL") → raise a written explanation +
     documentary evidence requirement. (this module)
  2. The same adverse status but WITH SBI Life itself → highlighted in
     the risk summary only; no requirement raised. (this module — simply
     omits SBI Life's own adverse records from rule 1's output; the
     highlight itself is renderer/triage_sheet.py's risk-summary panel)
  3. Additional Source of Funds if premium payment capacity is not
     justified by verified income. (this module)
  4. Additional Income Proof if verified income < declared income AND
     the income-floor/qualification gate fails. (this module)
  5. BHB case: spouse qualification below the acceptable range → raise a
     requirement — distinct from spouse qualification simply not being
     extracted/on file. (this module, keyed off
     engine.sbi_financial.check_bhb_eligibility's evidence text)
  6. Reason for non-acceptance of a provided document must be displayed.
     Satisfied by the existing architecture: every non-PASS gating item
     and every flag already carries a populated evidence/description
     string that the renderer displays alongside it — there is no
     separate "document rejected" extraction field anywhere upstream to
     wire up.
  7. Any deviation from underwriting guidelines must be highlighted.
     Satisfied by renderer/triage_sheet.py's risk-summary panel, which is
     generically built from every FAIL/WARN gating item (known SBI
     criteria get a tailored row; anything else falls back to a generic
     one) — not specific to this module.
  8. Avoid raw technical jargon ("floors" etc.) in this (client-facing)
     box — every string below uses plain language instead.

A 9th rule, added 2026-10-09 and NOT from Case sheet format.docx: missing
Photo ID / OVD (no legible Aadhaar, Passport, or Driving Licence on file).
Unlike Insta-PIWC below, this isn't a routine operational step — it's a
core identity-verification gap SBI's own requirements letter (R02/R06)
already asks the applicant to close, and a case can't be a clean Standard
Issuance while it's open. See the Photo-ID block below for the full
justification and the case that surfaced the gap.

Insta-PIWC/Video MER confirmation is deliberately NOT raised here (and
never was a risk-summary row either — see
renderer.triage_sheet._NON_PROFILE_CRITERIA). It's a real SBI pre-
issuance requirement (Underwriting Guidelines for Smart Shield Plus
V02.docx, PIWC row) but it isn't one of the 8 rules above, and it's an
operational/medical verification step, not a financial-underwriting
finding, on a sheet titled "Financial Underwriting Case Sheet."

This module is additive and SBI-bureau-specific (same guard as the rest
of engine/sbi_financial.py).
"""

from __future__ import annotations

import re as _re

from .calculator import _safe_int
from .types import GatingItem

# Scoped exactly to rule 1/2's own wording ("Declined, postponed,
# ratedup") — deliberately does NOT include "withdraw"/"not taken up"
# (customer-initiated, not an insurer adverse decision), even though the
# pre-existing engine.sbi_financial.check_material_non_disclosure's own
# bad_terms tuple does, for a differently-scoped gate.
_ADVERSE_TERMS = ("declin", "postpone", "rated up", "rate up")

# Undocumented premium-to-income heuristic, strictly advisory. It is not
# an eligibility gate and must never create an issuance-blocking requirement.
_PREMIUM_CAPACITY_MAX_PCT = 15.0


def _is_adverse(policy: dict) -> bool:
    status = (policy.get("status") or "").lower()
    uw = (policy.get("uw_decision") or "").lower()
    return any(t in status for t in _ADVERSE_TERMS) or any(t in uw for t in _ADVERSE_TERMS)


def _is_sbi_life(policy: dict) -> bool:
    return "sbi life" in (policy.get("insurer") or "").lower()


def _inr(n) -> str:
    """Indian digit grouping (lakh/crore), e.g. 809007 -> 'Rs. 8,09,007' —
    matches the client's reference case-sheet number format."""
    v = _safe_int(n) or 0
    sign = "-" if v < 0 else ""
    s = str(abs(v))
    if len(s) <= 3:
        grouped = s
    else:
        last3, rest = s[-3:], s[:-3]
        parts = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        grouped = ",".join(parts) + "," + last3
    return f"Rs. {sign}{grouped}"


def _cv(calcs: dict, name: str):
    c = (calcs or {}).get(name)
    return getattr(c, "value", None) if c is not None else None


def _gating_by_criterion(gating: list[GatingItem], *criteria) -> GatingItem | None:
    for g in gating or []:
        if g.criterion in criteria:
            return g
    return None


def compute_requirements_to_raise(case: dict, calcs: dict, gating: list[GatingItem]) -> list[dict]:
    """Returns an ordered list of {"text": str, "critical": bool} items for
    the case sheet's "Requirements to Raise" box. "critical" marks an item
    that blocks any UW decision until resolved."""
    items: list[dict] = []
    for g in gating:
        if (
            g.criterion in ("sbi_bureau_evidence", "sbi_core_evidence", "sbi_hlv_evidence")
            and g.status == "FAIL"
        ):
            items.append({"text": g.evidence, "critical": True, "criterion": g.criterion})
    bureau = case.get("sbi_bureau") or {}
    policies = bureau.get("iib_policies") or []
    iib_gate = _gating_by_criterion(gating, "sbi_iib_gate")
    unidentified_iib = bool(
        iib_gate
        and iib_gate.status == "WARN"
        and "insurer identities" in (iib_gate.evidence or "").lower()
    )

    # ── Rules 1 & 2: adverse bureau policy status ──────────────────────
    adverse_other = [p for p in policies if _is_adverse(p) and not _is_sbi_life(p)]
    # adverse_sbi (adverse + SBI Life) is deliberately NOT turned into a
    # requirement here — rule 2: "may just be highlighted, requirement
    # not to be raised for the same." It still surfaces via the gating /
    # flags-driven risk summary (sbi_material_non_disclosure, sbi_iib_gate).
    for p in adverse_other:
        date = p.get("uw_decision_date") or ""
        items.append(
            {
                "text": (
                    f"Decline/postponement letter + explanation — {p.get('insurer') or 'other insurer'} "
                    f"{_inr(p.get('sum_assured_inr'))} {p.get('product_type') or 'policy'} "
                    f"({p.get('status')}{', ' + date if date else ''})."
                ),
                "critical": True,
            }
        )

    # ── Pending-underwriting bureau records at other insurers ──────────
    # Distinct from rules 1/2 (adverse status only) — "Proposal Pending
    # for Underwriting" is neither adverse nor an in-force disclosure
    # gap, so neither existing rule surfaces it on its own.
    pending_other = [
        p for p in policies if "pending" in (p.get("status") or "").lower() and not _is_sbi_life(p)
    ]
    if pending_other:
        nos = sorted({p.get("policy_or_wf_no") for p in pending_other if p.get("policy_or_wf_no")})
        policy_txt = f" ({', '.join(nos)})" if nos else ""
        items.append(
            {
                "text": (
                    f"Current status of Proposal Pending polic{'y' if len(pending_other) == 1 else 'ies'}"
                    f"{policy_txt} at another insurer — could still turn out adverse."
                ),
                # critical=True: an unconfirmed pending proposal at another
                # insurer could still turn out adverse, materially changing
                # the picture — not routine pre-issuance paperwork.
                # engine.verdict.make_verdict's SBI STANDARD_ISSUANCE/
                # FURTHER_REQUIREMENT split keys directly off this flag.
                "critical": True,
            }
        )

    # ── Photo-ID / OVD missing (KYC) ────────────────────────────────────
    # Not one of Case sheet format.docx's 8 verbatim rules, but grounded
    # directly in SBI Life's own requirements letter present in the case
    # document set (mailbody R02/R06: requests Aadhaar front-and-back, or
    # Passport, or Driving Licence, as Photo Identity + Residence proof,
    # and a PAN Card as a separate item) and in engine.gating.check_kyc /
    # sbi_financial._upgrade_kyc_evidence_with_specifics's own finding of
    # which documents are on file. Policy issuance is genuinely blocked
    # without these — unlike Insta-PIWC (a routine operational step,
    # deliberately excluded above), a case with zero legible photo ID or
    # PAN across every submitted document cannot be called a clean
    # Standard Issuance. Scoped tightly to the core OVD/PAN gap rather
    # than every minor KYC nit (e.g. CKYC number alone missing doesn't
    # trigger this).
    if unidentified_iib:
        confirmation = "Confirm insurer identities for masked IIB records before applying the SBI exposure band."
        if pending_other:
            items[-1]["text"] += " " + confirmation
        else:
            items.append({"text": confirmation, "critical": True, "criterion": "sbi_iib_gate"})

    kyc_gate = _gating_by_criterion(gating, "kyc")
    kyc_ev = (case.get("compliance") or {}).get("kyc_evidence") or {}
    if kyc_gate is not None and kyc_gate.status in ("WARN", "FAIL"):
        photo_id_present = any(
            kyc_ev.get(k)
            for k in ("aadhaar_present", "passport_present", "driving_licence", "voter_id_present")
        )
        pan_present = bool(kyc_ev.get("pan_present"))
        missing = []
        if not photo_id_present:
            missing.append("Photo ID (Aadhaar, Passport, or Driving Licence)")
        if not pan_present:
            missing.append("PAN Card")
        if missing:
            items.append(
                {
                    "text": f"{' and '.join(missing)} not on file; required before issuance.",
                    "critical": True,
                }
            )

    # ── Proof of business missing (Business/Self-employed) ─────────────
    # Not one of Case sheet format.docx's original 8 rules, but grounded
    # directly in V02.docx: "Standard Income Proof along with Proof of
    # business mandatory" (Business >₹30L SA, Self-employed any SA) — see
    # engine.sbi_financial.check_occupation_income_proof's own docstring.
    # Scoped specifically to the "proof of business" phrase in that
    # gate's evidence (not the whole WARN) so this never duplicates the
    # separate, pre-existing "Standard income proof" item below (sourced
    # from income_documented's own FAIL, a different underlying rule)
    # when a case is missing income proof only.
    occ_proof_gate = _gating_by_criterion(gating, "sbi_occupation_income_proof")
    business_proof = (case.get("compliance") or {}).get("business_proof_evidence") or {}
    confirmed_unavailable = business_proof.get(
        "availability"
    ) == "customer_confirmed_unavailable" and bool(business_proof.get("source_document"))
    if (
        occ_proof_gate is not None
        and occ_proof_gate.status == "WARN"
        and "proof of business" in (occ_proof_gate.evidence or "").lower()
        and not confirmed_unavailable
    ):
        items.append(
            {
                "text": "Proof of business (e.g. GST/MSME/trade-licence registration) not received.",
                "critical": False,
            }
        )

    # ── Exact nature of business not on file (Business/Self-employed) ──
    # V02.docx, Business row: "For business profile, exact nature of
    # business must be mentioned without which proposal should not be
    # processed." A distinct ask from the documentary proof-of-business
    # item above — when a customer has already confirmed no GST/MSME/
    # trade-licence registration exists, the next best verification is
    # establishing what the business actually is and its name, not
    # repeating the same documentary request.
    _req_app = case.get("applicant") or {}
    _req_occ = (_req_app.get("occupation") or "").strip().lower()
    is_business_occ = bool(_req_occ) and any(
        k in _req_occ for k in ("business", "self-employed", "self employed")
    )
    nature = (_req_app.get("business_nature") or "").strip()
    business_name = (_req_app.get("employer_name") or "").strip()
    coarse = {"finance", "business", "self employed", "self-employed", "own business", "promoter"}
    # An industry or role alone is not an exact business description/name.
    broad_nature = nature.split("(", 1)[0].strip().lower() in coarse
    broad_name = business_name.lower() in coarse
    if is_business_occ and (not nature or (broad_nature and (not business_name or broad_name))):
        items.append(
            {
                "text": (
                    "Exact nature of business, with business/employer name, not received. "
                    "Customer confirms documentary proof is unavailable; UW approval of alternative evidence is required."
                    if confirmed_unavailable
                    else "Exact nature of business, with business/employer name, not received."
                ),
                "critical": False,
            }
        )

    # ── Mandatory medical tests (Smart Shield Plus medical grid) ───────
    # Not one of Case sheet format.docx's original 8 rules, but grounded
    # directly in V02.docx's own "Medical | Medically underwritten. Refer
    # attached medical grid" row — a real, named, mandatory requirement,
    # not an optional/operational step like Insta-PIWC above. This
    # product's case.json carries no populated "medical" block at all
    # (see engine.sbi_financial.check_medical_requirement's own
    # docstring), so there is no way to confirm completion; the open
    # requirement is raised rather than silently assumed either way. Kept
    # short and self-contained here — the full test list is in the
    # risk-summary row only (render_triage_sheet draws this Requirements
    # box BEFORE the risk summary, so a "see above" pointer here would
    # point the wrong direction).
    if (
        confirmed_unavailable
        and occ_proof_gate
        and occ_proof_gate.status == "WARN"
        and not any("Customer confirms documentary proof" in item["text"] for item in items)
    ):
        items.append(
            {
                "text": "Customer confirms proof of business is unavailable; obtain UW approval of alternative business evidence.",
                "critical": True,
            }
        )

    medical_gate = _gating_by_criterion(gating, "sbi_medical_grid")
    if medical_gate is not None and medical_gate.status == "WARN":
        items.append(
            {
                "text": "Mandatory medical tests not yet completed, required for this applicant's age and sum assured.",
                "critical": True,
            }
        )

    app = case.get("applicant") or {}
    verified_income = _safe_int(_cv(calcs, "selected_income"))
    declared_income = _safe_int(app.get("declared_annual_income"))

    # ── Existing-cover re-declaration (undisclosed bureau cover) ──────
    cover_gate = _gating_by_criterion(gating, "existing_cover_disclosed")
    # existing_cover_disclosed is WARN for high/medium-severity undisclosed
    # cover and FAIL for critical severity (engine.gating.
    # check_existing_cover_disclosure) — both need the re-declaration
    # requirement.
    # Per the IIB source email (Rakesh Shetty, SBI Life, 2026-06-18 —
    # the complete and only source for the IIB rules): "we seek details
    # from the customer if his/her existing policies with other companies
    # fall under medical decision like decline/postponed/medically
    # nonstandard." That's the entire stated scope for chasing the
    # customer on an existing policy — adverse-status ones. A simply
    # undeclared but healthy IN-FORCE policy isn't covered by that
    # sentence. Suppress the requirement when ALL of: no policy (IIB
    # record) is adverse, AND the case's own financial eligibility is
    # independently met (sbi_qualification PASS, sa_within_hlv not FAIL)
    # even with the undeclared cover folded into the math — i.e. nothing
    # about the non-disclosure actually changes the outcome.
    # Requires actual IIB policy records to evaluate "non-adverse" against
    # — a bank-statement/reconciler-detected signal (no IIB data at all)
    # has no confirmed policy status to check, so it must never be
    # suppressed this way: "unknown, suspected undisclosed policy" is not
    # the same as "confirmed non-adverse policy."
    _any_adverse_policy = any(_is_adverse(p) for p in policies)
    _sq_for_cover = _gating_by_criterion(gating, "sbi_qualification")
    _sa_hlv_for_cover = _gating_by_criterion(gating, "sa_within_hlv")
    _eligibility_independently_met = (
        _sq_for_cover is not None
        and _sq_for_cover.status == "PASS"
        and (_sa_hlv_for_cover is None or _sa_hlv_for_cover.status != "FAIL")
    )
    _cover_requirement_needed = not (
        bool(policies) and not _any_adverse_policy and _eligibility_independently_met
    )
    if (
        cover_gate is not None
        and cover_gate.status in ("WARN", "FAIL")
        and _cover_requirement_needed
    ):
        in_force_sa = sum(
            _safe_int(p.get("sum_assured_inr"))
            for p in policies
            if any(
                t in (p.get("status") or "").lower()
                for t in ("inforce", "in force", "paid-up", "paid up")
            )
        )
        extras = []
        if any(
            "freelook" in (p.get("status") or "").lower()
            or "cooling off" in (p.get("status") or "").lower()
            for p in policies
        ):
            extras.append("freelook-cancelled")
        if any("surrender" in (p.get("status") or "").lower() for p in policies):
            extras.append("surrendered")
        extra_txt = (" + " + " + ".join(extras) + " policies") if extras else ""
        if in_force_sa > 0:
            items.append(
                {
                    "text": (
                        f"Corrected existing-cover re-declaration ({_inr(in_force_sa)} "
                        f"in force{extra_txt})."
                    ),
                    "critical": False,
                    "criterion": "existing_cover_disclosed",
                }
            )
        else:
            # The WARN/FAIL didn't come from an IIB bureau record with a
            # known sum assured — e.g. the reconciler's bank-statement-
            # detected recurring-debit signal instead (engine.gating.
            # check_existing_cover_disclosure can fire from either source).
            # Citing "(Rs. 0 in force)" in that case is actively
            # misleading — it reads as "nothing found" when the opposite
            # triggered this requirement. Use the gate's own evidence
            # instead.
            items.append(
                {
                    "text": "Existing-cover re-declaration required — bank statements suggest an undeclared existing policy.",
                    "critical": False,
                    "criterion": "existing_cover_disclosed",
                }
            )

    # ── Rule 4: additional income proof ────────────────────────────────
    # Prefer the SBI-specific qualification floor over the generic SA-tier
    # cross-check (min_income_for_sa) — the generic gate's own evidence is
    # explicitly labelled "not an SBI-specific rule" and can carry a
    # different, non-authoritative number. NOTE: this only decides whether
    # EITHER gate genuinely fails (the boolean cover_not_justified) — it
    # must check both gates' own status, not just prefer one object's
    # existence, or a case where sbi_qualification exists but is only
    # WARN/PASS/NA while min_income_for_sa is the one that actually FAILs
    # would silently lose its floor failure entirely.
    _sq_gate = _gating_by_criterion(gating, "sbi_qualification")
    _mi_gate = _gating_by_criterion(gating, "min_income_for_sa")
    # Mirror engine.verdict.make_verdict's own exclusion rule: once the
    # SBI-specific qualification gate — the authoritative income floor
    # for this product — has PASSED, the generic/non-SBI
    # min_income_for_sa cross-check's FAIL must not independently justify
    # a "fresh income proof" ask here either, otherwise this box
    # contradicts the verdict sitting right next to it (Standard Issuance
    # next to "income below minimum"). Only excludes min_income_for_sa
    # when sbi_qualification itself PASSED — a WARN/NA/missing
    # sbi_qualification still lets a genuine min_income_for_sa FAIL fire
    # Rule 4 normally.
    _sq_authoritative_pass = _sq_gate is not None and _sq_gate.status == "PASS"
    cover_not_justified = (_sq_gate is not None and _sq_gate.status == "FAIL") or (
        _mi_gate is not None and _mi_gate.status == "FAIL" and not _sq_authoritative_pass
    )
    income_doc_gate = _gating_by_criterion(gating, "income_documented")
    # Declared-only income (no slip/Form-16/ITR/CA-computation at all) means
    # verified_income is just a copy of declared_income with zero
    # independent check behind it — the "verified < declared" comparison
    # below can never be true for such a case (there's nothing to diverge
    # from), so the standard-income-proof ask would silently never fire
    # even though we have ZERO verification the declared figure is real.
    income_undocumented = income_doc_gate is not None and income_doc_gate.status == "FAIL"
    income_proof_item_added = False
    if income_undocumented:
        items.append(
            {
                "text": "Standard income proof of Life Assured not received.",
                "critical": False,
            }
        )
        income_proof_item_added = True
    # DRC High-Risk ₹10L floor is looked up here (not just below) so the
    # over-declaration branch immediately below can merge it into the same
    # requirement line when both fail together, instead of risking a
    # second, redundant "fresh income proof" item.
    drc_gate = _gating_by_criterion(gating, "sbi_drc_income_floor")
    drc_fails = drc_gate is not None and drc_gate.status == "FAIL"

    if (
        not income_undocumented
        and verified_income
        and declared_income
        and verified_income < declared_income
        and (cover_not_justified or drc_fails)
    ):
        # When the CIBIL surrogate-income route is procedurally available
        # (score ≥650 — sbi_smart_shield_rules.cibil_gate /
        # Protection Plans and Surrogate Income guidelines.docx §13, row
        # "Credit Score"), the correct next step per that route is to
        # obtain the CIBIL income-estimator range ("lower range of CIBIL
        # income estimator ≥ declared income") — our source data only
        # ever has the raw score, never that estimator range, so this is
        # a genuine open requirement, not something the engine can
        # resolve itself. Asking for undifferentiated "fresh income
        # proof" here is misleading: hard proof (ITR/bank-credits) is
        # already on file and already confirmed insufficient — asking for
        # more of the same documentation type won't change the number.
        # Floor-comparison detail (which threshold, by how much) lives in
        # the risk-summary row below, not repeated here.
        # PASS = score clears the bar AND loan details are already on
        # file; WARN = score clears the bar but loan details are still
        # pending (check_cibil_gate's own loan_mandatory branch) — both
        # mean the CIBIL score itself qualifies for the surrogate route,
        # so both should word this as a surrogate-income ask rather than
        # undifferentiated "fresh income proof". The missing-loan-details
        # case is independently covered by the "Loan details for CIBIL
        # route" bundled item below.
        _cibil_gate = _gating_by_criterion(gating, "sbi_cibil_score")
        _cibil_route_available = _cibil_gate is not None and _cibil_gate.status in ("PASS", "WARN")
        if _cibil_route_available:
            items.append(
                {
                    "text": (
                        f"Surrogate income proof — verified {_inr(verified_income)} below required "
                        f"minimum; obtain CIBIL income-estimator range."
                    ),
                    "critical": False,
                }
            )
        else:
            items.append(
                {
                    "text": (
                        f"Fresh income proof: verified {_inr(verified_income)} below required "
                        f"minimum; reconcile against declared {_inr(declared_income)}."
                    ),
                    "critical": False,
                }
            )
        income_proof_item_added = True

    # ── DRC High-Risk ₹10L floor — independent of Rule 4's declared-vs-
    # verified comparison. The DRC rule itself (V02.docx, DRC row:
    # "minimum verified income must be 10 lakhs for consideration of
    # cover") is unconditional — it doesn't require verified<declared to
    # apply. Rule 4 as literally worded only fires on over-declaration, so
    # a DRC-High-Risk case where verified income is simply insufficient
    # (regardless of the declared figure, or even when verified >
    # declared) needs its own check here.
    if not income_proof_item_added and drc_fails and verified_income:
        items.append(
            {
                "text": f"Fresh income proof — verified {_inr(verified_income)} below High-Risk DRC minimum.",
                "critical": False,
                "criterion": "sbi_drc_income_floor",
            }
        )

    # ── Rule 3: premium payment capacity ───────────────────────────────
    foir_gate = _gating_by_criterion(gating, "foir")
    capacity_not_justified = (
        foir_gate is not None and foir_gate.status == "FAIL"
        # Undocumented premium-to-income ratio is advisory only.
    )
    if capacity_not_justified:
        items.append(
            {
                "text": "Additional source of funds required — premium capacity not supported by verified income.",
                "critical": False,
            }
        )

    # ── HLV-remaining eligibility (after existing in-force cover) ───────
    # Proposed SA exceeding what's left of this applicant's HLV once
    # bureau-verified in-force cover is subtracted needs a concrete next
    # step here, not just the raw gating finding left in the risk summary.
    hlv_remaining_gate = _gating_by_criterion(gating, "sbi_hlv_remaining")
    hlv_remaining_fails = hlv_remaining_gate is not None and hlv_remaining_gate.status == "FAIL"
    if hlv_remaining_fails:
        items.append(
            {
                "text": "Surrogate income proof of Life Assured — financial eligibility not met once existing cover is added.",
                "critical": False,
            }
        )

    # ── Disqualifying bureau records (IIB aggregation gate) ───────────
    # Suppressed when the HLV-remaining item above already fired — both
    # trace to the same over-exposure fact (existing + proposed cover too
    # high for this applicant), so showing both duplicates one finding as
    # two separate box items.
    iib_gate = _gating_by_criterion(gating, "sbi_iib_gate")
    if iib_gate is not None and iib_gate.status == "FAIL" and not hlv_remaining_fails:
        items.append(
            {
                "text": "Reconcile disqualifying bureau records — combined cover across insurers exceeds the allowed limit for this applicant.",
                "critical": False,
            }
        )

    # ── Bundled lower-priority sign-offs ───────────────────────────────
    bundle: list[str] = []
    bhb_gate = _gating_by_criterion(gating, "sbi_bhb_eligibility")
    bhb_evidence = (bhb_gate.evidence or "").lower() if bhb_gate is not None else ""
    if "qualification sub-condition fails" in bhb_evidence:
        # Rule: "BHB case, spouse qualification below acceptable range" —
        # data WAS extracted and specifically failed the gate's minimum,
        # distinct from the "not extracted" case below.
        bundle.append("spouse qualification below acceptable range for BHB")
    elif "qualification" in bhb_evidence and "not on file" in bhb_evidence:
        bundle.append("spouse qualification for BHB")
    cibil_gate = _gating_by_criterion(gating, "sbi_cibil_score")
    if (
        cibil_gate is not None
        and cibil_gate.status == "WARN"
        and "loan details" in (cibil_gate.evidence or "").lower()
    ):
        bundle.append("loan details for CIBIL route")
    # Rider-stacked exposure is explicitly supplementary, not a carrier
    # eligibility violation. Keep it advisory; it cannot create a requirement.
    # Insta-PIWC/Video MER confirmation deliberately NOT bundled here.
    # It's a real SBI requirement (Underwriting Guidelines for Smart
    # Shield Plus V02.docx, PIWC row) but it is not one of the 8 rules in
    # Input_Docs/Case sheet format.docx — the actual source document that
    # defines what belongs in THIS "Requirements to Raise" box — and it's
    # a pre-issuance operational/medical verification step, not a
    # financial-underwriting finding, on a sheet titled "Financial
    # Underwriting Case Sheet." The sbi_piwc_requirement gate itself
    # (engine.sbi_financial.check_piwc_requirement) is untouched — only
    # its surfacing here is withheld.
    for c in (case.get("reconciliation") or {}).get("contradictions") or []:
        field = (c.get("field") or "").lower()
        # A "low" severity here means the reconciler itself already
        # resolved the variant names to one winning value (e.g. a title
        # truncated across two source PDFs, or a common honorific
        # present in one field and not another) and explicitly says no
        # action is required — raising a KYC correction requirement for
        # an already-resolved, non-discrepant name contradicts the
        # reconciler's own finding. Only a genuine (medium+) mismatch
        # belongs here.
        if "nominee" in field and "name" in field and (c.get("severity") or "").lower() != "low":
            bundle.append("nominee name correction")
            break
    if bundle:
        text = "; ".join(bundle)
        items.append({"text": text[0].upper() + text[1:] + ".", "critical": False})

    return items


def affordability_advisory(case: dict, calcs: dict) -> list[dict]:
    """Undocumented 15% heuristic: informational, never a gate or requirement."""
    income = _safe_int(_cv(calcs, "selected_income"))
    premium = _safe_int((case.get("applicant") or {}).get("annual_premium"))
    if income > 0 and premium / income * 100 > _PREMIUM_CAPACITY_MAX_PCT:
        return [
            {
                "code": "PREMIUM_INCOME_RATIO",
                "advisory_only": True,
                "ratio_pct": round(premium / income * 100, 2),
                "note": "Premium/income ratio above 15%; threshold lacks carrier approval and does not affect issuance.",
            }
        ]
    return []
