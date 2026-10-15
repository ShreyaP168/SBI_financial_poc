import pytest

from underwriting_platform.extraction.bank import recurring_credits
from underwriting_platform.extraction.documents import proposal_form
from underwriting_platform.extraction.idp import read_slips, slip_targets
from underwriting_platform.extraction.income_documents import (
    business_description,
    computation_page,
    slip_page,
)
from underwriting_platform.extraction.layout import rows_text
from underwriting_platform.extraction.local_fields import extract_local
from underwriting_platform.llm.pdf_utils import Document


def line(text, x0, y0, x1=None, y1=None, confidence=0.99):
    return {"text": text, "x0": x0, "y0": y0, "x1": x1 or x0 + 50, "y1": y1 or y0 + 10, "confidence": confidence}


def test_rows_join_a_label_with_the_value_at_the_same_height():
    lines = [
        line("GROSSTOTALINCOME", 10, 100),
        line("10,25,828", 300, 101),
        line("TOTALINCOME", 10, 130),
        line("10,25,828", 300, 129),
    ]
    assert rows_text(lines).splitlines() == ["GROSSTOTALINCOME 10,25,828", "TOTALINCOME 10,25,828"]


def test_low_confidence_lines_are_left_out_of_rows():
    lines = [line("GROSSPAY", 10, 100), line("999999", 300, 100, confidence=0.5)]
    assert rows_text(lines) == "GROSSPAY"


@pytest.mark.parametrize(
    "text,month,gross",
    [
        ("Salary Slip May-2026 PAYSLIPFORTHEMONTHOFMAY2026\nGROSSEARNINGS(E) 19,965.00 TOTALDEDUCTIONS(D) 2,813.00", "2026-05", 19965),
        ("PAYSLIPFORTHEMONTHOFMay-2026\nGrossPay: ：102331 Net Pay: 93101", "2026-05", 102331),
        (
            "Salary Slip for the Month of July - 2026\nGross Earnings 31,703 5,114\nNet Pay 4,732\n"
            "Income Tax Computation Sheet\nGross Salary 2,58,738",
            "2026-07",
            5114,
        ),
    ],
)
def test_salary_slip_gross_and_month_from_merged_ocr_text(text, month, gross):
    row, _ = slip_page(text)
    assert row == {"month_year": month, "gross_salary": gross}


def test_a_page_without_a_readable_month_or_gross_is_not_a_slip():
    assert slip_page("PAYSLIPFORTHMONTHOFJUe-26\nGrossPay 111533") is None
    assert slip_page("Statement of income\nGross Salary 2,58,738") is None


COMPUTATION = """NAMEOFASSESSEE :RAMU
RESIDENTIALADDRESS :2026-2027
STATUS :INDIVIDUAL ASSESSMENTYEAR :2025-2026
COMPUTATIONDATE :29-08-2026
COMPUTATIONOFTOTALINCOME
PROFITSANDGAINSFROMBUSINESSORPROFESSION 10,25,828
GROSSTOTALINCOME 10,25,828
TOTALINCOME 10,25,828
TOTALINCOMEROUNDEDOFFU/S288A 10,25,830
TAX ON RS. 2,25,830 (10,25,830 -8,00,000) @ 10% 22,583
1 RAMU 09028-WHOLESALEANDRETAILTRADE-Retail kirana store sale of other products n.e.c RAMU
"""


def test_itr_computation_rows_give_the_latest_assessment_year_and_rounded_total():
    row, raw = computation_page(COMPUTATION)
    assert row["assessment_year"] == "2026-2027"
    assert (row["business_head"], row["gross_total_income"], row["total_income"]) == (
        1025828,
        1025828,
        1025830,
    )
    assert "10,25,830" in raw


def test_business_description_keeps_the_trade_and_drops_the_activity_boilerplate():
    assert business_description(COMPUTATION) == "WHOLESALEANDRETAILTRADE-Retail kirana store"
    assert business_description("09028-RETAIL") is None


