"""
engine/flags.py — derive engine-level flags from extraction + reconciliation.

We DO NOT re-run an LLM here. Flags are deterministic transforms of the
already-extracted blocks and the already-reconciled risk signals. Each flag
records the function that produced it so the renderer can show provenance.
"""

from __future__ import annotations
from .routing import is_sbi_case
import re
from typing import Any

from .calculator import _safe_int, _safe_float
from .types import Flag, Source, Calculation, GatingItem


# Severity mapper for reconciler signals.
_RECON_SEV_MAP = {"critical": "CRITICAL", "high": "HIGH", "medium": "MEDIUM", "low": "LOW"}


def flags_from_reconciliation(case: dict) -> list[Flag]:
    """Lift reconciler.risk_signals into engine Flag objects."""
    out: list[Flag] = []
    recon = case.get("reconciliation") or {}
    for r in recon.get("risk_signals") or []:
        code = (r.get("code") or "OTHER").strip()
        sev_raw = (r.get("severity") or "low").strip().lower()
        sev = _RECON_SEV_MAP.get(sev_raw, "LOW")
        desc = r.get("description") or ""
        title = code.replace("_", " ").title()
        if code.upper() == "OTHER" and desc:
            # "OTHER" is the reconciler's catch-all bucket — title-casing it
            # literally renders as the meaningless "Other", and two unrelated
            # OTHER signals on the same case collide under the identical flag
            # code RECON_OTHER (e.g. case SYNTHETIC_CASE: the MHR wrong-document/
            # null-GPS signal and an unrelated PAN-format signal both landed
            # as "RECON_OTHER" / "Other"). Derive both title and code from
            # the description's first clause instead, so each gets its own
            # distinguishable identity. Found via case SYNTHETIC_CASE review
            # (2026-06-29).
            first_clause = re.split(r"(?<!\d)[:.](?!\d)", desc, maxsplit=1)[0].strip()
            if len(first_clause) > 80:
                cut = first_clause[:77].rstrip()
                cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
                first_clause = cut + "..."
            title = first_clause
            slug = re.sub(r"[^A-Z0-9]+", "_", title.upper()).strip("_")[:40]
            if slug:
                code = f"OTHER_{slug}"
        out.append(
            Flag(
                code=f"RECON_{code}",
                severity=sev,
                title=title or "Other",
                description=desc,
                function="underwriting_platform.engine.flags.flags_from_reconciliation",
                rule="reconciliation.risk_signals[]",
            )
        )
    return out


def flags_from_gating(items: list[GatingItem]) -> list[Flag]:
    out: list[Flag] = []
    for it in items:
        if it.status == "FAIL":
            sev = "HIGH"
        elif it.status == "WARN":
            sev = "MEDIUM"
        else:
            continue
        out.append(
            Flag(
                code=f"GATE_{it.criterion.upper()}",
                severity=sev,
                title=f"Gating {it.status}: {it.label}",
                description=it.evidence,
                function="underwriting_platform.engine.flags.flags_from_gating",
                rule=it.rule,
                sources=it.sources,
            )
        )
    return out


def _family_member_set(family: list[dict] | None) -> set[tuple[str, int]]:
    """Set of (relationship, age) tuples — used to diff primary medical
    agent's family_history against the focused re-OCR read.

    Tolerant of both schemas:
      - primary: `{"relationship": "brother", "age_current": 36, …}`
      - re-ocr:  `{"relationship": "brother", "age": 38, …}`
    """
    out: set[tuple[str, int]] = set()
    for m in family or []:
        if not isinstance(m, dict):
            continue
        rel = (m.get("relationship") or "").strip().lower()
        age = m.get("age_current") if "age_current" in m else m.get("age")
        if not rel or age is None:
            continue
        try:
            out.add((rel, int(age)))
        except (TypeError, ValueError):
            continue
    return out


