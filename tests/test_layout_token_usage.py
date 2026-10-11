from reportlab.pdfgen.canvas import Canvas

from underwriting_platform.llm.pdf_utils import prepare_doc


def test_native_form_skips_clear_page_but_keeps_graphical_checkbox(tmp_path):
    path = tmp_path / "form.pdf"
    canvas = Canvas(str(path))
    for page in range(2):
        for row in range(15):
            canvas.drawString(50, 750 - row * 20, "Applicant declaration and policy evidence text")
        if page == 0:
            canvas.rect(50, 400, 8, 8, fill=1)
        canvas.showPage()
    canvas.save()
    document = prepare_doc(path)
    document.force_vision = True
    assert document.native_text_pages == {1, 2}
    assert document.layout_vision_pages == {1}
    blocks = document.to_anthropic_blocks()
    assert sum(block["type"] == "image" for block in blocks) == 1
    assert document.text in [block.get("text") for block in blocks]
    assert any("Image page 1/2" in block.get("text", "") for block in blocks)


def test_scanned_form_cannot_become_text_only(tmp_path):
    from PIL import Image

    image = tmp_path / "scan.png"
    Image.new("RGB", (600, 800), "white").save(image)
    path = tmp_path / "scan.pdf"
    canvas = Canvas(str(path))
    canvas.drawImage(str(image), 0, 0, width=600, height=800)
    canvas.save()
    document = prepare_doc(path)
    document.force_vision = True
    assert not document.native_text_pages
    assert sum(b["type"] == "image" for b in document.to_anthropic_blocks()) == 1


def test_optional_duplicate_medical_pass_is_disabled_by_default(monkeypatch):
    import underwriting_platform.llm as llm
    from underwriting_platform.orchestrator.master import MasterOrchestrator

    monkeypatch.delenv("UW_DUAL_VENDOR_MEDICAL", raising=False)
    calls = []
    monkeypatch.setattr(llm, "build_gemini_client", lambda: calls.append(1))
    assert MasterOrchestrator(object(), rules={}, reconcile=False).gemini is None
    assert not calls
    monkeypatch.setenv("UW_DUAL_VENDOR_MEDICAL", "true")
    MasterOrchestrator(object(), rules={}, reconcile=False)
    assert calls == [1]