def pension_table(count=5):
    header = ["Transaction Date", "Cheque Number", "Withdrawal", "Deposit", "Balance", "Narration"]
    rows = [header]
    for month in range(count):
        rows.append(["03/08/2026", "", "", "63,040.00", "1,00,000.00", f"GBSS/PENS/{month + 1:02d}-26/TDS0"])
    rows.append(["07/08/2026", "", "", "14,000.00", "1,00,000.00", "UPI/CR/621150599179/Rohit"])
    return {"page": 1, "rows": rows}


def test_recurring_pension_credits_become_an_income_stream_even_on_one_date():
    credits, sources = recurring_credits([Document("stmt.pdf", "", tables=[pension_table()])])
    assert len(credits) == 5 and {c["employer"] for c in credits} == {"GBSS/PENS"}
    assert sources[0]["parser_version"] == "bank-recurring-1.0"


def test_person_to_person_credits_are_not_income_and_a_short_run_is_not_a_stream():
    assert recurring_credits([Document("s.pdf", "", tables=[pension_table(2)])]) == ([], [])


def test_pension_statement_gives_bank_credit_income_through_extract_local():
    block, sources, _, _ = extract_local([Document("stmt.pdf", "", tables=[pension_table(12)])])
    assert block["income"]["primary_source"] == "bank_credits"
    assert block["income"]["annual_income"] == 756480
    assert "income.annual_income" in sources


def test_declared_income_is_used_only_when_no_proof_exists():
    text = (
        "--- page 1 ---\nOccupation Business\nName of Business Organization run by you:- "
        "Your role: Duration of business Org (years):\nFinance Promoter 2\n"
        "Annual Total Income (Rs.) : 500000\n"
    )
    block, sources, _, _ = extract_local([Document("p.pdf", text)], {("p.pdf", 1): ("proposal_form", "X")})
    assert block["income"] == {
        "primary_source": "declared_only",
        "annual_income": 500000,
        "declared_only": True,
    }
    assert sources["income.annual_income"] == sources["applicant.declared_annual_income"]


COLON_PROPOSAL = (
    "First Name Ravi Middle Name Kant\nLast Name Mehra Gender Male\nDate of Birth 06/02/1987 "
    "Spouse's Name NA\nAge 39 C-KYC No NA\nQualification Post graduate\n"
    "NCPF.ver.08.10-25 PF Page 1 of 12\n"
)


def test_colon_labelled_proposal_gives_age_qualification_and_listed_business():
    found = proposal_form(
        COLON_PROPOSAL
        + "Name of Business Organization run by you:- Your role: Duration of business Org (years): "
        "Finance Promoter 2 Annual Total Income (Rs.) : 500000"
    )
    assert found["applicant.age"] == "39"
    assert found["applicant.education_level"] == "Post graduate"
    assert found["applicant.business_nature"] == "Finance Promoter"
    assert found["applicant.declared_annual_income"] == "500000"


def slip_document(name, text, page_count=1):
    return Document(name, f"--- page 1 ---\n{text}", ["image"], page_count, image_page_numbers=[1])


class FakeClient:
    def __init__(self, slips):
        self.slips = slips
        self.calls = []

    async def extract_json(self, system, blocks, max_tokens=None):
        self.calls.append(blocks)
        return {"slips": self.slips}


async def test_idp_slip_read_is_validated_and_flags_values_ocr_cannot_confirm():
    document = slip_document("slip.jpg", "garbled header")
    document.ocr_pages = [{"page": 1, "lines": [line("GrossPay 111533", 10, 10)]}]
    client = FakeClient(
        [
            {"id": "slip.jpg#1", "month_year": "2026-06", "gross_earnings": 111533, "evidence": "Gross Pay 111533"},
            {"id": "slip.jpg#1", "month_year": "June", "gross_earnings": 5, "evidence": "x"},
            {"id": "other.jpg#1", "month_year": "2026-06", "gross_earnings": 111533, "evidence": "x"},
        ]
    )
    accepted, findings = await read_slips(client, [document], [(document, 1)])
    assert [(row["month_year"], row["gross_salary"]) for row, _ in accepted] == [("2026-06", 111533)]
    assert accepted[0][1]["parser_version"] == "idp-slip-1.0"
    assert findings == []

    unconfirmed = FakeClient(
        [{"id": "slip.jpg#1", "month_year": "2026-07", "gross_earnings": 22926, "evidence": "Gross 22,926"}]
    )
    _, findings = await read_slips(unconfirmed, [document], [(document, 1)])
    assert [f["code"] for f in findings] == ["IDP_VALUE_REVIEW_REQUIRED"]


