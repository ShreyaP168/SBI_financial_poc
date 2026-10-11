"""
engine/calculator.py — provenance-emitting calculations over the v9 case_json.

Each public function returns a `Calculation` (engine.types.Calculation) whose
`function` field is the qualified callable name, and whose `sources` list
points back to the agent blocks the value originated from.

Inputs:
  - case_json   : the merged v9 case_json (applicant + income + assets + ...)
  - rules       : uw_rules.json
  - extras (kw) : sometimes a previously-computed Calculation, e.g. compute_hlv
                   takes the selected_income Calculation so its formula text
                   and sources flow downstream.
"""

from __future__ import annotations
from underwriting_platform.configuration.paths import CONFIG_ROOT
from datetime import date, datetime
from typing import Any

from .types import Calculation, Source


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _safe_int(v: Any) -> int:
    try:
        if v is None or v == "":
            return 0
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _safe_float(v: Any) -> float:
    try:
        if v is None or v == "":
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _parse_dob(dob: str | None) -> date | None:
    if not dob:
        return None
    # Tolerate datetime strings ("2025-09-10 00:00:00", ISO "…T…") — policy_data
    # date columns serialise as str(datetime), so keep only the date part.
    s = str(dob).strip().replace("T", " ").split(" ")[0]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except (TypeError, ValueError):
            continue
    return None


def _age_from_dob(dob_str: str | None, ref: date | None = None) -> int | None:
    dob = _parse_dob(dob_str)
    if dob is None:
        return None
    ref = ref or date.today()
    years = ref.year - dob.year - ((ref.month, ref.day) < (dob.month, dob.day))
    return years


def _income_sources_from_agent(income_block: dict) -> list[Source]:
    """Convert income agent's `income_sources[]` strings into Source records."""
    out: list[Source] = []
    for s in income_block.get("income_sources") or []:
        if isinstance(s, str) and s.strip():
            out.append(
                Source(document=s.strip(), agent="income", field_path="income.income_sources[]")
            )
    return out


def _bank_sources(assets_block: dict) -> list[Source]:
    out: list[Source] = []
    for bs in assets_block.get("bank_statements") or []:
        bank = bs.get("bank_name") or "Bank"
        acct = bs.get("account_number") or ""
        period = f"{bs.get('period_from') or '?'}..{bs.get('period_to') or '?'}"
        out.append(
            Source(
                document=f"{bank} {acct} ({period})",
                agent="assets_liabilities",
                field_path="assets_liabilities.bank_statements[]",
            )
        )
    return out


# ──────────────────────────────────────────────────────────────────────────
# Age + age band
# ──────────────────────────────────────────────────────────────────────────


def compute_age(case: dict) -> Calculation:
    """Resolve the applicant's **age at entry** — the age the SI / policy system
    records, NOT the age as of the processing date.

    Cascade (first hit wins):
      1. ``policy_data.age_at_entry`` — TALIC's system-of-record Age at Entry
         (Age Last Birthday at risk commencement). This is exactly the figure
         the SI prints and QC reconciles against.
      2. ALB from ``applicant.dob`` anchored to the risk-commencement date
         (``policy_data.effective_dt`` → ``submission_dt``).
      3. ALB from ``applicant.dob`` anchored to the proposal form's own
         signature/submission date (``applicant.submission_date``) — covers
         insurers with no Policy_Data.xlsx system-of-record join (e.g. the
         SBI flow), where step 2's anchor is never available.
      4. ALB from ``applicant.dob`` as of today (legacy fallback — only when
         no policy date AND no proposal-form date is on file).
      5. ``applicant.age`` (declared).

    Root cause this guards (verified 2026-06-06): step-4 was the *only* path, so
    once a birthday fell between the effective date and the run date the engine
    read +1 — the dominant QC finding in the 2026-06-06 batch. Extended
    2026-06-25 (Gap 3, case SYNTHETIC_CASE): the same bug recurs for any insurer
    with no Policy_Data.xlsx — step 3 closes that gap using the proposal
    form's own date instead of falling straight through to "today".
    """
    applicant = case.get("applicant") or {}
    pd_block = case.get("policy_data") or {}
    dob = applicant.get("dob")
    declared = applicant.get("age")

    age_at_entry = _safe_int(pd_block.get("age_at_entry"))
    anchor_raw = pd_block.get("effective_dt") or pd_block.get("submission_dt")
    anchor = _parse_dob(anchor_raw)
    proposal_anchor_raw = applicant.get("submission_date")
    proposal_anchor = _parse_dob(proposal_anchor_raw)

    if 0 < age_at_entry < 100:
        age, method = age_at_entry, "policy_data.age_at_entry"
        formula = (
            "policy_data.AGE_AT_ENTRY (SI age at entry — Age Last Birthday at risk commencement)"
        )
        anchor_used = "policy_data.age_at_entry"
    elif anchor is not None and _age_from_dob(dob, anchor) is not None:
        age, method = _age_from_dob(dob, anchor), "from_dob_at_commencement"
        formula = "(risk-commencement date − applicant.dob) in years (Age Last Birthday)"
        anchor_used = str(anchor_raw)
    elif proposal_anchor is not None and _age_from_dob(dob, proposal_anchor) is not None:
        age, method = _age_from_dob(dob, proposal_anchor), "from_dob_at_proposal_date"
        formula = (
            "(proposal form signature/submission date − applicant.dob) in years (Age Last Birthday)"
        )
        anchor_used = str(proposal_anchor_raw)
    elif _age_from_dob(dob) is not None:
        age, method = _age_from_dob(dob), "from_dob_today"
        formula = "(today − applicant.dob) in years"
        anchor_used = "today"
    else:
        age, method = _safe_int(declared), "from_declared_age"
        formula = "applicant.age (declared)"
        anchor_used = None

    return Calculation(
        name="age",
        value=age,
        unit="years",
        method=method,
        function="underwriting_platform.engine.calculator.compute_age",
        formula=formula,
        inputs={
            "dob": dob,
            "declared_age": declared,
            "age_at_entry": pd_block.get("age_at_entry"),
            "proposal_submission_date": proposal_anchor_raw,
            "anchor_date": anchor_used,
        },
        sources=[
            Source(document=f"Applicant DOB={dob}", agent="identity", field_path="applicant.dob"),
            Source(
                document="Policy_Data.xlsx AGE_AT_ENTRY",
                agent="policy_data",
                field_path="policy_data.age_at_entry",
            ),
        ],
        rule="Age at entry = Age Last Birthday at risk commencement (SI basis)",
        note="Age anchored to the SI entry age, not the processing date.",
    )


_AGE_BANDS = ["18-30", "31-35", "36-40", "41-45", "46-50", "51-55", "56-60", "61-65"]


def get_age_band(age: int | None) -> str | None:
    if age is None:
        return None
    for band in _AGE_BANDS:
        lo, hi = map(int, band.split("-"))
        if lo <= age <= hi:
            return band
    if age > 65:
        return "61-65"
    return None


# ──────────────────────────────────────────────────────────────────────────
# FX conversion (config/fx_rates.json)
# ──────────────────────────────────────────────────────────────────────────

_FX_CACHE: dict | None = None


