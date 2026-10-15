import pytest

from underwriting_platform.extraction import kyc
from underwriting_platform.extraction.coordinator import extract_local_first
from underwriting_platform.extraction.documents import (
    aadhaar_letter,
    itr_computation,
    medical_declarations,
    names_compatible,
    pan_card,
    passport,
    proposal_form,
)
from underwriting_platform.extraction.financial import computation_year, computation_years
from underwriting_platform.extraction.local_fields import extract_local
from underwriting_platform.llm.pdf_utils import Document


class NoCloud:
    async def extract_json(self, *args, **kwargs):
        pytest.fail("Unexpected cloud extraction")


PROPOSAL = """Proposal No.: 4TEST000001
SECTION A PERSONAL DETAILS :
First Name Asha Middle
Name
Last Name K
Date of Birth 07-MAR-1990 Gender Female
Age 36 Years
Marital Status Married
Resident Status Resident Indian
Nationality INDIAN
Qualification Graduate
CONTACT DETAILS :
Name of Nominee 1 Date of Birth Gender Relationship with
RAVI K 01-JAN-1985 Male Husband 100
"""

PAN_NATIVE = "ABCPK1234D\nASHA K\nKUMAR K\n07/03/1990\nFemale\nABCPK1234D\nASHA K"

PAN_OCR = """faHTST
INCOMETAXDEPARTMENT
e -Permanent Account Number (e-PAN) Card
ABCPK1234D
T/Name
ASHA K
TT/Father'sname
KUMAR K
/DateofBirth
07/03/1990
/Gender
Female
/Signature
Permanent Account Number(PAN)facilitate linking"""

AADHAAR = """Enrolment No.: 0000/00000/00000
To
Asha. K
D/O: Kumar. K,
VTC: Town,
Date of Birth/DOB: 07/03/1990
Female/ FEMALE
"""

ITR = """A.Y. 2025-2026
Name : Asha Kumar Previous Year : 2024-2025
Father's Name : Kumar K PAN : ABCPK 1234 D
Address : 1 Main Road Aadhaar No. : 1111 2222 3333
Date of Birth : 07-Mar-1990
Statement of Income
"""


def test_proposal_form_personal_details_page():
    assert proposal_form(PROPOSAL) == {
        "applicant.applicant_name": "Asha K",
        "applicant.dob": "07-MAR-1990",
        "applicant.gender": "Female",
        "applicant.age": "36",
        "applicant.residential_status": "Resident Indian",
        "applicant.education_level": "Graduate",
    }


@pytest.mark.parametrize("text", [PAN_NATIVE, PAN_OCR])
def test_pan_card_reads_unlabeled_and_labeled_layouts(text):
    assert pan_card(text) == {
        "applicant.applicant_name": "ASHA K",
        "applicant.dob": "07/03/1990",
        "applicant.gender": "Female",
    }


PASSPORT = """REPUBLICOF INDIA
IND
AB1234567
KUMAR
ASHA
07/03/1990
P<INDKUMAR<<ASHA<<<<<<<<<<<<<<<<<<<<<<<<<<<<
AB12345678IND9003074F3001019<<<<<<<<<<<<<<04
"""


def test_passport_zone_gives_name_sex_and_a_dob_confirmed_by_the_printed_date():
    assert passport(PASSPORT) == {
        "applicant.applicant_name": "ASHA KUMAR",
        "applicant.dob": "07/03/1990",
        "applicant.gender": "Female",
    }
    assert "applicant.dob" not in passport(PASSPORT.replace("07/03/1990", "08/03/1990"))
    assert names_compatible("Asha Kumar", "ASHA  KU MAR")


def test_pan_card_where_each_value_precedes_its_label():
    text = (
        "02/10/1994\nTa/DateofBirth\nKISHANA RAM\nfuaT T TH/Father'sName\nRAKESH DOBAR\n/Name\n"
        "INCOME TAX DEPARTMENT\nPermanent Account Number Card\nEODPD6550P\nGOVT.OFINDIA\n"
    )
    assert pan_card(text) == {
        "applicant.dob": "02/10/1994",
        "applicant.applicant_name": "RAKESH DOBAR",
    }


def test_pan_header_text_is_never_taken_as_the_name():
    text = "02/10/1994\nKISHANA RAM\nRAKESH DOBAR\nINCOME TAX DEPARTMENT\nEODPD6550P\nGOVT.OFINDIA\n"
    assert "applicant.applicant_name" not in pan_card(text)


