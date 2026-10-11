import copy
import json

import fitz
from support.extraction_fixture import synthetic_case

from underwriting_platform.configuration.paths import CONFIG_ROOT
from underwriting_platform.engine import run_engine
from underwriting_platform.renderer import render_triage_sheet


def test_reference_section_order_content_and_no_underwriting_mutation(tmp_path):
    case = synthetic_case("FORMAT")
    rules = json.loads((CONFIG_ROOT / "uw_rules.json").read_text(encoding="utf8"))
    decision = run_engine(case, rules, 10000000)
    before = copy.deepcopy(decision)
    output = tmp_path / "sheet.pdf"
    render_triage_sheet(decision, output)
    with fitz.open(output) as pdf:
        text = "\n".join(page.get_text() for page in pdf)
        sections = [
            "Requirements to Raise",
            "DECISION",
            "HIGH RISK",
            "IIB & COMPLIANCE",
            "FINANCIAL OVERVIEW",
            "BANK STATEMENT ANALYSIS",
            "DOCUMENTS / KYC",
            "POLICY DETAILS",
        ]
        positions = [text.index(section) for section in sections]
        assert positions == sorted(positions)
        assert "No additional requirements raised for this terminal decline" in text
        assert "Not provided" in text
        assert "QC CONCURS" not in text
        assert "floors" not in text.lower()
        for page in pdf:
            for x0, y0, x1, y1, value, *_ in page.get_text("blocks"):
                if value.strip():
                    assert y0 >= 0 and y1 <= page.rect.height
                    assert x0 >= 0 and x1 <= page.rect.width
    assert decision == before


def test_income_jargon_does_not_change_a_physical_address():
    from underwriting_platform.renderer.reference_tables import plain_text

    assert (
        plain_text("Verified income below qualification floors")
        == "Verified income below qualification minimum income"
    )
    assert plain_text("Flat 2, Ground Floor") == "Flat 2, Ground Floor"