def _load_fx_rates() -> dict:
    """Read config/fx_rates.json once. Returns the rates_to_inr dict +
    aliases + country→currency map. Falls back to {INR: 1.0} if missing."""
    global _FX_CACHE
    if _FX_CACHE is not None:
        return _FX_CACHE
    from pathlib import Path

    p = CONFIG_ROOT / "fx_rates.json"
    if not p.exists():
        _FX_CACHE = {"rates": {"INR": 1.0}, "aliases": {}, "country_map": {}, "updated": None}
        return _FX_CACHE
    try:
        import json

        data = json.loads(p.read_text(encoding="utf-8"))
        _FX_CACHE = {
            "rates": dict(data.get("rates_to_inr") or {"INR": 1.0}),
            "aliases": {
                k.upper(): v.upper()
                for k, v in (data.get("_aliases") or {}).items()
                if isinstance(v, str)
            },
            "country_map": {
                k.upper(): v.upper()
                for k, v in (data.get("_country_to_currency") or {}).items()
                if isinstance(v, str)
            },
            "updated": data.get("_updated"),
        }
    except Exception:
        _FX_CACHE = {"rates": {"INR": 1.0}, "aliases": {}, "country_map": {}, "updated": None}
    return _FX_CACHE


def _resolve_currency(code: str | None) -> str:
    """Normalize a currency string ('AED'/'Dirham'/'aed-dubai'/...) to ISO."""
    if not code:
        return "INR"
    s = str(code).strip().upper()
    fx = _load_fx_rates()
    if s in fx["rates"]:
        return s
    return fx["aliases"].get(s, s)


def _convert_to_inr(amount: float, currency: str | None) -> tuple[int, dict]:
    """Convert `amount` in `currency` to INR. Returns (inr_int, audit_dict).
    If currency is INR or unknown, returns (round(amount), audit) unchanged."""
    fx = _load_fx_rates()
    iso = _resolve_currency(currency)
    rate = float(fx["rates"].get(iso, 1.0))
    inr_value = int(round(_safe_float(amount) * rate))
    audit = {
        "original_amount": round(_safe_float(amount), 2),
        "original_currency": iso,
        "rate_to_inr": rate,
        "inr_value": inr_value,
        "rates_updated": fx.get("updated"),
        "applied": iso != "INR" and iso in fx["rates"],
    }
    return inr_value, audit


# ──────────────────────────────────────────────────────────────────────────
# Income selection
# ──────────────────────────────────────────────────────────────────────────

_PRIMARY_SOURCE_LABELS = {
    "salary_slip": "Salary Slip (3-month average)",
    "form_16": "Form 16",
    "itr": "Income Tax Return (earned heads only)",
    "ca_computation": "CA Computation Statement",
    "audited_financials": "Audited Financials (P&L + Balance Sheet)",
    "bank_credits": "Bank Salary Credits (annualised)",
    "declared": "Declared Income (CAF) — soft signal only",
}

# Cascade priority — lower index wins.
_PRIMARY_PRIORITY = [
    "salary_slip",
    "form_16",
    "ca_computation",
    "audited_financials",
    "itr",
    "bank_credits",
    "declared",
]