def test_aadhaar_letter_and_itr_computation():
    assert aadhaar_letter(AADHAAR) == {
        "applicant.applicant_name": "Asha. K",
        "applicant.dob": "07/03/1990",
        "applicant.gender": "Female",
    }
    assert itr_computation(ITR) == {
        "applicant.applicant_name": "Asha Kumar",
        "applicant.dob": "07-Mar-1990",
    }
    assert itr_computation("Income Tax Return acknowledgement Name : Asha") == {}


@pytest.mark.parametrize(
    "first,second,expected",
    [
        ("Asha K", "Asha Kumar", True),
        ("ASHA K", "Asha  Kumar", True),
        ("Asha K", "Meera K", False),
        ("Asha Kumar", "Asha Bose", False),
    ],
)
def test_name_compatibility_allows_initials_only(first, second, expected):
    assert names_compatible(first, second) is expected


def test_sources_agree_across_documents_and_the_declared_name_wins():
    documents = [
        Document("form.pdf", "--- page 1 ---\n" + PROPOSAL),
        Document("pan.pdf", "--- page 1 ---\n" + PAN_NATIVE),
        Document("itr.pdf", "--- page 1 ---\n" + ITR),
        Document("aadhaar.pdf", "--- page 1 ---\n" + AADHAAR),
    ]
    kinds = {
        ("form.pdf", 1): ("proposal_form", "FORM_OR_TEXT"),
        ("pan.pdf", 1): ("id_proof", "PAN_CARD"),
        ("itr.pdf", 1): ("itr", "FORM_OR_TEXT"),
        ("aadhaar.pdf", 1): ("id_proof", "AADHAAR_LETTER"),
    }
    block, sources, findings, _ = extract_local(documents, kinds)
    applicant = block["applicant"]
    assert applicant["applicant_name"] == "Asha K"
    assert (applicant["dob"], applicant["gender"], applicant["age"]) == ("1990-03-07", "Female", 36)
    assert applicant["education_level"] == "Graduate"
    assert {s["document"] for s in sources["applicant.dob"]} == {
        "form.pdf",
        "pan.pdf",
        "itr.pdf",
        "aadhaar.pdf",
    }
    assert not findings


TABLES = [
    {
        "page": 3,
        "rows": [
            ["NOMINEE DETAILS", "", "", "", "", ""],
            ["Name of Nominee 1", "Date of Birth", "Gender", "Relationship with Life Assured",
             "Percentage Share (%)*", "Permanent Address"],
            ["RAVI K", "01-JAN-1985", "Male", "Husband", "60", "1 Main Road"],
            ["MEERA K", "02-FEB-2012", "Female", "Daughter", "40", "1 Main Road"],
            ["Bank Name", "Account No", "", "IFS Code", "Mob. No", "Communication Address:"],
        ],
    },
    {
        "page": 5,
        "rows": [["Do you have any other individual existing life insurance policy/policies "
                  "(from SBI Life or any other life insurer)?", "", "No"]],
    },
    {
        "page": 13,
        "rows": [
            ["", "Rider Name: SBI Life - Accident Benefit Rider (111B041V01)", "Rider Policy Term",
             "Rider Premium payment Term", "Rider Sum Assured(Rs.)", "Rider Instalment Premium", ""],
            ["", "Option A : Accidental Death Benefit (ADB)", "20", "10", "5000000", "1200.50", ""],
            ["", "Option B : Accidental Partial Permanent Disability Benefit (APPD)", "0", "0", "0",
             "0", ""],
        ],
    },
    {
        "page": 4,
        "rows": [["Better Half Benefit", "0", "0", "", ""]],
    },
]


def test_proposal_tables_give_nominees_riders_and_a_no_existing_cover_answer():
    found = proposal_form("Occupation SELFEMPLOYED Annual Total Income : Rs. 1000000", TABLES)
    nominees = found["applicant.nominees"][1]
    assert [(n["name"], n["relation"], n["share_pct"], n["dob"]) for n in nominees] == [
        ("RAVI K", "Husband", 60, "1985-01-01"),
        ("MEERA K", "Daughter", 40, "2012-02-02"),
    ]
    riders = found["applicant.riders"][1]
    assert [(r["option"], r["sum_assured"]) for r in riders] == [("ADB", 5000000), ("APPD", 0)]
    assert found["applicant.adb_sum_assured"][1] == 5000000
    assert "applicant.appd_sum_assured" not in found
    assert "applicant.bhb_sum_assured" not in found
    assert found["compliance.existing_cover_declared"][1]["total_life_cover_other"] == 0
    assert found["applicant.declared_annual_income"] == "1000000"


