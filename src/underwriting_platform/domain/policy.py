"""Shared routing and required evaluation names; independent of the rule engine."""

GENERIC_GATES = {
    "income_documented",
    "residential_status",
    "kyc",
    "sa_within_hlv",
    "existing_cover_disclosed",
}
SBI_GATES = {
    "sbi_cibil_score",
    "sbi_iib_gate",
    "sbi_drc_income_floor",
    "sbi_qualification",
    "sbi_occupation_income_proof",
    "sbi_student_agriculturist_riders",
    "sbi_keyman_partnership_ee_age_cap",
    "sbi_salaried_extended_cover",
    "sbi_material_non_disclosure",
    "sbi_pending_proposal_disclosure",
    "sbi_bhb_eligibility",
    "sbi_hlv_remaining",
    "sbi_income_over_declaration",
    "sbi_piwc_requirement",
    "sbi_medical_grid",
}


def is_sbi_case(case: dict) -> bool:
    fields = []
    for block in (
        case,
        case.get("applicant"),
        case.get("system_data"),
        case.get("policy_data"),
        case.get("routing"),
    ):
        if isinstance(block, dict):
            fields.extend(
                str(block.get(key) or "").lower()
                for key in (
                    "insurer",
                    "carrier",
                    "product",
                    "product_name",
                    "plan_name",
                    "rule_set",
                )
            )
    if any("sbi" in value or "smart shield" in value for value in fields):
        return True
    if any("tata" in value or "talic" in value or "bajaj" in value for value in fields):
        return False
    return "sbi_bureau" in case