def _salary_slip_mean_annual(slips: list[dict]) -> tuple[int, dict] | None:
    """Deterministic salary-slip annualization.

    Per TATA AIA Term UW guideline (Feb-2026 page 4 + List of Standard
    Income Proof) — a "salary slip" income picks the MEAN gross of the
    submitted months × 12, BUT excludes one-off outliers where a single
    month's gross > 1.5× the median (typically arrears/back-pay) so we
    don't over-count. LTA/bonus months that are ≤ 1.5× median stay in.

    Returns (annual_int, audit_dict) or None if not enough clean slips.
    """
    if not slips:
        return None
    rows = []
    for s in slips:
        if not isinstance(s, dict):
            continue
        g = _safe_float(s.get("gross_salary"))
        if g <= 0:
            continue
        rows.append(
            {
                "month_year": s.get("month_year") or "",
                "gross_salary": int(round(g)),
                "net_salary": _safe_int(s.get("net_salary")),
                "employer": s.get("employer"),
            }
        )
    if len(rows) < 2:
        return None

    # Median by gross_salary
    sorted_g = sorted(r["gross_salary"] for r in rows)
    n = len(sorted_g)
    median_g = sorted_g[n // 2] if n % 2 == 1 else (sorted_g[n // 2 - 1] + sorted_g[n // 2]) / 2

    # Outlier rule: gross > 1.5 × median is treated as one-off arrears
    excluded: list[dict] = []
    kept: list[dict] = []
    cutoff = median_g * 1.5
    for r in rows:
        if r["gross_salary"] > cutoff:
            excluded.append({**r, "reason": "gross > 1.5 × median (likely arrears)"})
        else:
            kept.append(r)

    if not kept:
        # All flagged outlier — fall back to median × 12
        annual = int(median_g * 12)
        audit = {
            "method": "median_x_12_fallback",
            "n_slips": len(rows),
            "median_gross": int(median_g),
            "all_grosses": [r["gross_salary"] for r in rows],
            "kept": [],
            "excluded": excluded,
            "annual_gross": annual,
        }
        return annual, audit

    mean_kept = sum(r["gross_salary"] for r in kept) / len(kept)
    annual = int(mean_kept * 12)
    audit = {
        "method": "mean_of_clean_slips_x_12",
        "n_slips": len(rows),
        "n_kept": len(kept),
        "n_excluded": len(excluded),
        "median_gross": int(median_g),
        "outlier_cutoff_gross": int(cutoff),
        "kept": kept,
        "excluded": excluded,
        "mean_kept": int(round(mean_kept)),
        "annual_gross": annual,
    }
    return annual, audit


def _bank_credits_modal_annual(credits: list[dict]) -> tuple[int, dict] | None:
    """Deterministic bank-credits annualization.

    Algorithm:
      1. Filter to positive-amount credits with 'salary'-like narration or
         from a frequently-recurring employer (3+ credits).
      2. Group by employer name (NEFT/CMS narration's primary string).
      3. Pick the employer with the most credits ⇒ "primary employer".
      4. From that employer's amounts, take the MODAL value (most repeated).
      5. annual_income = modal_amount × 12.
      6. monthly_gross = modal_amount.

    Returns (annual_int, audit_dict) or None if no clean signal.
    """
    if not credits:
        return None
    pos = [c for c in credits if isinstance(c, dict) and (_safe_int(c.get("amount")) or 0) > 0]
    if not pos:
        return None

    def _employer_key(c: dict) -> str:
        # Use the agent-extracted `employer` if present; else fall back to
        # the leading token of the narration.
        emp = (c.get("employer") or "").strip()
        if emp:
            return emp.upper()
        narr = (c.get("narration") or "").upper()
        return narr.split("/")[2] if "/" in narr and len(narr.split("/")) > 2 else narr[:40]

    by_emp: dict[str, list[dict]] = {}
    for c in pos:
        by_emp.setdefault(_employer_key(c), []).append(c)
    if not by_emp:
        return None
    primary_emp = max(by_emp, key=lambda e: len(by_emp[e]))
    primary_credits = by_emp[primary_emp]
    if len(primary_credits) < 2:
        return None  # not enough recurrence to trust

    amounts = [int(round(_safe_float(c.get("amount")))) for c in primary_credits]
    # Modal amount — most-frequently-occurring value
    counts: dict[int, int] = {}
    for a in amounts:
        counts[a] = counts.get(a, 0) + 1
    modal_amount = max(counts, key=counts.get)
    n_modal = counts[modal_amount]
    if n_modal < 2:
        # Highly variable salary — fall back to median of all primary credits
        amounts_sorted = sorted(amounts)
        modal_amount = amounts_sorted[len(amounts_sorted) // 2]
        n_modal = 1
    annual = int(modal_amount * 12)
    audit = {
        "method": "modal_amount_x_12",
        "primary_employer": primary_emp,
        "n_credits_primary": len(primary_credits),
        "amounts": amounts,
        "modal_amount": modal_amount,
        "modal_count": n_modal,
        "annual_net": annual,
    }
    return annual, audit


def _itr_year_sort_key(y: dict) -> int:
    import re

    m = re.match(r"\s*(\d{4})", str(y.get("assessment_year") or ""))
    return int(m.group(1)) if m else 0


def _itr_is_salaried(years: list[dict], applicant: dict, income: dict) -> bool:
    """Salaried (ITR-1, salary-only) → latest year; else self-employed /
    business / professional (ITR-3/4) → 2-year average.

    The guideline trigger is the ITR form (salary-only ITR-1 vs ITR-3/4 with
    Computation of Income). Best deterministic proxy is salary dominance;
    a declared `income.is_salaried`, business head, and occupation label are
    the explicit override / tiebreakers.
    """
    flag = income.get("is_salaried")
    if isinstance(flag, bool):
        return flag
    # Any business head ⇒ ITR-3/4 ⇒ not salaried.
    if any(_safe_int(y.get("business_head")) > 0 for y in years):
        return False
    # Salary dominance across the latest (up to 2) usable years — an ITR-1
    # salary-only filing has salary ≈ the whole earned total.
    fracs = []
    for y in years[:2]:
        earned = _safe_int(y.get("earned_total")) or _safe_int(y.get("gross_total_income"))
        sal = _safe_int(y.get("salary_head"))
        if earned > 0:
            fracs.append(sal / earned)
    if fracs:
        return (sum(fracs) / len(fracs)) >= 0.85
    # No head detail — fall back to the occupation label.
    occ = (applicant.get("occupation") or "").lower()
    if "salar" in occ and not any(k in occ for k in ("self", "business", "profession", "propriet")):
        return True
    return False


def _itr_eligibility_annual(
    years: list[dict], applicant: dict, income: dict
) -> tuple[int, dict] | None:
    """Eligibility income from ITR per Term UW V5 Feb-26 p20.

    Self-employed/business/professional → average of latest 2 years' earned
    income; salaried → latest year. Per-year earned = `earned_total` when
    present (else `gross_total_income`). Returns (annual_int, audit) or None.
    """
    if not years:
        return None
    ys = sorted(years, key=_itr_year_sort_key, reverse=True)
    usable: list[tuple[str, int, int]] = []  # (ay, earned, gti)
    inconsistent: list[str] = []
    seen_years: dict[str, int] = {}
    duplicate_years: list[str] = []
    # REVERTED 2026-10-06. This function briefly preferred a narrower
    # "earned = salary + business/profession only, passive excluded"
    # figure (citing "Term UW V5 Feb-26 p20") over gross_total_income,
    # intending to fix cases SYNTHETIC_CASE/SYNTHETIC_CASE (ITR-2 filers whose
    # GTI includes interest/dividends/capital-gains/commission). That
    # premise was WRONG: the actual client QC spreadsheet's "Datamatics
    # summary" column records BOTH cases' income as the original
    # GTI-based ₹9,61,560 figure, with "FINANCIAL ELIGIBILITY MET" / "CASE
    # OK TO ACCEPT" / AI Output Result "CORRECT" — i.e. SBI's own
    # underwriters confirmed the GTI-based figure as correct, not the
    # narrower earned-only figure. Restored the original behaviour (GTI
    # fallback) rather than ship a "fix" that overrides confirmed,
    # human-verified QC outcomes.
    for y in ys:
        gti = _safe_int(y.get("gross_total_income"))
        raw_earned = _safe_int(y.get("earned_total"))
        # earned = salary + business/profession by definition (Term UW V5
        # p20). Use the heads when present so the figure is internally
        # consistent with its displayed components — the agent's earned_total
        # can carry an arithmetic slip (e.g. C048084622 AY22-23: 11,44,603 vs
        # 6,70,000 + 4,74,388 = 11,44,388). The >GTI data-quality flag below
        # still keys off the agent's RAW earned_total.
        _sal = _safe_int(y.get("salary_head"))
        _biz = _safe_int(y.get("business_head"))
        earned = (_sal + _biz) if (_sal > 0 or _biz > 0) else raw_earned
        if earned <= 0:
            earned = gti
        if earned <= 0:
            continue
        assessment_year = str(y.get("assessment_year"))
        if assessment_year in seen_years:
            if seen_years[assessment_year] != earned:
                raise ValueError(
                    f"Conflicting ITR earned income for assessment year {assessment_year}; verify source documents"
                )
            duplicate_years.append(assessment_year)
            continue
        seen_years[assessment_year] = earned
        if gti > 0 and raw_earned > gti:
            inconsistent.append(str(y.get("assessment_year")))
        usable.append((str(y.get("assessment_year")), earned, gti))
    if not usable:
        return None
    salaried = _itr_is_salaried(ys, applicant, income)
    if salaried:
        used, basis = usable[:1], "latest_year"
    elif len(usable) < 2:
        used, basis = usable[:1], "latest_year_single_itr"
    else:
        used, basis = usable[:2], "avg_latest_2_years"
    annual = int(round(sum(e for _, e, _ in used) / len(used)))
    audit = {
        "basis": basis,
        "is_salaried": salaried,
        "n_years_available": len(usable),
        "years_used": [a for a, _, _ in used],
        "per_year_earned": {a: e for a, e, _ in used},
        "latest_year_earned": usable[0][1],
        "earned_gt_gti_years": inconsistent,
        "annual": annual,
        "duplicate_years_ignored": duplicate_years,
    }
    return annual, audit


def normalize_primary_source(value) -> str:
    source = str(value or "").strip().lower().replace(" ", "_")
    return {
        "salary_slips": "salary_slip",
        "payslips": "salary_slip",
        "payslip": "salary_slip",
        "declared_only": "declared",
        "form16": "form_16",
        "income_tax_returns": "itr",
    }.get(source, source)


def compute_selected_income(case: dict) -> Calculation:
    """Pick income from the agent block, with deterministic recompute when
    primary_source = bank_credits (the LLM's monthly-aggregation has been
    observed to drift; we trust the raw bank_salary_credits[] list and
    apply modal-amount × 12)."""
    income = case.get("income") or {}
    primary = normalize_primary_source(income.get("primary_source"))
    label = _PRIMARY_SOURCE_LABELS.get(primary, primary or "unknown")
    annual = _safe_int(income.get("annual_income"))
    sources = _income_sources_from_agent(income)
    rung_idx = _PRIMARY_PRIORITY.index(primary) + 1 if primary in _PRIMARY_PRIORITY else None

    method = primary or "unknown"
    formula = f"income.annual_income from {label}"
    extra_inputs: dict = {}
    note = "Lower cascade rung = stronger evidence. 1=salary_slip, 7=declared (soft)."

    # Deterministic override for bank_credits — the agent's monthly aggregation
    # has been observed to drift (different runs include partial months / IMPS
    # transfers inconsistently). Recompute from raw bank_salary_credits[].
    if primary == "bank_credits":
        bank_recompute = _bank_credits_modal_annual(income.get("bank_salary_credits") or [])
        if bank_recompute is not None:
            recomputed_annual, audit = bank_recompute
            extra_inputs["agent_reported_annual"] = annual
            extra_inputs["bank_credits_audit"] = audit
            if annual != recomputed_annual:
                note = (
                    f"Engine recomputed: modal salary credit "
                    f"₹{audit['modal_amount']:,} × 12 = "
                    f"₹{recomputed_annual:,} from {audit['n_credits_primary']} "
                    f"credits by {audit['primary_employer']}. "
                    f"Agent had reported ₹{annual:,}; "
                    f"engine value takes precedence."
                )
            else:
                note = (
                    f"Modal salary credit ₹{audit['modal_amount']:,} × 12 = "
                    f"₹{recomputed_annual:,} (matches agent)."
                )
            annual = recomputed_annual
            method = "bank_credits_modal_x_12"
            formula = (
                "modal(bank_salary_credits[].amount, by employer) × 12 "
                "(deterministic engine recompute)"
            )

    # Deterministic override for salary_slip — agent's outlier handling has
    # drifted (some runs picked single-month gross). Mean of all slips minus
    # arrears outliers (>1.5× median), then × 12.
    #
    # Low-slip-divergence guardrail (2026-05-26): the 1.5×-median outlier
    # rule is statistically toothless when only 2 slips are kept after
    # filtering — the "outlier" you want to drop IS pulling the median.
    # Observed failure modes:
    #   • C278817070 — 2 slips, Oct contained arrears not flagged numerically;
    #     engine over-counted by 33% (₹26.7L vs agent's ₹20.0L).
    #   • C440052207 — 3 slips; engine dropped July arrears (good), kept
    #     pre-increment Jun + post-increment Aug; engine under-counted by
    #     15.6% (₹52.8L vs agent's ₹62.5L).
    # Rule: when n_kept ≤ 2 AND |engine − agent| / agent > 10%, defer to
    # agent. Emit INCOME_LOW_SLIP_DEFERRED_TO_AGENT flag downstream.
    if primary == "salary_slip":
        slip_recompute = _salary_slip_mean_annual(income.get("salary_slips") or [])
        if slip_recompute is not None:
            recomputed_annual, audit = slip_recompute
            extra_inputs["agent_reported_annual"] = annual
            extra_inputs["salary_slip_audit"] = audit

            n_kept = int(audit.get("n_kept") or 0)
            agent_val = annual  # `annual` is still the agent's reported value here
            divergence_pct = abs(recomputed_annual - agent_val) / max(agent_val, 1) * 100.0
            low_slip_divergence = n_kept <= 2 and agent_val > 0 and divergence_pct > 10.0

            if low_slip_divergence:
                # Defer to agent — engine's mechanical rule is statistically
                # weak on ≤2-slip samples + materially differs from the
                # agent's contextual read.
                annual = agent_val
                method = "salary_slip_agent_value_low_slip_defer"
                formula = (
                    "agent_reported_annual (engine recompute deferred — "
                    "n_kept ≤ 2 AND |engine − agent| > 10%)"
                )
                note = (
                    f"Engine deferred to agent: only {n_kept} slip(s) "
                    f"survived the 1.5×-median filter, and the "
                    f"engine recompute (₹{recomputed_annual:,}) "
                    f"diverged from the agent's read (₹{agent_val:,}) "
                    f"by {divergence_pct:.1f}%. With ≤2 slips the "
                    "outlier-rule is mathematically toothless; "
                    "agent's contextual read prevails. UW: confirm "
                    "against appointment letter / ITR if available."
                )
                extra_inputs["engine_recomputed_annual"] = recomputed_annual
                extra_inputs["divergence_pct"] = round(divergence_pct, 1)
                extra_inputs["low_slip_deferred"] = True
            else:
                # Normal path — engine recompute wins.
                if annual != recomputed_annual:
                    kept = audit.get("n_kept", 0)
                    excl = audit.get("n_excluded", 0)
                    note = (
                        f"Engine recomputed: mean(gross) over "
                        f"{kept} clean slip(s) "
                        f"(excluded {excl} outlier(s) > 1.5× median) "
                        f"× 12 = ₹{recomputed_annual:,}. "
                        f"Agent had reported ₹{annual:,}; "
                        f"engine value takes precedence."
                    )
                else:
                    note = (
                        f"Mean gross of {audit.get('n_kept', 0)} clean slips × 12 "
                        f"= ₹{recomputed_annual:,} (matches agent)."
                    )
                annual = recomputed_annual
                method = "salary_slip_mean_x_12"
                formula = (
                    "mean(salary_slips[].gross_salary, "
                    "excluding gross > 1.5 × median) × 12 "
                    "(deterministic engine recompute)"
                )

    # Deterministic override for ITR — Term UW V5 Feb-26 p20 + TALIC policy
    # confirmation 2026-06-07:
    #   • Earned income = salary + business/profession (+ farm income for
    #     agriculturists). Passive income — bank interest, capital gains,
    #     house-property/rental, dividends — is NOT counted.
    #   • Self-employed / business / professional → AVERAGE of the latest 2
    #     years' earned income; salaried (ITR-1) → latest year's earned income.
    # The engine ALWAYS uses the earned recompute. The agent's headline
    # annual_income often used GTI (which includes passive income) — that is
    # not eligible, so we do not defer to it. When the headline materially
    # exceeds the earned figure we record the gap + raise an awareness flag
    # (passive income excluded, or a recent ITR is acknowledgement-only).
    if primary == "itr":
        itr_recompute = _itr_eligibility_annual(
            income.get("itr_years") or [], case.get("applicant") or {}, income
        )
        if itr_recompute is not None:
            recomputed_annual, audit = itr_recompute
            agent_val = annual
            extra_inputs["agent_reported_annual"] = agent_val
            extra_inputs["itr_income_audit"] = audit

            dq = ""
            if audit.get("earned_gt_gti_years"):
                dq = (
                    f" DATA QUALITY: earned_total exceeds gross_total_income "
                    f"for AY {audit['earned_gt_gti_years']} — verify ITR "
                    f"extraction."
                )
                extra_inputs["itr_earned_gt_gti"] = audit["earned_gt_gti_years"]
            if audit["basis"] == "avg_latest_2_years":
                note = (
                    f"Engine (Term UW V5 p20): average of latest 2 ITR "
                    f"years' earned income {audit['years_used']} = "
                    f"₹{recomputed_annual:,} (salary + business/profession; "
                    f"passive income excluded).{dq}"
                )
            else:
                note = (
                    f"Engine (Term UW V5 p20): "
                    f"{'salaried — ' if audit['is_salaried'] else ''}latest "
                    f"year earned income ₹{recomputed_annual:,} "
                    f"({audit['years_used']}; passive income excluded).{dq}"
                )
            method = "itr_" + audit["basis"]
            formula = (
                "avg(latest 2 ITR yrs earned) [self-employed] | latest "
                "yr earned [salaried]; earned = salary + business/"
                "profession, passive excluded — Term UW V5 p20"
            )
            annual = recomputed_annual

            # Headline gap — agent reported materially more than the eligible
            # earned figure (usually passive income they rolled into GTI, or a
            # latest ITR that is acknowledgement-only / lacks COI). Surface.
            if agent_val > 0 and recomputed_annual > 0:
                gap_pct = (agent_val - recomputed_annual) / recomputed_annual * 100.0
                if abs(gap_pct) > 15.0:
                    extra_inputs["itr_headline_gap_pct"] = round(gap_pct, 1)
                    extra_inputs["itr_headline_excluded"] = max(0, agent_val - recomputed_annual)
                    note += (
                        f" Agent headline ₹{agent_val:,} was "
                        f"{gap_pct:+.0f}% — likely passive income excluded "
                        f"per policy; verify per-year earned heads."
                    )

    # FX conversion — last step. If the income agent flagged a non-INR
    # currency (e.g., AED for an NRI Dubai case), convert now and record
    # the rate + audit. Downstream HLV / FOIR / SA-band gating runs on
    # the converted INR value. Fallback chain when income.currency is None:
    #   (a) text scan income_sources[] for ISO codes + common short forms
    #       (KD / Dh / S$ / RO / etc.)
    #   (b) bank_statements[*].currency (any non-INR account)
    #   (c) applicant.country_of_residence → currency map
    #   (d) employer_name suffix scan (… LLC Dubai → AED, … Co. Kuwait → KWD)
    currency = _resolve_currency(income.get("currency"))
    fx_inferred_via = None
    if currency == "INR" and not income.get("currency"):
        fx = _load_fx_rates()
        # (a) ISO codes + every short alias (KD, Dh, S$, RO, …)
        markers = list(fx["rates"].keys()) + list(fx["aliases"].keys())
        markers = [m for m in markers if m and m != "INR"]
        # Sort longest-first so 'KWD' wins over 'KW' if both exist
        markers.sort(key=len, reverse=True)
        for src in (income.get("income_sources") or [])[:5]:
            up = str(src).upper()
            for m in markers:
                import re

                # token boundary; some short codes contain '$' so use a permissive boundary
                if re.search(rf"(^|\W){re.escape(m)}(\W|$)", up):
                    currency = _resolve_currency(m)
                    fx_inferred_via = f"text_scan:{m}"
                    break
            if currency != "INR":
                break
        # (b) Bank statement currency
        if currency == "INR":
            for bs in (case.get("assets_liabilities") or {}).get("bank_statements") or []:
                if isinstance(bs, dict):
                    bc = _resolve_currency(bs.get("currency"))
                    if bc != "INR":
                        currency = bc
                        fx_inferred_via = f"bank_statement.currency:{bc}"
                        break
        # (c) Country-of-residence map
        if currency == "INR":
            applicant = case.get("applicant") or {}
            cor = (applicant.get("country_of_residence") or "").strip().upper()
            if cor and cor in (fx.get("country_map") or {}):
                currency = fx["country_map"][cor]
                fx_inferred_via = f"applicant.country_of_residence:{cor}→{currency}"
        # (d) Employer-name suffix scan
        if currency == "INR":
            applicant = case.get("applicant") or {}
            emp = (applicant.get("employer_name") or "").upper()
            if emp:
                for country, ccy in (fx.get("country_map") or {}).items():
                    import re

                    if re.search(rf"\b{re.escape(country)}\b", emp):
                        currency = ccy
                        fx_inferred_via = f"employer_name:{country}→{currency}"
                        break

    if currency != "INR" and annual > 0:
        inr_value, fx_audit = _convert_to_inr(annual, currency)
        if fx_audit.get("applied"):
            if fx_inferred_via:
                fx_audit["inferred_via"] = fx_inferred_via
            extra_inputs["fx_conversion"] = fx_audit
            via_str = f" (currency inferred via {fx_inferred_via})" if fx_inferred_via else ""
            note = (
                note + (" " if note else "") + f"Native amount {fx_audit['original_amount']:,.0f} "
                f"{fx_audit['original_currency']} × "
                f"{fx_audit['rate_to_inr']} INR/unit = "
                f"₹{fx_audit['inr_value']:,}{via_str}."
            )
            method = method + "_fx_inr"
            annual = inr_value

    return Calculation(
        name="selected_income",
        value=annual,
        unit="INR",
        method=method,
        function="underwriting_platform.engine.calculator.compute_selected_income",
        formula=formula,
        inputs={
            "primary_source": primary,
            "annual_income": annual,
            "currency_native": currency,
            "cascade_priority": rung_idx,
            "n_income_sources": len(income.get("income_sources") or []),
            "itr_years_count": len(income.get("itr_years") or []),
            "n_bank_credits": len(income.get("bank_salary_credits") or []),
            **extra_inputs,
        },
        sources=sources,
        rule=f"income cascade rung #{rung_idx} of 7" if rung_idx else "no rule matched",
        note=note,
    )


def compute_monthly_gross(income_calc: Calculation) -> Calculation:
    annual = _safe_int(income_calc.value)
    monthly = round(annual / 12) if annual else 0
    return Calculation(
        name="monthly_gross",
        value=monthly,
        unit="INR",
        method="annual_div_12",
        function="underwriting_platform.engine.calculator.compute_monthly_gross",
        formula="selected_income.annual / 12",
        inputs={"annual_income": annual},
        sources=income_calc.sources,
    )


# ──────────────────────────────────────────────────────────────────────────
# HLV grids — base / ADB-ATPD / CI-HC
# ──────────────────────────────────────────────────────────────────────────


def _multiplier_for_band(grid: dict, band: str) -> float:
    return _safe_float(grid.get(band) or 0)


def compute_hlv_base(income_calc: Calculation, age: int | None, rules: dict) -> Calculation:
    grid = (rules.get("hlv_grids") or {}).get("base") or rules.get("income_multiples") or {}
    band = get_age_band(age) or "61-65"
    mult = _multiplier_for_band(grid, band)
    annual = _safe_int(income_calc.value)
    hlv = int(annual * mult)
    return Calculation(
        name="hlv_base",
        value=hlv,
        unit="INR",
        method="income_multiple_age_band",
        function="underwriting_platform.engine.calculator.compute_hlv_base",
        formula=f"annual_income × multiplier(age_band={band})",
        inputs={"annual_income": annual, "age": age, "age_band": band, "multiplier": mult},
        sources=income_calc.sources,
        rule=f"uw_rules.hlv_grids.base.{band} = {mult}",
        note="Single-life HLV ceiling per Term UW Feb 2026 p.15.",
    )


def compute_hlv_adb_atpd(
    income_calc: Calculation, age: int | None, rules: dict, proposed_sa: int
) -> Calculation:
    """ADB / ATPD rider HLV. SA > 3 Cr uses fixed grids."""
    cfg = (rules.get("hlv_grids") or {}).get("adb_atpd") or {}
    sa_threshold = int(cfg.get("sa_threshold_cr") or 3) * 10_000_000
    annual = _safe_int(income_calc.value)
    if proposed_sa <= sa_threshold:
        # Use base grid
        return _hlv_with_named_grid(
            income_calc,
            age,
            rules,
            grid_name="base",
            calc_name="hlv_adb_atpd",
            note=f"SA ≤ ₹{sa_threshold // 10_000_000} Cr → base grid",
        )
    # SA > threshold → fixed multiplier
    if age is not None and age <= 60:
        mult = _safe_float(cfg.get("default_18-60") or 15)
        band = "18-60"
    else:
        mult = _safe_float(cfg.get("default_61-65") or 5)
        band = "61-65"
    hlv = int(annual * mult)
    return Calculation(
        name="hlv_adb_atpd",
        value=hlv,
        unit="INR",
        method="adb_atpd_high_sa_grid",
        function="underwriting_platform.engine.calculator.compute_hlv_adb_atpd",
        formula=f"annual_income × {mult} (ADB/ATPD high-SA fixed multiplier)",
        inputs={
            "annual_income": annual,
            "age": age,
            "age_band": band,
            "multiplier": mult,
            "proposed_sa": proposed_sa,
        },
        sources=income_calc.sources,
        rule=f"uw_rules.hlv_grids.adb_atpd.default_{band} = {mult}",
        note=f"SA > ₹{sa_threshold // 10_000_000} Cr → ADB/ATPD high-SA grid.",
    )


def compute_hlv_ci_hc(income_calc: Calculation, age: int | None, rules: dict) -> Calculation:
    return _hlv_with_named_grid(
        income_calc,
        age,
        rules,
        grid_name="ci_hc",
        calc_name="hlv_ci_hc",
        note="Critical-Illness / Hospital-Cash rider HLV.",
    )


def _hlv_with_named_grid(
    income_calc: Calculation,
    age: int | None,
    rules: dict,
    grid_name: str,
    calc_name: str,
    note: str = "",
) -> Calculation:
    grid = (rules.get("hlv_grids") or {}).get(grid_name) or {}
    band = get_age_band(age) or "61-65"
    mult = _multiplier_for_band(grid, band)
    annual = _safe_int(income_calc.value)
    hlv = int(annual * mult)
    return Calculation(
        name=calc_name,
        value=hlv,
        unit="INR",
        method=f"income_multiple_age_band[{grid_name}]",
        function=f"underwriting_platform.engine.calculator._hlv_with_named_grid (grid={grid_name})",
        formula=f"annual_income × multiplier(age_band={band}, grid={grid_name})",
        inputs={
            "annual_income": annual,
            "age": age,
            "age_band": band,
            "multiplier": mult,
            "grid": grid_name,
        },
        sources=income_calc.sources,
        rule=f"uw_rules.hlv_grids.{grid_name}.{band} = {mult}",
        note=note,
    )


# ──────────────────────────────────────────────────────────────────────────
# FOIR + Affordability
# ──────────────────────────────────────────────────────────────────────────


def _bank_statement_coverage_days(assets_block: dict) -> int:
    """Widest period_from..period_to span across all bank_statements, in
    days. 0 if no statement or dates unparseable."""
    best = 0
    for bs in assets_block.get("bank_statements") or []:
        d1 = _parse_dob(bs.get("period_from"))
        d2 = _parse_dob(bs.get("period_to"))
        if d1 and d2 and d2 >= d1:
            best = max(best, (d2 - d1).days + 1)
    return best


def compute_monthly_emi(case: dict) -> Calculation:
    assets = case.get("assets_liabilities") or {}
    emi = _safe_int(assets.get("monthly_emi_total"))
    loans = assets.get("loans") or []
    sources = _bank_sources(assets)
    coverage_days = _bank_statement_coverage_days(assets)
    liabilities_total = _safe_int(assets.get("liabilities_total"))
    # monthly_emi_total raw None means "no EMI breakdown was ever provided"
    # (genuinely unknown), distinct from a raw 0 (confirmed zero EMIs) —
    # _safe_int() collapses both to 0 above, so check the raw value here
    # before that distinction is lost. A case with substantial declared
    # liabilities but no EMI figure cannot honestly claim "FOIR 0% PASS".
    # Found via case SYNTHETIC_CASE (2026-06-26): ~₹7 Cr liabilities (LIC loan,
    # SBI home/gold/education loans) on the balance sheet, monthly_emi_total
    # never populated — FOIR showed a flat, unqualified 0% PASS.
    emi_unknown_with_liabilities = assets.get("monthly_emi_total") is None and liabilities_total > 0
    inputs = {
        "monthly_emi_total": emi,
        "n_loans": len(loans),
        "loan_lenders": [l.get("lender") for l in loans if l.get("lender")],
        "bank_statement_coverage_days": coverage_days,
        "liabilities_total": liabilities_total,
        "emi_unknown_with_liabilities": emi_unknown_with_liabilities,
    }
    note = (
        "Aggregated by the assets_liabilities agent from bank statements "
        "and loan sanction documents."
    )
    # A short statement window means "no EMI detected" reflects absence of
    # data, not a confirmed absence of obligations — surfaced here (rather
    # than silently assumed reliable) so check_foir can caveat its PASS.
    # Found via case SYNTHETIC_CASE (2026-06-25): a 2-day bank statement drove
    # a flat, unqualified "FOIR effectively 0%" PASS.
    if 0 < coverage_days < 30:
        note += (
            f" Statement covers only {coverage_days} day(s) — too short "
            f"to reliably confirm the absence of EMIs/recurring obligations."
        )
    if emi_unknown_with_liabilities:
        note += (
            f" Declared liabilities total ₹{liabilities_total:,} but no EMI "
            f"breakdown was provided — absence of EMI data, not a confirmed "
            f"absence of obligations."
        )
    if not sources and not loans:
        note = (
            "No bank/loan evidence captured; the EMI aggregate and derived FOIR are not verified."
        )
    return Calculation(
        name="monthly_emi_total",
        value=emi,
        unit="INR",
        method="from_assets_liabilities_agent",
        function="underwriting_platform.engine.calculator.compute_monthly_emi",
        formula="sum of EMI debits detected in bank narrations + loan EMIs",
        inputs=inputs,
        sources=sources,
        rule="assets_liabilities.monthly_emi_total — extracted aggregate; verify against source evidence",
        note=note,
    )


def compute_foir(
    monthly_gross: Calculation, monthly_emi: Calculation, rules: dict, is_sbi_case: bool = False
) -> Calculation:
    g = _safe_int(monthly_gross.value)
    e = _safe_int(monthly_emi.value)
    foir = (e / g) * 100 if g else 0.0
    foir_pct = round(foir, 1)
    coverage_days = _safe_int((monthly_emi.inputs or {}).get("bank_statement_coverage_days"))
    emi_unknown_with_liabilities = bool(
        (monthly_emi.inputs or {}).get("emi_unknown_with_liabilities")
    )
    liabilities_total = _safe_int((monthly_emi.inputs or {}).get("liabilities_total"))
    inputs = {
        "monthly_emi_total": e,
        "monthly_gross": g,
        "bank_statement_coverage_days": coverage_days,
        "emi_unknown_with_liabilities": emi_unknown_with_liabilities,
        "liabilities_total": liabilities_total,
    }
    if is_sbi_case:
        # The ratio itself (EMI load vs gross income) is a neutral, generic
        # financial metric with no insurer-specific meaning attached — kept
        # for informational value. uw_rules.foir_threshold_pct (55%) is
        # TATA AIA's own affordability threshold; no SBI document defines
        # an equivalent, so it is never computed, stored, or cited here for
        # an SBI case — not just unused downstream, absent from this
        # Calculation's own metadata too.
        return Calculation(
            name="foir",
            value=foir_pct,
            unit="%",
            method="emi_to_gross_ratio",
            function="underwriting_platform.engine.calculator.compute_foir",
            formula="(monthly_emi_total / monthly_gross) × 100",
            inputs=inputs,
            sources=monthly_emi.sources + monthly_gross.sources,
            rule="Informational metric only — no SBI document defines a FOIR threshold for this product",
            note="FOIR shown for information only; not evaluated against any threshold.",
        )
    threshold = _safe_int(rules.get("foir_threshold_pct") or 55)
    inputs["threshold_pct"] = threshold
    return Calculation(
        name="foir",
        value=foir_pct,
        unit="%",
        method="emi_to_gross_ratio",
        function="underwriting_platform.engine.calculator.compute_foir",
        formula="(monthly_emi_total / monthly_gross) × 100",
        inputs=inputs,
        sources=monthly_emi.sources + monthly_gross.sources,
        rule=f"uw_rules.foir_threshold_pct = {threshold}",
        note=f"FOIR ≤ {threshold}% required for STP; excess triggers gating WARN/FAIL.",
    )


# ──────────────────────────────────────────────────────────────────────────
# Max SA eligible
# ──────────────────────────────────────────────────────────────────────────


def compute_max_sa(
    hlv_base: Calculation,
    proposed_sa: int,
    rules: dict,
    existing_cover: int = 0,
    is_sbi_case: bool = False,
) -> Calculation:
    """Maximum sum-assured the underwriter COULD offer on this policy —
    the true eligibility ceiling, not what was merely requested.

    Computed as `min(hlv_remaining, per_life_cap)` where
    `hlv_remaining = max(0, hlv_base - existing_cover)`. Per the
    Datamatics LAYOUT spec (row 45-46): "Deduct Existing Insurance from
    HLV ... Amount remaining after deduction of existing insurance
    across all policies."

    `proposed_sa` is NOT folded into this ceiling — "what's the maximum
    I'm eligible for" and "what was actually proposed" are two different
    questions, and conflating them previously made this figure silently
    collapse to the proposed SA whenever the applicant asked for less
    than their true ceiling (e.g. case SYNTHETIC_CASE: showed ₹25L instead of
    the true ~₹38.5L ceiling — found via observation review, 2026-06-26).
    The proposed-vs-ceiling comparison is handled separately by
    `check_sa_within_hlv` / the SBI HLV-remaining gate.

    `existing_cover` aggregates the applicant-declared cross-insurer
    cover (compliance.existing_cover_declared.total_life_cover_other);
    when IIB / prior-policy feed is wired in future, that total replaces
    the declared figure as the input here.

    hlv_base.value is None (not just 0) for the SBI "Individual
    Consideration" scenario (engine.sbi_financial.compute_sbi_hlv) — the
    SBI source table has no numeric multiplier for that age/income
    combination at all, which is a genuinely different state from a
    confirmed-zero HLV and must not be silently treated as one. Returns a
    None-valued Calculation (not a 0-valued one) so that distinction
    survives into max_sa_eligible for check_sa_within_hlv / the SBI
    HLV-remaining gate to report honestly.
    """
    if hlv_base.value is None:
        return Calculation(
            name="max_sa_eligible",
            value=None,
            unit="INR",
            method="not_specified_by_sbi_guidelines",
            function="underwriting_platform.engine.calculator.compute_max_sa",
            formula="Not computed — HLV base is not specified by SBI guidelines for this scenario",
            inputs={"hlv_base": None, "existing_cover": existing_cover, "proposed_sa": proposed_sa},
            sources=hlv_base.sources,
            rule="sbi_smart_shield_rules.income_multiplier_table — Individual Consideration cell",
            note=(
                hlv_base.note
                or "HLV not specified by SBI guidelines for this age/income "
                "combination — refer for manual UW; maximum eligible SA cannot be "
                "automatically determined."
            ),
        )
    hlv_val = _safe_int(hlv_base.value)
    existing_cover = max(0, _safe_int(existing_cover))
    hlv_remaining = max(0, hlv_val - existing_cover)
    # uw_rules.max_per_life_crore is TATA AIA's own per-life ceiling
    # (config/uw_rules.json: "insurer": "TATA AIA Life Insurance") — no
    # SBI document defines an equivalent cap; SBI's own entry-SA table
    # (config/sbi_smart_shield_rules.json basic_sum_assured._note) reads
    # "Max: No Limit per source table." Strict SBI-rule-purity pass
    # (2026-10-09, client requirement: "Do not use TATA/TALIC-specific
    # thresholds or logic for SBI cases unless independently supported
    # by an SBI reference") — SBI cases use hlv_remaining alone as the
    # ceiling, uncapped, matching the real guideline rather than a
    # borrowed TATA number.
    per_life_cap = (
        10**15 if is_sbi_case else _safe_int(rules.get("max_per_life_crore") or 100) * 10_000_000
    )
    # Both inputs are always real computed numbers here (hlv_remaining is
    # clamped to >= 0 above, per_life_cap always defaults to a positive
    # value) — neither is ever a "missing data" sentinel, so a legitimate
    # hlv_remaining == 0 (existing cover already meets or exceeds HLV —
    # zero remaining capacity, a real and important result, not an
    # absent one) must be allowed to win the min() comparison. Found via
    # case SYNTHETIC_CASE (2026-10-01): filtering out zero candidates here
    # made max_sa_eligible silently report the full ₹100 Cr per-life cap
    # instead of the correct ₹0 remaining capacity.
    if hlv_remaining <= per_life_cap:
        chosen, label = hlv_remaining, "hlv_remaining"
    else:
        chosen, label = per_life_cap, "per_life_cap"
    formula = (
        "min(hlv_remaining, per_life_cap) "
        "where hlv_remaining = max(0, hlv_base - existing_cover) "
        "— NOT capped by proposed_sa; that comparison is a separate gate"
    )
    return Calculation(
        name="max_sa_eligible",
        value=chosen,
        unit="INR",
        method="min_of_hlv_remaining_and_perlifecap",
        function="underwriting_platform.engine.calculator.compute_max_sa",
        formula=formula,
        inputs={
            "hlv_base": hlv_val,
            "existing_cover": existing_cover,
            "hlv_remaining": hlv_remaining,
            "per_life_cap": per_life_cap,
            "proposed_sa": proposed_sa,
        },
        sources=hlv_base.sources,
        rule=f"uw_rules.max_per_life_crore = {per_life_cap // 10_000_000}",
        note=f"Limiting input: {label}.",
    )


# ──────────────────────────────────────────────────────────────────────────
# Cover-to-loan ratio (Credit Protect path)
# ──────────────────────────────────────────────────────────────────────────


def compute_cover_to_loan(case: dict) -> Calculation | None:
    cp = case.get("credit_protect") or {}
    if not cp.get("applicable"):
        return None
    sa = _safe_int(cp.get("loan_amount_sanctioned"))
    ratio = _safe_float(cp.get("cover_to_loan_ratio"))
    return Calculation(
        name="cover_to_loan_ratio",
        value=ratio,
        unit="ratio",
        method="from_credit_protect_agent",
        function="underwriting_platform.engine.calculator.compute_cover_to_loan",
        formula="proposed_sa / loan_amount_sanctioned",
        inputs={"loan_amount_sanctioned": sa, "cover_to_loan_ratio": ratio},
        sources=[
            Source(
                document="Loan sanction letter",
                agent="business_surrogate",
                field_path="credit_protect.cover_to_loan_ratio",
            )
        ],
        rule="Term UW Feb 2026 p.32 — must be ≤ 1.05",
    )


# ──────────────────────────────────────────────────────────────────────────
# Business insurance — Keyman / Partnership / Employer-Employee (Term UW V5 p42)
# ──────────────────────────────────────────────────────────────────────────


def compute_business_insurance_value(
    case: dict, hlv_base: Calculation, rules: dict
) -> Calculation | None:
    """Deterministic insurable value for Keyman / Partnership / Employer-Employee
    cover, per Term UW V5 Feb-26 p41-42 (confirmed 2026-06-07). Only ACTIVE
    business income heads count; passive heads are never used.

      Keyman      = LOWER of {avg dir. remuneration × 10;
                              avg gross profit × 3 × shareholding;
                              (avg NPAT + 30% avg depreciation) × 5 × shareholding}
                    − existing/simultaneous keyperson cover in the company's name.
                    No cover if a loss was incurred in the latest / last-2 years.
                    Capped at ₹100 Cr per key person.
      Partnership = LOWER of the last two of those − existing partnership cover.
      Empl-Empl   = latest-2-yr salary × age-multiple (= hlv_base)
                    + avg-3-yr NPAT × shareholding (only if the employee holds stake).

    Returns a Calculation, or None when business_insurance is not applicable.
    """
    bi = case.get("business_insurance") or {}
    if not bi.get("applicable"):
        return None
    btype = (bi.get("type") or "").strip().lower()

    share = _safe_float(bi.get("shareholding_pct")) / 100.0
    dir_rem = _safe_int(bi.get("avg_directors_remuneration_3yr"))
    gp = _safe_int(bi.get("avg_gross_profit_3yr"))
    npat = _safe_int(bi.get("avg_net_profit_after_tax_3yr"))
    dep = _safe_int(bi.get("avg_depreciation_3yr"))
    existing = _safe_int(bi.get("existing_keyman_partnership_cover"))
    loss = bool(bi.get("business_loss_latest_year"))

    # Formula components (None when a required input is missing)
    comp_a = dir_rem * 10 if dir_rem > 0 else None
    comp_b = int(gp * 3 * share) if (gp > 0 and share > 0) else None
    comp_c = int((npat + 0.30 * dep) * 5 * share) if (npat > 0 and share > 0) else None

    missing: list[str] = []
    if share <= 0 and btype in ("partnership", "keyman"):
        missing.append("shareholding_pct")

    comps: dict[str, int] = {}
    cap = 0
    if "keyman" in btype:
        if loss:
            value, basis = 0, "loss_latest_year_no_cover"
            note = "Loss in latest / last-2 years → no Keyman cover (Term UW V5 p42)."
        else:
            comps = {
                k: v
                for k, v in {
                    "avg_dir_remuneration_x10": comp_a,
                    "avg_gross_profit_x3_x_share": comp_b,
                    "(avg_NPAT+30pct_dep)_x5_x_share": comp_c,
                }.items()
                if v is not None
            }
            for need, lbl in (
                (comp_a, "avg_directors_remuneration_3yr"),
                (comp_b, "avg_gross_profit_3yr/shareholding_pct"),
                (comp_c, "avg_net_profit_after_tax_3yr/shareholding_pct"),
            ):
                if need is None:
                    missing.append(lbl)
            value = (min(comps.values()) - existing) if comps else 0
            basis = "keyman_lower_of"
            note = "Keyman: lower of the available formulas − existing cover."
        cap = int(_safe_float(rules.get("keyman_cap_crore") or 100) * 10_000_000)
    elif "partnership" in btype:
        comps = {
            k: v
            for k, v in {
                "avg_gross_profit_x3_x_share": comp_b,
                "(avg_NPAT+30pct_dep)_x5_x_share": comp_c,
            }.items()
            if v is not None
        }
        if comp_b is None:
            missing.append("avg_gross_profit_3yr/shareholding_pct")
        if comp_c is None:
            missing.append("avg_net_profit_after_tax_3yr/shareholding_pct")
        value = (min(comps.values()) - existing) if comps else 0
        basis = "partnership_lower_of"
        note = "Partnership: lower of the available formulas − existing cover."
    elif "employer" in btype or "employee" in btype or btype == "ee":
        base = _safe_int(hlv_base.value)  # latest-2-yr salary × age-mult
        stake = int(npat * share) if (npat > 0 and share > 0) else 0
        comps = {"salary_HLV_base": base, "avg_NPAT_x_share": stake}
        value = base + stake
        basis = "ee_salary_hlv_plus_npat_share"
        note = (
            "Employer-Employee: salary HLV (latest-2-yr salary × age "
            "multiple) + avg-3-yr NPAT × shareholding (stake only)."
        )
        if base <= 0:
            missing.append("salary income (hlv_base)")
    else:
        return None  # HUF / unknown — not deterministically handled yet

    value = max(0, int(value))
    if cap and value > cap:
        value = cap
        note += f" Capped at ₹{cap:,} (₹{cap // 10_000_000} Cr per key person)."
    if missing:
        note += (
            f" INCOMPLETE INPUTS ({', '.join(sorted(set(missing)))}) — "
            "agent extraction missing; value uses only available formulas, "
            "verify against audited financials."
        )

    agent_value = _safe_int(bi.get("computed_insurable_value"))
    return Calculation(
        name="business_insurance_value",
        value=value,
        unit="INR",
        method=basis,
        function="underwriting_platform.engine.calculator.compute_business_insurance_value",
        formula=(
            "Keyman: lower(dir_rem×10, gross_profit×3×share, "
            "(NPAT+30%dep)×5×share) − existing | Partnership: lower of last "
            "two − existing | EE: salary_HLV + NPAT×share — Term UW V5 p42"
        ),
        inputs={
            "type": bi.get("type"),
            "shareholding_pct": bi.get("shareholding_pct"),
            "components": comps,
            "existing_cover": existing,
            "business_loss_latest_year": loss,
            "missing_inputs": sorted(set(missing)),
            "agent_computed_insurable_value": agent_value,
        },
        sources=[
            Source(
                document="Audited financials / shareholding cert / partnership deed",
                agent="business_surrogate",
                field_path="business_insurance",
            )
        ],
        rule="Term UW V5 Feb-26 p41-42",
        note=note,
    )
