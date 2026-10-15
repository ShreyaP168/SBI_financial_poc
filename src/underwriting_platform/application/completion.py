"""Decision completeness is independent of artifact quality or business approval."""

from __future__ import annotations

from underwriting_platform.contracts.canonical import case_issues, decision_issues
from underwriting_platform.domain.policy import GENERIC_GATES, SBI_GATES, is_sbi_case

POLICY_VERSION = "delivery-3.1"
CORE_FIELDS = (
    "applicant.applicant_name",
    "applicant.age",
    "applicant.occupation",
    "applicant.education_level",
    "applicant.residential_status",
    "applicant.sum_assured",
    "applicant.gender",
)
CONDITIONAL_EVIDENCE_FIELDS = ("applicant.employer_name", "applicant.business_nature")


def field_value(case: dict, path: str):
    value = case
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def identity_blocked(case: dict) -> bool:
    return case.get("_extraction_mode") == "local_first" and any(
        error.get("type") != "LOCAL_EVIDENCE_REVIEW_REQUIRED"
        for error in case.get("processing_errors") or []
    )


def evidence_present(records) -> bool:
    return isinstance(records, list) and any(
        isinstance(s, dict)
        and s.get("document")
        and (s.get("page") or s.get("field_name") or s.get("json_pointer"))
        and s.get("raw_value") is not None
        and s.get("raw_value") != ""
        for s in records
    )


def assess_decision(case: dict, decision: dict) -> dict:
    if not isinstance(case, dict) or not isinstance(decision, dict):
        return {
            "policy_version": POLICY_VERSION,
            "decision_complete": False,
            "issues": ["CANONICAL_OBJECT_REQUIRED"],
            "issuance_approved": False,
        }
    issues = (
        case_issues(case)
        + decision_issues({**decision, "_normalized_input": case})
        + list(case.get("_contract_issues") or [])
        + list(decision.get("_contract_issues") or [])
    )
    if any(issue.startswith(("CASE_SCHEMA:", "DECISION_SCHEMA:")) for issue in issues):
        return {
            "policy_version": POLICY_VERSION,
            "decision_complete": False,
            "issues": sorted(set(issues)),
            "issuance_approved": False,
        }
    for stage in ("extraction", "identity"):
        if (case.get("_processing_stages") or {}).get(stage) != "completed":
            issues.append(stage.upper() + "_NOT_COMPLETED")
    if case.get("extraction_findings"):
        issues.append("SOURCE_EVIDENCE_REVIEW_REQUIRED")
    if case.get("processing_errors") or decision.get("processing_errors"):
        issues.append("MANDATORY_PROCESSING_FAILED")
    if (decision.get("verdict") or {}).get("label") in {None, "INCOMPLETE", "ERROR"}:
        issues.append("ENGINE_INCOMPLETE")
    if (decision.get("verdict") or {}).get("label") in {"STANDARD_ISSUANCE", "STANDARD_ACCEPTANCE"}:
        app = case.get("applicant") or {}
        match = app.get("photo_match") or {}
        review = case.get("identity_review") or {}
        approved_review = review.get("status") == "approved" and all(
            review.get(key) for key in ("reviewer_id", "reviewed_at", "reference")
        )
        if (case.get("_identity_classifier") or {}).get("requires_review") and not approved_review:
            issues.append("DOCUMENT_CLASSIFICATION_REVIEW_REQUIRED")
        if not approved_review and (
            match.get("match_result") != "match"
            or match.get("requires_manual_review")
            or match.get("error")
        ):
            issues.append("IDENTITY_REVIEW_REQUIRED")
    for path in CORE_FIELDS:
        value = field_value(case, path)
        if path in {"applicant.age", "applicant.sum_assured"}:
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                issues.append("INVALID_FIELD:" + path)
        elif not isinstance(value, str) or not value.strip():
            issues.append("INVALID_FIELD:" + path)
        if field_value(case, path) in (None, "", 0):
            issues.append("REQUIRED_FIELD:" + path)
        source_path = (
            "applicant.dob"
            if path == "applicant.age" and not (case.get("field_sources") or {}).get(path)
            else path
        )
        if not evidence_present((case.get("field_sources") or {}).get(source_path)):
            issues.append("FIELD_EVIDENCE:" + path)
    for path in CONDITIONAL_EVIDENCE_FIELDS:
        if field_value(case, path) and not evidence_present(
            (case.get("field_sources") or {}).get(path)
        ):
            issues.append("FIELD_EVIDENCE:" + path)
    gates = {g.get("criterion"): g for g in decision.get("gating") or []}
    required = set(SBI_GATES if is_sbi_case(case) else GENERIC_GATES)
    required.update({"income_documented", "kyc", "sa_within_hlv"})
    if case.get("medical"):
        required.update(
            {"medical_compliance", "fraud_signal"}
            if not is_sbi_case(case)
            else {"sbi_medical_grid"}
        )
    for name in sorted(required):
        gate = gates.get(name)
        if gate is None:
            issues.append("REQUIRED_GATE:" + name)
        elif gate.get("status") not in {"PASS", "WARN", "FAIL", "NA"}:
            issues.append("GATE_STATUS:" + name)
    for gate in gates.values():
        if gate.get("status") == "NA" or gate.get("criterion") not in required:
            continue
        if not gate.get("rule") or not gate.get("evidence"):
            issues.append("GATE_CITATION:" + str(gate.get("criterion")))
        if not any(s.get("document") and s.get("field_path") for s in gate.get("sources") or []):
            issues.append("GATE_SOURCE:" + str(gate.get("criterion")))
    for name in ("age", "selected_income", "hlv_base", "max_sa_eligible"):
        calc = (decision.get("calculations") or {}).get(name) or {}
        if not calc or (
            calc.get("value") is not None and (not calc.get("rule") or not calc.get("sources"))
        ):
            issues.append("CALC_AUDIT:" + name)
    return {
        "policy_version": POLICY_VERSION,
        "decision_complete": not issues,
        "issues": sorted(set(issues)),
        "issuance_approved": not issues
        and (decision.get("verdict") or {}).get("label")
        in {"STANDARD_ISSUANCE", "STANDARD_ACCEPTANCE"},
    }


def delivery_outcome(
    completion: dict, qc: dict | None, failures: list[str], *, pdf_required: bool = True
) -> dict:
    reasons = list(failures) + list(completion.get("issues") or [])
    if pdf_required:
        if not qc:
            reasons.append("QC_NOT_COMPLETED")
        elif qc.get("status") not in {"PASS", "WARN"} or any(
            f.get("code") == "QC_INFRASTRUCTURE_FAILURE" for f in qc.get("findings") or []
        ):
            reasons.append("QC_BLOCKED_OR_FAILED")
    allowed = not reasons and pdf_required
    return {
        "policy_version": POLICY_VERSION,
        "status": "OK" if allowed else "INCOMPLETE" if reasons else "DIAGNOSTIC",
        "delivery_allowed": allowed,
        "decision_complete": completion.get("decision_complete", False),
        "qc_detected_no_block": bool(qc and qc.get("status") in {"PASS", "WARN"}),
        "blocking_reasons": sorted(set(reasons)),
        "exit_code": 1 if reasons else 0,
    }
