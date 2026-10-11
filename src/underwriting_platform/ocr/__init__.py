"""Offline page OCR with bounded rendering and retained geometry/provenance."""

import math
import os
import subprocess
import time
from dataclasses import asdict
from functools import lru_cache
from importlib.metadata import version
from threading import Lock

from underwriting_platform.configuration import local_ocr

_lock = Lock()


@lru_cache(maxsize=4)
def engine_version(provider, binary):
    if provider == "paddle_structure":
        return version("paddleocr")
    if provider == "rapidocr":
        return version("rapidocr-onnxruntime")
    result = subprocess.run([binary, "--version"], capture_output=True, timeout=10, check=True)
    return result.stdout.decode("utf8", errors="replace").splitlines()[0].strip()


@lru_cache(maxsize=4)
def extractor(provider, psm, language):
    if provider == "paddle_structure":
        from .paddle_structure import PaddleStructureExtractor

        return PaddleStructureExtractor()
    if provider == "tesseract":
        from .tesseract import TesseractTextExtractor

        return TesseractTextExtractor(psm=psm, language=language)
    from .rapidocr import RapidOCRFullPageTextExtractor

    return RapidOCRFullPageTextExtractor(intra_op_num_threads=2, inter_op_num_threads=1)


def read_page(image, page):
    provider = local_ocr.provider()
    if provider == "auto":
        import importlib.util

        provider = "rapidocr" if importlib.util.find_spec("rapidocr_onnxruntime") else "tesseract"
    if provider == "off":
        return {"page": page, "status": "disabled", "lines": [], "text": ""}
    if image.width * image.height > 25_000_000:
        return {"page": page, "status": "render_limit", "lines": [], "text": ""}
    started = time.perf_counter()
    try:
        with _lock:
            reader = extractor(provider, local_ocr.get("OCR_PSM"), local_ocr.get("OCR_LANGUAGE"))
            try:
                lines = reader.extract(image)
            except Exception:
                if local_ocr.provider() != "auto" or provider != "rapidocr":
                    raise
                provider = "tesseract"
                reader = extractor(
                    provider, local_ocr.get("OCR_PSM"), local_ocr.get("OCR_LANGUAGE")
                )
                lines = reader.extract(image)
            if (
                local_ocr.provider() == "auto"
                and os.getenv("UW_PADDLE_CONFIG")
                and sum(any(c.isdigit() for c in line.text) for line in lines) >= 5
            ):
                reader = extractor("paddle_structure", 6, "eng")
                lines = reader.extract(image)
                provider = "paddle_structure"
        for line in lines:
            values = (line.x0, line.y0, line.x1, line.y1, line.confidence)
            if line.mean_confidence is not None:
                values += (line.mean_confidence,)
            if not all(math.isfinite(value) for value in values) or not (
                0 <= line.confidence <= 1
                and 0 <= line.x0 < line.x1 <= image.width + 1
                and 0 <= line.y0 < line.y1 <= image.height + 1
            ):
                raise ValueError("Invalid OCR geometry")
        return {
            "page": page,
            "status": "completed",
            "provider": provider,
            "model": reader.model_name,
            "version": engine_version(provider, local_ocr.executable()),
            "coordinate_unit": "pixel",
            "image_size": list(image.size),
            "lines": [asdict(line) for line in lines],
            "text": "\n".join(line.text for line in lines),
            "confidence": min((line.confidence for line in lines), default=0),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "provider_tokens": 0,
            "tables": getattr(reader, "last_tables", []),
        }
    except Exception as error:
        # Never persist/log subprocess stderr or OCR content in error messages.
        return {
            "page": page,
            "status": "failed",
            "provider": provider,
            "error_code": type(error).__name__,
            "lines": [],
            "text": "",
            "provider_tokens": 0,
        }