def test_riders_printed_as_na_are_recorded_as_not_opted():
    rows = [
        ["Rider Name : SBI Life - Accident Benefit Rider (111B041V01)", "", "", "", ""],
        ["Option A : Accidental Death Benefit (ADB)", "NA", "NA", "NA", "NA"],
        ["Option B: Accidental Partial Permanent Disability Benefit (APPD)", "NA", "NA", "NA", "NA"],
    ]
    riders = proposal_form("", [{"page": 5, "rows": rows}])["applicant.riders"][1]
    assert [(r["option"], r["sum_assured"]) for r in riders] == [("ADB", 0), ("APPD", 0)]


def test_a_yes_answer_is_not_reported_as_resolved():
    rows = [["Do you have any other individual existing life insurance policy?", "", "Yes"]]
    assert "compliance.existing_cover_declared" not in proposal_form("", [{"page": 5, "rows": rows}])


COMPUTATION = """A.Y. 2025-2026
Name : Asha Kumar Previous Year : 2024-2025
Father's Name : Kumar K PAN : ABCPK 1234 D
Date of Birth : 07-Mar-1990
Statement of Income
Income chargeable under the head "House Property" 5,44,456
Speculation business profits 5 -29,64,480
Income chargeable under the head "Business and Profession" 22,551
Total income of Business and Profession 3,53,584
Income from other sources
Interest income 8 62,733
Total Income 22,72,568
"""

COMPUTATION_WITH_GTI = """A.Y. 2023-2024
Name : Asha Kumar Previous Year : 2022-2023
Statement of Income
Income chargeable under the head "Salaries" 2,00,000
Income chargeable under the head "Business and Profession" -5,000
Gross Total Income 10,70,896
Total Income 10,39,090
"""


def test_computation_sheet_heads_exclude_passive_income_and_losses():
    row, raw, page = computation_year([(1, COMPUTATION)])
    assert page == 1 and "Business and Profession" in raw
    assert row == {
        "assessment_year": "2025-2026",
        "gross_total_income": 2272568,
        "salary_head": 0,
        "business_head": 22551,
        "other_head": 2250017,
        "earned_total": 22551,
        "name_on_document": "Asha Kumar",
    }
    loss_year, _, _ = computation_year([(1, COMPUTATION_WITH_GTI)])
    assert (loss_year["gross_total_income"], loss_year["earned_total"]) == (1070896, 200000)
    assert loss_year["business_head"] == 0
    assert computation_year([(1, "Income Tax Return acknowledgement Total Income 5,00,000")]) is None


def test_a_bundled_multi_year_pdf_is_split_per_assessment_year():
    pages = [
        (1, 'A.Y. 2024-2025\nName : Asha Kumar Previous Year : 2023-2024\nStatement of Income\n'
            'Income chargeable under the head "Business and Profession" 4,24,013\n'
            "nTotal Income 10,27,014\n"),
        (2, "Acknowledgement Total Income 2 10,27,010\nAdjusted Total Income under AMT 4 0\n"),
        (3, COMPUTATION),
        (4, "Total Income 1A 22,72,570\n"),
    ]
    rows = [row for row, _, _ in computation_years(pages)]
    assert [(r["assessment_year"], r["gross_total_income"], r["earned_total"]) for r in rows] == [
        ("2024-2025", 1027014, 424013),
        ("2025-2026", 2272568, 22551),
    ]


ASSESSEE_PAGE_1 = """Name of Assessee Asha Kumar
Father's Name Kumar K
Status Individual Assessment Year 2024-2025
PAN ABCPK1234D Date of Birth 07/03/1990
Residential Status Resident Gender Female
Particular of Business Retail Trading
Nature of Business HOTELS, RESTAURANTS AND HOSPITALITY SERVICES-Restaurants
without bars(10008),Trade Name:Asha & Co.
Stock Valuation Method Cost Price or Market Price Whichever is less
Filing Status Original
Computation of Total Income [As per Section 115BAC (New Tax Regime)]
Income from Business or Profession (Chapter IV D) 4,90,120
Income u/s 44AD 4,90,120
Income from Other Sources (Chapter IV F) 2,463
Gross Total Income 4,92,583
Total Income 4,92,583
"""
ASSESSEE_PAGE_2 = (
    "NAME OF ASSESSEE : Asha Kumar A.Y. 2024-2025 PAN : ABCPK1234D\n"
    "Income Declared u/s 44 AD Foodcart\nGross Receipts/Turnover (Total) 1650480.00\n"
)


