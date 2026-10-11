"""
engine/handwritten_reocr.py — focused 2nd-pass OCR for handwritten MER sections.

The primary medical agent reads the entire MER (often 14+ pages) at the
vision pipeline's downsampled resolution. Handwritten box-grid digits and
hand-printed labels are the largest source of misreads — a hand-printed
8 with a heavy upper loop regularly gets read as a 6, and an "M" / "F"
relationship marker is easy to flip.

This module runs ONE focused 2nd pass per registered section:

  1. Text-layer screen — find pages whose text contains the section's
     keywords, so we don't pay an LLM call per page.
  2. Locate (Claude) — render the candidate page at 180 DPI, ask the
     primary LLM to return a percentage-bbox of the section.
  3. Crop + read (Gemini, if available) — re-render the located page at
     600 DPI, crop to the bbox, ask Gemini to read every value into a
     constrained JSON schema specific to the section.

Why Gemini for the read step: dual-vendor consensus. The 4↔7 (vitals) /
6↔8 (ages) / M↔F (relationship) confusions are encoder-specific. A
different vision encoder trained on different data will disagree with
Claude when Claude is wrong — that disagreement is the actual signal
we want. Same-vendor re-OCR can't catch this because the encoder's
character priors don't change.

Returns a dict suitable for `case_json["medical"]["<section_key>_reocr"]`.
The engine flag stage compares each section's read against the medical
agent's primary block and escalates per-section flags
(`VITALS_REOCR_DISAGREEMENT`, `FAMILY_HISTORY_REOCR_DISAGREEMENT`, …).

Backend selection (read step):
  1. If GEMINI_API_KEY is set → Gemini (default vendor for digit read).
     Model: GEMINI_VITAL_REOCR_MODEL env var, default "gemini-2.5-pro".
  2. Else → primary LLM client (Claude). Same-vendor; less informative.
"""

from __future__ import annotations
import asyncio
import base64
import io
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Section schema ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Section:
    """Declarative spec for one handwritten region of a form.

    Each registered section produces one focused 2nd-pass read. The
    engine flag stage owns the comparator; this module just locates,
    crops, reads, and returns the structured payload.
    """

    key: str  # e.g. "vitals", "family_history"
    text_keywords: tuple[str, ...]  # for the text-layer pre-screen
    min_keyword_hits: int  # min keyword hits to qualify a page
    locate_system: str
    locate_user: str
    read_system: str
    read_user: str
    locate_dpi: int = 180
    read_dpi: int = 600
    fallback_first_n_pages: int = 5  # if text screen finds nothing
    crop_pad_top: float = 8.0  # bbox padding %
    crop_pad_bottom: float = 4.0
    crop_pad_side: float = 2.0


# ── Helpers (section-agnostic) ─────────────────────────────────────


def _img_block_png(png_bytes: bytes) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(png_bytes).decode("ascii"),
        },
    }


def _safe_json(raw: str) -> dict | None:
    """Tolerant JSON parse — strips fences, finds outermost {...}."""
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


