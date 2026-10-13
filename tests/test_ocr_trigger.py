import pytest
from PIL import Image
from reportlab.pdfgen import canvas

from underwriting_platform.llm.pdf_utils import prepare_doc


@pytest.mark.parametrize("with_image,expected", [(True, 1), (False, 0)])
def test_short_native_text_page_with_images_is_also_read_locally(
    tmp_path, monkeypatch, with_image, expected
):
    import underwriting_platform.ocr as ocr

    monkeypatch.setattr(
        ocr, "read_page", lambda image, page: {"page": page, "status": "completed", "text": "x"}
    )
    path = tmp_path / "card.pdf"
    pdf = canvas.Canvas(str(path))
    pdf.drawString(72, 700, "BYMPS7486M SREEKANTH S 07/03/1989 Male " * 3)
    if with_image:
        Image.new("RGB", (40, 40), "white").save(tmp_path / "pixel.png")
        pdf.drawImage(str(tmp_path / "pixel.png"), 72, 400, width=100, height=100)
    pdf.save()
    assert len(prepare_doc(path).ocr_pages) == expected
