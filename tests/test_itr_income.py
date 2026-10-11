"""
tests/test_itr_income.py — ITR eligibility income per Term UW V5 Feb-26 p20.

Rule (p20, item 5):
  • Self-employed / business / professional (ITR-3/4 + Computation of Income):
    eligibility income = AVERAGE of the latest 2 years' EARNED income.
  • Salaried (ITR-1, salary-only): LATEST year's earned income.

The engine recomputes this deterministically from income.itr_years[] (the
agent has historically reported latest-year only). Guardrail: when the
itr_years[] extraction is internally inconsistent with the agent's headline
annual_income (itr_years incomplete), defer to the agent value + flag, rather
than emit a recompute built on bad inputs — see verified regression risk on
C313777567 (agent ₹82.5L, QC-passed, vs itr_years latest ₹7.7L).
"""
from underwriting_platform.engine.calculator import compute_selected_income


def _case(itr_years, *, agent_annual, occupation="", is_salaried=None):
    income = {"primary_source": "itr", "annual_income": agent_annual,
              "currency": "INR", "itr_years": itr_years}
    if is_salaried is not None:
        income["is_salaried"] = is_salaried
    return {"income": income, "applicant": {"occupation": occupation}}


def test_self_employed_averages_latest_two_years():
    yrs = [
        {"assessment_year": "2025-26", "gross_total_income": 700000,
         "earned_total": 700000, "salary_head": 0, "business_head": 700000},
        {"assessment_year": "2024-25", "gross_total_income": 500000,
         "earned_total": 500000, "salary_head": 0, "business_head": 500000},
        {"assessment_year": "2023-24", "gross_total_income": 400000,
         "earned_total": 400000, "salary_head": 0, "business_head": 400000},
    ]
    calc = compute_selected_income(_case(yrs, agent_annual=700000,
                                          occupation="Self-Employed Business"))
    assert calc.value == 600000, calc.value          # (700k + 500k) / 2
    assert calc.method == "itr_avg_latest_2_years", calc.method


def test_salaried_uses_latest_year():
    yrs = [
        {"assessment_year": "2025-26", "gross_total_income": 800000,
         "earned_total": 800000, "salary_head": 800000, "business_head": 0},
        {"assessment_year": "2024-25", "gross_total_income": 600000,
         "earned_total": 600000, "salary_head": 600000, "business_head": 0},
    ]
    calc = compute_selected_income(_case(yrs, agent_annual=800000,
                                          occupation="Salaried"))
    assert calc.value == 800000, calc.value
    assert calc.method == "itr_latest_year", calc.method


def test_earned_exceeding_gti_uses_heads_but_flags():
    # AY25-26 agent earned_total (705,886) exceeds GTI (630,886) — a data
    # slip. The engine uses the head breakdown (salary 202,418 + business
    # 200,000 = 402,418) for the value (earned = salary + business/profession
    # by definition), but still FLAGS the year because the agent's RAW
    # earned_total exceeded GTI.
    yrs = [
        {"assessment_year": "2025-26", "gross_total_income": 630886,
         "earned_total": 705886, "salary_head": 202418, "business_head": 200000},
        {"assessment_year": "2024-25", "gross_total_income": 470200,
         "earned_total": 465620, "salary_head": 0, "business_head": 465620},
    ]
    calc = compute_selected_income(_case(yrs, agent_annual=705886,
                                          occupation="Self-Employed"))
    assert calc.value == 434019, calc.value          # (402418 + 465620) / 2 from heads
    assert "2025-26" in (calc.inputs.get("itr_earned_gt_gti") or []), calc.inputs


def test_passive_income_excluded_uses_earned_not_headline():
    # Agent headline ₹82.5L rolled in rental/interest (GTI); the earned figure
    # is salary+business only. Per policy, engine uses the earned 2-yr average
    # and flags the gap — it does NOT defer to the GTI headline.
    yrs = [
        {"assessment_year": "2025-26", "gross_total_income": 8249952,
         "earned_total": 800000, "salary_head": 0, "business_head": 800000,
         "other_head": 7449952},
        {"assessment_year": "2024-25", "gross_total_income": 5000000,
         "earned_total": 500000, "salary_head": 0, "business_head": 500000},
    ]
    calc = compute_selected_income(_case(yrs, agent_annual=8249952,
                                          occupation="Self-Employed Business"))
    assert calc.value == 650000, calc.value           # (800k + 500k) / 2 earned
    assert calc.method == "itr_avg_latest_2_years", calc.method
    assert calc.inputs.get("itr_headline_gap_pct") is not None, calc.inputs
    assert calc.inputs.get("itr_headline_excluded") == 8249952 - 650000


def test_agent_zero_uses_recompute():
    yrs = [
        {"assessment_year": "2025-26", "gross_total_income": 3000000,
         "earned_total": 3000000, "salary_head": 0, "business_head": 3000000},
        {"assessment_year": "2024-25", "gross_total_income": 2000000,
         "earned_total": 2000000, "salary_head": 0, "business_head": 2000000},
    ]
    calc = compute_selected_income(_case(yrs, agent_annual=0,
                                          occupation="Self-Employed Business"))
    assert calc.value == 2500000, calc.value          # (3M + 2M) / 2
    assert calc.method == "itr_avg_latest_2_years", calc.method


def test_is_salaried_flag_overrides_heuristic():
    yrs = [
        {"assessment_year": "2025-26", "gross_total_income": 700000,
         "earned_total": 700000, "salary_head": 0, "business_head": 700000},
        {"assessment_year": "2024-25", "gross_total_income": 500000,
         "earned_total": 500000, "salary_head": 0, "business_head": 500000},
    ]
    calc = compute_selected_income(_case(yrs, agent_annual=700000,
                                          is_salaried=True))
    assert calc.value == 700000, calc.value           # latest year
    assert calc.method == "itr_latest_year", calc.method


def test_single_itr_year_uses_latest():
    yrs = [{"assessment_year": "2025-26", "gross_total_income": 600000,
            "earned_total": 600000, "salary_head": 0, "business_head": 600000}]
    calc = compute_selected_income(_case(yrs, agent_annual=600000,
                                          occupation="Self-Employed"))
    assert calc.value == 600000, calc.value
    assert calc.method == "itr_latest_year_single_itr", calc.method
