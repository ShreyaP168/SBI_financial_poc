"""Explicitly marked, non-deliverable PDF copies for authenticated reviewers."""

from pathlib import Path

import fitz


def review_pdf(path: Path) -> bytes:
    with fitz.open(path) as document:
        for page in document:
            rectangle = fitz.Rect(
                12, page.rect.height - 28, page.rect.width - 12, page.rect.height - 8
            )
            page.draw_rect(rectangle, color=(0.8, 0, 0), fill=(1, 1, 1), overlay=True)
            page.insert_textbox(
                rectangle,
                "REVIEW ONLY - UNVALIDATED - NOT FOR DECISION DELIVERY",
                fontsize=8,
                color=(0.8, 0, 0),
                align=1,
                overlay=True,
            )
        return document.tobytes()
