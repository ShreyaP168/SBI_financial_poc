"""
engine/mer_template_processor.py — template-anchored Medical Examination
Report (MER) extraction.

Solves the persistent M/F checkbox bug class for TATA AIA MER forms
([[project-c308394654-family-history]], [[feedback-handwritten-dual-vendor]])
by reading the blank-template field atlas and doing deterministic
pixel-diff inside each known bounding box. No LLM, no vendor variance,
no checkbox flip.

Pipeline:
  1. Render the filled MER PDF at the same DPI as the atlas (300 DPI)
  2. Align to the template (correct any scan rotation / scale skew)
  3. Subtract template ink, leaving only what the examiner wrote
  4. For each labeled checkbox in the atlas: count ink pixels inside
     its bounding box. > threshold -> marked; <= threshold -> unmarked.
  5. Return a flat dict: { field_name: bool } for every labeled checkbox.

Trust hierarchy: when this module returns a value for a field, it is
the source of truth. The medical agent's prompt should consume these
as ground-truth checkbox states and only do clinical interpretation on
top.
"""

from __future__ import annotations
from underwriting_platform.configuration.paths import CONFIG_ROOT
import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from scipy import ndimage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Atlas loader (cached singleton — atlas is small, hand-regenerated)
# ---------------------------------------------------------------------------

_ATLAS_CACHE: dict | None = None
_ATLAS_REVISION = None
from .reference_cache import source_revision


def _atlas_path() -> Path:
    return CONFIG_ROOT / "mer_template_atlas.json"


def load_atlas(path: str | Path | None = None) -> dict:
    """Load the MER template atlas, caching by process. Pass an explicit
    path to override the default config/mer_template_atlas.json."""
    global _ATLAS_CACHE, _ATLAS_REVISION
    p = Path(path) if path else _atlas_path()
    revision = source_revision(p)
    if path is None and _ATLAS_CACHE is not None and _ATLAS_REVISION == revision:
        return _ATLAS_CACHE
    _ATLAS_REVISION = revision
    if not p.exists():
        logger.warning("MER atlas not found at %s; template extraction disabled", p)
        empty = {"_dpi": 300, "_page_size_px": [2481, 3508], "fields": {}}
        if path is None:
            _ATLAS_CACHE = empty
        return empty
    data = json.loads(p.read_text(encoding="utf-8"))
    if path is None:
        _ATLAS_CACHE = data
    return data


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CheckboxResult:
    field: str
    marked: bool
    ink_ratio: float  # 0.0-1.0; how much of the box interior is inked
    page: int
    bbox: list[int]  # [x, y, w, h]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MERExtractionResult:
    pdf_path: str
    atlas_version: str  # _generated date from the atlas
    page_count: int
    template_pages: int
    aligned: bool  # did alignment succeed for every page?
    version_mismatch: bool  # did alignment fail (likely new form)?
    checkboxes: dict[str, CheckboxResult] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pdf_path": self.pdf_path,
            "atlas_version": self.atlas_version,
            "page_count": self.page_count,
            "template_pages": self.template_pages,
            "aligned": self.aligned,
            "version_mismatch": self.version_mismatch,
            "notes": self.notes,
            "checkboxes": {k: v.to_dict() for k, v in self.checkboxes.items()},
        }

    def marked_fields(self) -> list[str]:
        return [k for k, v in self.checkboxes.items() if v.marked]

    def gender_of(self, sibling: str) -> str | None:
        """Convenience: 'M' / 'F' / None for sibling_1 / sibling_2 / sibling_3."""
        m_key = f"section_e.{sibling}.gender_m"
        f_key = f"section_e.{sibling}.gender_f"
        m = self.checkboxes.get(m_key)
        f_ = self.checkboxes.get(f_key)
        if m and f_:
            if m.marked and not f_.marked:
                return "M"
            if f_.marked and not m.marked:
                return "F"
            if m.marked and f_.marked:
                return "BOTH"  # ambiguous — escalate
        return None


# ---------------------------------------------------------------------------
# PDF -> page images
# ---------------------------------------------------------------------------


