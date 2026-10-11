import pytest
from PIL import Image

from underwriting_platform.extraction.coordinator import extract_local_first
from underwriting_platform.extraction.local_fields import extract_local
from underwriting_platform.llm.pdf_utils import Document
from underwriting_platform.ocr.paddle_structure import PaddleStructureExtractor


class NoCloud:
    async def extract_json(self, *args, **kwargs):
        pytest.fail("Unexpected cloud extraction")


def test_labeled_fields_preserve_evidence_and_currency_units():
    document = Document(
        "proposal.pdf",
        "--- page 3 ---\nApplicant Name: Example Person\nApplicant Age: 35\nDOB: 01/02/1991\nGender: Male\nOccupation: Self-Employed Business\nEducation: Graduate\nResidential Status: Resident\nSum Assured: 50 lakh\nName of Business Organization Finance Your Role Owner",
    )
    block, sources, findings, _ = extract_local([document])
    assert block["applicant"]["sum_assured"] == 5000000
    assert block["applicant"]["employer_name"] == "Finance"
    assert block["applicant"]["dob"] == "1991-02-01"
    assert sources["applicant.sum_assured"][0]["page"] == 3
    assert sources["applicant.sum_assured"][0]["raw_value"] == "50 lakh"
    assert not findings


def test_conflicting_age_is_not_arbitrarily_resolved():
    block, _, findings, _ = extract_local(
        [Document("a.pdf", "Applicant Age: 35"), Document("b.pdf", "Applicant Age: 45")]
    )
    assert "age" not in block["applicant"]
    assert findings[0]["code"] == "CONFLICTING_LOCAL_EVIDENCE"


def test_salary_periods_deduplicate_and_engine_calculates_annual_income():
    documents = [
        Document(str(index), f"Salary Slip\nSalary Month: 2026-0{index}\nGross Salary: 70,000")
        for index in (1, 2, 3)
    ]
    block, sources, _, _ = extract_local(documents + [documents[0]])
    assert len(block["income"]["salary_slips"]) == 3
    assert block["income"]["annual_income"] == 840000
    assert len(sources["income.salary_slips"]) == 3


def test_itr_earned_salary_is_not_gross_total_and_duplicate_ay_is_removed():
    doc = Document(
        "itr.pdf",
        "Indian Income Tax Return ITR-1\nAssessment Year: 2025-26\nIncome from Salary: 500000\nGross Total Income: 700000",
    )
    block, _, _, _ = extract_local([doc, doc])
    assert block["income"]["annual_income"] == 500000
    assert len(block["income"]["itr_years"]) == 1


async def test_local_default_makes_no_cloud_calls(monkeypatch):
    monkeypatch.delenv("UW_TARGETED_LLM_FALLBACK", raising=False)
    result, report = await extract_local_first(
        NoCloud(), [Document("unknown.pdf", "Unclear evidence")]
    )
    assert report["targeted_calls"] == 0
    assert result.error == "LOCAL_EVIDENCE_REVIEW_REQUIRED"


async def test_targeted_fallback_is_bounded_text_only_and_does_not_override_local_fields(
    monkeypatch,
):
    monkeypatch.setenv("UW_TARGETED_LLM_FALLBACK", "true")
    calls = []

    class Client:
        async def extract_json(self, system, blocks, **kwargs):
            calls.append(1)
            assert all(b["type"] == "text" for b in blocks)
            assert len(blocks[0]["text"]) <= 18000
            assert kwargs["max_tokens"] == 3500
            return {
                "fields": [
                    {
                        "path": "applicant.age",
                        "value": 99,
                        "document": "p.pdf",
                        "page": 1,
                        "raw_value": "35",
                    },
                    {
                        "path": "applicant.gender",
                        "value": "Male",
                        "document": "p.pdf",
                        "page": 1,
                        "raw_value": "Male",
                    },
                    {
                        "path": "applicant.education_level",
                        "value": "Graduate",
                        "document": "invented.pdf",
                        "page": 4,
                        "raw_value": "Graduate",
                    },
                ]
            }

    result, report = await extract_local_first(
        Client(), [Document("p.pdf", "Applicant Age: 35\nApplicant gender is Male")]
    )
    assert len(calls) == 1 and report["targeted_calls"] == 1
    assert result.block["applicant"]["age"] == 35
    assert result.block["applicant"]["gender"] == "Male"
    assert "education_level" not in result.block["applicant"]


def test_paddle_adapter_retains_text_boxes_and_table_html_without_network():
    class Backend:
        def predict(self, **kwargs):
            yield {
                "res": {
                    "overall_ocr_res": {
                        "rec_texts": ["Gross Salary 70000"],
                        "rec_scores": [0.95],
                        "rec_boxes": [[1, 2, 100, 20]],
                    },
                    "table_res_list": [
                        {"pred_html": "<table><tr><td>Gross Salary</td><td>70000</td></tr></table>"}
                    ],
                }
            }

    reader = PaddleStructureExtractor(Backend())
    lines = reader.extract(Image.new("RGB", (200, 100)))
    assert lines[0].text == "Gross Salary 70000"
    assert lines[0].confidence == 0.95
    assert reader.last_tables[0]["html"].startswith("<table>")


def test_bank_credits_use_credit_not_balance_and_require_three_months():
    doc = Document(
        "bank.pdf",
        "--- page 1 ---\nBank statement",
        tables=[
            {
                "page": 1,
                "rows": [
                    ["Date", "Narration", "Debit", "Credit", "Balance"],
                    ["01/01/2026", "SALARY", "0", "70000", "900000"],
                    ["01/02/2026", "SALARY", "0", "70000", "970000"],
                    ["01/03/2026", "SALARY", "0", "70000", "1040000"],
                ],
            }
        ],
    )
    block, _, _, _ = extract_local([doc])
    assert block["income"]["annual_income"] == 840000


async def test_unsupported_rider_is_not_silently_dropped(monkeypatch):
    monkeypatch.delenv("UW_TARGETED_LLM_FALLBACK", raising=False)
    _, report = await extract_local_first(
        NoCloud(), [Document("p.pdf", "Applicant Age: 22\nOccupation: Student\nRider: ADB")]
    )
    assert "applicant.rider_review" in report["unresolved"]


async def test_default_orchestrator_does_not_call_domain_agents(tmp_path, monkeypatch):
    import underwriting_platform.orchestrator.master as master

    monkeypatch.delenv("UW_EXTRACTION_MODE", raising=False)
    monkeypatch.delenv("UW_TARGETED_LLM_FALLBACK", raising=False)
    monkeypatch.setattr(master, "_collect_case_files", lambda *args: [tmp_path / "p.pdf"])
    monkeypatch.setattr(master, "prepare_doc", lambda *args: Document("p.pdf", "Applicant Age: 35"))
    result = await master.MasterOrchestrator(NoCloud(), reconcile=False).run(tmp_path, "LOCAL")
    assert result.case_json["_extraction_mode"] == "local_first"
    assert result.case_json["applicant"]["age"] == 35
    assert result.extraction_report["source_owner"]["targeted_calls"] == 0


def test_paddle_requires_explicit_local_models(monkeypatch):
    monkeypatch.delenv("UW_PADDLE_CONFIG", raising=False)
    with pytest.raises(ValueError, match="local Paddle"):
        PaddleStructureExtractor().extract(Image.new("RGB", (100, 100)))
