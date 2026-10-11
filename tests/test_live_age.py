"""Entry age normalization must retain the engine decision and diagnostics."""

import copy

import pytest
from support.extraction_fixture import synthetic_case

from underwriting_platform.application.completion import assess_decision
from underwriting_platform.configuration.settings import Settings
from underwriting_platform.contracts.v1 import DecisionRequest
from underwriting_platform.infrastructure.kernel import KernelUnderwriter


@pytest.mark.parametrize("anchor", ["proposal", "commencement", "policy_entry"])
async def test_source_derived_entry_age_difference_preserves_decision(tmp_path, anchor):
    case = synthetic_case("AGE")
    case["applicant"].update(age=36, dob="1990-06-15", submission_date="2025-01-01")
    if anchor == "commencement":
        case["policy_data"] = {"effective_dt": "2025-01-01"}
    if anchor == "policy_entry":
        case["policy_data"] = {"age_at_entry": 34}
    case["field_sources"]["applicant.dob"] = [
        {"document": "identity.pdf", "page": 1, "raw_value": "1990-06-15"}
    ]
    original = copy.deepcopy(case)
    response = await KernelUnderwriter(Settings()).decide(
        DecisionRequest(case_id="AGE", case_json=case)
    )
    decision = response.decision
    assert decision["applicant"]["age"] == 34
    assert decision["calculations"]["age"]["value"] == 34
    assert decision["verdict"]["label"] != "INCOMPLETE"
    assert decision["gating"] and decision["calculations"]
    assert assess_decision(original, decision)["decision_complete"]
    assert decision["_normalized_input"]["applicant"]["age"] == 36
    assert any(
        f["code"] == "ENTRY_AGE_DIFFERS_FROM_EXTRACTED" for f in decision["advisory_findings"]
    )