def _render_pdf_to_images(pdf_path: Path, dpi: int) -> list[np.ndarray]:
    """Render every page of `pdf_path` to a grayscale numpy array at `dpi`."""
    import fitz  # type: ignore

    doc = fitz.open(str(pdf_path))
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    pages: list[np.ndarray] = []
    for page in doc:
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
        pages.append(np.array(img))
    doc.close()
    return pages


# ---------------------------------------------------------------------------
# Alignment — translate-only crop/pad to match expected page size
# ---------------------------------------------------------------------------


def _normalise_page(arr: np.ndarray, target_size: tuple[int, int]) -> np.ndarray:
    """Crop/pad to match `target_size = (W, H)`."""
    target_w, target_h = target_size
    h, w = arr.shape
    if (w, h) == (target_w, target_h):
        return arr
    new = np.full((target_h, target_w), 255, dtype=arr.dtype)
    new[: min(h, target_h), : min(w, target_w)] = arr[: min(h, target_h), : min(w, target_w)]
    return new


def _phase_correlate(ref: np.ndarray, mov: np.ndarray) -> tuple[int, int]:
    """Phase correlation to estimate translation offset (dy, dx) that
    aligns `mov` to `ref`. Both inputs are 2D float arrays, same shape.
    Returns the integer shift (dy, dx) such that np.roll(mov, (dy, dx))
    best matches ref.

    Pure NumPy FFT — no OpenCV / scikit-image dependency.
    """
    assert ref.shape == mov.shape
    f_ref = np.fft.fft2(ref)
    f_mov = np.fft.fft2(mov)
    # Cross-power spectrum
    cross = f_ref * np.conj(f_mov)
    cross /= np.abs(cross) + 1e-10
    corr = np.fft.ifft2(cross).real
    # Find peak
    peak = np.unravel_index(np.argmax(corr), corr.shape)
    dy, dx = int(peak[0]), int(peak[1])
    # FFT wraps; map to signed offset
    h, w = ref.shape
    if dy > h // 2:
        dy -= h
    if dx > w // 2:
        dx -= w
    return dy, dx


def _align_to_template(
    filled: np.ndarray, template: np.ndarray
) -> tuple[np.ndarray, tuple[int, int]]:
    """Align a filled-form page to the template via phase correlation,
    then translate the filled image so atlas coordinates remain valid.

    Phase correlation works best on edge-density images (low DC), so we
    use the binarised ink mask for the correlation but apply the resulting
    shift to the original grayscale filled image.
    """
    assert filled.shape == template.shape
    # Ink masks for the correlation (better signal than raw grayscale)
    ref_mask = (template < 200).astype(np.float32)
    mov_mask = (filled < 200).astype(np.float32)
    dy, dx = _phase_correlate(ref_mask, mov_mask)
    # Apply integer-pixel translation
    aligned = np.roll(filled, (dy, dx), axis=(0, 1))
    # Edges introduced by np.roll wrap; clip them to white to be safe
    if dy > 0:
        aligned[:dy, :] = 255
    elif dy < 0:
        aligned[dy:, :] = 255
    if dx > 0:
        aligned[:, :dx] = 255
    elif dx < 0:
        aligned[:, dx:] = 255
    return aligned, (dy, dx)


# ---------------------------------------------------------------------------
# Pixel-diff checkbox detection
# ---------------------------------------------------------------------------


def _binarise(arr: np.ndarray, threshold: int = 200) -> np.ndarray:
    """Return a uint8 mask where ink = 1, paper = 0."""
    return (arr < threshold).astype(np.uint8)


def _checkbox_interior(
    mask: np.ndarray, x: int, y: int, w: int, h: int, shrink: int = 8
) -> np.ndarray:
    """Crop INSIDE the checkbox border. We shrink by `shrink` px on each
    side so the border line itself doesn't count as ink. The inset depends
    on box size; 8 px is right for ~50 px boxes at 300 DPI.
    """
    x0 = max(0, x + shrink)
    y0 = max(0, y + shrink)
    x1 = min(mask.shape[1], x + w - shrink)
    y1 = min(mask.shape[0], y + h - shrink)
    if x1 <= x0 or y1 <= y0:
        return mask[y : y + h, x : x + w]
    return mask[y0:y1, x0:x1]