def _candidate_pages(pdf_path: Path, section: Section) -> list[int]:
    """Pages whose text layer suggests the section is present. Cheap
    pre-filter so we don't LLM every page. Falls back to first N pages
    when the text layer is too garbled to keyword-screen.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.warning("[reocr/%s] PyMuPDF not installed — cannot screen pages", section.key)
        return []
    try:
        doc = fitz.open(str(pdf_path))
        out: list[int] = []
        for i in range(len(doc)):
            txt = (doc[i].get_text() or "").upper()
            score = sum(1 for kw in section.text_keywords if kw in txt)
            if score >= section.min_keyword_hits:
                out.append(i)
        n_pages = len(doc)
        doc.close()
        if not out:
            out = list(range(min(section.fallback_first_n_pages, n_pages)))
        return out
    except Exception as e:
        logger.warning("[reocr/%s] page scan failed on %s: %s", section.key, pdf_path.name, e)
        return []


# Maximum raw PNG bytes we'll send to the LLM. Azure's content-length
# limit is 32 MB; after base64 (+33%) that's ~24 MB of raw bytes for the
# WHOLE request (incl. JSON envelope + system prompt). 5 MB raw PNG keeps
# us comfortably under even on multi-image requests.
_MAX_PNG_BYTES = 5_000_000


def _render_page_png(pdf_path: Path, page_idx: int, dpi: int) -> bytes | None:
    """Render a page to PNG with an adaptive-DPI guardrail.

    Hi-res scanned KYC documents (Aadhaar with embedded photo, multi-page
    passport scans) can produce >30 MB PNGs at 180 DPI which trips the
    Azure 32 MB request-size limit. When the first render exceeds
    `_MAX_PNG_BYTES` we step the DPI down and retry; if even 90 DPI is
    too large we give up on the page and return None so the caller can
    skip it cleanly.
    """
    try:
        import fitz
    except ImportError:
        return None
    candidate_dpis = [dpi]
    for fallback in (120, 90):
        if fallback < dpi and fallback not in candidate_dpis:
            candidate_dpis.append(fallback)
    for d in candidate_dpis:
        try:
            doc = fitz.open(str(pdf_path))
            pix = doc[page_idx].get_pixmap(dpi=d)
            png = pix.tobytes("png")
            doc.close()
        except Exception as e:
            logger.warning(
                "[reocr] render failed (%s p%d @%ddpi): %s", pdf_path.name, page_idx, d, e
            )
            return None
        if len(png) <= _MAX_PNG_BYTES:
            if d != dpi:
                logger.info(
                    "[reocr] %s p%d: downscaled to %d DPI (PNG was %d bytes at %d)",
                    pdf_path.name,
                    page_idx,
                    d,
                    len(png),
                    dpi,
                )
            return png
        logger.info(
            "[reocr] %s p%d: PNG %d bytes at %d DPI > %d MB cap; retrying lower DPI",
            pdf_path.name,
            page_idx,
            len(png),
            d,
            _MAX_PNG_BYTES // 1_000_000,
        )
    logger.warning(
        "[reocr] %s p%d: PNG still exceeds %d MB at 90 DPI — skipping page",
        pdf_path.name,
        page_idx,
        _MAX_PNG_BYTES // 1_000_000,
    )
    return None


# Filename-keyword filter — these document types are KYC / identity
# proofs and never contain medical sections. Skipping them in re-OCR
# saves the wasted LLM round-trips that were burning ~3-5 min/case on
# 32 MB content-length retries (smoke run 2026-05-26).
_KYC_FILENAME_EXCLUDE: tuple[str, ...] = (
    "aadhar",
    "aadhaar",
    "uidai",
    "pan card",
    "pancard",
    "pan-card",
    "pan_card",
    "panproof",
    "passport",
    "voter",
    "voterid",
    "voter_id",
    "driving licence",
    "driving license",
    "dl card",
    "dl_card",
    "kyc",
    # Note: standalone "uid" intentionally NOT included — substring-matches
    # against innocuous medical filenames ("liquid biopsy", "fluid balance",
    # "guideline"). "aadhar"/"uidai" already cover the Aadhaar case.
)


def _is_kyc_doc(pdf_path: Path) -> bool:
    """True iff the filename looks like a KYC/identity proof rather than
    a medical document. Conservative substring match on lowercased name.
    """
    name = pdf_path.name.lower()
    return any(kw in name for kw in _KYC_FILENAME_EXCLUDE)


def _crop_pct(png_bytes: bytes, bbox_pct: list[float], section: Section) -> bytes | None:
    """Crop a rendered page PNG by percentage bbox [x1, y1, x2, y2]."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(png_bytes))
        w, h = img.size
        clamped = [max(0.0, min(100.0, float(v))) for v in bbox_pct]
        x1, y1, x2, y2 = clamped
        if x2 <= x1 or y2 <= y1:
            return None
        # Pad generously — locator sometimes misses adjacent rows.
        x1 = max(0.0, x1 - section.crop_pad_side)
        y1 = max(0.0, y1 - section.crop_pad_top)
        x2 = min(100.0, x2 + section.crop_pad_side)
        y2 = min(100.0, y2 + section.crop_pad_bottom)
        box = (int(w * x1 / 100), int(h * y1 / 100), int(w * x2 / 100), int(h * y2 / 100))
        crop = img.crop(box)
        # Cap longest edge to 1800 px — Anthropic's image ceiling.
        cw, ch = crop.size
        longest = max(cw, ch)
        if longest > 1800:
            scale = 1800 / longest
            crop = crop.resize((int(cw * scale), int(ch * scale)), Image.LANCZOS)
        out = io.BytesIO()
        crop.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception as e:
        logger.warning("[reocr] crop failed: %s", e)
        return None