def _flags_from_family_history_reocr(case: dict | None) -> list[Flag]:
    """Compare `medical.family_history` (primary) against
    `medical.family_history_reocr.values.members` (focused re-OCR)
    by (relationship, age) tuples. Any mismatch becomes a MEDIUM flag.
    """
    out: list[Flag] = []
    if not case:
        return out
    medical = case.get("medical") or {}
    primary = medical.get("family_history") or []
    reocr_block = medical.get("family_history_reocr") or {}
    reocr_members = ((reocr_block.get("values") or {}).get("members")) or []
    if not reocr_members and not primary:
        return out

    primary_set = _family_member_set(primary)
    reocr_set = _family_member_set(reocr_members)
    only_primary = primary_set - reocr_set
    only_reocr = reocr_set - primary_set
    if not only_primary and not only_reocr:
        return out

    pretty = lambda s: ", ".join(f"{r}={a}" for r, a in sorted(s)) or "—"
    # Client-safe prose: "Primary" / "Secondary" instead of any model brand
    # or version string. Reconciler prompt rule 9 + RULES § 15.6.
    description = (
        f"Primary read: [{pretty(primary_set)}]. "
        f"Secondary re-OCR ({reocr_block.get('source_pdf')} "
        f"p{reocr_block.get('source_page')}): "
        f"[{pretty(reocr_set)}]. "
        f"Only in primary: [{pretty(only_primary)}]. "
        f"Only in secondary: [{pretty(only_reocr)}]. "
        "Open the source MER family-history table and pick the correct read."
    )
    out.append(
        Flag(
            code="FAMILY_HISTORY_REOCR_DISAGREEMENT",
            severity="MEDIUM",
            title="Family-history re-OCR disagrees with primary read",
            description=description,
            function="underwriting_platform.engine.flags._flags_from_family_history_reocr",
            rule="family_history.{relationship,age} set diff",
        )
    )
    return out


def _flags_from_identity_verification(case: dict | None) -> list[Flag]:
    """Flags driven by `engine.identity_verification` outputs:
    - DC_DISTANCE_HIGH (≥50 km) — MEDIUM
    - DC_DISTANCE_NOT_DETERMINED (one or both endpoints not geocoded) — LOW
    - LIVE_PHOTO_MISSING — MEDIUM
    - PHOTO_MATCH_FAIL (Gemini → no-match) — HIGH
    - PHOTO_MATCH_REVIEW (partial-match / undetectable) — MEDIUM
    """
    out: list[Flag] = []
    if not case:
        return out
    afm = case.get("anti_fraud_markers") or {}
    applicant = case.get("applicant") or {}

    # ── DC distance ─────────────────────────────────────────────
    dc_km = afm.get("dc_distance_km")
    dc_band = afm.get("dc_distance_band")
    if dc_km is None or dc_band in (None, "Unknown"):
        out.append(
            Flag(
                code="DC_DISTANCE_NOT_DETERMINED",
                severity="LOW",
                title="DC distance from applicant pin not determined",
                description=(
                    "Diagnostic-centre distance could not be computed — "
                    f"applicant address={applicant.get('address') or 'not provided'}, "
                    f"DC name={afm.get('diagnostic_centre_name') or 'not provided'}, "
                    f"DC city={afm.get('diagnostic_centre_city') or 'not provided'}. "
                    "Either the addresses are missing from the documents or "
                    "Google Maps geocoding returned no result."
                ),
                function="underwriting_platform.engine.flags._flags_from_identity_verification",
                rule="dc_distance_km is None OR dc_distance_band == 'Unknown'",
            )
        )
    else:
        try:
            km_f = float(dc_km)
        except (TypeError, ValueError):
            km_f = None
        if km_f is not None and km_f >= 50.0:
            out.append(
                Flag(
                    code="DC_DISTANCE_HIGH",
                    severity="MEDIUM",
                    title=f"Diagnostic centre is {km_f:.0f} km from applicant",
                    description=(
                        f"Straight-line distance applicant → DC = {km_f:.2f} km "
                        f"({dc_band}). Long DC distances can indicate fraud / "
                        "address-of-convenience; verify the applicant chose this "
                        "DC for a legitimate reason."
                    ),
                    function="underwriting_platform.engine.flags._flags_from_identity_verification",
                    rule="dc_distance_km >= 50",
                )
            )

    # ── Live photo presence ─────────────────────────────────────
    live = (applicant.get("live_photo_at_medicals") or "").strip().lower()
    if live in ("no", "missing", ""):
        out.append(
            Flag(
                code="LIVE_PHOTO_MISSING",
                severity="MEDIUM",
                title="Live photo at medicals not on file",
                description=(
                    "No 'live photo' / 'TPA photo identification' / 'geotag' file "
                    "was found in the case folder. Per Term UW guidelines, a "
                    "live photo must be captured at the diagnostic centre and "
                    "matched against the KYC photo for fraud control."
                ),
                function="underwriting_platform.engine.flags._flags_from_identity_verification",
                rule="applicant.live_photo_at_medicals == 'No'",
            )
        )

    # ── Photo match outcome ─────────────────────────────────────
    pm = applicant.get("photo_match") or {}
    mr = (pm.get("match_result") or "").strip().lower()
    if mr == "no-match":
        out.append(
            Flag(
                code="PHOTO_MATCH_FAIL",
                severity="HIGH",
                title="Live photo does NOT match KYC photo",
                description=(
                    f"Gemini face-match: NO-MATCH (confidence={pm.get('confidence')}). "
                    f"KYC: {pm.get('kyc_file')!r}; Live: {pm.get('live_file')!r}. "
                    f"Reasoning: {pm.get('reasoning')!r}. "
                    "Manual underwriter review required before issuance."
                ),
                function="underwriting_platform.engine.flags._flags_from_identity_verification",
                rule="applicant.photo_match.match_result == 'no-match'",
            )
        )
    elif mr in ("partial-match", "undetectable"):
        out.append(
            Flag(
                code="PHOTO_MATCH_REVIEW",
                severity="MEDIUM",
                title=f"Live ↔ KYC photo: {mr.replace('-', ' ')}",
                description=(
                    f"Gemini face-match: {mr.upper()} "
                    f"(confidence={pm.get('confidence')}). "
                    f"KYC: {pm.get('kyc_file')!r}; Live: {pm.get('live_file')!r}. "
                    f"Reasoning: {pm.get('reasoning')!r}. "
                    "Manual underwriter review recommended."
                ),
                function="underwriting_platform.engine.flags._flags_from_identity_verification",
                rule="applicant.photo_match.match_result ∈ {'partial-match','undetectable'}",
            )
        )
    return out


