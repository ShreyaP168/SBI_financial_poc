"""
llm/pdf_utils.py — turn PDFs into LLM-ready Document objects.

A Document is a small container holding:
  - filename
  - extracted text (via pdfplumber)
  - per-page image base64 (low-res JPEG, vision-friendly)
  - page count

The orchestrator hands these to agents in Anthropic message-content blocks.
"""

from __future__ import annotations
import base64
import io
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Document:
    filename: str
    text: str
    images_b64: list[str] = field(default_factory=list)
    page_count: int = 0
    extraction_error: str | None = None
    native_text_pages: set[int] = field(default_factory=set)
    force_vision: bool = False
    layout_vision_pages: set[int] | None = None
    ocr_pages: list[dict] = field(default_factory=list)
    image_page_numbers: list[int] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)

    def to_anthropic_blocks(
        self, include_images: bool = True, text_max_chars: int = 200_000
    ) -> list[dict]:
        """Convert to Anthropic message-content blocks for an LLM call.

        Strategy: send full text (bumped to 200K chars to cover multi-page
        bank statements) + a finite number of page images (vision). When the
        rendered images cover only the first N pages, we tell the model
        explicitly so it knows to scan the text for the rest.
        """
        out: list[dict] = []
        header = f"=== DOCUMENT: {self.filename}  ({self.page_count} pages) ==="
        out.append({"type": "text", "text": header})
        if self.text:
            if len(self.text) <= text_max_chars:
                txt = self.text
            else:
                txt = (
                    self.text[: text_max_chars // 2]
                    + "\n[... full text truncated at "
                    + f"{text_max_chars:,} chars; "
                    + "rendered images cover the document head; "
                    + "key trailing content kept ...]\n"
                    + self.text[-text_max_chars // 2 :]
                )
            out.append({"type": "text", "text": txt})
        if include_images and self.images_b64:
            n_imgs = len(self.images_b64)
            for i, img_b64 in enumerate(self.images_b64):
                page_number = self.image_page_numbers[i] if self.image_page_numbers else i + 1
                if page_number in self.native_text_pages and (
                    not self.force_vision
                    or (
                        self.layout_vision_pages is not None
                        and page_number not in self.layout_vision_pages
                    )
                ):
                    continue
                out.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64},
                    }
                )
                out.append(
                    {
                        "type": "text",
                        "text": f"[Image page {page_number}/{self.page_count} of "
                        f"document with {self.page_count} pages]",
                    }
                )
            if n_imgs < self.page_count:
                out.append(
                    {
                        "type": "text",
                        "text": f"[NOTE: only first {n_imgs} of "
                        f"{self.page_count} pages rendered as images. "
                        f"The full text-extraction above covers ALL pages.]",
                    }
                )
        return out


# Anthropic rejects multi-image requests where ANY dimension exceeds 2000 px
# (server-side limit). We cap to a safe ceiling below that.
_MAX_IMAGE_DIM = 1800


