"""
engine/family_checkbox.py — per-cell M/F checkbox isolation for the
family-history table on the standard TALIC MER form.

The standard TALIC MER encodes sibling rows with a generic "Sibling"
label PLUS adjacent M-checkbox and F-checkbox cells in the relation
column. When both checkboxes are visible to a vision model in the same
crop, both Claude and Gemini consistently flip M↔F (verified on UAT
case C308394654 — see memory `feedback_handwritten_dual_vendor.md`).

Fix: isolate each checkbox into its own tiny crop and ask the LLM
ONE checkbox at a time whether it is filled. With no neighbour visible,
the M↔F flip cannot occur.

Pipeline (called after `handwritten_reocr.reocr_section_for_case`
returns a family_history result):

  1. Re-render the located page at high DPI and re-crop the table bbox.
  2. Detect horizontal row separators via per-row dark-pixel density.
  3. For each member whose `relation_label_on_form == "Sibling"`,
     map its `row_position` to the matching detected row strip.
  4. Crop the relation cell (leftmost RELATION_FRAC of the row).
  5. Crop the bottom half of that cell (where M and F checkboxes live).
  6. Split that into LEFT half (M-cell) and RIGHT half (F-cell).
  7. Per cell: send the small PNG ALONE to Gemini with a strict
     "is the checkbox filled? JSON only" prompt.
  8. Resolve relationship:
       - M filled & F empty   → "brother"
       - F filled & M empty   → "sister"
       - both filled          → "ambiguous_both"
       - neither              → "ambiguous_neither"

The original (full-pass) member dict is preserved in
`_pre_isolation_relationship` so the audit trail shows what changed.
"""

from __future__ import annotations
import asyncio
import base64
import io
import logging
import os
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image

logger = logging.getLogger(__name__)


# Layout constants for the standard TALIC MER family-history table.
# Measured on the C308394654 scan @ 600 DPI; expressed as fractions of
# the table-crop width / row-strip height so they stay scan-resolution
# agnostic.
RELATION_CELL_FRAC = 0.13  # leftmost 13% of table width = relation cell
ROW_LINE_DENSITY = 0.45  # row of crop is a horizontal line if ≥45% black
ROW_LINE_MIN_GAP_PX = 8  # cluster line-pixels within this gap
SIBLING_BOX_TOP_FRAC = 0.50  # bottom half of relation cell
SIBLING_BOX_PAD_PX = 4  # safety pad around each isolated checkbox


# Default model — same as the rest of the re-OCR pipeline.
_DEFAULT_GEMINI_MODEL = "gemini-2.5-pro"

_CHECKBOX_PROMPT = """\
You are looking at a tightly-cropped image of EXACTLY ONE small square
checkbox printed on a paper form. Your job: decide whether that single
checkbox has been MARKED (any visible tick, cross, slash, scribble,
filled-in shading, or pen stroke crossing into the box).

Rules:
  - Ignore any printed text label NEXT to the box. The label is just
    context — it is NOT a mark.
  - Ignore form-printed border lines of the box itself. Only count
    HAND-DRAWN ink inside or crossing into the box.
  - A box that contains ONLY the printed empty square is NOT marked.
  - A box with ANY hand-drawn ink (tick, cross, slash, scribble, dot,
    smear) IS marked.

Return JSON ONLY in this exact schema:

{
  "filled":     true | false,
  "confidence": 0.0 - 1.0,
  "reasoning":  "<one short sentence — what you saw inside the box>"
}

First char {, last char }. No prose, no markdown fences.
"""

_CHECKBOX_SYSTEM = (
    "You are a vision-OCR assistant inspecting a single isolated checkbox "
    "from a paper form. Return JSON only — no prose, no markdown."
)


# ── Helpers ────────────────────────────────────────────────────────────


