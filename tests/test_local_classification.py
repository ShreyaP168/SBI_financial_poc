import io

import pytest
from PIL import Image

from underwriting_platform.llm.pdf_utils import Document
from underwriting_platform.ocr.classification import classify_text, survey_documents
from underwriting_platform.orchestrator import MasterOrchestrator


@pytest.mark.parametrize(
    "text,kind,label",
    [
        (
            "INCOME TAX DEPARTMENT Government of India Permanent Account Number Card ABCDE1234F",
            "id_proof",
            "PAN_CARD",
        ),
        ("Government of India DOB 01/01/1980 1234 5678 9012", "id_proof", "AADHAAR_FRONT"),
        ("Unique Identification Authority Address 1234 5678 9012", "id_proof", "AADHAAR_BACK"),
        ("Republic of India Passport", "id_proof", "PASSPORT"),
        ("Driving Licence Transport Department Date of Issue", "id_proof", "DRIVING_LICENSE"),
        (
            "Indian Income Tax Return ITR-3 Assessment Year 2025-26 Gross Total Income 800000",
            "itr",
            "FORM_OR_TEXT",
        ),
        ("FORM 16 Certificate of Tax Deducted at Source", "form_16", "FORM_OR_TEXT"),
        ("PAYSLIP Employee 555 Gross Pay 70000 Net Pay 65000", "salary_slip", "FORM_OR_TEXT"),
        ("Statement of Account Date Debit Credit Balance", "bank_statement", "FORM_OR_TEXT"),
        ("Medical Examination Report Patient history", "mer", "FORM_OR_TEXT"),
        ("BYMPS7486M SREEKANTH S SARAVANA BHAVAN 07/03/1989 Male BYMPS7486M", "id_proof", "PAN_CARD"),
        (
            "Enrolment No.: 0636/16266/01188 To Sreekanth S VTC: Kollam PIN Code: 691001",
            "id_proof",
            "AADHAAR_LETTER",
        ),
        (
            "A.Y. 2024-2025 Name : Sreekanth Previous Year : 2023-2024 Aadhaar No. : 7240 6984 8650 "
            "Date of Birth : 07-Mar-1989 Total Income 640000",
            "itr",
            "FORM_OR_TEXT",
        ),
        (
            "Nominee Details Nomination in favor of non beneficial nominee "
            "Declaration to be given by the Proposer",
            "proposal_form",
            "FORM_OR_TEXT",
        ),
        ("REPUBLICOF INDIA ZA345280 P<INDDOE<<JANE<<<<<<<<<<<<<<", "id_proof", "PASSPORT"),
        (
            "Payout intimation for your ICICI Pru Life application no L0949151",
            "correspondence",
            "INSURER_LETTER",
        ),
        (
            "Payments <no-reply@razorpay.com> Payment successful for SBI LIFE INSURANCE",
            "correspondence",
            "PAYMENT_RECEIPT",
        ),
        (
            "Type of Organisation Proprietary Owner Name Do you have GSTIN No",
            "corporate_doc",
            "FORM_OR_TEXT",
        ),
        (
            "From : a@b.com Subject : RE: income tax return clarification Body : see ITR-1 "
            "Assessment Year 2025-26",
            "correspondence",
            "EMAIL",
        ),
    ],
)
def test_local_content_classification(text, kind, label):
    result = classify_text(text)
    assert (result["kind"], result["label"]) == (kind, label)
    assert not result["requires_review"]


def test_proposal_id_references_do_not_become_an_id_card():
    text = "Proposal Form attach INCOME TAX DEPARTMENT Permanent Account Number ABCDE1234F"
    assert classify_text(text)["kind"] == "proposal_form"


def test_bare_identity_pattern_in_a_long_document_is_not_a_pan_card():
    text = "BYMPS7486M 07/03/1989 Male " + "unrelated narrative " * 40
    result = classify_text(text)
    assert (result["kind"], result["extraction_required"]) == ("other", True)