def _encode_image_capped(pil_im, quality: int = 70) -> str:
    """Cap either dimension to _MAX_IMAGE_DIM, convert to RGB JPEG, return base64."""
    from PIL import Image

    w, h = pil_im.size
    longest = max(w, h)
    if longest > _MAX_IMAGE_DIM:
        scale = _MAX_IMAGE_DIM / longest
        new_size = (int(w * scale), int(h * scale))
        pil_im = pil_im.resize(new_size, Image.LANCZOS)
    buf = io.BytesIO()
    pil_im.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def prepare_doc(
    path: str | Path, max_pages_text: int | None = None, max_pages_image: int = 12, dpi: int = 110
) -> Document:
    """Read a PDF (or image) and return a Document.

    Page strategy (post-2026-04 fix for under-extraction on long docs):
      - text:   extract from EVERY page (max_pages_text=None means unlimited)
      - images: render only the first max_pages_image pages — vision is the
                expensive call and most multi-page docs (bank stmts, ITR
                schedules, audited annexures) yield well to text.

    Each rendered image is still capped to 1800 px on its longest dimension
    to satisfy Anthropic's per-image 2000-px ceiling.
    """
    p = Path(path)
    if not p.exists():
        return Document(filename=p.name, text="", extraction_error="file not found")

    text = ""
    images_b64: list[str] = []
    page_count = 0
    native_text_pages: set[int] = set()
    layout_vision_pages: set[int] = set()
    ocr_pages: list[dict] = []
    image_page_numbers: list[int] = []
    uncovered_pages: list[int] = []
    tables: list[dict] = []

    def ocr_image(image, number):
        from underwriting_platform.ocr import read_page

        result = read_page(image, number)
        ocr_pages.append(result)
        tables.extend({"page": number, **table} for table in result.get("tables", []))
        return result.get("text") or ""

    if p.suffix.lower() == ".pdf":
        try:
            import pdfplumber

            with pdfplumber.open(str(p)) as pdf:
                page_count = len(pdf.pages)
                # Text from every page (or up to max_pages_text if set)
                pages_for_text = pdf.pages if max_pages_text is None else pdf.pages[:max_pages_text]
                text_parts: list[str] = []
                for i, pg in enumerate(pages_for_text):
                    try:
                        page_text = pg.extract_text() or ""
                        for table in pg.find_tables():
                            tables.append(
                                {
                                    "page": i + 1,
                                    "rows": table.extract(),
                                    "bbox": list(table.bbox),
                                    "basis": "native_pdf",
                                }
                            )
                        if len(page_text.strip()) < 80 or (
                            pg.images and len(page_text.strip()) < 300
                        ):
                            if pg.width * pg.height * (150 / 72) ** 2 > 25_000_000:
                                ocr_pages.append({"page": i + 1, "status": "render_limit"})
                            else:
                                candidate = ocr_image(pg.to_image(resolution=150).original, i + 1)
                                if candidate:
                                    # OCR is a candidate transcription, never native text.
                                    text_parts.append(
                                        f"--- page {i + 1} ---\n[Local OCR candidate]\n{candidate}"
                                    )
                                    if page_text.strip():
                                        text_parts.append(
                                            f"[Native text on this page]\n{page_text}"
                                        )
                                    continue
                            if i + 1 > max_pages_image and not page_text.strip():
                                uncovered_pages.append(i + 1)
                        # Native forms need vision only where graphical marks,
                        # widgets or unreadable glyphs can carry extra facts.
                        checkbox_shapes = any(
                            3 <= box["width"] <= 24
                            and 3 <= box["height"] <= 24
                            and 0.7 <= box["width"] / box["height"] <= 1.4
                            for box in pg.rects
                        )
                        if (
                            pg.images
                            or pg.annots
                            or pg.curves
                            or checkbox_shapes
                            or "(cid:" in page_text
                            or "\ufffd" in page_text
                        ):
                            layout_vision_pages.add(i + 1)
                        if page_text.strip():
                            # Tag each chunk with its page number so the agents
                            # can cite "page 14 of bank_stmt.pdf"
                            text_parts.append(f"--- page {i + 1} ---\n{page_text}")
                            if len(page_text.strip()) >= 200 and not pg.images and not pg.annots:
                                native_text_pages.add(i + 1)
                    except Exception:
                        if i + 1 > max_pages_image:
                            uncovered_pages.append(i + 1)
                text = "\n\n".join(text_parts)
        except Exception as e:
            logger.warning("pdfplumber failed on %s: %s", p.name, e)
            return Document(filename=p.name, text="", extraction_error=str(e))

        # Render only the FIRST max_pages_image pages as JPEGs for vision.
        try:
            with pdfplumber.open(str(p)) as pdf:
                pages_for_image = pdf.pages[:max_pages_image]
                for index, pg in enumerate(pages_for_image):
                    try:
                        im = pg.to_image(resolution=dpi).original
                        images_b64.append(_encode_image_capped(im))
                        image_page_numbers.append(index + 1)
                    except Exception as ie:
                        logger.warning("page-render failed on %s: %s", p.name, ie)
        except Exception as e:
            logger.warning("page rendering setup failed on %s: %s", p.name, e)

    elif p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}:
        try:
            from PIL import Image

            im = Image.open(str(p))
            images_b64.append(_encode_image_capped(im))
            page_count = 1
            candidate = ocr_image(im, 1)
            if candidate:
                text = f"--- page 1 ---\n[Local OCR candidate]\n{candidate}"
            image_page_numbers.append(1)
        except Exception as e:
            return Document(filename=p.name, text="", extraction_error=str(e))
    else:
        return Document(
            filename=p.name, text="", extraction_error=f"unsupported file type: {p.suffix}"
        )

    return Document(
        filename=p.name,
        text=text,
        images_b64=images_b64,
        page_count=page_count,
        native_text_pages=native_text_pages,
        layout_vision_pages=layout_vision_pages,
        ocr_pages=ocr_pages,
        image_page_numbers=image_page_numbers,
        tables=tables,
        extraction_error="UNCOVERED_SCANNED_PAGES" if uncovered_pages else None,
    )


def survey_preview(encoded: str, max_dimension: int = 640) -> str:
    """Only the routing preview is resized; downstream source images stay intact."""
    from PIL import Image

    with Image.open(io.BytesIO(base64.b64decode(encoded))) as original:
        preview = original.convert("RGB")
        preview.thumbnail((max_dimension, max_dimension))
        buffer = io.BytesIO()
        preview.save(buffer, format="JPEG", quality=70)
        return base64.b64encode(buffer.getvalue()).decode("ascii")