def _flags_from_dual_vendor(case: dict | None) -> list[Flag]:
    """One Flag per disagreed field in `case.medical.dual_vendor_diff`."""
    out: list[Flag] = []
    if not case:
        return out
    diff = (case.get("medical") or {}).get("dual_vendor_diff") or {}
    disagreed = diff.get("disagreed") or []
    if not disagreed:
        return out
    # Client-safe labels — the underlying vendor identities ("claude" /
    # "gemini") stay in diff.primary_vendor / secondary_vendor for JSON
    # provenance, but never appear in client-facing prose. Reconciler
    # prompt rule 9 + RULES § 15.6.
    primary_label = "Primary"
    secondary_label = "Secondary"
    for d in disagreed:
        field_name = d.get("field", "?")
        sev = (d.get("severity") or "MEDIUM").upper()
        if "condition_set" in field_name:
            only_p = d.get("only_in_primary") or []
            only_s = d.get("only_in_secondary") or []
            description = (
                f"{primary_label} extracted: {sorted(d.get('primary') or [])}. "
                f"{secondary_label} extracted: {sorted(d.get('secondary') or [])}. "
                f"Only in {primary_label}: {only_p}. "
                f"Only in {secondary_label}: {only_s}. "
                "Open the source MER and decide which extraction is correct."
            )
        else:
            description = (
                f"{primary_label}={d.get('primary')!r} vs "
                f"{secondary_label}={d.get('secondary')!r}. "
                "Open the source MER and pick the correct read."
            )
        out.append(
            Flag(
                code=f"MEDICAL_DUAL_VENDOR_DISAGREEMENT__{field_name.replace('.', '_').upper()}",
                severity=sev,
                title=f"Dual-vendor medical disagreement: {field_name}",
                description=description,
                function="underwriting_platform.engine.flags._flags_from_dual_vendor",
                rule="medical_dual_vendor.compare_medical_blocks",
            )
        )
    return out