def test_unknown_and_conflicting_documents_are_retained_for_all_domains():
    agents = ["identity", "income", "compliance", "medical"]
    documents = [
        Document("PAN_CARD.pdf", "Medical Examination Report"),
        Document("unknown.pdf", "unrecognized fragment"),
        Document("mixed.pdf", "Statement of Account Debit Credit Balance PAYSLIP Employee Net Pay"),
    ]
    result = survey_documents(documents, agents)
    assert result["files"][0]["kind"] == "mer"
    for row in result["files"][1:]:
        assert row["kind"] == "other" and row["requires_review"] and row["extraction_required"]
        assert row["relevance"] == agents


def test_low_confidence_ocr_cannot_supply_identity_anchors():
    document = Document(
        "scan.pdf",
        "--- page 1 ---\nINCOME TAX DEPARTMENT Permanent Account Number ABCDE1234F",
        ocr_pages=[
            {
                "page": 1,
                "lines": [
                    {
                        "text": "INCOME TAX DEPARTMENT Permanent Account Number ABCDE1234F",
                        "confidence": 0.1,
                    }
                ],
            }
        ],
    )
    assert survey_documents([document], ["identity"])["files"][0]["kind"] == "other"


@pytest.mark.parametrize("face,kind,review", [(True, "photograph", False), (False, "other", True)])
def test_textless_image_is_a_photograph_only_when_a_face_is_detected(monkeypatch, face, kind, review):
    import underwriting_platform.ocr.photo as photo

    monkeypatch.setattr(photo, "contains_face", lambda encoded: face)
    document = Document("scan.jpg", "", ["encoded"], 1, image_page_numbers=[1])
    row = survey_documents([document], ["identity"])["files"][0]
    assert (row["kind"], row["requires_review"]) == (kind, review)
    assert row["extraction_required"] is not face
    if face:
        assert row["relevance"] == ["identity"]


def test_pages_without_distinctive_text_continue_the_documents_kind():
    text = (
        "--- page 1 ---\nPAYSLIP Employee 555 Gross Pay 70000 Net Pay 65000\n"
        "--- page 2 ---\nnotes\n--- page 3 ---\nPAYSLIP Employee 555 Gross Pay 71000 Net Pay 66000\n"
    )
    row = survey_documents([Document("slips.pdf", text)], ["income"])["files"][0]
    assert (row["kind"], row["requires_review"], row["extraction_required"]) == (
        "salary_slip",
        False,
        False,
    )
    assert row["pages"][1]["label"] == "CONTINUATION"


def test_sections_of_one_form_classify_as_its_dominant_kind_with_subdocs():
    text = (
        "--- page 1 ---\nProposal No. 1 Common Proposal Form life assured\n"
        "--- page 2 ---\nProposal No. 1 life assured\n"
        "--- page 3 ---\nProposal No. 1 life assured FATCA declaration\n"
        "--- page 4 ---\nProposal No. 1 life assured Benefit Illustration premium\n"
    )
    row = survey_documents([Document("form.pdf", text)], ["identity"])["files"][0]
    assert (row["kind"], row["requires_review"]) == ("proposal_form", False)
    assert row["subdocs"] == ["kyc_declaration", "policy_illustration"]


def test_photo_page_does_not_hide_the_kind_of_a_text_page(monkeypatch):
    import underwriting_platform.ocr.photo as photo

    monkeypatch.setattr(photo, "contains_face", lambda encoded: True)
    text = (
        "--- page 1 ---\nPAYSLIP Employee 555 Gross Pay 70000 Net Pay 65000\n"
        "--- page 2 ---\n"
    )
    document = Document("mixed.pdf", text, ["a", "b"], 2, image_page_numbers=[1, 2])
    row = survey_documents([document], ["income"])["files"][0]
    assert (row["kind"], row["requires_review"]) == ("salary_slip", False)


