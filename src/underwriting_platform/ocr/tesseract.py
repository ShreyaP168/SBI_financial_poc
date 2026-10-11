"""Tesseract CLI adapter returning the platform's common TextLine shape."""

from __future__ import annotations

import csv
import io
import shutil
import subprocess
from pathlib import Path

from PIL import Image

from underwriting_platform.configuration import local_ocr as settings

from .rapidocr import TextLine


class TesseractTextExtractor:
    def __init__(self, psm: int = 11, language: str = "eng", whitelist: str | None = None) -> None:

        self._psm = psm

        self._language = language

        self._whitelist = whitelist

    @property
    def psm(self) -> int:

        return self._psm

    @property
    def engine_name(self) -> str:

        return f"tesseract_psm_{self._psm}"

    @property
    def model_name(self) -> str:

        return f"tesseract-{self._language}"

    @property
    def model_version(self) -> str:

        return "5.x"

    def extract(self, image: Image.Image) -> list[TextLine]:

        executable = settings.get("TESSERACT_CMD") or shutil.which("tesseract")

        if executable is None:
            windows_default = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")

            executable = str(windows_default) if windows_default.is_file() else "tesseract"

        command = [
            executable,
            "stdin",
            "stdout",
            "-l",
            self._language,
            "--psm",
            str(self._psm),
        ]

        if self._whitelist:
            command.extend(["-c", f"tessedit_char_whitelist={self._whitelist}"])

        command.append("tsv")

        process = subprocess.run(
            command,
            input=_png_bytes(image),
            capture_output=True,
            check=True,
            timeout=settings.get("OCR_TIMEOUT_SECONDS"),
        )

        return parse_tsv(process.stdout.decode("utf-8", errors="replace"))

    def extract_region(
        self, image: Image.Image, x0: int, y0: int, x1: int, y1: int
    ) -> list[TextLine]:

        lines = self.extract(image.crop((x0, y0, x1, y1)))

        return [
            TextLine(
                line.text,
                line.x0 + x0,
                line.y0 + y0,
                line.x1 + x0,
                line.y1 + y0,
                line.confidence,
            )
            for line in lines
        ]


def _png_bytes(image: Image.Image) -> bytes:

    buffer = io.BytesIO()

    image.save(buffer, format="PNG")

    return buffer.getvalue()


def parse_tsv(payload: str) -> list[TextLine]:

    groups = {}

    for row in csv.DictReader(io.StringIO(payload), delimiter="\t"):
        text = (row.get("text") or "").strip()

        try:
            confidence = float(row.get("conf") or -1)

        except ValueError:
            continue

        if not text or confidence < 0:
            continue

        key = tuple(row.get(k) for k in ("page_num", "block_num", "par_num", "line_num"))

        x0, y0 = float(row["left"]), float(row["top"])

        groups.setdefault(key, []).append(
            TextLine(
                text, x0, y0, x0 + float(row["width"]), y0 + float(row["height"]), confidence / 100
            )
        )

    return [
        TextLine(
            " ".join(w.text for w in words),
            min(w.x0 for w in words),
            min(w.y0 for w in words),
            max(w.x1 for w in words),
            max(w.y1 for w in words),
            min(w.confidence for w in words),
            sum(w.confidence for w in words) / len(words),
        )
        for words in groups.values()
    ]
