"""tests/test_income_divergence_refer.py — REFER-on-income-divergence guard.

A hard HLV-ceiling DECLINE is downgraded to REFER when independent
corroboration (TALIC system-assessed income / recurring salary credits)
implies an HLV that clears the proposed SA — the verified ITR income is
likely understated (NRI part-year, presumptive 44AD, passive-heavy).
Regression: NRI case C291475745 (India ITR ₹9.84L vs ~₹24L corroborated).
Self-declared CAF income and raw bank totals are NOT corroboration.
"""
from underwriting_platform.engine.verdict import make_verdict, _corroborated_income_floor
from underwriting_platform.engine.types import Calculation, GatingItem


def _calcs(selected_income, hlv_base, **extra):
    c = {
        "selected_income": Calculation(name="selected_income", value=selected_income),
        "hlv_base":        Calculation(name="hlv_base", value=hlv_base),
    }
    c.update(extra)
    return c


_HLV_FAIL = [GatingItem(criterion="hlv_ceiling",
                        label="Proposed SA within HLV ceiling", status="FAIL")]


def test_refer_when_xlsx_income_corroborates_higher():
    # verified ₹9.84L, HLV ₹2.46cr, proposed ₹3cr → would DECLINE; xlsx
    # assessed ₹21.6L implies HLV ₹5.4cr ≥ ₹3cr → REFER (C291475745).
    case = {"policy_data": {"insured_income": 2163250}}
    v = make_verdict(case, _calcs(984214, 24605350), _HLV_FAIL, [], 30000000)
    assert v.label == "REFER"
    assert "manual income reconciliation" in v.rationale.lower()


def test_refer_when_salary_credits_corroborate_higher():
    # No xlsx income, but recurring salary-like credits ₹1.5L/mo (₹18L/yr).
    case = {"bank_analysis": {"recurring_credits": [
        {"is_salary_like": True, "avg_amount": 150000}]}}
    v = make_verdict(case, _calcs(984214, 24605350), _HLV_FAIL, [], 30000000)
    assert v.label == "REFER"


def test_refer_counter_offer_when_no_reliable_corroboration():
    # The current engine refers financial shortfalls for a reduced-cover offer.
    case = {"policy_data": {}, "bank_analysis": {}}
    v = make_verdict(case, _calcs(515504, 18042640), _HLV_FAIL, [], 25000000)
    assert v.label == "REFER"
    assert "manual uw" in v.rationale.lower()


def test_refer_when_corroboration_still_below_proposed():
    # Insufficient corroboration still requires manual review of a reduced offer.
    case = {"policy_data": {"insured_income": 1200000}}
    v = make_verdict(case, _calcs(1000000, 25000000), _HLV_FAIL, [], 50000000)
    assert v.label == "REFER"


def test_caf_total_bank_not_used_as_corroboration():
    # Raw bank totals / non-salary credits are NOT corroboration: a business
    # account with huge turnover must not flip a genuine decline.
    floor = _corroborated_income_floor({
        "bank_analysis": {"recurring_credits": [
            {"is_salary_like": False, "avg_amount": 5000000}]}})
    assert floor == 0


def test_medical_high_still_declines_despite_income_corroboration():
    # Medical-HIGH + gating-fail must still DECLINE (precedence over the
    # income-divergence downgrade).
    case = {"policy_data": {"insured_income": 2163250}}
    calcs = _calcs(984214, 24605350,
                   medical_risk=Calculation(name="medical_risk", value="HIGH"),
                   medical_uw_terminal=Calculation(name="medical_uw_terminal", value="Decline"))
    v = make_verdict(case, calcs, _HLV_FAIL, [], 30000000)
    assert v.label == "DECLINE"