# Default to Gemini 2.5 Pro for accuracy on handwritten content. Pro reads
# detail-heavy crops materially better than Flash; per-section the cost
# delta is negligible while accuracy gain is meaningful for digit-level
# and label-level reads.
_DEFAULT_GEMINI_MODEL = "gemini-2.5-pro"


async def _gemini_read(
    crop_png: bytes, gemini_model: str, read_system: str, read_user: str
) -> tuple[str | None, str]:
    """Call Gemini on the cropped image. Returns (raw_text, model)."""
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return None, gemini_model

    full_prompt = read_system + "\n\n" + read_user

    # Path 1 — google.genai (new SDK)
    try:
        from google import genai
        from google.genai import types as genai_types

        def _call_new() -> str:
            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(
                model=gemini_model,
                contents=[
                    genai_types.Part.from_bytes(data=crop_png, mime_type="image/png"),
                    full_prompt,
                ],
            )
            return resp.text or ""

        text = await asyncio.to_thread(_call_new)
        return text, gemini_model
    except ImportError:
        pass
    except Exception as e:
        logger.warning("[reocr] Gemini (new SDK) call failed: %s", e)

    # Path 2 — google.generativeai (legacy)
    try:
        import google.generativeai as legacy_genai
        from PIL import Image

        def _call_legacy() -> str:
            legacy_genai.configure(api_key=api_key)
            model = legacy_genai.GenerativeModel(gemini_model)
            img = Image.open(io.BytesIO(crop_png))
            resp = model.generate_content([full_prompt, img])
            return getattr(resp, "text", "") or ""

        text = await asyncio.to_thread(_call_legacy)
        return text, gemini_model
    except ImportError:
        logger.warning("[reocr] no Gemini SDK available — install `google-genai`")
        return None, gemini_model
    except Exception as e:
        logger.warning("[reocr] Gemini (legacy SDK) call failed: %s", e)
        return None, gemini_model


# ── Per-section pipeline ──────────────────────────────────────────