async def test_idp_failure_is_recorded_without_exposing_the_error():
    class Broken:
        async def extract_json(self, *args, **kwargs):
            raise RuntimeError("secret applicant detail")

    document = slip_document("slip.jpg", "x")
    accepted, findings = await read_slips(Broken(), [document], [(document, 1)])
    assert accepted == [] and findings[0]["code"] == "IDP_PROCESSING_FAILED"
    assert "secret" not in str(findings)


def test_only_unresolved_slip_pages_are_sent_to_the_image_read():
    good = slip_document("good.jpg", "Salary Slip May-2026\nGROSSEARNINGS(E) 19,965.00")
    bad = slip_document("bad.jpg", "PAYSLIPFORTHMONTHOFJUe-26\nGrossPay 111533")
    other = slip_document("other.jpg", "nothing")
    kinds = {(name, 1): ("salary_slip", "X") for name in ("good.jpg", "bad.jpg")}
    kinds[("other.jpg", 1)] = ("other", "OTHER")
    assert [d.filename for d, _ in slip_targets([good, bad, other], kinds)] == ["bad.jpg"]


def test_slips_from_the_image_read_complete_a_three_month_average():
    documents = [
        slip_document("a.jpg", "Salary Slip May-2026\nGROSSEARNINGS(E) 20,000.00"),
        slip_document("b.jpg", "Salary Slip June-2026\nGROSSEARNINGS(E) 22,000.00"),
    ]
    extra = [
        (
            {"month_year": "2026-07", "gross_salary": 24000},
            {"document": "c.jpg", "page": 1, "raw_value": "Gross 24,000", "parser_version": "idp-slip-1.0"},
        )
    ]
    block, sources, _, _ = extract_local(documents, extra_slips=extra)
    assert block["income"]["annual_income"] == 264000
    assert [s["month_year"] for s in block["income"]["salary_slips"]] == ["2026-05", "2026-06", "2026-07"]
    assert {c["document"] for c in sources["income.annual_income"]} == {"a.jpg", "b.jpg", "c.jpg"}


def test_a_single_slip_is_indicative_and_never_overrides_an_itr():
    only = [slip_document("a.jpg", "Salary Slip July-2026\nGross Earnings 31,703 5,114")]
    block, _, _, _ = extract_local(only)
    assert block["income"]["annual_income"] == 61368 and block["income"]["indicative_only"]


def test_declared_and_filed_business_natures_are_both_kept_when_they_differ():
    form = "--- page 1 ---\nYour role OWNER OF SWEET BUSINESS TOWNX Duration of business Org. (years) 20\n"
    itr = "--- page 1 ---\n1 RAMU 09028-COACHING CENTRES AND TUITIONS-PRIVATE COACHING sale of other n.e.c\n"
    kinds = {("form.pdf", 1): ("proposal_form", "X"), ("itr.pdf", 1): ("itr", "X")}
    block, sources, findings, _ = extract_local(
        [Document("form.pdf", form), Document("itr.pdf", itr)], kinds
    )
    nature = block["applicant"]["business_nature"]
    assert nature.startswith("OWNER OF SWEET BUSINESS TOWNX; per filed ITR: ")
    assert "PRIVATE COACHING" in nature and not findings
    assert {s["document"] for s in sources["applicant.business_nature"]} == {"form.pdf", "itr.pdf"}