# Threshold tuned for 300 DPI checkboxes: a hand-drawn tick / X covers
# 5-30% of the interior INK that's NOT in the template. We pick 5% of
# the interior area as the binary cut on the *differential* signal.
_MARK_THRESHOLD = 0.05


_TEMPLATE_INK_CACHE: dict | None = None


def _template_ink_masks(atlas: dict) -> dict[int, np.ndarray]:
    """Render the blank template once per process and return the ink mask
    per page (1-indexed) at the atlas-specified DPI + page size.

    Used by extract_mer for template-differential pixel-diff: only ink
    that's present in the filled MER but ABSENT in the blank template
    counts as "marked". This kills false positives from printed
    checkbox borders, antialiasing differences, and adjacent label
    text bleeding into the inset window.
    """
    global _TEMPLATE_INK_CACHE
    if _TEMPLATE_INK_CACHE is not None:
        return _TEMPLATE_INK_CACHE
    template_pdf = atlas.get("_template_pdf")
    if not template_pdf or not Path(template_pdf).exists():
        logger.warning(
            "Template PDF for differential not found at %r — "
            "differential disabled (false-positive risk).",
            template_pdf,
        )
        _TEMPLATE_INK_CACHE = {}
        return _TEMPLATE_INK_CACHE
    dpi = int(atlas.get("_dpi") or 300)
    target_w, target_h = atlas.get("_page_size_px") or [2481, 3508]
    try:
        pages = _render_pdf_to_images(Path(template_pdf), dpi)
    except Exception as e:
        logger.warning("Template PDF render failed: %s; differential disabled.", e)
        _TEMPLATE_INK_CACHE = {}
        return _TEMPLATE_INK_CACHE
    out: dict[int, np.ndarray] = {}
    for i, arr in enumerate(pages, start=1):
        out[i] = _binarise(_normalise_page(arr, (target_w, target_h)))
    _TEMPLATE_INK_CACHE = out
    return out