async def reocr_section_for_case(
    case_dir: Path,
    section: Section,
    llm_client: Any,
    model_override: Optional[str] = None,
    debug_dir: Optional[Path] = None,
) -> dict | None:
    """Run the focused locate→crop→read pipeline for one section."""
    case_dir = Path(case_dir)
    pdfs_all = sorted(case_dir.glob("*.pdf"))
    if not pdfs_all:
        return None
    # Skip KYC / identity-proof PDFs — they never contain MER sections
    # but their embedded high-res photos blow past Azure's 32 MB request
    # limit when rendered at 180 DPI. See _KYC_FILENAME_EXCLUDE above.
    pdfs = [p for p in pdfs_all if not _is_kyc_doc(p)]
    skipped_kyc = [p.name for p in pdfs_all if _is_kyc_doc(p)]
    if skipped_kyc:
        logger.info(
            "[reocr/%s] skipping %d KYC doc(s): %s", section.key, len(skipped_kyc), skipped_kyc
        )
    if not pdfs:
        logger.info("[reocr/%s] no non-KYC PDFs in %s", section.key, case_dir.name)
        return None

    chosen_model = (
        model_override
        or os.environ.get("VITAL_REOCR_MODEL")
        or os.environ.get("AZURE_CLAUDE_HAIKU_DEPLOYMENT")
        or None
    )
    model_label = chosen_model or "<client_default>"

    # ── Step 1: locate the section's bbox ─────────────────────────
    located: dict | None = None
    for pdf_path in pdfs:
        cands = _candidate_pages(pdf_path, section)
        if not cands:
            continue
        for page_idx in cands:
            png = _render_page_png(pdf_path, page_idx, dpi=section.locate_dpi)
            if png is None:
                continue
            try:
                raw = await llm_client.chat(
                    system=section.locate_system,
                    blocks=[
                        _img_block_png(png),
                        {"type": "text", "text": section.locate_user},
                    ],
                    max_tokens=200,
                    model=chosen_model,
                )
            except Exception as e:
                logger.warning(
                    "[reocr/%s] locate call failed (%s p%d): %s",
                    section.key,
                    pdf_path.name,
                    page_idx,
                    e,
                )
                continue
            data = _safe_json(raw) or {}
            bbox = data.get("bbox_pct")
            if data.get("has_section") and isinstance(bbox, list) and len(bbox) == 4:
                located = {
                    "pdf_path": pdf_path,
                    "page_idx": page_idx,
                    "bbox_pct": [float(x) for x in bbox],
                }
                logger.info(
                    "[reocr/%s] located on %s p%d bbox=%s",
                    section.key,
                    pdf_path.name,
                    page_idx + 1,
                    bbox,
                )
                break
        if located:
            break

    if not located:
        logger.info("[reocr/%s] no section located in case folder", section.key)
        return None

    # ── Step 2: hi-res render → crop → read ──────────────────────
    hi_png = _render_page_png(located["pdf_path"], located["page_idx"], dpi=section.read_dpi)
    if hi_png is None:
        return None
    crop_png = _crop_pct(hi_png, located["bbox_pct"], section)
    if crop_png is None:
        return None

    if debug_dir is not None:
        try:
            debug_dir = Path(debug_dir)
            debug_dir.mkdir(parents=True, exist_ok=True)
            crop_path = debug_dir / f"reocr_{section.key}_crop.png"
            crop_path.write_bytes(crop_png)
            logger.info("[reocr/%s] crop saved → %s", section.key, crop_path)
        except Exception as e:
            logger.warning("[reocr/%s] could not save debug crop: %s", section.key, e)

    # ── Read step — prefer Gemini for vendor diversity ────────────
    raw: str | None = None
    read_vendor = "claude"
    read_model = model_label
    use_gemini = bool((os.environ.get("GEMINI_API_KEY") or "").strip())
    if use_gemini and not model_override:
        gemini_model = os.environ.get("GEMINI_VITAL_REOCR_MODEL") or _DEFAULT_GEMINI_MODEL
        raw, read_model = await _gemini_read(
            crop_png, gemini_model, section.read_system, section.read_user
        )
        if raw:
            read_vendor = "gemini"
        else:
            logger.info("[reocr/%s] Gemini unavailable; falling back to primary LLM", section.key)

    if raw is None:
        try:
            raw = await llm_client.chat(
                system=section.read_system,
                blocks=[
                    _img_block_png(crop_png),
                    {"type": "text", "text": section.read_user},
                ],
                max_tokens=600,
                model=chosen_model,
            )
            read_vendor = "claude"
            read_model = model_label
        except Exception as e:
            logger.warning("[reocr/%s] read call failed: %s", section.key, e)
            return None

    values = _safe_json(raw) or {}
    return {
        "section": section.key,
        "source_pdf": located["pdf_path"].name,
        "source_page": located["page_idx"] + 1,
        "bbox_pct": located["bbox_pct"],
        "vendor": read_vendor,
        "model": read_model,
        "values": values,
        "function": "underwriting_platform.engine.handwritten_reocr.reocr_section_for_case",
    }