def flags_from_calculations(
    calcs: dict[str, Calculation], rules: dict, case: dict | None = None
) -> list[Flag]:
    out: list[Flag] = []
    foir = _safe_float((calcs.get("foir") or Calculation(name="foir", value=0)).value)
    if foir >= 100:
        out.append(
            Flag(
                code="FOIR_OVER_100",
                severity="CRITICAL",
                title="FOIR exceeds 100 %",
                description=f"FOIR {foir:.1f}% — total EMIs exceed monthly gross income.",
                function="underwriting_platform.engine.flags.flags_from_calculations",
                rule=f"foir.value >= 100 (uw_rules.foir_threshold_pct = {rules.get('foir_threshold_pct')})",
                sources=calcs["foir"].sources,
            )
        )
    income_val = _safe_int((calcs.get("selected_income") or Calculation(name="x", value=0)).value)
    if income_val <= 0:
        # A hard-proof method (ITR, salary slip, Form-16, CA computation,
        # audited financials) confirming ₹0 earned income is a different
        # fact from the cascade genuinely finding nothing at all — the old
        # fixed wording ("could not be established... cascade exhausted")
        # described every income_val<=0 case as a documentation gap, even
        # when complete 3-year ITR filings explicitly confirmed zero
        # earned income (an applicant with passive-only income: capital
        # gains, interest, dividends, commission — no salary or business/
        # profession head). Found via case SYNTHETIC_CASE (2026-10-06),
        # downstream of the engine.calculator.compute_selected_income
        # Missing documentation is resolvable and must not itself trigger
        # terminal decline. Preserve the adverse flag for verified zero income.
        _method = (
            (calcs.get("selected_income") or Calculation(name="x", value=0, method="")).method or ""
        ).lower()
        _hard = _method.startswith("itr") or _method.split("_fx_inr")[0] in (
            "salary_slip",
            "form_16",
            "ca_computation",
            "audited_financials",
        )
        if _hard:
            title = "Earned income confirmed as zero"
            description = (
                "Hard-proof documentation (ITR/salary-slip/Form-16/CA computation) is on "
                "file and confirms ₹0 earned income (salary + business/profession) — not "
                "a missing-data gap."
            )
        else:
            title = "Income could not be established"
            description = "No income value extracted from any document — cascade exhausted."
        out.append(
            Flag(
                code="INCOME_NOT_ESTABLISHED",
                severity="CRITICAL" if _hard else "HIGH",
                title=title,
                description=description,
                function="underwriting_platform.engine.flags.flags_from_calculations",
                rule="selected_income.value <= 0",
            )
        )

    # Losses-set-off marker — when ITR(s) show GTI dropping to ≈ 0 due
    # to speculative / business losses absorbing earned heads, surface
    # the discrepancy. Engine uses earned_total (stable income stream)
    # for HLV per current policy; reviewer should know that GTI was
    # mechanically wiped to NIL by the loss and decide whether to
    # follow the strict GTI read or the lenient earned-head read.
    # Sourced from case_json.income.itr_years[] (agent-level path; the
    # extraction_blocks copy lives in decision_json after engine.run).
    income_block = (case.get("income") or {}) if case else {}
    itr_years = income_block.get("itr_years") or []
    flagged_ays: list[dict] = []
    for itr in itr_years:
        if not isinstance(itr, dict):
            continue
        gti = _safe_int(itr.get("gross_total_income"))
        earned = _safe_int(itr.get("earned_total") or itr.get("earned_subtotal"))
        # Trigger: GTI is < 25% of earned heads (large delta = real
        # loss absorbing earned income), AND earned > ₹3L (filter noise).
        if earned >= 300_000 and gti < earned * 0.25 and gti < earned - 500_000:
            flagged_ays.append(
                {
                    "ay": itr.get("assessment_year"),
                    "gti": gti,
                    "earned": earned,
                    "delta": earned - gti,
                    "note": (itr.get("notes") or "")[:300],
                }
            )
    if flagged_ays:
        worst = max(flagged_ays, key=lambda x: x["delta"])
        ay_list = ", ".join(
            f"AY {x['ay']}: earned ₹{x['earned']:,} / GTI ₹{x['gti']:,}" for x in flagged_ays[:3]
        )
        out.append(
            Flag(
                code="INCOME_LOSSES_SET_OFF",
                severity="MEDIUM",
                title="ITR shows losses set off against earned income",
                description=(
                    f"Earned head(s) reported but GTI is materially lower because "
                    f"of speculative / business losses absorbing the earned heads. "
                    f"Engine used earned_total ₹{worst['earned']:,} for HLV "
                    f"(stable income view); strict GTI read would be "
                    f"₹{worst['gti']:,}. Affected years: {ay_list}. "
                    f"UW: decide whether to follow strict GTI (loss-absorbed) or "
                    "earned-head (stable-stream) interpretation for HLV; if losses "
                    "are recurring, consider rate-up or reduced cover."
                ),
                function="underwriting_platform.engine.flags.flags_from_calculations",
                rule="ITR earned_total >= ₹3L AND gross_total_income < earned × 0.25",
                sources=(calcs.get("selected_income") or Calculation(name="x", value=0)).sources,
            )
        )

    # Low-slip-divergence deferral marker — the engine deferred to the
    # agent's annual because the 1.5×-median outlier rule was statistically
    # weak on ≤2 surviving slips AND the engine recompute diverged from
    # the agent's contextual read by >10%. Surface so the UW reviewer can
    # confirm against appointment letter / ITR / bank statements.
    sel = calcs.get("selected_income")
    if sel is not None:
        inp = sel.inputs or {}
        if inp.get("low_slip_deferred"):
            engine_v = inp.get("engine_recomputed_annual")
            div_pct = inp.get("divergence_pct")
            n_kept = (inp.get("salary_slip_audit") or {}).get("n_kept", 0)
            out.append(
                Flag(
                    code="INCOME_LOW_SLIP_DEFERRED_TO_AGENT",
                    severity="MEDIUM",
                    title="Income — engine deferred to agent on low-slip sample",
                    description=(
                        f"Engine deferred its salary-slip mean recompute "
                        f"(₹{engine_v:,}) to the agent's annual "
                        f"(₹{income_val:,}). Only {n_kept} slip(s) survived "
                        f"the 1.5×-median outlier filter, and engine vs agent "
                        f"diverged by {div_pct}% — outlier rule is "
                        "statistically weak at this sample size. Likely "
                        "causes: an arrears month not flagged by numeric "
                        "magnitude, OR a pre-increment slip pulling the "
                        "average. Confirm against appointment letter / "
                        "ITR / bank statements before relying on HLV."
                    ),
                    function="underwriting_platform.engine.flags.flags_from_calculations",
                    rule="selected_income.inputs.low_slip_deferred == True",
                    sources=sel.sources,
                )
            )

    # ITR headline-gap awareness marker — the engine used the eligible EARNED
    # income (salary + business/profession; passive excluded per TALIC policy
    # 2026-06-07), but the agent's headline annual_income was materially higher
    # (it rolled in passive income — rent/interest/dividends/cap-gains — or a
    # recent ITR is acknowledgement-only / lacks COI). Surface for awareness.
    if sel is not None:
        inp = sel.inputs or {}
        if inp.get("itr_headline_gap_pct") is not None:
            gap = inp.get("itr_headline_gap_pct")
            excl = _safe_int(inp.get("itr_headline_excluded"))
            agent_v = _safe_int(inp.get("agent_reported_annual"))
            out.append(
                Flag(
                    code="INCOME_ITR_HEADLINE_GAP",
                    severity="MEDIUM",
                    title="Income — earned ITR figure differs from agent headline",
                    description=(
                        f"Engine used eligible earned income ₹{income_val:,} "
                        f"(salary + business/profession only; passive income "
                        f"excluded per policy). The agent headline was "
                        f"₹{agent_v:,} ({gap:+.0f}% / ₹{excl:,} higher) — usually "
                        "passive income (house-property rent / interest / "
                        "dividends / capital gains) that is NOT counted, or a "
                        "recent ITR is acknowledgement-only / lacks Computation "
                        "of Income. Verify the per-year earned heads against the "
                        "filed ITRs."
                    ),
                    function="underwriting_platform.engine.flags.flags_from_calculations",
                    rule="selected_income.inputs.itr_headline_gap_pct present",
                    sources=sel.sources,
                )
            )

    # Sanity check — salaried incomes ≥ ₹2 Cr that don't come from FX
    # or audited financials warrant a comma-misread check. ₹29,10,610
    # read as 29,106,100 (≈₹2.91 Cr) is a common 10× error.
    if income_val >= 20_000_000:
        sel = calcs.get("selected_income") or Calculation(name="x", value=0)
        method = (sel.method or "").lower()
        from_fx = "_fx_inr" in method
        from_audpl = method.startswith("audited")
        from_keyman = method.startswith("audited") or "keyman" in method
        if not from_fx and not from_audpl:
            sev = "HIGH" if income_val >= 50_000_000 else "MEDIUM"
            out.append(
                Flag(
                    code="INCOME_SANITY_HIGH",
                    severity=sev,
                    title=f"Verified income ≥ ₹2 Cr — sanity-check for Indian-comma misread",
                    description=(
                        f"selected_income = ₹{income_val:,} (method={method}). At this level "
                        "the applicant is senior leadership / promoter-director — OR the agent "
                        "has miscounted Indian commas (₹29,10,610 read as 29,106,100 → 10× "
                        "error). Confirm against the printed lakh/crore tag on the source "
                        "document."
                    ),
                    function="underwriting_platform.engine.flags.flags_from_calculations",
                    rule="selected_income.value >= 20_000_000 AND method ∉ {fx, audited_pl}",
                    sources=sel.sources,
                )
            )

    # ── Medical-derived flags (Phase 8b) ───────────────────────────
    fraud = calcs.get("fraud_risk")
    if fraud is not None and str(fraud.value or "LOW").upper() == "HIGH":
        out.append(
            Flag(
                code="COTININE_SMOKER_MISMATCH",
                severity="CRITICAL",
                title="Smoker fraud — Cotinine positive but declared non-smoker",
                description=fraud.note or "Cotinine ≥10 ng/mL but declared smoker = No.",
                function="underwriting_platform.engine.flags.flags_from_calculations",
                rule="medical_rules.MEDICAL_GRID.special_rules",
                sources=fraud.sources,
            )
        )

    bmi = calcs.get("bmi_band")
    if bmi is not None:
        cat = (bmi.inputs or {}).get("category", "")
        if cat == "obese":
            out.append(
                Flag(
                    code="BMI_OBESE",
                    severity="HIGH",
                    title="BMI in obese range",
                    description=f"BMI {bmi.value} → obese category.",
                    function="underwriting_platform.engine.flags.flags_from_calculations",
                    rule="uw_rules.bmi_bands.overweight_max",
                    sources=bmi.sources,
                )
            )
        elif cat == "underweight":
            out.append(
                Flag(
                    code="BMI_UNDERWEIGHT",
                    severity="MEDIUM",
                    title="BMI in underweight range",
                    description=f"BMI {bmi.value} → underweight category.",
                    function="underwriting_platform.engine.flags.flags_from_calculations",
                    rule="uw_rules.bmi_bands.underweight_max",
                    sources=bmi.sources,
                )
            )

        # Advisory: BMI rests on handwritten box-grid digits on the MER —
        # vision OCR can flip 4↔7, 1↔7, 0↔6/9. Always emit a LOW flag so
        # underwriters know to eyeball the source PDF, especially when the
        # value sits near a band boundary.
        h_cm = _safe_float((bmi.inputs or {}).get("height_cm"))
        w_kg = _safe_float((bmi.inputs or {}).get("weight_kg"))
        bmi_val = _safe_float(bmi.value)
        if bmi_val > 0 and h_cm > 0 and w_kg > 0:
            near_boundary = (
                23.5 <= bmi_val <= 26.0  # straddling normal ↔ overweight
                or 28.5 <= bmi_val <= 30.5  # straddling overweight ↔ obese
                or 17.5 <= bmi_val <= 19.0  # straddling underweight ↔ normal
            )
            boundary_note = (
                " Value sits near a category boundary — "
                "a single-digit misread would change the BMI band."
                if near_boundary
                else ""
            )
            out.append(
                Flag(
                    code="BMI_HANDWRITTEN_DIGITS",
                    severity="LOW",
                    title="Verify height/weight on source MER (handwritten)",
                    description=(
                        f"BMI {bmi_val} computed from MER height={h_cm} cm / "
                        f"weight={w_kg} kg — both hand-printed in box-grid fields. "
                        "Vision-OCR can flip handwritten 4↔7, 1↔7, 0↔6/9. "
                        "Eyeball the digits on the MER scan before relying on BMI." + boundary_note
                    ),
                    function="underwriting_platform.engine.flags.flags_from_calculations",
                    rule="underwriting_platform.engine.flags — handwritten-digit-source advisory on MER vitals",
                    sources=bmi.sources,
                )
            )

            # Re-OCR cross-check: when a focused 2nd-pass disagrees with
            # the medical agent's primary read, escalate. This is the
            # actual safety net — the prompt + LOW advisory above only
            # tell the underwriter to look; this tells them WHAT to look for.
            if case is not None:
                reocr = (case.get("medical") or {}).get("vitals_reocr") or {}
                rv = reocr.get("values") or {}
                rh = _safe_float(rv.get("height_cm"))
                rw = _safe_float(rv.get("weight_kg"))
                if rh > 0 and rw > 0:
                    h_diff_cm = abs(rh - h_cm)
                    w_diff_kg = abs(rw - w_kg)
                    h_diff_pct = h_diff_cm / max(h_cm, 1.0) * 100.0
                    w_diff_pct = w_diff_kg / max(w_kg, 1.0) * 100.0
                    max_diff = max(h_diff_pct, w_diff_pct)
                    # Trigger: ≥2% relative OR ≥2 cm absolute height OR
                    # ≥1 kg absolute weight. Sub-2% deltas were missing
                    # the C278817070 class of misread (180.34 → 177 cm,
                    # 1.85% — would shift BMI band on a heavier adult).
                    abs_floor_tripped = (h_diff_cm >= 2.0) or (w_diff_kg >= 1.0)
                    if max_diff >= 2.0 or abs_floor_tripped:
                        sev = "HIGH" if max_diff >= 10.0 else "MEDIUM"
                        try:
                            rbmi = round(rw / ((rh / 100.0) ** 2), 1)
                        except Exception:
                            rbmi = None
                        out.append(
                            Flag(
                                code="VITALS_REOCR_DISAGREEMENT",
                                severity=sev,
                                title="Vitals re-OCR disagrees with primary read",
                                description=(
                                    f"Primary read: height={h_cm} cm, "
                                    f"weight={w_kg} kg, BMI={bmi_val}. "
                                    f"Secondary re-OCR ({reocr.get('source_pdf')} "
                                    f"p{reocr.get('source_page')}): "
                                    f"height={rh} cm, weight={rw} kg, BMI={rbmi}. "
                                    f"Δ height={abs(rh - h_cm):.1f} cm / "
                                    f"weight={abs(rw - w_kg):.1f} kg "
                                    f"(max {max_diff:.1f}%). "
                                    "Open the source MER and pick the correct read."
                                ),
                                function="underwriting_platform.engine.flags.flags_from_calculations",
                                rule=(
                                    "vitals_reocr disagreement — MEDIUM when "
                                    "max%≥2 OR Δh≥2cm OR Δw≥1kg; HIGH when "
                                    "max%≥10"
                                ),
                                sources=bmi.sources,
                            )
                        )

    abn = calcs.get("lab_abnormalities")
    if abn is not None:
        n_abn = _safe_int(abn.value)
        if n_abn >= 5:
            out.append(
                Flag(
                    code="MULTIPLE_LAB_ABNORMALITIES",
                    severity="HIGH",
                    title=f"{n_abn} abnormal lab results",
                    description=f"{n_abn} lab rows flagged HIGH/LOW/ABNORMAL across categories.",
                    function="underwriting_platform.engine.flags.flags_from_calculations",
                    rule="lab_results[].status ∈ {HIGH, LOW, ABNORMAL}",
                    sources=abn.sources,
                )
            )

    med_risk = calcs.get("medical_risk")
    if med_risk is not None and str(med_risk.value or "").upper() == "HIGH":
        out.append(
            Flag(
                code="MEDICAL_RISK_HIGH",
                severity="HIGH",
                title="Medical risk band: HIGH",
                description=med_risk.note or "Composite medical risk band = HIGH.",
                function="underwriting_platform.engine.flags.flags_from_calculations",
                rule=med_risk.rule,
                sources=med_risk.sources,
            )
        )

    comp = calcs.get("test_compliance_pct")
    if comp is not None:
        pct = _safe_float(comp.value)
        if pct < 50:
            out.append(
                Flag(
                    code="MEDICAL_DOCS_INCOMPLETE",
                    severity="HIGH",
                    title=f"Medical document compliance only {int(pct)}%",
                    description=f"Missing: {', '.join((comp.inputs or {}).get('missing') or [])}",
                    function="underwriting_platform.engine.flags.flags_from_calculations",
                    rule="medical_rules.MEDICAL_GRID grid lookup",
                    sources=comp.sources,
                )
            )
    return out


