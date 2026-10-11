"""
tests/test_age_entry.py — regression for the case-sheet age off-by-one.

Root cause (verified 2026-06-06 against output_batch_102_201_v9): the engine
computed Age Last Birthday as of ``date.today()`` (the run date), whereas the
SI / policy system records Age Last Birthday as of the risk-commencement
(effective) date in ``policy_data.age_at_entry``. When a birthday falls between
the effective date and the processing date, the engine read +1 — the dominant
QC finding in the 2026-06-06 batch (~20 of 58 cases, all off by exactly +1).

compute_age must therefore prefer the authoritative entry age:
  1. policy_data.age_at_entry  (SI Age at Entry — ALB at risk commencement)
  2. ALB(dob, effective_dt | submission_dt)
  3. ALB(dob, today)            (legacy fallback)
  4. applicant.age (declared)
"""
from underwriting_platform.engine.calculator import compute_age


def test_age_at_entry_wins_over_today_based_dob():
    # dob implies a today-based age nowhere near 37; age_at_entry must win.
    case = {
        "applicant": {"dob": "1990-06-15", "age": 99},
        "policy_data": {"age_at_entry": 37,
                        "effective_dt": "2025-09-10 00:00:00"},
    }
    calc = compute_age(case)
    assert calc.value == 37, calc.value
    assert calc.method == "policy_data.age_at_entry", calc.method


def test_falls_back_to_dob_at_commencement_when_no_age_at_entry():
    # dob 1987-11-15, effective 2025-09-10 → birthday (11-15) not yet reached
    # at commencement → ALB = 37. (today-based would be 38 after 15-Nov-2025.)
    case = {
        "applicant": {"dob": "1987-11-15"},
        "policy_data": {"effective_dt": "2025-09-10 00:00:00"},
    }
    calc = compute_age(case)
    assert calc.value == 37, calc.value
    assert calc.method == "from_dob_at_commencement", calc.method


def test_submission_dt_used_when_effective_absent():
    case = {
        "applicant": {"dob": "1996-03-07"},
        "policy_data": {"submission_dt": "2025-11-04 00:00:00"},
    }
    calc = compute_age(case)
    # 2025-11-04, birthday 03-07 already passed → ALB = 29.
    assert calc.value == 29, calc.value
    assert calc.method == "from_dob_at_commencement", calc.method


def test_legacy_today_fallback_when_no_policy_data():
    case = {"applicant": {"dob": "2000-01-01"}}
    calc = compute_age(case)
    assert calc.method == "from_dob_today", calc.method
    assert isinstance(calc.value, int)