def test_assessee_style_computation_gives_presumptive_business_income_and_nature():
    found = computation_year([(1, ASSESSEE_PAGE_1), (2, ASSESSEE_PAGE_2)])
    row, raw, page = found
    assert page == 1 and "4,90,120" in raw
    assert (row["assessment_year"], row["gross_total_income"], row["earned_total"]) == (
        "2024-2025",
        492583,
        490120,
    )
    assert (row["business_head"], row["other_head"], row["name_on_document"]) == (
        490120,
        2463,
        "Asha Kumar",
    )
    fields = itr_computation(ASSESSEE_PAGE_1)
    assert fields["applicant.business_nature"].startswith("HOTELS, RESTAURANTS")
    assert fields["applicant.business_nature"].endswith("Trade Name:Asha & Co.")
    assert (fields["applicant.dob"], fields["applicant.gender"]) == ("07/03/1990", "Female")


def test_acknowledgement_only_pages_are_not_mistaken_for_a_computation():
    ack = "Acknowledgement Number:1 INDIAN INCOME TAX RETURN ACKNOWLEDGEMENT Total Income 1A 6,24,200"
    assert computation_year([(1, ack)]) is None


def test_business_filer_income_block_uses_itr_years_and_is_not_salaried():
    documents = [
        Document("a.pdf", "--- page 1 ---\n" + COMPUTATION),
        Document("b.pdf", "--- page 1 ---\n" + COMPUTATION_WITH_GTI),
    ]
    block, sources, findings, _ = extract_local(documents)
    income = block["income"]
    assert income["primary_source"] == "itr" and income["is_salaried"] is False
    assert income["annual_income"] == 22551
    assert [y["assessment_year"] for y in income["itr_years"]] == ["2023-2024", "2025-2026"]
    assert sources["income.annual_income"][0]["document"] == "a.pdf"
    assert not findings


HEALTH = """Health Details of Life Assured
1. Height : 5.9 Weight (kgs) : 95 kgs
Have you lost weight of 5Kgs or more in last 6 months No
2. Have you ever been treated, hospitalized or diagnosed for any of the following
a.Diabetes Mellitus/ High Blood Sugar, High/Low Blood Pressure or High Cholesterol No
c.Lung /Respiratory disorder of any nature: Asthma, COPD, Tuberculosis (TB), Pneumonia,
Bronchitis, emphysema, or any other chest or lung
No
Questions For Female Lives: 1. Are you currently Pregnant? NA
4. Do you currently or have you in the past Smoked, Consumed Tobacco, Alcohol No
Smoking No
Alcohol No
"""


def test_health_declarations_are_recorded_and_all_no_marks_them_reviewed():
    answers, _, _ = medical_declarations(HEALTH)
    assert {a["answer"] for a in answers} == {"No"}
    assert len(answers) == 6
    document = Document("form.pdf", "--- page 1 ---\n" + HEALTH)
    block, sources, _, _ = extract_local([document], {("form.pdf", 1): ("proposal_form", "X")})
    assert block["medical"]["declaration_reviewed"] is True
    assert sources["medical.declaration_reviewed"] == sources["medical.declaration_answers"]


def test_yes_answers_after_the_health_section_do_not_count():
    text = HEALTH + "SECTION ‘E’ PREMIUM\nIs the deposit being paid by you? Yes\n"
    answers, _, _ = medical_declarations(text)
    assert {a["answer"] for a in answers} == {"No"}


@pytest.mark.parametrize("line", ["a.Heart disease of any kind Yes", "Yes"])
def test_any_yes_answer_keeps_the_declarations_under_review(line):
    text = HEALTH + line + "\n"
    answers, _, _ = medical_declarations(text)
    assert any(a["answer"] == "Yes" for a in answers)
    document = Document("form.pdf", "--- page 1 ---\n" + text)
    block, sources, _, _ = extract_local([document], {("form.pdf", 1): ("proposal_form", "X")})
    assert "declaration_reviewed" not in block["medical"]
    assert "medical.declaration_reviewed" not in sources


