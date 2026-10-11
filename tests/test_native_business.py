from underwriting_platform.agents.native_business import reconcile_native_business
from underwriting_platform.llm.pdf_utils import Document


def test_exact_declared_business_is_cited_without_claiming_inferred_nature():
    case = {
        "applicant": {
            "employer_name": "Kirana store",
            "business_nature": "Kirana store / grocery retail",
        }
    }
    doc = Document(
        "proposal.pdf",
        "--- page 3 ---\nName of Business Organization Kirana store Your Role Proprietor",
    )
    changes = reconcile_native_business(case, [doc], {doc.filename: "proposal_form"})
    assert len(changes) == 2
    assert case["field_sources"]["applicant.employer_name"][0]["page"] == 3
    assert case["field_sources"]["applicant.employer_name"][0]["raw_value"] == "Kirana store"
    assert case["applicant"]["business_nature"] is None
    assert case["_model_inferences"]["business_nature"] == "Kirana store / grocery retail"


def test_unmatched_name_or_cited_nature_is_never_replaced():
    case = {"applicant": {"employer_name": "Different business", "business_nature": "Finance"}}
    doc = Document(
        "proposal.pdf",
        "--- page 1 ---\nName of Business Organization Kirana store Your Role Proprietor",
    )
    assert not reconcile_native_business(case, [doc], {doc.filename: "proposal_form"})
    assert case["applicant"]["business_nature"] == "Finance"
    case["applicant"]["employer_name"] = "Kirana store"
    case["applicant"]["business_nature"] = "Kirana store / explicitly declared trade"
    case["field_sources"]["applicant.business_nature"] = [
        {"document": "proof.pdf", "page": 1, "raw_value": "trade"}
    ]
    reconcile_native_business(case, [doc], {doc.filename: "proposal_form"})
    assert case["applicant"]["business_nature"] == "Kirana store / explicitly declared trade"
