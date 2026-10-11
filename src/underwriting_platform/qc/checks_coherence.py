"""
qc/checks_coherence.py — D1–D7 cross-section consistency, plus D10
TALIC-specific APP_STATUS cross-check.

Asserts that the same fact appears with the same value across all
sections of the case sheet:
  D1  applicant name uniform across header / applicant / rationale
  D2  proposed_sa rendered identically wherever it appears
  D3  age single-source-of-truth
  D4  verdict ↔ pill colour (label==DECLINE ⇒ red pill rendered)
  D5  verdict ↔ rationale directional consistency — LLM-only; deferred
       to qc/llm_second_eye.py
  D6  risk-dial coherence (3 HIGH dials + STANDARD_ACCEPTANCE = anomaly)
  D7  OUT_OF_SCOPE invariant — empty gating + flags + calcs ⇒ banner
  D10 [TALIC] verdict-label vs policy_data.app_status — WARN-only,
       benchmark signal, not a delivery block.

D8/D9 (separate-LA invariant, existing-cover invariant) are IFL-
specific and deferred until that profile ports the QC layer.
"""

from __future__ import annotations
import re

from .case_sheet import register_check
from .types import QCFinding


def _locate(pages: list[str], match_text: str) -> str:
    for i, page_text in enumerate(pages, start=1):
        if match_text in page_text:
            return f"PDF p{i}"
    return "—"


# ---------------------------------------------------------------------------
# D1 — Applicant name consistency
# ---------------------------------------------------------------------------


def check_name_consistency(ctx: dict) -> list[QCFinding]:
    """The applicant name should appear at least once in the rendered
    PDF. If it appears, it should match (case-insensitive) the
    canonical decision.applicant.applicant_name. We can't trivially
    verify uniform spelling across sections via pdfplumber text alone,
    so we lean on the JSON: if decision carries multiple name fields
    (applicant.applicant_name vs nominee.insured_name vs
    policy_data.insured_name) we flag mismatches."""
    decision: dict = ctx["decision"]
    text: str = ctx["full_text"]
    out: list[QCFinding] = []

    a_name = ((decision.get("applicant") or {}).get("applicant_name") or "").strip()
    if not a_name:
        return out

    # Confirm presence in the PDF
    if a_name and a_name not in text:
        out.append(
            QCFinding(
                code="APPLICANT_NAME_MISSING_IN_PDF",
                category="coherence",
                severity="WARN",
                location="PDF (full text)",
                snippet=a_name,
                rule="underwriting_platform.qc.checks_coherence.check_name_consistency",
                remediation=("Applicant name from decision JSON not found in the rendered text."),
            )
        )

    # JSON cross-check: applicant_name vs policy_data.insured_name
    pd_block = decision.get("policy_data") or {}
    pd_name = (pd_block.get("insured_name") or "").strip()
    if pd_name and a_name:
        # Normalise — drop punctuation, collapse whitespace, lowercase
        def _norm(s: str) -> str:
            return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", s)).strip().lower()

        if _norm(pd_name) != _norm(a_name):
            out.append(
                QCFinding(
                    code="NAME_MISMATCH_PDF_VS_XLSX",
                    category="coherence",
                    severity="WARN",
                    location="applicant.applicant_name vs policy_data.insured_name",
                    snippet=f"PDF: {a_name!r}  ·  xlsx: {pd_name!r}",
                    rule="underwriting_platform.qc.checks_coherence.check_name_consistency",
                    remediation=(
                        "Engine and Policy_Data.xlsx disagree on the "
                        "applicant's name — investigate KYC vs "
                        "system-of-record."
                    ),
                )
            )
    return out


# ---------------------------------------------------------------------------
# D2 — Proposed SA consistency
# ---------------------------------------------------------------------------