def test_kyc_evidence_lists_documents_and_agreement_without_verifying_kyc():
    kinds = {
        ("pan.pdf", 1): ("id_proof", "PAN_CARD"),
        ("aadhaar.pdf", 1): ("id_proof", "AADHAAR_LETTER"),
        ("pivc.pdf", 1): ("vmer", "FORM_OR_TEXT"),
    }
    documents = [
        Document("pan.pdf", "--- page 1 ---\n" + PAN_NATIVE),
        Document("aadhaar.pdf", "--- page 1 ---\n" + AADHAAR),
    ]
    block, sources, _, _ = extract_local(documents, kinds)
    evidence = block["compliance"]["kyc_evidence"]
    assert evidence["pan_present"] and evidence["aadhaar_present"] and evidence["piv_present"]
    assert evidence["address_proof_present"] and not evidence["passport_present"]
    assert (evidence["name_match_across_ids"], evidence["dob_match_across_ids"]) == (
        "Match",
        "Match",
    )
    assert "kyc_status" not in block["compliance"]
    assert {s["document"] for s in sources["compliance.kyc_evidence"]} == {"pan.pdf", "aadhaar.pdf"}


@pytest.mark.parametrize(
    "names,dates,name_result,dob_result",
    [
        (["Asha K", "Asha Kumar"], ["07/03/1990", "07-MAR-1990"], "Minor Mismatch", "Match"),
        (["Asha K", "Meera R"], ["07/03/1990", "08/03/1990"], "Major Mismatch", "Mismatch"),
        (["Asha K"], ["07/03/1990"], None, "Single Source"),
    ],
)
def test_agreement_across_identity_sources(names, dates, name_result, dob_result):
    def records(values):
        return [{"raw_value": value} for value in values]

    assert kyc._name_match(records(names)) == name_result
    assert kyc._dob_match(records(dates)) == dob_result


def test_kyc_evidence_is_withheld_while_any_page_is_unclassified():
    kinds = {("pan.pdf", 1): ("id_proof", "PAN_CARD"), ("scan.pdf", 1): ("other", "OTHER")}
    block, _, _, _ = extract_local([Document("pan.pdf", "--- page 1 ---\n" + PAN_NATIVE)], kinds)
    assert "kyc_evidence" not in block["compliance"]


async def test_resolved_review_items_no_longer_raise_processing_errors(monkeypatch):
    monkeypatch.delenv("UW_TARGETED_LLM_FALLBACK", raising=False)
    document = Document(
        "form.pdf",
        "--- page 1 ---\n" + PROPOSAL + HEALTH + "\nnominee rider existing insurance cover",
        tables=[{**table, "page": 1} for table in TABLES],
    )
    pan = Document("pan.pdf", "--- page 1 ---\n" + PAN_NATIVE)
    kinds = {("form.pdf", 1): ("proposal_form", "X"), ("pan.pdf", 1): ("id_proof", "PAN_CARD")}
    _, report = await extract_local_first(NoCloud(), [document, pan], page_kinds=kinds)
    assert not [item for item in report["unresolved"] if item.endswith("_review")]


STACKED_PROPOSAL = """SECTION A PERSONAL DETAILS
First Name Asha
Middle Name
Last Name Kumar
Gender Female
Date of Birth 02-08-1974 Age 52 Years
Resident Status Resident Indian
Nationality Indian
Qualification Graduate
CONTACT DETAILS
Occupation Business
Force Name NA
Name of Business Organization run by you SELF EMPLOYED
Your role OWNER OF SWEET BUSINESS KOLKAT
Duration of business Org. (years) 20
Annual Total Income Rs. 1800000
Sum Assured(Rs.) 50,00,000.00 Base Premium without
SBI Life 3NNK537749 609110 4200000 Self Inforce
SBI Life 53NA412738 150000 1500000 Self Terminated
"""


def test_stacked_proposal_layout_gives_identity_business_and_declared_policies():
    found = proposal_form(STACKED_PROPOSAL)
    assert found["applicant.applicant_name"] == "Asha Kumar"
    assert (found["applicant.dob"], found["applicant.gender"]) == ("02-08-1974", "Female")
    assert found["applicant.occupation"] == "Business"
    assert found["applicant.business_nature"] == "OWNER OF SWEET BUSINESS KOLKAT"
    cover = found["compliance.existing_cover_declared"][1]
    assert (cover["answer"], cover["total_life_cover_other"]) == ("Yes", 4200000)
    assert [p["status"] for p in cover["policies_in_other_companies"]] == ["Inforce", "Terminated"]


def test_a_bare_role_is_not_taken_as_the_nature_of_business():
    text = "Occupation BUSINESS If Business:- Name of Business Organization Chai Co Your Role Proprietor Duration"
    assert "applicant.business_nature" not in proposal_form(text)


