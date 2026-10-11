"""
engine/photo_match.py — Gemini-only face match for identity verification.

Compares two facial images (typically Aadhaar/KYC photo vs the live photo
captured at the diagnostic centre) and returns a structured verdict with
confidence + reasoning.

The prompt is adapted from kaiuw-sbi-prod's `utility/photo_match.py`
GPT-4o block — same hard rules around grayscale↔colour, aging, head
coverings, low-resolution input. Single-vendor for the first cut; we
can layer DeepFace or a second vendor in later if accuracy warrants
(same dual-vendor philosophy as `handwritten_reocr.py`).

Decision thresholds match the kaiuw average-confidence ladder, single-
score variant:
  confidence ≥ 0.70           → "match"           (no manual review)
  0.45 ≤ confidence < 0.70    → "partial-match"   (manual review)
  confidence < 0.45            → "no-match"        (manual review)
  face genuinely missing       → "undetectable"    (manual review)
"""

from __future__ import annotations
from underwriting_platform.llm.budget import TokenBudgetExceeded
import asyncio
import base64
import io
import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MATCH_THRESHOLD = 0.70
_PARTIAL_THRESHOLD = 0.45

_DEFAULT_GEMINI_MODEL = "gemini-2.5-pro"

_PROMPT = """\
You are a face verification system for insurance fraud detection. You
will be shown TWO photos:

  Image 1: Applicant identity document photo (e.g. Aadhaar / PAN / passport).
  Image 2: Live photo captured at the diagnostic centre during medicals.

Your task: determine whether both photos show the SAME person.

## CRITICAL — base your decision ONLY on STRUCTURAL FACIAL FEATURES

  - Face shape (oval / round / square)
  - Eyes (shape, spacing, size)
  - Nose (shape, width, bridge)
  - Mouth and lips
  - Jawline and chin
  - Forehead structure
  - Beard, moustache, facial hair (if visible)

DO NOT use background, clothing, hair style, or skin tone as matching
criteria.

## CRITICAL — GRAYSCALE vs COLOUR

Image 1 is often a grayscale Aadhaar ID photo; Image 2 is a colour
live photo. NEVER compare skin tone or complexion between a grayscale
and a colour photo — it is a meaningless comparison. Focus ONLY on
geometric facial structure: bone shape, eye spacing, nose dimensions,
jawline geometry.

## CRITICAL — AGING

ID photos may be 5–15 years old. A person's face changes with age —
skin texture, weight, wrinkles, and facial fullness will differ.
Focus on features that remain stable: eye-socket position and spacing,
nose-bridge width, ear shape, forehead height, overall bone structure.
Do NOT penalise for differences in skin texture, weight, or wrinkles.

## CRITICAL — HEAD COVERINGS

Turbans, hijabs, caps, and scarves do NOT obscure the face. Compare
the visible face area below the covering. NEVER mark a face as
undetectable just because a head covering is present. Only mark
undetectable if the actual face area is blank or severely blurred.

## CRITICAL — LOW QUALITY / BLUR

Aadhaar ID crops are frequently small, compressed, and soft-focus.
This is NOT grounds to declare the face undetectable. If you can see
ANY of: the outline of the face, the position of the eyes, a moustache
or beard, the shape of the jaw, or the hairline — produce a real
best-effort comparison and return a confidence in the 0.30–0.70 range
based on how well visible features align.

The "undetectable" bail-out is RESERVED for the case where the face
region is entirely blank, fully black, fully white, fully pixelated
noise, or missing from the crop. Do NOT use it merely because the image
is small or soft.

## Output schema (STRICT — JSON only, no prose, no markdown fences)

{
  "match":      true | false,
  "confidence": 0.0 - 1.0,
  "reasoning":  "describe specific facial features compared, and which (if any) could not be resolved"
}

Confidence guide:
  - ≥ 0.70 — high certainty of same face
  - 0.40 – 0.69 — features mostly align but at least one ambiguous
  - 0.30 – 0.40 — partially visible features conflict, or only a subset compared
  - < 0.30 — visible features clearly differ
  - 0.05 — face region genuinely missing or solid noise (per the rule above)

First char {, last char }. No prose, no markdown fences.
"""


def _img_mime(img_bytes: bytes) -> str:
    if img_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if img_bytes[:2] == b"\xff\xd8":
        return "image/jpeg"
    if img_bytes[:2] == b"BM":
        return "image/bmp"
    if img_bytes[:4] == b"RIFF" and img_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _safe_json(raw: str) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3].rstrip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip("\n")
    try:
        return json.loads(text)
    except Exception:
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            try:
                return json.loads(text[i : j + 1])
            except Exception:
                return None
    return None


