"""
tests/test_business_insurance.py — Keyman / Partnership / Employer-Employee
insurable value per Term UW V5 Feb-26 p41-42 (confirmed 2026-06-07).

  Keyman      = lower(dir_rem×10, gross_profit×3×share, (NPAT+30%dep)×5×share)
                − existing cover; no cover if loss in latest/last-2 yrs; cap ₹100 Cr.
  Partnership = lower(gross_profit×3×share, (NPAT+30%dep)×5×share) − existing.
  Empl-Empl   = salary HLV (latest-2-yr salary × age multiple) + NPAT×share (stake).
"""
from underwriting_platform.engine.calculator import compute_business_insurance_value
from underwriting_platform.engine.types import Calculation


def _hlv(v):
    return Calculation(name="hlv_base", value=v)


def _case(bi):
    return {"business_insurance": {"applicable": True, **bi}}


RULES = {"keyman_cap_crore": 100}


def test_keyman_lower_of_three():
    bi = {"type": "Keyman", "shareholding_pct": 50,
          "avg_directors_remuneration_3yr": 8500000,   # ×10 = 8.5 Cr
          "avg_gross_profit_3yr": 5000000,             # ×3×.5 = 75 L
          "avg_net_profit_after_tax_3yr": 3000000,
          "avg_depreciation_3yr": 500000}              # (3M+150k)×5×.5 = 78.75 L
    calc = compute_business_insurance_value(_case(bi), _hlv(0), RULES)
    assert calc.value == 7500000, calc.value          # lowest = gross-profit path
    assert calc.method == "keyman_lower_of", calc.method


def test_keyman_loss_no_cover():
    bi = {"type": "Keyman", "shareholding_pct": 50,
          "avg_directors_remuneration_3yr": 8500000,
          "business_loss_latest_year": True}
    calc = compute_business_insurance_value(_case(bi), _hlv(0), RULES)
    assert calc.value == 0, calc.value
    assert calc.method == "loss_latest_year_no_cover", calc.method


def test_keyman_minus_existing_cover():
    bi = {"type": "Keyman", "shareholding_pct": 50,
          "avg_gross_profit_3yr": 5000000,             # ×3×.5 = 75 L
          "existing_keyman_partnership_cover": 2000000}
    calc = compute_business_insurance_value(_case(bi), _hlv(0), RULES)
    assert calc.value == 7500000 - 2000000, calc.value


def test_keyman_cap_100cr():
    bi = {"type": "Keyman", "shareholding_pct": 100,
          "avg_directors_remuneration_3yr": 200000000}  # ×10 = 200 Cr
    calc = compute_business_insurance_value(_case(bi), _hlv(0), RULES)
    assert calc.value == 1000000000, calc.value          # capped at ₹100 Cr


def test_partnership_lower_of_two():
    bi = {"type": "Partnership", "shareholding_pct": 50,
          "avg_gross_profit_3yr": 10000000,            # ×3×.5 = 1.5 Cr
          "avg_net_profit_after_tax_3yr": 4000000,
          "avg_depreciation_3yr": 2000000}             # (4M+600k)×5×.5 = 1.15 Cr
    calc = compute_business_insurance_value(_case(bi), _hlv(0), RULES)
    assert calc.value == 11500000, calc.value
    assert calc.method == "partnership_lower_of", calc.method


def test_employer_employee_salary_hlv_plus_stake():
    bi = {"type": "Employer-Employee", "shareholding_pct": 50,
          "avg_net_profit_after_tax_3yr": 2000000}     # stake = 2M×.5 = 1M
    calc = compute_business_insurance_value(_case(bi), _hlv(30000000), RULES)
    assert calc.value == 31000000, calc.value          # 3 Cr salary HLV + 10 L
    assert calc.method == "ee_salary_hlv_plus_npat_share", calc.method


def test_missing_inputs_flagged():
    bi = {"type": "Keyman", "avg_directors_remuneration_3yr": 5000000}  # only (a)
    calc = compute_business_insurance_value(_case(bi), _hlv(0), RULES)
    assert calc.value == 50000000, calc.value          # uses available formula
    assert calc.inputs.get("missing_inputs"), calc.inputs


def test_not_applicable_returns_none():
    assert compute_business_insurance_value(
        {"business_insurance": {"applicable": False}}, _hlv(0), RULES) is None