async def reocr_all_sections_for_case(
    case_dir: Path,
    llm_client: Any,
    sections: Optional[list[Section]] = None,
    debug_dir: Optional[Path] = None,
) -> dict[str, dict]:
    """Run all registered sections in parallel. Returns {section_key: result}."""
    sections = sections or list(REGISTRY.values())
    results = await asyncio.gather(
        *[reocr_section_for_case(case_dir, s, llm_client, debug_dir=debug_dir) for s in sections]
    )
    return {s.key: r for s, r in zip(sections, results) if r is not None}


# ══════════════════════════════════════════════════════════════════
# Section definitions
# ══════════════════════════════════════════════════════════════════

# ── VITALS ─────────────────────────────────────────────────────────

_VITALS_LOCATE_SYSTEM = (
    "You are a vision-OCR assistant looking at one page of a Medical "
    "Examination Report (MER). Return JSON only — no prose, no markdown."
)
_VITALS_LOCATE_USER = """\
This is one page of a Medical Examination Report (MER). Find the bbox
that COVERS BOTH of these regions, even when they are separated by
unrelated content (e.g. URINE DIPSTICK) between them:

  Region A — the 'HEIGHT & WEIGHT INFORMATION' table containing the
             handwritten Height, Weight, Chest Circumference, and
             Abdomen Circumference boxes.

  Region B — the 'BLOOD PRESSURE' table and the 'PULSE' row, with
             their handwritten numeric boxes.

Return ONE bbox that is the UNION of both regions — i.e. y1 = top of
Region A's section header, y2 = bottom of Region B's last row. If only
one of the two regions is on this page, return its bbox. If NEITHER is
on this page, return has_section=false.

Return JSON in EXACTLY this schema (numbers, no quotes):
{"has_section": true, "bbox_pct": [x1, y1, x2, y2]}

bbox_pct values are PERCENTAGES of the page (0-100). Err on the side
of including a little extra above and below — it is far worse to
crop out a vital row than to include adjacent content.

If neither region is on this page, return:
{"has_section": false, "bbox_pct": null}

First char {, last char }. No prose.
"""