def check_sa_consistency(ctx: dict) -> list[QCFinding]:
    """decision.proposed_sa (input) should match calculations.max_sa_eligible.inputs.proposed_sa
    (engine echoed) and the policy_data.sum_assured (xlsx system-of-record).
    Engine internal coherence is BLOCK-grade; xlsx mismatch is WARN."""
    decision: dict = ctx["decision"]
    out: list[QCFinding] = []
    proposed = decision.get("proposed_sa")
    calcs = decision.get("calculations") or {}
    max_sa = calcs.get("max_sa_eligible") or {}
    inputs = max_sa.get("inputs") or {}
    echoed = inputs.get("proposed_sa")
    if proposed and echoed and int(proposed) != int(echoed):
        out.append(
            QCFinding(
                code="SA_MISMATCH_ENGINE",
                category="coherence",
                severity="BLOCK",
                location="proposed_sa vs calculations.max_sa_eligible.inputs.proposed_sa",
                snippet=f"input={proposed}  ·  echoed={echoed}",
                rule="underwriting_platform.qc.checks_coherence.check_sa_consistency",
                remediation=(
                    "Engine echo of proposed_sa disagrees with the "
                    "input — investigate compute_max_sa wiring."
                ),
            )
        )
    pd_sa = (decision.get("policy_data") or {}).get("sum_assured")
    if proposed and pd_sa:
        diff_pct = abs(int(proposed) - int(pd_sa)) / max(1, int(pd_sa)) * 100
        if diff_pct > 5:  # > 5 % delta is meaningful
            out.append(
                QCFinding(
                    code="SA_MISMATCH_XLSX",
                    category="coherence",
                    severity="WARN",
                    location="proposed_sa vs policy_data.sum_assured",
                    snippet=f"engine={proposed}  ·  xlsx={pd_sa}  ·  Δ={diff_pct:.1f}%",
                    rule="underwriting_platform.qc.checks_coherence.check_sa_consistency",
                    remediation=("Engine proposed_sa differs from xlsx SUM_ASURD by > 5 %."),
                )
            )
    return out


# ---------------------------------------------------------------------------
# D3 — Age consistency
# ---------------------------------------------------------------------------


def check_age_consistency(ctx: dict) -> list[QCFinding]:
    """applicant.age (extracted) should match calculations.age.value
    (engine computed) and policy_data.age_at_entry (xlsx)."""
    decision: dict = ctx["decision"]
    out: list[QCFinding] = []
    applicant_age = (decision.get("applicant") or {}).get("age")
    calc_age = ((decision.get("calculations") or {}).get("age") or {}).get("value")
    pd_age = (decision.get("policy_data") or {}).get("age_at_entry")

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    a, c, p = _int(applicant_age), _int(calc_age), _int(pd_age)
    if a is not None and c is not None and a != c:
        out.append(
            QCFinding(
                code="AGE_MISMATCH_ENGINE",
                category="coherence",
                severity="WARN",
                location="applicant.age vs calculations.age.value",
                snippet=f"applicant={a}  ·  engine_calc={c}",
                rule="underwriting_platform.qc.checks_coherence.check_age_consistency",
                remediation=("Engine recompute disagrees with extracted age."),
            )
        )
    if c is not None and p is not None and abs(c - p) > 1:
        # 1-year drift is normal (year boundaries); >1 is worth a look
        out.append(
            QCFinding(
                code="AGE_MISMATCH_XLSX",
                category="coherence",
                severity="INFO",
                location="calculations.age vs policy_data.age_at_entry",
                snippet=f"engine={c}  ·  xlsx={p}",
                rule="underwriting_platform.qc.checks_coherence.check_age_consistency",
                remediation=("Engine and xlsx differ on age at entry by more than a year."),
            )
        )
    return out


# ---------------------------------------------------------------------------
# D4 — Verdict ↔ pill rendering
# ---------------------------------------------------------------------------


