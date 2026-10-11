from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from underwriting_platform.configuration import local_ocr
from underwriting_platform.llm.pdf_utils import Document, prepare_doc
from underwriting_platform.ocr import read_page
from underwriting_platform.ocr.tesseract import parse_tsv


def test_bond_tsv_line_grouping_preserves_geometry_and_conservative_confidence():
    header = "page_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    lines = parse_tsv(
        header
        + "1\t1\t1\t1\t10\t20\t40\t15\t95\tFinance\n"
        + "1\t1\t1\t1\t60\t20\t20\t15\t80\tShop\n"
    )
    assert len(lines) == 1
    assert lines[0].text == "Finance Shop"
    assert (lines[0].x0, lines[0].x1, lines[0].confidence) == (10, 80, 0.8)


def test_ocr_failure_keeps_vision_and_uses_safe_error_metadata(tmp_path, monkeypatch, caplog):
    import underwriting_platform.ocr as ocr

    class Broken:
        def extract(self, image):
            raise RuntimeError("Applicant Private Name PAN ABCDE1234F")

    monkeypatch.setenv("UW_LOCAL_OCR_PROVIDER", "tesseract")
    monkeypatch.setattr(ocr, "extractor", lambda *args: Broken())
    path = tmp_path / "scan.png"
    Image.new("RGB", (300, 300), "white").save(path)
    document = prepare_doc(path)
    assert document.images_b64
    assert document.ocr_pages[0]["error_code"] == "RuntimeError"
    assert "Applicant Private" not in str(document.ocr_pages) + caplog.text
    assert any(block["type"] == "image" for block in document.to_anthropic_blocks())


def test_render_limit_does_not_invoke_engine(monkeypatch):
    import underwriting_platform.ocr as ocr

    monkeypatch.setattr(ocr, "extractor", lambda *args: pytest.fail("Unbounded render"))
    image = Image.new("1", (5001, 5000))
    assert read_page(image, 3)["status"] == "render_limit"


def test_image_page_number_survives_a_render_gap():
    document = Document("scan.pdf", "", ["page3"], 5, image_page_numbers=[3])
    blocks = document.to_anthropic_blocks()
    assert any("Image page 3" in block.get("text", "") for block in blocks)


@pytest.mark.skipif(not local_ocr.executable(), reason="Tesseract runtime not installed")
def test_real_local_tesseract_scanned_pdf_preserves_page_and_financial_text(tmp_path, monkeypatch):
    monkeypatch.setenv("UW_LOCAL_OCR_PROVIDER", "tesseract")
    monkeypatch.setenv("OCR_PSM", "6")
    font_path = next(
        (
            path
            for path in (
                Path(r"C:\Windows\Fonts\arial.ttf"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            )
            if path.exists()
        ),
        None,
    )
    font = ImageFont.truetype(str(font_path), 38) if font_path else ImageFont.load_default(size=38)
    pages = []
    for text in ("BUSINESS NAME FINANCE\nANNUAL INCOME 860764", "POLICY SUM ASSURED 5000000"):
        page = Image.new("RGB", (1200, 450), "white")
        ImageDraw.Draw(page).multiline_text((50, 70), text, font=font, fill="black", spacing=25)
        pages.append(page)
    path = tmp_path / "scanned.pdf"
    pages[0].save(path, save_all=True, append_images=pages[1:])
    document = prepare_doc(path, max_pages_image=1)
    assert "860764" in document.text and "5000000" in document.text
    assert [r["page"] for r in document.ocr_pages] == [1, 2]
    assert all(r["provider_tokens"] == 0 for r in document.ocr_pages)
    assert document.ocr_pages[0]["lines"][0]["confidence"] > 0
    assert not document.extraction_error
    assert not document.native_text_pages


def test_ocr_language_change_invalidates_extraction(tmp_path, monkeypatch):
    from underwriting_platform.configuration.extraction import cache_revision

    monkeypatch.setenv("UW_LOCAL_OCR_PROVIDER", "off")
    first = cache_revision("same-files", tmp_path)
    monkeypatch.setenv("OCR_LANGUAGE", "eng+hin")
    assert cache_revision("same-files", tmp_path) != first


def test_local_model_change_invalidates_extraction(tmp_path, monkeypatch):
    from underwriting_platform.configuration.extraction import cache_revision

    model = tmp_path / "model.traineddata"
    model.write_bytes(b"model-v1")
    monkeypatch.setattr(local_ocr, "assets", lambda: [model])
    first = cache_revision("same-files", tmp_path)
    model.write_bytes(b"model-v2")
    assert cache_revision("same-files", tmp_path) != first


def test_failed_ocr_beyond_vision_limit_blocks_document(tmp_path, monkeypatch):
    import underwriting_platform.ocr as ocr

    monkeypatch.setattr(
        ocr,
        "read_page",
        lambda image, page: {"page": page, "status": "failed", "text": "", "lines": []},
    )
    image = Image.new("RGB", (300, 300), "white")
    path = tmp_path / "scan.pdf"
    image.save(path, save_all=True, append_images=[image.copy()])
    document = prepare_doc(path, max_pages_image=1)
    assert document.extraction_error == "UNCOVERED_SCANNED_PAGES"
    assert document.ocr_pages[1]["page"] == 2


def test_ocr_candidates_never_become_native_business_repairs():
    from underwriting_platform.agents.native_business import reconcile_native_business

    document = Document(
        "proposal.pdf",
        "--- page 1 ---\nName of Business Organization Finance Your Role Owner",
        ocr_pages=[{"page": 1, "status": "completed"}],
    )
    case = {"applicant": {"employer_name": "Finance", "business_nature": "Finance"}}
    assert not reconcile_native_business(case, [document], {document.filename: "proposal_form"})
    assert not case["field_sources"]
