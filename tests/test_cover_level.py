"""
tests/test_cover_level.py — Final-Decision cover-level (full/restricted/stretched).

Datamatics LAYOUT FORMAT sheet defines the three cover levels. Corrected
2026-06-07: "restricted" = insurer offers LESS than applied (max_sa_eligible <
proposed_sa), NOT a customer who applied below their HLV.
"""
from underwriting_platform.engine.verdict import _classify_final_decision_cell
from underwriting_platform.engine.types import Calculation


def _calcs(max_sa, emr=0, med_risk="LOW"):
    return {
        "max_sa_eligible": Calculation(name="max_sa_eligible", value=max_sa),
        "loading":         Calculation(name="loading", value=emr,
                                       inputs={"emr": emr}),
        "medical_risk":    Calculation(name="medical_risk", value=med_risk),
    }


def _cell(label, max_sa, proposed_sa, hlv_base):
    return _classify_final_decision_cell(
        label=label, case={"medical": {}}, calcs=_calcs(max_sa),
        proposed_sa=proposed_sa, hlv_base=hlv_base)


def test_full_cover_when_full_sa_grantable():
    # proposed ₹4Cr, HLV ₹7Cr, eligible = full ₹4Cr → full
    assert _cell("STANDARD_ACCEPTANCE", 40000000, 40000000, 70000000) \
        .endswith("· full")


def test_restricted_when_eligible_below_proposed():
    # eligible ₹3Cr < proposed ₹5Cr (existing cover / cap bind) → restricted
    assert _cell("CONDITIONAL", 30000000, 50000000, 70000000) \
        .endswith("· restricted")


def test_stretched_when_proposed_above_hlv():
    # proposed ₹7.2Cr just above HLV ₹7Cr (within 1.05 tolerance) → stretched
    assert _cell("STANDARD_ACCEPTANCE", 70000000, 72000000, 70000000) \
        .endswith("· stretched")


def test_under_buying_is_full_not_restricted():
    # Regression (C278817070): proposed ₹4Cr is < 0.7×HLV (₹4.9Cr) but the full
    # ₹4Cr is grantable → must be FULL, not the old "restricted".
    assert _cell("CONDITIONAL", 40000000, 40000000, 70000000) \
        .endswith("· full")