def check_verdict_pill_renders(ctx: dict) -> list[QCFinding]:
    """The translated verdict label (per profile.verdict_terminology)
    should appear at least once in the rendered text.

    We can't check pill *colour* from extracted text — that needs
    ReportLab introspection. But we can confirm the *label* renders.
    """
    decision: dict = ctx["decision"]
    text: str = ctx["full_text"]
    profile: dict = ctx["profile"]
    out: list[QCFinding] = []

    raw_label = ((decision.get("verdict") or {}).get("label") or "").upper()
    mapping = profile.get("verdict_terminology") or {}
    rendered_label = mapping.get(raw_label, raw_label)
    if not rendered_label or rendered_label == "ERROR":
        return out

    # Acceptable rendered forms of the verdict pill: the mapped label
    # (profile.verdict_terminology), the raw engine label, and the raw
    # label with underscores turned to spaces — the generic fallback
    # renderer.triage_sheet._draw_decision_bar's label_map uses for any
    # SBI label it doesn't have a specific display string for (e.g.
    # STANDARD_ISSUANCE -> "STANDARD ISSUANCE").
    candidates = {rendered_label, raw_label, raw_label.title(), raw_label.replace("_", " ")}

    if not any(c and c in text for c in candidates):
        out.append(
            QCFinding(
                code="VERDICT_LABEL_NOT_RENDERED",
                category="coherence",
                severity="BLOCK",
                location="header verdict pill (expected)",
                snippet=f"engine={raw_label!r}  ·  none of {sorted(candidates)} found in PDF",
                rule="underwriting_platform.qc.checks_coherence.check_verdict_pill_renders",
                remediation=(
                    "The verdict label is missing from the rendered "
                    "text — header pill render is broken."
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# D6 — Risk-dial coherence
# ---------------------------------------------------------------------------


def check_risk_dial_coherence(ctx: dict) -> list[QCFinding]:
    """Three HIGH dials + STANDARD_ACCEPTANCE / Issued = anomaly."""
    decision: dict = ctx["decision"]
    out: list[QCFinding] = []
    label = ((decision.get("verdict") or {}).get("label") or "").upper()
    calcs = decision.get("calculations") or {}
    drivers = (decision.get("verdict") or {}).get("decision_drivers") or []

    def _band(prefix: str) -> str:
        for d in drivers:
            if isinstance(d, str) and d.startswith(f"{prefix}="):
                return d.split("=", 1)[1].upper()
        return "N/A"

    fin = _band("financial_risk")
    med = (calcs.get("medical_risk") or {}).get("value") or "N/A"
    frd = (calcs.get("fraud_risk") or {}).get("value") or "N/A"
    high_count = sum(1 for b in (fin, str(med).upper(), str(frd).upper()) if b == "HIGH")
    if high_count >= 3 and label == "STANDARD_ACCEPTANCE":
        out.append(
            QCFinding(
                code="RISK_DIAL_VS_VERDICT_ANOMALY",
                category="coherence",
                severity="BLOCK",
                location="risk dials vs verdict.label",
                snippet=f"fin={fin} med={med} fraud={frd} · label={label}",
                rule="underwriting_platform.qc.checks_coherence.check_risk_dial_coherence",
                remediation=(
                    "Three HIGH dials but STANDARD_ACCEPTANCE — investigate the verdict logic."
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# D7 — OUT_OF_SCOPE invariant
# ---------------------------------------------------------------------------


def check_oos_invariant(ctx: dict) -> list[QCFinding]:
    """If verdict label is OUT_OF_SCOPE (or equivalent in the profile),
    gating + flags + calculations should all be empty AND the rendered
    text should carry the OOS banner."""
    decision: dict = ctx["decision"]
    label = ((decision.get("verdict") or {}).get("label") or "").upper()
    if label != "OUT_OF_SCOPE":
        return []
    out: list[QCFinding] = []
    if decision.get("gating") or decision.get("flags") or decision.get("calculations"):
        out.append(
            QCFinding(
                code="OOS_INVARIANT_VIOLATED",
                category="coherence",
                severity="BLOCK",
                location="verdict.label=OUT_OF_SCOPE",
                snippet=(
                    f"gating={len(decision.get('gating') or [])} · "
                    f"flags={len(decision.get('flags') or [])} · "
                    f"calcs={len(decision.get('calculations') or {})}"
                ),
                rule="underwriting_platform.qc.checks_coherence.check_oos_invariant",
                remediation=(
                    "OUT_OF_SCOPE case has populated gating / flags / "
                    "calculations — those should be empty and the "
                    "banner section should render."
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# D10 — TALIC bonus: engine verdict vs xlsx APP_STATUS
# ---------------------------------------------------------------------------

# Engine-label → expected xlsx APP_STATUS. WARN-only — the case sheet
# is still deliverable; this is a benchmark signal for accuracy tracking.
_TALIC_EXPECTED_APP_STATUS = {
    "STANDARD_ACCEPTANCE": {"Issued"},
    "CONDITIONAL": {"Postpone"},
    "REFER": {"Postpone"},
    "POSTPONE": {"Postpone"},
    "DECLINE": {"Decline", "Probable Decline"},
}


def check_tmt_opinion_contradiction(ctx: dict) -> list[QCFinding]:
    """D12 — Datamatics LAYOUT Sheet1 Med #3 (C314061926 regression).

    Catches the bug where the TMT recommendation says "Positive" but
    the lab interpretation describes a negative / normal study (no
    inducible ischemia, THR achieved, no ectopic beats). The medical
    agent's prompt rule #12 SHOULD prevent this, but a brittle read
    can still flip the call — this delivery-time check catches it.
    """
    decision: dict = ctx["decision"]
    medical = (decision.get("extraction_blocks") or {}).get("medical") or {}
    tmt = medical.get("tmt") or {}
    rec = (tmt.get("recommendation") or "").strip().lower()
    interp = (tmt.get("interpretation") or "").strip().lower()
    if not rec or not interp:
        return []
    # "Positive" rec but interp describes a clearly-negative study
    negative_markers = (
        "no inducible ischemia",
        "no significant ischemia",
        "no ischemic changes",
        "normal study",
        "negative tmt",
        "tmt negative",
        "no ischemia",
    )
    if rec == "positive" and any(m in interp for m in negative_markers):
        return [
            QCFinding(
                code="TMT_OPINION_CONTRADICTION",
                category="coherence",
                severity="BLOCK",
                location="medical.tmt.recommendation vs medical.tmt.interpretation",
                snippet=f"recommendation={rec!r} but interpretation reads "
                f"as negative: {interp[:80]!r}",
                rule="underwriting_platform.qc.checks_coherence.check_tmt_opinion_contradiction",
                remediation=(
                    "Medical agent flipped the TMT call — re-extract "
                    "with focused attention on the lab's first-"
                    "impression line vs the physician's final "
                    "classification (prompt rule #11-#12)."
                ),
            )
        ]
    return []


def check_ecg_tmt_missing_opinion(ctx: dict) -> list[QCFinding]:
    """D13 — Datamatics LAYOUT Sheet1 Med #8 (C304418178, C306847174,
    C312315043 regressions).

    Catches cases where ECG or TMT is recorded as performed (documents
    present, rate / interpretation / status sometimes shown) but the
    AGENT NARRATIVE doesn't carry an opinion. Defined as: ecg.performed
    or tmt.performed is true but the corresponding interpretation /
    recommendation field is empty.
    """
    decision: dict = ctx["decision"]
    medical = (decision.get("extraction_blocks") or {}).get("medical") or {}
    out: list[QCFinding] = []
    ecg = medical.get("ecg") or {}
    if ecg.get("performed"):
        interp = (ecg.get("interpretation") or "").strip()
        status = (ecg.get("status") or "").strip()
        if not interp and not status:
            out.append(
                QCFinding(
                    code="ECG_OPINION_MISSING",
                    category="coherence",
                    severity="WARN",
                    location="medical.ecg.interpretation + medical.ecg.status",
                    snippet="ECG performed but neither interpretation nor status populated",
                    rule="underwriting_platform.qc.checks_coherence.check_ecg_tmt_missing_opinion",
                    remediation=(
                        "Medical agent extracted ECG presence but no "
                        "clinical opinion. Re-extract with prompt rule "
                        "#11 (defer to physician's signed read)."
                    ),
                )
            )
    tmt = medical.get("tmt") or {}
    if tmt.get("performed") or tmt.get("recommendation") or tmt.get("exercise_duration_min"):
        rec = (tmt.get("recommendation") or "").strip()
        interp = (tmt.get("interpretation") or "").strip()
        if not rec and not interp:
            out.append(
                QCFinding(
                    code="TMT_OPINION_MISSING",
                    category="coherence",
                    severity="WARN",
                    location="medical.tmt.recommendation + medical.tmt.interpretation",
                    snippet="TMT performed but neither recommendation nor interpretation populated",
                    rule="underwriting_platform.qc.checks_coherence.check_ecg_tmt_missing_opinion",
                    remediation=(
                        "Medical agent extracted TMT performance but "
                        "no clinical recommendation. Re-extract with "
                        "prompt rule #12 (THR / ectopic-beat-based "
                        "recommendation logic)."
                    ),
                )
            )
    return out


def check_audited_pl_for_business(ctx: dict) -> list[QCFinding]:
    """D14 — Datamatics LAYOUT Sheet1 Fin #1.

    Catches the pattern where the case has a business-insurance signal
    (`business_insurance.applicable=true`, or applicant.occupation is
    self-employed / business owner) but the income agent has only
    sourced from ITR-with-COI and not from a CA-audited P&L. For
    business cases, the audited P&L is the gold-standard income
    evidence and skipping it is a known DATAMATICS finding.
    """
    decision: dict = ctx["decision"]
    from underwriting_platform.engine.routing import is_sbi_case

    if is_sbi_case(decision):
        # SBI explicitly accepts ITRs as an alternative standard income proof.
        # Its occupation/income-proof gate already evaluates evidence completeness.
        return []
    eb = decision.get("extraction_blocks") or {}
    business = eb.get("business_insurance") or {}
    surrogate = eb.get("surrogate") or {}
    income = eb.get("income") or {}
    applicant = decision.get("applicant") or {}

    occupation = (applicant.get("occupation") or "").lower()
    is_business = (
        bool(business.get("applicable"))
        or "self-employed" in occupation
        or "business" in occupation
        or "director" in occupation
        or "partner" in occupation
        or "proprietor" in occupation
    )
    if not is_business:
        return []
    primary = (income.get("primary_source") or "").lower()
    docs = income.get("docs_consumed") or []
    has_audited = (
        primary == "audited_pl"
        or any("audit" in str(d).lower() for d in docs)
        or any("p&l" in str(d).lower() or "p & l" in str(d).lower() for d in docs)
    )
    if not has_audited:
        return [
            QCFinding(
                code="AUDITED_PL_MISSING_FOR_BUSINESS",
                category="coherence",
                severity="WARN",
                location=f"income.primary_source={primary!r} (occupation={occupation!r})",
                snippet=("Business / self-employed applicant but no CA-audited P&L on file."),
                rule="underwriting_platform.qc.checks_coherence.check_audited_pl_for_business",
                remediation=(
                    "Business / self-employed cases require CA-audited "
                    "P&L per Datamatics LAYOUT Fin #1. Request audited "
                    "financial statements or move to surrogate path."
                ),
            )
        ]
    return []


def check_ecm_pending_vs_standard(ctx: dict) -> list[QCFinding]:
    """D11 — Datamatics LAYOUT Sheet1 Fin #8 (C304418178).

    Catches the pattern where the policy_data ECM bureau score is
    "STANDARD" (i.e., the system has already classified the ACM as
    standard) but the rendered narrative or compliance concerns still
    call it "pending". This contradicts the system-of-record and
    misleads the underwriter.
    """
    decision: dict = ctx["decision"]
    text: str = ctx["full_text"]
    pd_block = decision.get("policy_data") or {}
    bureau = pd_block.get("bureau_scores") or {}
    ecm = bureau.get("ecm") or {}
    ecm_score = (
        (ecm.get("score") or "").strip().upper() if isinstance(ecm.get("score"), str) else ""
    )
    if ecm_score != "STANDARD":
        return []
    # Look for "ACM pending" / "ECM pending" / "ACM is pending" in the
    # rendered narrative. These come from compliance_concerns or
    # reconciler_notes when the agent doesn't know the xlsx already
    # has a STANDARD score.
    import re as _re

    # Match "ACM/ECM ... pending" with up to ~30 chars of intervening
    # text — catches "ACM is pending", "ACM score is pending",
    # "ECM/ACM stated as pending in review", etc.
    pat = _re.compile(r"\b(?:ACM|ECM)\b[\s\w/]{0,30}\bpending\b", _re.IGNORECASE)
    if pat.search(text):
        return [
            QCFinding(
                code="ECM_PENDING_VS_XLSX_STANDARD",
                category="coherence",
                severity="WARN",
                location="rendered narrative vs policy_data.bureau_scores.ecm",
                snippet=f"xlsx.ECM_SCORE=STANDARD but narrative says 'ACM/ECM pending'",
                rule="underwriting_platform.qc.checks_coherence.check_ecm_pending_vs_standard",
                remediation=(
                    "Policy_Data xlsx already has ECM standard; "
                    "update narrative / compliance_concerns to "
                    "match the system-of-record."
                ),
            )
        ]
    return []


def check_verdict_vs_app_status(ctx: dict) -> list[QCFinding]:
    decision: dict = ctx["decision"]
    pd_block = decision.get("policy_data") or {}
    app_status = (pd_block.get("app_status") or "").strip()
    label = ((decision.get("verdict") or {}).get("label") or "").upper()
    if not app_status or not label:
        return []
    expected = _TALIC_EXPECTED_APP_STATUS.get(label, set())
    if not expected:
        return []
    if app_status not in expected:
        return [
            QCFinding(
                code="VERDICT_VS_APP_STATUS_MISMATCH",
                category="benchmark",
                severity="WARN",
                location="verdict.label vs policy_data.app_status",
                snippet=f"engine={label}  ·  xlsx={app_status}  ·  expected_for_engine={sorted(expected)}",
                rule="underwriting_platform.qc.checks_coherence._TALIC_EXPECTED_APP_STATUS",
                remediation=(
                    "Engine verdict disagrees with TALIC system "
                    "outcome. WARN-only — tracked for accuracy "
                    "benchmarking, not blocking delivery."
                ),
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

register_check("checks_coherence.name_consistency", check_name_consistency)
register_check("checks_coherence.sa_consistency", check_sa_consistency)
register_check("checks_coherence.age_consistency", check_age_consistency)
register_check("checks_coherence.verdict_pill", check_verdict_pill_renders)
register_check("checks_coherence.risk_dials", check_risk_dial_coherence)
register_check("checks_coherence.oos_invariant", check_oos_invariant)
register_check("checks_coherence.verdict_vs_app", check_verdict_vs_app_status)
register_check("checks_coherence.ecm_pending", check_ecm_pending_vs_standard)
register_check("checks_coherence.tmt_contradiction", check_tmt_opinion_contradiction)
register_check("checks_coherence.ecg_tmt_missing", check_ecg_tmt_missing_opinion)
register_check("checks_coherence.audited_pl_business", check_audited_pl_for_business)