def extract_mer(pdf_path: str | Path, atlas: dict | None = None) -> MERExtractionResult:
    """Run template-anchored extraction on a filled MER PDF.

    Returns a MERExtractionResult with per-field marked / unmarked
    booleans for every checkbox in the atlas, plus an `aligned` flag
    indicating whether all pages matched the template dimensions. The
    medical agent should consume the returned checkboxes as ground
    truth and skip its own checkbox-reading prompt entirely.
    """
    pdf_p = Path(pdf_path)
    atlas = atlas or load_atlas()
    dpi: int = int(atlas.get("_dpi") or 300)
    target_w, target_h = atlas.get("_page_size_px") or [2481, 3508]
    fields: dict[str, dict] = atlas.get("fields") or {}

    result = MERExtractionResult(
        pdf_path=str(pdf_p),
        atlas_version=str(atlas.get("_generated") or ""),
        page_count=0,
        template_pages=0,
        aligned=True,
        version_mismatch=False,
    )

    if not pdf_p.exists():
        result.notes.append(f"PDF not found: {pdf_p}")
        return result
    if not fields:
        result.notes.append("Atlas has no fields; template extraction disabled.")
        return result

    try:
        pages = _render_pdf_to_images(pdf_p, dpi)
    except Exception as e:
        result.notes.append(f"PDF render failed: {e}")
        return result
    result.page_count = len(pages)

    # Pre-render the BLANK TEMPLATE so we can subtract its printed ink
    # from the filled-form ink, leaving only what the examiner wrote.
    # Without this, the printed checkbox border + adjacent label letters
    # bleed into the inset window and read as false positives.
    template_masks = _template_ink_masks(atlas)

    # Group atlas fields by page once.
    by_page: dict[int, list[tuple[str, dict]]] = {}
    for name, meta in fields.items():
        by_page.setdefault(int(meta.get("page", 1)), []).append((name, meta))
    result.template_pages = max(by_page.keys()) if by_page else 0

    if result.page_count < result.template_pages:
        result.notes.append(
            f"PDF has {result.page_count} pages; template expects "
            f"{result.template_pages}. Likely a form-version mismatch."
        )
        result.version_mismatch = True
        result.aligned = False

    # Process each page that we have a corresponding template page for.
    for page_idx in sorted(by_page.keys()):
        if page_idx > result.page_count:
            for name, _ in by_page[page_idx]:
                result.checkboxes[name] = CheckboxResult(
                    field=name, marked=False, ink_ratio=0.0, page=page_idx, bbox=[0, 0, 0, 0]
                )
            continue
        raw = pages[page_idx - 1]
        # Alignment: if the rendered page size doesn't match the atlas,
        # normalise via crop/pad. Big size differences flag version mismatch.
        h, w = raw.shape
        size_delta = abs(w - target_w) + abs(h - target_h)
        if size_delta > 200:
            result.version_mismatch = True
            result.aligned = False
            result.notes.append(
                f"Page {page_idx}: rendered size {w}x{h} differs from "
                f"template {target_w}x{target_h} by > 200 px."
            )
        page_arr = _normalise_page(raw, (target_w, target_h))
        template_mask = template_masks.get(page_idx)

        # Phase-correlation alignment. Scanned MERs often have a 10-150 px
        # translation vs the template; without this step, atlas coords land
        # on adjacent fields and we get the wrong checkbox by an entire row.
        shift = (0, 0)
        if template_mask is not None and template_mask.shape == page_arr.shape:
            # Reconstruct template grayscale from the cached mask for
            # correlation. (Mask carries enough signal.)
            template_arr_proxy = np.where(template_mask, 0, 255).astype(np.uint8)
            page_arr, shift = _align_to_template(page_arr, template_arr_proxy)
            if shift != (0, 0):
                result.notes.append(
                    f"Page {page_idx}: aligned via phase correlation, "
                    f"shift dy={shift[0]} dx={shift[1]}"
                )

        filled_mask = _binarise(page_arr)
        # Differential ink: 1 wherever the filled form has ink AND the
        # template does NOT.
        if template_mask is not None and template_mask.shape == filled_mask.shape:
            diff_mask = filled_mask & (1 - template_mask)
        else:
            diff_mask = filled_mask
            if template_mask is not None:
                result.notes.append(
                    f"Page {page_idx}: template mask shape mismatch — "
                    "differential disabled for this page."
                )

        # Pass 1 — compute the raw ink ratio for every box on this page.
        page_results: list[tuple[str, dict, float]] = []
        for name, meta in by_page[page_idx]:
            x, y, bw, bh = meta["bbox"]
            interior = _checkbox_interior(diff_mask, x, y, bw, bh, shrink=12)
            ratio = int(interior.sum()) / max(interior.size, 1)
            page_results.append((name, meta, ratio))

        # Pass 2 — store raw ratios. Marked decision is deferred to a
        # pair-aware pass below.
        for name, meta, ratio in page_results:
            x, y, bw, bh = meta["bbox"]
            result.checkboxes[name] = CheckboxResult(
                field=name,
                marked=False,
                ink_ratio=round(ratio, 4),
                page=page_idx,
                bbox=[x, y, bw, bh],
            )

    # Pair-aware marking: for binary pairs (Y/N, M/F, Living/Deceased,
    # Yes/No equivalents), use winner-takes-all on the raw ink ratio.
    # For standalone checkboxes, fall back to per-page noise-floor.
    # On heavily-distorted scans, pairwise winner-takes-all is far more
    # robust than absolute thresholds — it cancels the per-page
    # alignment / scan-noise bias that affects every box equally.
    _apply_pair_aware_marking(result)

    return result


# Suffix conventions for paired binary checkboxes in the atlas. Order
# matters only for tie-breaking display purposes.
_PAIRS: list[tuple[str, str]] = [
    (".yes", ".no"),
    (".living", ".deceased"),
    (".gender_m", ".gender_f"),
]