_VITALS_READ_SYSTEM = (
    "You are reading a tightly-cropped image of the vital-signs section "
    "of an Indian Medical Examination Report (MER). Every number is "
    "hand-written into small printed boxes. Return JSON only — no prose, "
    "no markdown."
)
_VITALS_READ_USER = """\
## Layout you will see

The MER's vital-signs section is laid out in labelled rows. Read each
row independently — DO NOT merge digits across different rows.

Row labels you may see (left column):
  - "Height"               — units cell shows "ft / inch" or "Mtr" or
                              "cm". Value boxes hold the digits. NB:
                              when units = Mtr the value spans TWO cells:
                              a leading "1" cell + "60" cell = 1.60 m =
                              160 cm. ALWAYS combine those cells.
  - "Weight"               — value in Kg. One cell, 2 digits typical.
  - "Chest Circumference"  — value in cm. 2-3 digits.
  - "Abdominal Circumference" — value in cm. 2-3 digits.
  - "Systolic"             — BP top number (3 digits typical, e.g. 130).
  - "Diastolic"            — BP bottom number (2-3 digits, e.g. 80).
  - "Rate" / "Pulse"       — bpm, 2-3 digits.

## Plausibility ranges (use these to validate your read — if your
digit-read is OUTSIDE the range, re-read; you almost certainly misread)

  - height_cm:     140 – 210     (adult; <140 only for minors)
  - weight_kg:      30 – 180     (adult)
  - chest_cm:       70 – 130
  - abdomen_cm:     60 – 140
  - bp_systolic:    80 – 220
  - bp_diastolic:   40 – 130
  - pulse_bpm:      40 – 120

If after re-reading your value is still outside the range, return it
anyway BUT list it in `ambiguities`.

## Digit-shape gotchas (handwritten Indian numerals, English-style)

  - **7 vs 4**: a hand-printed 7 often has a horizontal cross-bar through
                its middle — it is STILL a 7, not a 4. A 7's right edge
                slants down-left. A 4 has an open or closed triangle on
                its upper-left and a long vertical stroke on the right.
  - **1 vs 7**: a 1 is a single vertical stroke (sometimes with a tiny
                top serif). A 7 has a clear horizontal top-stroke.
  - **0 vs 6 vs 9**: 0 is fully closed; 6 has an open top-loop; 9 has
                an open bottom-loop.
  - **3 vs 5 vs 8**: check whether the loops are closed (8) or open (3, 5).

## Cross-checks before finalising

1. Compute BMI = weight_kg / (height_cm/100)². If BMI is below 12 or
   above 50 your reads are almost certainly wrong — re-check height and
   weight.
2. Compute systolic - diastolic; pulse pressure should be 20-80 mmHg.
   Outside that range, re-read.

## Output schema (STRICT — JSON only, no prose, no markdown)

{
  "height_cm":     <integer or null>,
  "weight_kg":     <integer or null>,
  "chest_cm":      <integer or null>,
  "abdomen_cm":    <integer or null>,
  "bp_systolic":   <integer or null>,
  "bp_diastolic":  <integer or null>,
  "pulse_bpm":     <integer or null>,
  "computed_bmi":  <number rounded to 1 decimal, or null if h/w missing>,
  "out_of_range":  ["<list of fields whose final value falls outside the plausibility range; [] if all clean>"],
  "ambiguities":   "<one line — describe any digit you re-read or are uncertain about; '' if confident>"
}

First char {, last char }. No prose, no markdown fences.
"""


# ── FAMILY HISTORY ────────────────────────────────────────────────

_FAMILY_LOCATE_SYSTEM = (
    "You are a vision-OCR assistant looking at one page of a Medical "
    "Examination Report (MER). Return JSON only — no prose, no markdown."
)
_FAMILY_LOCATE_USER = """\
This is one page of a Medical Examination Report (MER). Find the bbox
of the FAMILY HISTORY / FAMILY DETAILS table that lists the applicant's
relatives (Father, Mother, Brother(s), Sister(s), and possibly
grandparents). The table will have columns like:

  Relationship | Age | Living/Deceased | Cause of death / Conditions

Each row is one relative. The relationship column may be a label
("Father", "Mother", "Brother", "Sister") OR a checkbox column with
"M / F" markers next to a generic "Sibling" label — read whichever
form is present.

Return ONE bbox covering the full table from the column-header row
through to the last filled row. If there is NO family history table
on this page, return has_section=false.

Return JSON in EXACTLY this schema (numbers, no quotes):
{"has_section": true, "bbox_pct": [x1, y1, x2, y2]}

bbox_pct values are PERCENTAGES of the page (0-100). Err on the side
of including extra above and below — losing a row is worse than
including adjacent content.

If absent, return:
{"has_section": false, "bbox_pct": null}

First char {, last char }. No prose.
"""

