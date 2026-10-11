"""Regression checks for the user's seven-case follow-up table."""

import json

from underwriting_platform.engine.requirements import compute_requirements_to_raise
from underwriting_platform.engine.run import run_engine
from underwriting_platform.engine.sbi_financial import _aggregate_iib_sa, check_iib_gate
from underwriting_platform.engine.types import GatingItem
from underwriting_platform.qc.reviewer_baseline import compare_reviewer_decisions
from underwriting_platform.renderer.triage_sheet import (
    _build_docs_col,
    _build_risk_rows,
    _display_employer,
    _display_nominees,
)
from underwriting_platform.configuration.paths import CONFIG_ROOT

SBI = json.loads((CONFIG_ROOT / "sbi_smart_shield_rules.json").read_text(encoding="utf8"))
RULES = json.loads((CONFIG_ROOT / "uw_rules.json").read_text(encoding="utf8"))


def test_validation_agreement_cannot_hide_uw_disagreement():
    comparison = compare_reviewer_decisions(
        " FURTHER REQUIREMENT", "DECLINE", "DECLINE", source_available=True
    )
    assert comparison["uw_match"] == "MISMATCH"
    assert comparison["validation_match"] == "MATCH"
    assert comparison["baseline_conflict"]
    assert comparison["overall"] == "MISMATCH_VS_UW"


def test_absent_sources_cannot_pass_both_baselines():
    comparison = compare_reviewer_decisions(
        "DECLINE", "DECLINE", "INCOMPLETE", source_available=False
    )
    assert comparison["uw_match"] == comparison["validation_match"] == "UNVERIFIED"


def test_completed_empty_iib_search_is_distinct_from_missing_evidence():
    case = {
        "case_id": "SYNTHETIC",
        "insurer": "SBI Life",
        "applicant": {"age": 35, "occupation": "Salaried", "education_level": "Graduate"},
        "sbi_bureau": {"drc_category": "Standard", "profile_score": "Green", "iib_policies": []},
    }
    decision = run_engine(case, RULES, 10000000)
    assert not any(g["criterion"] == "sbi_bureau_evidence" for g in decision["gating"])
    del case["sbi_bureau"]["iib_policies"]
    decision = run_engine(case, RULES, 10000000)
    assert any(
        g["criterion"] == "sbi_bureau_evidence" and g["status"] == "FAIL"
        for g in decision["gating"]
    )


def test_masked_insurer_cannot_be_assumed_other_or_known_sbi_band():
    bureau = {
        "iib_policies": [
            {
                "insurer": "Company name is masked/not found",
                "sum_assured_inr": 11000000,
                "status": "Inforce",
            }
        ]
    }
    aggregate = _aggregate_iib_sa(bureau)
    assert aggregate["combined_total_sa"] == 11000000
    assert aggregate["unknown_insurer_total_sa"] == 11000000
    assert aggregate["iib_other_total_sa"] == 0
    gate = check_iib_gate({"sbi_bureau": bureau}, SBI)
    assert gate.status == "WARN"
    assert "cannot be confirmed" in gate.evidence
    requirements = compute_requirements_to_raise({"sbi_bureau": bureau}, {}, [gate])
    assert any(r["critical"] and "Confirm insurer identities" in r["text"] for r in requirements)


def test_named_iib_exposure_still_enforces_carrier_cap():
    gate = check_iib_gate(
        {
            "sbi_bureau": {
                "iib_policies": [
                    {"insurer": "LIC", "sum_assured_inr": 11000000, "status": "Inforce"}
                ]
            }
        },
        SBI,
    )
    assert gate.status == "FAIL"
    assert "exceeds cap" in gate.evidence


def test_unavailable_business_proof_is_consolidated_not_waived():
    case = {
        "applicant": {
            "occupation": "Self-Employed Business",
            "employer_name": "Finance",
            "business_nature": "Finance (Promoter)",
        },
        "compliance": {
            "business_proof_evidence": {
                "availability": "customer_confirmed_unavailable",
                "source_document": "customer-letter.pdf",
            }
        },
    }
    gate = GatingItem(
        "sbi_occupation_income_proof",
        "Business proof",
        "WARN",
        evidence="Standard income proof and proof of business required",
    )
    items = compute_requirements_to_raise(case, {}, [gate])
    assert not any(r["text"].startswith("Proof of business (") for r in items)
    assert any(
        "Exact nature of business" in r["text"] and "UW approval" in r["text"] for r in items
    )
    del case["compliance"]["business_proof_evidence"]["source_document"]
    assert any(
        r["text"].startswith("Proof of business (")
        for r in compute_requirements_to_raise(case, {}, [gate])
    )


def test_both_nominees_displayed_and_repeated_mention_does_not_duplicate():
    app = {
        "nominees": [
            {"name": "Synthetic Parent", "relation": "Husband", "share_pct": 50},
            {"name": "Synthetic Child", "relation": "Daughter", "share_pct": 50},
            {"name": "Ms. Synthetic Child", "relation": "Daughter", "share_pct": None},
        ]
    }
    assert len(_display_nominees(app)) == 2
    rows = _build_docs_col({"applicant": app})
    text = " ".join(value for label, value, tone in rows if label.startswith("NOMINEE"))
    assert "Husband 50%" in text and "Daughter 50%" in text
    assert len(app["nominees"]) == 3


def test_employer_display_uses_consistent_payslips_without_overwriting_declaration():
    decision = {
        "applicant": {"occupation": "Salaried", "employer_name": "BATTALION LOCALITY DIST"},
        "extraction_blocks": {
            "income": {"salary_slips": [{"employer": "Government Police Department"}]}
        },
    }
    assert _display_employer(decision) == "Government Police Department"
    assert decision["applicant"]["employer_name"] == "BATTALION LOCALITY DIST"


def test_conflicting_duplicate_nominee_shares_are_not_certified_as_matching():
    decision = {
        "applicant": {
            "nominees": [
                {"name": "Name variant A", "relation": "Wife", "share_pct": 100},
                {"name": "Name variant B", "relation": "Wife", "share_pct": 100},
            ]
        }
    }
    rows = _build_risk_rows(decision)["LOW"]
    nominee_row = next(row for row in rows if row[0] == "Nominee relationship")
    assert "shares exceed 100%" in nominee_row[1]
    assert "insurable interest match" not in nominee_row[1]