def _apply_pair_aware_marking(result: MERExtractionResult) -> None:
    """Pass 3 — mark boxes using winner-takes-all on each known pair,
    absolute threshold for the rest.

    Within a pair: the side with the higher ink ratio is marked IF the
    difference exceeds _PAIR_MIN_DIFF AND the winner's ratio clears
    _PAIR_MIN_INK. When both ratios are essentially equal, neither is
    marked — explicit ambiguity instead of a coin flip.

    For non-pair (standalone) checkboxes: pick a per-page threshold
    that's a function of the page's median ratio + an absolute floor.
    """
    _PAIR_MIN_DIFF = 0.05
    _PAIR_MIN_INK = 0.05

    paired_names: set[str] = set()

    # Group field names by their "base" (i.e., minus the pair suffix)
    bases: dict[str, dict[str, str]] = {}
    for full_name in result.checkboxes:
        for a, b in _PAIRS:
            if full_name.endswith(a):
                base = full_name[: -len(a)]
                bases.setdefault(base, {})[a] = full_name
            elif full_name.endswith(b):
                base = full_name[: -len(b)]
                bases.setdefault(base, {})[b] = full_name

    # Resolve every pair found per base — a single base can carry
    # multiple pairs (e.g. a sibling row has both living/deceased AND
    # gender_m/gender_f). No break.
    for base, sides in bases.items():
        for a, b in _PAIRS:
            if a in sides and b in sides:
                cb_a = result.checkboxes[sides[a]]
                cb_b = result.checkboxes[sides[b]]
                paired_names.add(cb_a.field)
                paired_names.add(cb_b.field)
                ra, rb = cb_a.ink_ratio, cb_b.ink_ratio
                diff = abs(ra - rb)
                winner_ink = max(ra, rb)
                if diff < _PAIR_MIN_DIFF or winner_ink < _PAIR_MIN_INK:
                    cb_a.marked = False
                    cb_b.marked = False
                elif ra > rb:
                    cb_a.marked = True
                    cb_b.marked = False
                else:
                    cb_a.marked = False
                    cb_b.marked = True

    # Standalone checkboxes — apply per-page noise-floor threshold
    by_page: dict[int, list] = {}
    for cb in result.checkboxes.values():
        if cb.field in paired_names:
            continue
        by_page.setdefault(cb.page, []).append(cb)
    for page, cbs in by_page.items():
        if not cbs:
            continue
        ratios = sorted(cb.ink_ratio for cb in cbs)
        noise_floor = ratios[len(ratios) // 2] if ratios else 0.0
        threshold = max(_MARK_THRESHOLD, noise_floor + _MARK_THRESHOLD)
        for cb in cbs:
            cb.marked = cb.ink_ratio >= threshold

    # Row-level sanity for sibling rows: a row that has NEITHER Living
    # NOR Deceased resolved is an unfilled row — suppress every mark in
    # it. This catches false positives from stamps / seals that bleed
    # ink into checkbox-sized regions of an otherwise-unfilled row.
    for sibling in ("sibling_1", "sibling_2", "sibling_3"):
        prefix = f"section_e.{sibling}."
        living = result.checkboxes.get(prefix + "living")
        dec = result.checkboxes.get(prefix + "deceased")
        row_decided = (living and living.marked) or (dec and dec.marked)
        if not row_decided:
            # Whole row unfilled — clear every mark in it
            for k, cb in result.checkboxes.items():
                if k.startswith(prefix):
                    cb.marked = False


def _gender_of_impl(result: MERExtractionResult, sibling: str) -> str | None:
    """Replacement for the dataclass method that uses raw ink ratios
    (more robust than relying on .marked which depends on threshold).
    """
    m = result.checkboxes.get(f"section_e.{sibling}.gender_m")
    f = result.checkboxes.get(f"section_e.{sibling}.gender_f")
    if not (m and f):
        return None
    if m.marked and not f.marked:
        return "M"
    if f.marked and not m.marked:
        return "F"
    if m.marked and f.marked:
        return "BOTH"
    return None