def _classify(confidence: float) -> tuple[str, bool]:
    """Return (match_result, requires_manual_review)."""
    if confidence < 0.10:
        return "undetectable", True
    if confidence >= _MATCH_THRESHOLD:
        return "match", False
    if confidence >= _PARTIAL_THRESHOLD:
        return "partial-match", True
    return "no-match", True


async def compare_faces_gemini(
    img1_bytes: bytes,
    img2_bytes: bytes,
    gemini_model: Optional[str] = None,
) -> dict:
    """Run Gemini Vision face comparison.

    Returns a dict shaped:
      {
        "match_result":  "match" | "partial-match" | "no-match" | "undetectable" | "skipped",
        "confidence":    float 0.0-1.0 (None if skipped),
        "requires_manual_review": bool,
        "reasoning":     str,
        "model":         "gemini-2.5-pro" or whatever was used,
        "error":         str | None,
      }
    """
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return {
            "match_result": "skipped",
            "confidence": None,
            "requires_manual_review": False,
            "reasoning": "GEMINI_API_KEY not set — face match disabled.",
            "model": gemini_model or _DEFAULT_GEMINI_MODEL,
            "error": "no_api_key",
        }

    model = gemini_model or os.environ.get("GEMINI_PHOTO_MATCH_MODEL") or _DEFAULT_GEMINI_MODEL
    mime1, mime2 = _img_mime(img1_bytes), _img_mime(img2_bytes)

    raw_text: Optional[str] = None
    err: Optional[str] = None

    # Path 1 — google.genai (new SDK)
    try:
        from google import genai
        from google.genai import types as genai_types

        from underwriting_platform.llm.gemini_budget import generate

        client = genai.Client(api_key=api_key)
        response = await generate(
            client,
            model=model,
            contents=[
                genai_types.Part.from_bytes(data=img1_bytes, mime_type=mime1),
                genai_types.Part.from_bytes(data=img2_bytes, mime_type=mime2),
                _PROMPT,
            ],
        )
        raw_text = response.text or ""
    except TokenBudgetExceeded:
        raise
    except ImportError:
        err = "google-genai not installed"
    except Exception as e:
        err = f"new SDK failed: {e}"
        logger.info("[photo_match] new SDK failed: %s", e)

    # Path 2 — google.generativeai (legacy)
    from underwriting_platform.llm.budget import current_budget

    if raw_text is None and current_budget.get() is None:
        try:
            import google.generativeai as legacy
            from PIL import Image

            def _call_legacy() -> str:
                legacy.configure(api_key=api_key)
                m = legacy.GenerativeModel(model)
                img1 = Image.open(io.BytesIO(img1_bytes))
                img2 = Image.open(io.BytesIO(img2_bytes))
                resp = m.generate_content([_PROMPT, img1, img2])
                return getattr(resp, "text", "") or ""

            raw_text = await asyncio.to_thread(_call_legacy)
            err = None
        except ImportError:
            err = err or "no Gemini SDK available"
        except Exception as e:
            err = f"legacy SDK failed: {e}"
            logger.info("[photo_match] legacy SDK failed: %s", e)

    if not raw_text:
        return {
            "match_result": "skipped",
            "confidence": None,
            "requires_manual_review": False,
            "reasoning": f"Gemini call failed: {err or 'unknown error'}",
            "model": model,
            "error": err or "no_response",
        }

    parsed = _safe_json(raw_text) or {}
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    match_result, manual = _classify(confidence)
    reasoning = str(parsed.get("reasoning") or "").strip()

    return {
        "match_result": match_result,
        "confidence": round(confidence, 4),
        "requires_manual_review": manual,
        "reasoning": reasoning,
        "model": model,
        "error": None,
    }


# ── File loaders ──────────────────────────────────────────────────


def load_image_bytes(path: Path) -> Optional[bytes]:
    """Read raw bytes from disk; None on failure."""
    try:
        return path.read_bytes()
    except Exception as e:
        logger.warning("[photo_match] could not read %s: %s", path, e)
        return None


def render_first_image_from_pdf(pdf_path: Path) -> Optional[bytes]:
    """Render page 1 of a PDF at 300 DPI as JPEG bytes — used when the
    KYC scan or live photo arrives as a PDF instead of an image file."""
    try:
        import fitz
    except ImportError:
        return None
    try:
        doc = fitz.open(str(pdf_path))
        if len(doc) == 0:
            doc.close()
            return None
        pix = doc[0].get_pixmap(dpi=300)
        png = pix.tobytes("png")
        doc.close()
        return png
    except Exception as e:
        logger.warning("[photo_match] could not render %s: %s", pdf_path, e)
        return None