async def test_master_survey_never_calls_the_model():
    class Forbidden:
        async def extract_json(self, *args, **kwargs):
            pytest.fail("Cloud classification called")

    result = await MasterOrchestrator(Forbidden(), reconcile=False)._survey(
        [Document("generic.pdf", "PAYSLIP Employee Earnings Gross Pay")]
    )
    assert result["files"][0]["kind"] == "salary_slip"
    assert result["provider_tokens"] == 0


async def test_identity_classification_uses_local_ocr_without_gemini(tmp_path, monkeypatch):
    import underwriting_platform.engine.identity_verification as identity
    import underwriting_platform.llm.gemini_budget as gemini
    import underwriting_platform.ocr as ocr

    buffer = io.BytesIO()
    Image.new("RGB", (300, 300), "white").save(buffer, format="PNG")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-unused-key")
    monkeypatch.setattr(
        identity,
        "_gather_classification_candidates",
        lambda *args: [("generic.png", buffer.getvalue())],
    )
    monkeypatch.setattr(
        ocr,
        "read_page",
        lambda *args: {
            "status": "completed",
            "lines": [
                {
                    "text": "INCOME TAX DEPARTMENT Permanent Account Number ABCDE1234F",
                    "confidence": 0.95,
                }
            ],
        },
    )

    async def forbidden(*args, **kwargs):
        pytest.fail("Gemini classification invoked")

    monkeypatch.setattr(gemini, "generate", forbidden)
    result = await identity.classify_case_documents(tmp_path)
    assert result["generic.png"][0] == "PAN_CARD"


async def test_prepared_classification_reuses_ocr_without_reading_it_again(tmp_path, monkeypatch):
    import underwriting_platform.engine.identity_verification as identity
    import underwriting_platform.ocr as ocr

    monkeypatch.setattr(
        identity, "_gather_classification_candidates", lambda *args: [("generic.pdf#p3", b"image")]
    )
    monkeypatch.setattr(ocr, "read_page", lambda *args: pytest.fail("Duplicate OCR"))
    prepared = {"files": [{"filename": "generic.pdf", "pages": [{"page": 3, "label": "PAN_CARD"}]}]}
    assert (await identity.classify_case_documents(tmp_path, prepared))["generic.pdf#p3"][
        0
    ] == "PAN_CARD"


async def test_classification_does_not_upgrade_kyc(tmp_path, monkeypatch):
    import underwriting_platform.engine.identity_verification as identity

    async def labels(*args, **kwargs):
        return {"proof.png": ("PAN_CARD", b"image")}

    async def noop(*args, **kwargs):
        pass

    monkeypatch.setattr(identity, "classify_case_documents", labels)
    for name in ("_resolve_dc_distance", "_resolve_photo_match", "_resolve_photo_match_set"):
        monkeypatch.setattr(identity, name, noop)
    case = {
        "applicant": {},
        "compliance": {"kyc_status": "Partial", "kyc_evidence": {"pan_present": False}},
    }
    case["_document_classification"] = {
        "files": [{"filename": "mixed.pdf", "pages": [{"requires_review": True}]}]
    }
    await identity.run_identity_verification(case, tmp_path)
    assert case["compliance"]["kyc_status"] == "Partial"
    assert case["compliance"]["kyc_evidence"]["pan_present"] is False
    assert case["_identity_classifier"]["requires_review"] is True


def test_unknown_classification_blocks_standard_delivery_without_manual_review():
    import copy

    from support.extraction_fixture import synthetic_case

    from underwriting_platform.application.completion import assess_decision
    from underwriting_platform.engine import run_engine

    case = synthetic_case("LOCALCLASS")
    case["_identity_classifier"] = {"requires_review": True}
    decision = run_engine(copy.deepcopy(case), {}, 10000000)
    decision["verdict"]["label"] = "STANDARD_ISSUANCE"
    assert "DOCUMENT_CLASSIFICATION_REVIEW_REQUIRED" in assess_decision(case, decision)["issues"]