# Medical-axis checks are out of scope for an SBI Financial Underwriting
# case (no medical documents are ever collected for this case-sheet type
# by design) — same scope as engine.verdict._SBI_OUT_OF_SCOPE_CRITERIA /
# renderer.triage_sheet._NON_PROFILE_CRITERIA. Left unfixed here, these
# flags independently re-derive the SAME medical/generic-income findings
# already excluded from the gating-based verdict count (GATE_* flags are
# generated 1:1 from gating items, MEDICAL_DOCS_INCOMPLETE/
# MEDICAL_RISK_HIGH from the calcs block directly) and still drove
# fc["HIGH"] >= 4 → financial_risk=HIGH / REFER even after that fix.
# Found via cases SYNTHETIC_CASE/526606/526688, still REFER post-gating-fix
# (2026-10-01 client QC review).
_SBI_OUT_OF_SCOPE_FLAG_CODES = {
    "GATE_MEDICAL_COMPLIANCE",
    "GATE_FRAUD_SIGNAL",
    "GATE_SBI_PIWC_REQUIREMENT",
    "MEDICAL_DOCS_INCOMPLETE",
    "MEDICAL_RISK_HIGH",
    # FOIR_OVER_100 is a CRITICAL flag keyed off a 100%-of-gross-income
    # sanity check that is not an SBI document rule (no SBI reference
    # defines any FOIR threshold at all — see engine.gating.check_foir and
    # engine.calculator.compute_foir, both already SBI-neutral). A CRITICAL
    # flag with no match in verdict.make_verdict's
    # _RESOLVABLE_CRITICAL_PATTERNS drives an unconditional DECLINE, so
    # leaving this unexcluded would let a non-SBI-sourced threshold decide
    # an SBI case outright. Found via full TATA-dependency audit
    # (2026-10-10) — not hit by any case on file today (none have
    # FOIR >= 100%), but structurally live for any SBI case that did.
    "FOIR_OVER_100",
}


def derive_all_flags(case: dict, calcs: dict, gating: list[GatingItem], rules: dict) -> list[Flag]:
    flags: list[Flag] = []
    flags.extend(flags_from_calculations(calcs, rules, case))
    flags.extend(flags_from_gating(gating))
    flags.extend(flags_from_reconciliation(case))
    flags.extend(_flags_from_dual_vendor(case))
    flags.extend(_flags_from_family_history_reocr(case))
    flags.extend(_flags_from_identity_verification(case))

    if is_sbi_case(case):
        exclude_codes = set(_SBI_OUT_OF_SCOPE_FLAG_CODES)
        sbi_qual = next((g for g in gating if g.criterion == "sbi_qualification"), None)
        if sbi_qual is not None and sbi_qual.status == "PASS":
            exclude_codes.add("GATE_MIN_INCOME_FOR_SA")
        flags = [
            f
            for f in flags
            if f.code not in exclude_codes and not f.code.startswith("RECON_MEDICAL")
        ]

    # Stable sort by severity (CRITICAL → HIGH → MEDIUM → LOW)
    sev_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    flags.sort(key=lambda f: sev_rank.get(f.severity, 99))
    return flags