def test_sum_assured_label_without_a_colon_is_read_locally():
    block, sources, _, _ = extract_local([Document("p.pdf", "Sum Assured(Rs.) 50,00,000.00 Base Premium")])
    assert block["applicant"]["sum_assured"] == 5000000
    assert sources["applicant.sum_assured"][0]["raw_value"].startswith("50,00,000")


def test_nominee_table_with_a_leading_serial_column_and_split_cells():
    rows = [
        ["", "Name of Nominee", "Date of Birth", "Gend er", "Relati onship", "Percen tage Share (%)*"],
        ["1.", "Ms. Avantika Kumar", "28-03- 2007", "Female", "Daughte r", "100"],
    ]
    nominees = proposal_form("", [{"page": 4, "rows": rows}])["applicant.nominees"][1]
    assert nominees == [
        {"name": "Avantika Kumar", "dob": "2007-03-28", "gender": "Female",
         "relation": "Daughter", "share_pct": 100}
    ]


FULL_ITR_PAGE_1 = (
    "Acknowledgement Number : 1 Date of Filing : 12-Sep-2025* INDIAN INCOME TAX RETURN "
    "Assessment ITR4 SUGAM Year 2025-26 PART A GENERAL INFORMATION (A1) First Name"
)
FULL_ITR_PAGE_2 = (
    "PART B GROSS TOTAL INCOME B1 Income from Business & Profession B1 7,89,560 "
    "B2 i Gross Salary i 0 v Income chargeable under the head 'Salaries' (iii - iv) B2 0 "
    "B4 Income from Other Sources B4 70,841 Gross Total Income (B1+B2+B3+B4) B5 B5 8,60,401"
)


def test_full_itr4_form_gives_the_business_head_and_gross_total_income():
    row, raw, page = computation_year([(1, FULL_ITR_PAGE_1), (2, FULL_ITR_PAGE_2)])
    assert page == 1 and "7,89,560" in raw
    assert (row["assessment_year"], row["business_head"], row["gross_total_income"]) == (
        "2025-26",
        789560,
        860401,
    )
    assert row["earned_total"] == 789560


def test_the_moral_hazard_report_does_not_supply_an_occupation():
    report = "--- page 1 ---\nOccupation : Proposer Business Professional Qualification : graduate"
    form = "--- page 1 ---\nOccupation: Business"
    kinds = {("mhr.pdf", 1): ("mhr", "MORAL_HAZARD_REPORT"), ("form.pdf", 1): ("proposal_form", "X")}
    block, _, findings, _ = extract_local([Document("mhr.pdf", report), Document("form.pdf", form)], kinds)
    assert block["applicant"]["occupation"] == "Business" and not findings


def test_different_people_remain_a_conflict():
    documents = [
        Document("form.pdf", "--- page 1 ---\n" + PROPOSAL),
        Document("pan.pdf", "--- page 1 ---\n" + PAN_NATIVE.replace("ASHA K", "MEERA R")),
    ]
    kinds = {("form.pdf", 1): ("proposal_form", ""), ("pan.pdf", 1): ("id_proof", "PAN_CARD")}
    block, _, findings, _ = extract_local(documents, kinds)
    assert "applicant_name" not in block["applicant"]
    assert {"field": "applicant.applicant_name", "code": "CONFLICTING_LOCAL_EVIDENCE",
            "retryable": False} in findings


@pytest.mark.parametrize(
    "printed,category",
    [
        ("Literate/Non-SSC", "Below SSC"),
        ("Illiterate", "Illiterate"),
        ("10th Pass/SSC", "SSC-10th"),
        ("12th Pass/HSC", "HSC-12th"),
        ("Graduate", "Graduate"),
        ("Post Graduate", "Postgraduate"),
        ("Diploma", "Diploma"),
    ],
)
def test_qualification_options_map_to_the_guideline_categories(printed, category):
    from underwriting_platform.extraction.local_fields import normalize

    assert normalize("applicant.education_level", printed) == category


def test_the_printed_qualification_stays_in_the_citation():
    text = "--- page 1 ---\n" + PROPOSAL.replace("Graduate", "Literate/Non-SSC")
    block, sources, _, _ = extract_local([Document("form.pdf", text)], {("form.pdf", 1): ("proposal_form", "X")})
    assert block["applicant"]["education_level"] == "Below SSC"
    assert sources["applicant.education_level"][0]["raw_value"] == "Literate/Non-SSC"