def _load_table_crop(
    located: dict, case_dir: Optional[Path] = None, dpi: int = 600
) -> Optional["Image.Image"]:
    """Re-render the located PDF page and re-crop to bbox_pct.

    `located` is a `family_history_reocr` block returned by
    `handwritten_reocr.reocr_section_for_case` — it has `source_pdf`
    (filename only) + `source_page` (1-based) + `bbox_pct`. The PDF
    filename is resolved against `case_dir`.
    """
    try:
        import fitz
        from PIL import Image
    except ImportError as e:
        logger.warning("[family_cb] missing dep for crop: %s", e)
        return None
    pdf_path = located.get("pdf_path")
    if pdf_path is None and located.get("source_pdf") and case_dir:
        pdf_path = Path(case_dir) / located["source_pdf"]
    page_idx = located.get("page_idx")
    if page_idx is None and located.get("source_page"):
        page_idx = int(located["source_page"]) - 1
    bbox = located.get("bbox_pct")
    if not pdf_path or page_idx is None or not bbox or len(bbox) != 4:
        return None
    if not Path(pdf_path).exists():
        logger.warning("[family_cb] source PDF not found: %s", pdf_path)
        return None
    try:
        doc = fitz.open(str(pdf_path))
        pix = doc[page_idx].get_pixmap(dpi=dpi)
        png = pix.tobytes("png")
        doc.close()
        img = Image.open(io.BytesIO(png))
        w, h = img.size
        x1, y1, x2, y2 = [max(0.0, min(100.0, float(v))) for v in bbox]
        if x2 <= x1 or y2 <= y1:
            return None
        return img.crop(
            (int(w * x1 / 100), int(h * y1 / 100), int(w * x2 / 100), int(h * y2 / 100))
        )
    except Exception as e:
        logger.warning("[family_cb] crop failed: %s", e)
        return None


def _detect_row_strips(crop) -> list[tuple[int, int]]:
    """Return list of (y_top, y_bottom) for each detected row strip in
    the table crop. Uses per-row dark-pixel density.
    """
    try:
        import numpy as np
    except ImportError:
        return []
    arr = np.array(crop.convert("L"))
    h, w = arr.shape
    bw = (arr < 200).astype(np.uint8)
    row_density = bw.sum(axis=1) / max(w, 1)
    line_ys = np.where(row_density > ROW_LINE_DENSITY)[0]
    if len(line_ys) == 0:
        return []
    clusters = [[int(line_ys[0])]]
    for y in line_ys[1:]:
        if int(y) - clusters[-1][-1] < ROW_LINE_MIN_GAP_PX:
            clusters[-1].append(int(y))
        else:
            clusters.append([int(y)])
    centers = [int(np.mean(c)) for c in clusters]
    # Pair consecutive line centres into strips. Drop the topmost strip
    # (header) — the LOCATE prompt instructs the model to include the
    # header row, so detected strip index 0 is the column-title row.
    strips: list[tuple[int, int]] = []
    for i in range(len(centers) - 1):
        strips.append((centers[i], centers[i + 1]))
    return strips