_FAMILY_READ_SYSTEM = (
    "You are reading a tightly-cropped image of the FAMILY HISTORY table "
    "from an Indian Medical Examination Report (MER). Every value is "
    "hand-written. Return JSON only — no prose, no markdown."
)
_FAMILY_READ_USER = """\
## Layout you will see

The family-history table lists one row per relative. Columns typically
include:
  - **Relationship** — "Father", "Mother", "Brother", "Sister", or a
    generic "Sibling" with a separate **M / F** column. There may be
    multiple Brother / Sister rows. Read the relationship per row.
  - **Age** — handwritten 2-digit integer (typically 20-90).
  - **Living / Deceased** — checkbox or letter ("L" / "D", or "Alive" /
    "Dead").
  - **Cause of death / Conditions** — handwritten free text. Often empty.

## Plausibility

  - Father / Mother age: 40 – 95 typically; over 95 is plausible but
    re-check the digits.
  - Brother / Sister age: 0 – 90; usually within 30 years of the
    applicant.
  - Grandparent age: 60 – 110.

If a value is outside these ranges, list it in `ambiguities` AND
describe the alternate read.

## Digit-shape gotchas (handwritten Indian numerals, English-style)

  - **6 vs 8**: an 8 has TWO closed loops; a 6 has an open top and one
                closed bottom loop.
  - **3 vs 5**: a 3 has two right-facing curves; a 5 has a horizontal
                top-bar followed by one curve.
  - **1 vs 7**: a 1 is a vertical stroke; a 7 has a clear horizontal top.
  - **4 vs 7**: a 4 has a triangle on the upper left; a 7 may have a
                horizontal cross-bar through its middle (still a 7).

## Relationship-marker policy — DO NOT GUESS M/F

When a row's relation cell shows the literal word "Sibling" with two
small checkboxes labelled "M" and "F", DO NOT attempt to determine
which checkbox is filled. A separate per-cell checkbox-isolation pass
runs after this one and resolves M/F deterministically — this avoids
the well-known M↔F flip that vision models exhibit when they see both
checkboxes side-by-side.

For sibling rows, set `relationship` to the literal string `"sibling"`.
The downstream pass will rewrite it to "brother" or "sister" using the
isolated read.

For Father / Mother / Grandfather / Grandmother rows where the
relationship is printed as a text label (not as M/F checkboxes), use
that printed label directly.

## Output schema (STRICT — JSON only, no prose, no markdown)

{
  "members": [
    {
      "row_position":            <integer — 0-based top-to-bottom row order on the form>,
      "relation_label_on_form":  "<literal text printed in the relation cell — 'Father' / 'Mother' / 'Sibling' / 'Grandfather' / etc. — even if blank, return ''>",
      "relationship":            "father|mother|sibling|grandfather|grandmother|other",
      "age":                     <integer or null>,
      "alive":                   true|false|null,
      "conditions":              "<free text from the conditions cell, or '' if blank>"
    }
  ],
  "ambiguities":  "<one line — describe any digit you re-read or are uncertain about; '' if confident>"
}

The `members` array MUST contain ONE entry per non-blank data row in
TOP-TO-BOTTOM ORDER as the rows appear on the form. The `row_position`
field MUST be the 0-based index of the row in that top-to-bottom order
(0 = topmost data row).

Skip fully blank rows.

First char {, last char }. No prose, no markdown fences.
"""


# ── REGISTRY ──────────────────────────────────────────────────────

_VITALS_SECTION = Section(
    key="vitals",
    text_keywords=(
        "HEIGHT",
        "WEIGHT",
        "BLOOD PRESSURE",
        "PULSE",
        "EXAMINATION",
        "CIRCUMFERENCE",
        "MER",
    ),
    min_keyword_hits=2,
    locate_system=_VITALS_LOCATE_SYSTEM,
    locate_user=_VITALS_LOCATE_USER,
    read_system=_VITALS_READ_SYSTEM,
    read_user=_VITALS_READ_USER,
    fallback_first_n_pages=3,
)

_FAMILY_SECTION = Section(
    key="family_history",
    text_keywords=(
        "FAMILY HISTORY",
        "FAMILY DETAIL",
        "FATHER",
        "MOTHER",
        "BROTHER",
        "SISTER",
        "RELATIVE",
        "RELATION",
    ),
    min_keyword_hits=2,
    locate_system=_FAMILY_LOCATE_SYSTEM,
    locate_user=_FAMILY_LOCATE_USER,
    read_system=_FAMILY_READ_SYSTEM,
    read_user=_FAMILY_READ_USER,
    fallback_first_n_pages=5,
)

REGISTRY: dict[str, Section] = {
    "vitals": _VITALS_SECTION,
    "family_history": _FAMILY_SECTION,
}
