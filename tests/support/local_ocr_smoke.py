"""Offline container smoke: real local engines, synthetic evidence, no network."""

import json
import os

from PIL import Image, ImageDraw, ImageFont

from underwriting_platform.ocr import read_page

image = Image.new("RGB", (1200, 450), "white")
ImageDraw.Draw(image).multiline_text(
    (50, 70),
    "BUSINESS NAME FINANCE\nANNUAL INCOME 860764",
    font=ImageFont.load_default(size=40),
    fill="black",
    spacing=25,
)
for provider in ("tesseract", "rapidocr"):
    os.environ["UW_LOCAL_OCR_PROVIDER"] = provider
    result = read_page(image, 1)
    assert result["status"] == "completed", result.get("error_code")
    assert "860764" in result["text"], "Critical numeric transcription missing"
    assert result["provider_tokens"] == 0
    print(
        json.dumps(
            {
                "test": "offline-local-ocr",
                "provider": provider,
                "version": result["version"],
                "status": "passed",
                "lines": len(result["lines"]),
                "provider_tokens": 0,
            }
        )
    )