def _crop_checkbox_cells(
    table_crop, row_top: int, row_bottom: int
) -> tuple[Optional[bytes], Optional[bytes]]:
    """For a single sibling row strip, return (m_png, f_png) — two tiny
    PNGs each containing exactly ONE checkbox cell.

    Layout assumption (verified on the standard TALIC MER):
      - Relation cell = leftmost RELATION_CELL_FRAC of the table width.
      - Inside that cell, the top half holds the printed "Sibling" label;
        the bottom half holds two checkbox+letter pairs side-by-side:
        [box]M [box]F.
      - Splitting the bottom half left/right cleanly isolates the M cell
        from the F cell.
    """
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        return None, None
    w, h = table_crop.size
    rel_x2 = int(w * RELATION_CELL_FRAC)
    box_y1 = row_top + int((row_bottom - row_top) * SIBLING_BOX_TOP_FRAC)
    box_y2 = row_bottom
    pad = SIBLING_BOX_PAD_PX
    m_box = (max(0, 0 - pad), max(0, box_y1 - pad), min(rel_x2 // 2 + pad, w), min(box_y2 + pad, h))
    f_box = (
        max(0, rel_x2 // 2 - pad),
        max(0, box_y1 - pad),
        min(rel_x2 + pad, w),
        min(box_y2 + pad, h),
    )
    m_crop = table_crop.crop(m_box)
    f_crop = table_crop.crop(f_box)
    out_m, out_f = io.BytesIO(), io.BytesIO()
    m_crop.save(out_m, format="PNG", optimize=True)
    f_crop.save(out_f, format="PNG", optimize=True)
    return out_m.getvalue(), out_f.getvalue()


# ── Gemini per-checkbox call ──────────────────────────────────────────


async def _gemini_is_filled(png_bytes: bytes, gemini_model: str) -> dict:
    """Ask Gemini whether the lone checkbox in this PNG is filled."""
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return {
            "filled": None,
            "confidence": None,
            "reasoning": "GEMINI_API_KEY not set",
            "model": gemini_model,
            "error": "no_api_key",
        }
    raw: Optional[str] = None
    err: Optional[str] = None
    full_prompt = _CHECKBOX_SYSTEM + "\n\n" + _CHECKBOX_PROMPT

    # Path 1 — google.genai (new SDK)
    try:
        from google import genai
        from google.genai import types as genai_types

        def _call_new() -> str:
            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(
                model=gemini_model,
                contents=[
                    genai_types.Part.from_bytes(data=png_bytes, mime_type="image/png"),
                    full_prompt,
                ],
            )
            return resp.text or ""

        raw = await asyncio.to_thread(_call_new)
    except ImportError:
        err = "google-genai not installed"
    except Exception as e:
        err = f"new SDK failed: {e}"

    # Path 2 — google.generativeai (legacy)
    if raw is None:
        try:
            import google.generativeai as legacy
            from PIL import Image

            def _call_legacy() -> str:
                legacy.configure(api_key=api_key)
                m = legacy.GenerativeModel(gemini_model)
                img = Image.open(io.BytesIO(png_bytes))
                resp = m.generate_content([full_prompt, img])
                return getattr(resp, "text", "") or ""

            raw = await asyncio.to_thread(_call_legacy)
            err = None
        except Exception as e:
            err = f"{err or ''} | legacy SDK failed: {e}"

    if not raw:
        return {
            "filled": None,
            "confidence": None,
            "reasoning": f"Gemini call failed: {err}",
            "model": gemini_model,
            "error": err or "no_response",
        }

    import json

    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3].rstrip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip("\n")
    try:
        data = json.loads(text)
    except Exception:
        i, j = text.find("{"), text.rfind("}")
        try:
            data = json.loads(text[i : j + 1]) if (i >= 0 and j > i) else {}
        except Exception:
            data = {}
    return {
        "filled": bool(data.get("filled")) if data.get("filled") is not None else None,
        "confidence": float(data.get("confidence") or 0.0),
        "reasoning": str(data.get("reasoning") or "").strip(),
        "model": gemini_model,
        "error": None,
    }


# ── Public entry point ────────────────────────────────────────────────


def _is_sibling_row(member: dict) -> bool:
    """True iff this member's relation cell printed the literal text
    'Sibling' — the only case where M/F isolation applies.
    """
    label = (member.get("relation_label_on_form") or "").strip().lower()
    if label.startswith("sibling"):
        return True
    # Fallback for older prompt versions / when the label field is
    # missing — treat brother/sister/sibling labels conservatively.
    rel = (member.get("relationship") or "").strip().lower()
    return rel in ("sibling", "brother", "sister")


async def refine_family_history_with_checkbox_isolation(
    family_result: dict,
    case_dir: Optional[Path] = None,
    debug_dir: Optional[Path] = None,
) -> dict:
    """Refine the family_history_reocr result by overriding sibling-row
    relationship labels with per-cell M/F isolation.

    Mutates `family_result["values"]["members"]` in place. Adds an audit
    trail under `family_result["values"]["checkbox_isolation"]`. Returns
    the mutated `family_result`.

    Safe to call when:
      - GEMINI_API_KEY is unset (returns unchanged with skipped marker)
      - the family_result contains no sibling rows
      - the located bbox cannot be re-rendered
    """
    if not isinstance(family_result, dict):
        return family_result
    values = family_result.get("values") or {}
    members = values.get("members") or []
    if not members:
        return family_result

    sibling_idx = [i for i, m in enumerate(members) if isinstance(m, dict) and _is_sibling_row(m)]
    if not sibling_idx:
        return family_result

    table_crop = _load_table_crop(family_result, case_dir=case_dir)
    if table_crop is None:
        logger.info("[family_cb] could not re-render table crop; skipping")
        return family_result

    strips = _detect_row_strips(table_crop)
    if not strips:
        logger.info("[family_cb] no row strips detected; skipping")
        return family_result

    # Map each sibling member's row_position → physical strip.
    # Convention: the LOCATE prompt instructs the model to include the
    # column-header row. So strips[0] is the header, strips[1:] are data
    # rows in top-to-bottom order. row_position is the 0-based index
    # over data rows (per the read prompt).
    data_strips = strips[1:]
    audit: list[dict] = []
    gemini_model = (
        os.environ.get("GEMINI_FAMILY_CHECKBOX_MODEL")
        or os.environ.get("GEMINI_VITAL_REOCR_MODEL")
        or _DEFAULT_GEMINI_MODEL
    )

    debug_dir_p = Path(debug_dir) if debug_dir else None
    if debug_dir_p:
        try:
            debug_dir_p.mkdir(parents=True, exist_ok=True)
        except Exception:
            debug_dir_p = None

    # Build the list of (member_idx, m_png, f_png) up front so we can
    # fan out the Gemini calls in parallel.
    jobs: list[tuple[int, bytes, bytes]] = []
    for member_idx in sibling_idx:
        member = members[member_idx]
        try:
            row_pos = int(member.get("row_position"))
        except (TypeError, ValueError):
            row_pos = None
        if row_pos is None or not (0 <= row_pos < len(data_strips)):
            audit.append(
                {
                    "member_idx": member_idx,
                    "row_position": row_pos,
                    "skipped": f"row_position out of range (0..{len(data_strips) - 1})",
                }
            )
            continue
        y1, y2 = data_strips[row_pos]
        m_png, f_png = _crop_checkbox_cells(table_crop, y1, y2)
        if not m_png or not f_png:
            audit.append(
                {
                    "member_idx": member_idx,
                    "row_position": row_pos,
                    "skipped": "checkbox crop failed",
                }
            )
            continue
        if debug_dir_p:
            try:
                (debug_dir_p / f"sibling_row{row_pos}_M.png").write_bytes(m_png)
                (debug_dir_p / f"sibling_row{row_pos}_F.png").write_bytes(f_png)
            except Exception:
                pass
        jobs.append((member_idx, m_png, f_png))

    if not jobs:
        if audit:
            values["checkbox_isolation"] = {"audit": audit, "calls": 0}
        return family_result

    # Fan out: 2 Gemini calls per job (M, F). Run all in parallel.
    coros: list = []
    coro_meta: list[tuple[int, str]] = []
    for member_idx, m_png, f_png in jobs:
        coros.append(_gemini_is_filled(m_png, gemini_model))
        coro_meta.append((member_idx, "M"))
        coros.append(_gemini_is_filled(f_png, gemini_model))
        coro_meta.append((member_idx, "F"))
    results = await asyncio.gather(*coros, return_exceptions=True)

    # Collect M and F results per member.
    per_member: dict[int, dict] = {}
    for (m_idx, kind), res in zip(coro_meta, results):
        if isinstance(res, Exception):
            res = {
                "filled": None,
                "confidence": None,
                "reasoning": f"exception: {res}",
                "model": gemini_model,
                "error": "exception",
            }
        per_member.setdefault(m_idx, {})[kind] = res

    # Decide and overwrite.
    for member_idx, reads in per_member.items():
        m = reads.get("M") or {}
        f = reads.get("F") or {}
        m_filled = m.get("filled")
        f_filled = f.get("filled")
        prev_rel = members[member_idx].get("relationship")
        if m_filled is True and f_filled is False:
            new_rel = "brother"
        elif f_filled is True and m_filled is False:
            new_rel = "sister"
        elif m_filled is True and f_filled is True:
            new_rel = "ambiguous_both"
        elif m_filled is False and f_filled is False:
            new_rel = "ambiguous_neither"
        else:
            new_rel = "ambiguous_unread"
        members[member_idx]["_pre_isolation_relationship"] = prev_rel
        members[member_idx]["relationship"] = new_rel
        audit.append(
            {
                "member_idx": member_idx,
                "row_position": members[member_idx].get("row_position"),
                "m_read": m,
                "f_read": f,
                "decided": new_rel,
                "previous": prev_rel,
            }
        )
        logger.info(
            "[family_cb] row=%s prev=%s → %s (M filled=%s conf=%s, F filled=%s conf=%s)",
            members[member_idx].get("row_position"),
            prev_rel,
            new_rel,
            m_filled,
            m.get("confidence"),
            f_filled,
            f.get("confidence"),
        )

    values["checkbox_isolation"] = {
        "audit": audit,
        "calls": len(coros),
        "model": gemini_model,
        "function": "underwriting_platform.engine.family_checkbox.refine_family_history_with_checkbox_isolation",
    }
    return family_result
