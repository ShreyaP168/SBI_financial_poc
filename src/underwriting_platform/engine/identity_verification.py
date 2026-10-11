"""Local OCR/content classification locates identity-document candidates.

Classification never verifies KYC or upgrades compliance status. Google Gemini
is used separately for photo matching; optional geocoding is unchanged. Unknown
local labels retain filename discovery as a candidate-selection aid, not proof.
"""

from __future__ import annotations
import asyncio
import json
import logging
import zipfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── File-name heuristics ──────────────────────────────────────────

_KYC_KEYWORDS = (
    "aadhaar",
    "aadhar",
    "uidai",
    "pan card",
    "pancard",
    "pan_card",
    "panproof",
    "passport",
    "voter",
    "voterid",
    "driving licence",
    "driving license",
    "dl_card",
    "dl card",
    "kyc",
)
# Things that scream "live photo / TPA identification" — file-naming
# patterns observed across this project's case-data folders. Order
# doesn't matter; we lowercase and substring-match.
_LIVE_PHOTO_KEYWORDS = (
    "live photo",
    "live_photo",
    "live-photo",
    "tpa photo",
    "tpa-photo",
    "photo identification",
    "person_photo",
    "person photo",
    "geotag",
    "geo_tag",
    "geo tag",
    "insta piwc",
    "insta_piwc",
    "instaphoto",
    "selfie",
    "applicant photo",
    "patient photo",
    # Insta-PIWC capture files are commonly named
    # "<case>_PIVCPHOTO_1_<timestamp>.jpeg" — no space or underscore
    # between "PIVC" and "PHOTO", so they matched none of the keywords
    # above and live_photo_at_medicals wrongly reported "No" despite the
    # capture being on file.
    "pivcphoto",
    "pivc",
)
# The proposal/CAF photo — use THIS as the base image for face-matching,
# compared separately against the KYC photo, the Insta-PIWC capture, and
# any medical-document photo. This batch's filename convention is
# "<case>_RECENTPHOTOGRAPH_<n>.<ext>".
_BASE_PHOTO_KEYWORDS = (
    "recentphotograph",
    "recent photograph",
    "recent_photograph",
    "proposal photo",
    "proposal_photo",
    "applicant photo",
)
# A photo embedded in/alongside a medical document (MER etc.) — distinct
# from the KYC/PIWC/base photos above. Resolves to "skipped" for any case
# with no medical documents on file (the normal case for this SBI
# financial-underwriting product); built generically for cases that do
# carry one.
_MEDICAL_PHOTO_KEYWORDS = (
    "mer photo",
    "mer_photo",
    "medical examination report photo",
    "vmer photo",
    "vmer_photo",
)
_KYC_EXTS = {".jpg", ".jpeg", ".png", ".pdf", ".webp", ".tiff", ".tif"}
_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".pdf"}


def _scan_case_dir(case_dir: Path) -> list[Path]:
    """All files (loose + extracted from .zip) we should consider."""
    out: list[Path] = []
    if not case_dir.exists():
        return out
    for p in case_dir.iterdir():
        if p.is_file() and p.suffix.lower() != ".zip":
            out.append(p)
    # Light ZIP scan — we won't extract here; the master orchestrator
    # already extracts ZIPs into a tempdir for agent input. For
    # photo-match we re-extract on demand if needed.
    for z in case_dir.glob("*.zip"):
        try:
            with zipfile.ZipFile(z, "r") as zf:
                for member in zf.namelist():
                    suf = Path(member).suffix.lower()
                    if suf in _KYC_EXTS or suf in _PHOTO_EXTS:
                        # Express as a virtual "zip:<archive>!<member>" path
                        # using a sentinel object the loader knows about.
                        out.append(_ZipMemberRef(z, member))
        except (zipfile.BadZipFile, OSError):
            continue
    return out


class _ZipMemberRef:
    """Lightweight virtual path for a member inside a zip archive."""

    __slots__ = ("zip_path", "member")

    def __init__(self, zip_path: Path, member: str):
        self.zip_path = zip_path
        self.member = member

    @property
    def name(self) -> str:
        return Path(self.member).name

    @property
    def suffix(self) -> str:
        return Path(self.member).suffix

    def read_bytes(self) -> bytes:
        with zipfile.ZipFile(self.zip_path, "r") as zf:
            return zf.read(self.member)

    def __str__(self) -> str:
        return f"{self.zip_path.name}!{self.member}"


def _matches(name: str, keywords: tuple[str, ...]) -> bool:
    n = name.lower()
    return any(kw in n for kw in keywords)


def _filter(candidates: list, keywords: tuple[str, ...], allowed_exts: set[str]) -> list:
    out = []
    for c in candidates:
        suf = (c.suffix if hasattr(c, "suffix") else Path(str(c)).suffix).lower()
        if suf not in allowed_exts:
            continue
        nm = c.name if hasattr(c, "name") else Path(str(c)).name
        if _matches(nm, keywords):
            out.append(c)
    return out


# ── Image bytes loader ────────────────────────────────────────────


def _load_face_image_bytes(ref) -> Optional[bytes]:
    """Return raw bytes for a face image — converts PDFs to PNG."""
    suf = (ref.suffix if hasattr(ref, "suffix") else Path(str(ref)).suffix).lower()
    try:
        if isinstance(ref, _ZipMemberRef):
            data = ref.read_bytes()
            if suf == ".pdf":
                from underwriting_platform.engine.photo_match import render_first_image_from_pdf

                # Write to tempfile so PyMuPDF can open it
                import tempfile

                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tf:
                    tf.write(data)
                    tmp_path = Path(tf.name)
                try:
                    return render_first_image_from_pdf(tmp_path)
                finally:
                    try:
                        tmp_path.unlink()
                    except Exception:
                        pass
            return data
        # Loose file
        path = ref if isinstance(ref, Path) else Path(str(ref))
        if suf == ".pdf":
            from underwriting_platform.engine.photo_match import render_first_image_from_pdf

            return render_first_image_from_pdf(path)
        return path.read_bytes()
    except Exception as e:
        logger.warning("[id_verify] could not load image %s: %s", ref, e)
        return None


# ── Content-based document classification ──────────────────────────
# (2026-10-09, client requirement — see module docstring item 0)

_CLASSIFY_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif"}
_CLASSIFY_TEXT_DENSITY_MAX = 150  # chars; a PDF page below this is treated
# as "likely a scanned photo/ID, not text"


def _gather_classification_candidates(case_dir: Path) -> list[tuple[str, bytes]]:
    """Every loose image file + every low-text-density PDF page in the
    case folder, as (id, jpeg_bytes). A PDF page's id is
    '<filename>#p<N>' (1-indexed) so a match can be traced back to its
    source document and page."""
    candidates: list[tuple[str, bytes]] = []
    if not case_dir.exists():
        return candidates
    for p in sorted(case_dir.iterdir()):
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        if suf in _CLASSIFY_IMG_EXTS:
            try:
                candidates.append((p.name, p.read_bytes()))
            except Exception as e:
                logger.warning("[doc_classify] could not read %s: %s", p.name, e)
        elif suf == ".pdf":
            try:
                import pdfplumber
                import fitz

                with pdfplumber.open(str(p)) as pdf:
                    from underwriting_platform.ocr.classification import classify_text

                    low_text_pages = [
                        i
                        for i, page in enumerate(pdf.pages)
                        if len((page.extract_text() or "").strip()) < _CLASSIFY_TEXT_DENSITY_MAX
                        or classify_text(page.extract_text() or "")["kind"] == "id_proof"
                    ]
                if low_text_pages:
                    doc = fitz.open(str(p))
                    for i in low_text_pages:
                        try:
                            pix = doc[i].get_pixmap(dpi=150)
                            candidates.append((f"{p.name}#p{i + 1}", pix.tobytes("jpeg")))
                        except Exception as e:
                            logger.warning(
                                "[doc_classify] could not render %s p%d: %s", p.name, i + 1, e
                            )
                    doc.close()
            except Exception as e:
                # pdfplumber is the stricter of the two parsers used in this
                # codebase — _load_face_image_bytes already uses fitz/
                # PyMuPDF for ".pdf" files specifically because it tolerates
                # malformed structure pdfplumber rejects outright. Mirror
                # that tolerance here instead of just giving up, and as a
                # last resort check whether the ".pdf" is actually a raw
                # image with the wrong extension (a real file in this
                # project's data has bytes starting with the JPEG magic
                # number \xff\xd8\xff, not %PDF-) — a file like that fails
                # BOTH parsers as a "PDF" since it isn't one, but its raw
                # bytes are a perfectly good JPEG.
                logger.warning(
                    "[doc_classify] pdfplumber could not scan %s (%s) — trying fitz", p.name, e
                )
                try:
                    import fitz

                    doc = fitz.open(str(p))
                    if len(doc) > 0:
                        pix = doc[0].get_pixmap(dpi=150)
                        candidates.append((f"{p.name}#p1", pix.tobytes("jpeg")))
                    doc.close()
                except Exception as e2:
                    try:
                        raw = p.read_bytes()
                        if raw[:3] == b"\xff\xd8\xff" or raw[:8] == b"\x89PNG\r\n\x1a\n":
                            candidates.append((p.name, raw))
                            logger.info(
                                "[doc_classify] %s is actually a raw image under a .pdf "
                                "extension — added directly",
                                p.name,
                            )
                        else:
                            logger.warning(
                                "[doc_classify] could not scan %s via fitz either: %s", p.name, e2
                            )
                    except Exception as e3:
                        logger.warning("[doc_classify] could not read %s at all: %s", p.name, e3)
    return candidates


async def classify_case_documents(
    case_dir: Path, classification=None
) -> dict[str, tuple[str, bytes]]:
    """Local OCR/content rules only. Labels locate candidates, never verify KYC."""
    import io
    from PIL import Image
    from underwriting_platform.ocr import read_page
    from underwriting_platform.ocr.classification import classify_text

    prepared = {}
    for row in (classification or {}).get("files", []):
        for page in row.get("pages", []):
            key = (
                f"{row['filename']}#p{page['page']}"
                if row["filename"].lower().endswith(".pdf")
                else row["filename"]
            )
            prepared[key] = page["label"]
    result = {}
    for identifier, content in await asyncio.to_thread(_gather_classification_candidates, case_dir):
        if identifier in prepared:
            label = prepared[identifier]
        else:
            with Image.open(io.BytesIO(content)) as image:
                evidence = await asyncio.to_thread(read_page, image.convert("RGB"), 1)
            if evidence.get("status") == "failed":
                raise RuntimeError("LOCAL_CLASSIFICATION_OCR_UNAVAILABLE")
            text = "\n".join(
                line["text"]
                for line in evidence.get("lines", [])
                if (
                    line.get("mean_confidence")
                    if line.get("mean_confidence") is not None
                    else line.get("confidence", 0)
                )
                >= 0.65
            )
            label = classify_text(text)["label"]
        result[identifier] = (label, content)
    return result


# Priority order matches SBI's own accepted-OVD list (Aadhaar / Passport /
# Driving Licence) from the real requirements-letter correspondence seen
# across these cases, PAN last (PAN is mandatory but not itself a
# standalone OVD for address proof).
_KYC_LABEL_PRIORITY = ("AADHAAR_FRONT", "PASSPORT", "DRIVING_LICENSE", "PAN_CARD", "AADHAAR_BACK")


async def run_identity_verification(case_json: dict, case_dir: Path) -> dict:
    """Mutates `case_json` in place and returns it. Safe to call when
    the identity agent didn't run — every step degrades gracefully.
    """
    case_dir = Path(case_dir)
    applicant = case_json.setdefault("applicant", {})
    afm = case_json.setdefault("anti_fraud_markers", {})

    # ── 0. Content-based document classification ───────────────────
    # Runs once, shared by the KYC backfill below and by steps 2-4's
    # fallback file discovery — not a separate Gemini call per step.
    doc_labels: dict[str, tuple[str, bytes]] = {}
    try:
        classification = case_json.get("_document_classification")
        doc_labels = (
            await classify_case_documents(case_dir, classification)
            if classification
            else await classify_case_documents(case_dir)
        )
        # Classification identifies candidate documents, not verified KYC facts.
        case_json["_identity_classifier"] = {
            "provider": "local_ocr_rules",
            "provider_tokens": 0,
            "labels": {key: value[0] for key, value in doc_labels.items()},
            "requires_review": any(value[0] == "OTHER" for value in doc_labels.values())
            or any(
                page.get("requires_review")
                for row in (classification or {}).get("files", [])
                for page in row.get("pages", [])
            ),
        }
    except Exception as e:
        logger.warning("[id_verify] content document classification failed: %s", e)
        case_json.setdefault("processing_errors", []).append(
            {"stage": "identity_classification", "type": type(e).__name__}
        )

    # The diagnostic-centre sits on the MER header, read by the MEDICAL agent
    # (the identity agent, which owns anti_fraud_markers, frequently doesn't
    # receive the MER → DC fields stayed null → distance Unknown on ~36% of
    # cases). Backfill afm from the medical block before geocoding.
    _med = case_json.get("medical") or {}
    for _k in ("diagnostic_centre_name", "diagnostic_centre_city"):
        if not afm.get(_k) and _med.get(_k):
            afm[_k] = _med.get(_k)

    # ── 1. DC distance via geocoding ──────────────────────────────
    try:
        await _resolve_dc_distance(applicant, afm)
    except Exception as e:
        logger.warning("[id_verify] DC distance step failed: %s", e)

    # ── 2 + 3. Live photo presence + face match ──────────────────
    try:
        await _resolve_photo_match(case_dir, applicant, doc_labels=doc_labels)
    except Exception as e:
        logger.warning("[id_verify] photo match step failed: %s", e)
        case_json.setdefault("processing_errors", []).append(
            {"stage": "identity_photo_match", "type": type(e).__name__}
        )

    # ── 4. Base-photo face-match set (KYC / PIWC / medical) ────────
    # Proposal photo as the base image, matched separately against KYC,
    # Insta-PIWC, and medical-document photos. Additive — does not
    # replace applicant.photo_match
    # (the existing KYC-vs-live-photo comparison engine.flags already
    # keys a flag off); writes the new result to applicant.photo_match_set.
    try:
        await _resolve_photo_match_set(case_dir, applicant, doc_labels=doc_labels)
    except Exception as e:
        logger.warning("[id_verify] base photo-match-set step failed: %s", e)
        case_json.setdefault("processing_errors", []).append(
            {"stage": "identity_photo_match_set", "type": type(e).__name__}
        )

    results = [applicant.get("photo_match") or {}] + list(
        (applicant.get("photo_match_set") or {}).values()
    )
    if any(isinstance(result, dict) and result.get("error") for result in results):
        case_json.setdefault("processing_errors", []).append(
            {"stage": "identity_photo_provider", "type": "ProviderFailure"}
        )

    return case_json


async def _resolve_dc_distance(applicant: dict, afm: dict) -> None:
    """Populate afm.dc_distance_km + dc_distance_band. Only writes if
    we successfully geocoded both endpoints — otherwise leaves the
    band as 'Unknown' and the km as None so downstream knows it
    couldn't be determined.
    """
    from underwriting_platform.engine.geocoding import geocode_address
    from underwriting_platform.engine.geo_utils import haversine_km, distance_band_km

    applicant_addr = applicant.get("address") or afm.get("applicant_address")
    applicant_pin = applicant.get("pin_code")
    dc_addr_parts = []
    dc_name = afm.get("diagnostic_centre_name")
    dc_city = afm.get("diagnostic_centre_city")
    if dc_name:
        dc_addr_parts.append(str(dc_name))
    if dc_city:
        dc_addr_parts.append(str(dc_city))
    dc_addr = ", ".join(dc_addr_parts) if dc_addr_parts else None

    if not applicant_addr and not applicant_pin:
        logger.info("[id_verify] no applicant address — DC distance unresolved")
        afm.setdefault("dc_distance_km", None)
        afm.setdefault("dc_distance_band", "Unknown")
        return
    if not dc_addr:
        logger.info("[id_verify] no diagnostic-centre address — DC distance unresolved")
        afm.setdefault("dc_distance_km", None)
        afm.setdefault("dc_distance_band", "Unknown")
        return

    applicant_coords = geocode_address(applicant_addr or "", pincode=applicant_pin)
    dc_coords = geocode_address(dc_addr)

    if not applicant_coords or not dc_coords:
        logger.info(
            "[id_verify] geocoding miss — applicant=%s dc=%s",
            applicant_coords is not None,
            dc_coords is not None,
        )
        afm["dc_distance_km"] = None
        afm["dc_distance_band"] = "Unknown"
        return

    km = haversine_km(applicant_coords[0], applicant_coords[1], dc_coords[0], dc_coords[1])
    band = distance_band_km(km)
    afm["dc_distance_km"] = km
    afm["dc_distance_band"] = band
    afm["_dc_distance_function"] = (
        "underwriting_platform.engine.identity_verification._resolve_dc_distance"
    )
    logger.info("[id_verify] DC distance = %.2f km (%s)", km, band)


def _doc_label_match(
    doc_labels: dict[str, tuple[str, bytes]] | None, wanted_labels: tuple[str, ...]
) -> tuple[str, bytes] | None:
    """First (id, bytes) from the content-classification result whose
    label is in wanted_labels, trying each label in priority order."""
    if not doc_labels:
        return None
    for want in wanted_labels:
        for id_str, (label, img_bytes) in doc_labels.items():
            if label == want:
                return (id_str, img_bytes)
    return None


async def _resolve_photo_match(
    case_dir: Path, applicant: dict, doc_labels: dict[str, tuple[str, bytes]] | None = None
) -> None:
    """Locate KYC + live photo files; run Gemini face match; populate
    applicant.live_photo_at_medicals + photo_matches_uid + photo_match.
    Keyword matching is tried first (fast path); when it finds nothing,
    falls back to the content-classification result (see module
    docstring item 0) so a real ID/PIWC photo under a generic filename
    isn't silently treated as absent.
    """
    files = _scan_case_dir(case_dir)
    kyc_candidates = _filter(files, _KYC_KEYWORDS, _KYC_EXTS)
    live_candidates = _filter(files, _LIVE_PHOTO_KEYWORDS, _PHOTO_EXTS)

    kyc_fallback = None if kyc_candidates else _doc_label_match(doc_labels, _KYC_LABEL_PRIORITY)
    live_fallback = None if live_candidates else _doc_label_match(doc_labels, ("PERSON_PHOTO",))

    # ── Live photo presence flag ──────────────────────────────────
    if live_candidates or live_fallback:
        applicant["live_photo_at_medicals"] = "Yes"
        applicant["_live_photo_files"] = (
            [str(c) for c in live_candidates]
            if live_candidates
            else [f"content-scan:{live_fallback[0]}"]
        )
    else:
        # Only downgrade Unknown → No; preserve an explicit Yes from the agent.
        if applicant.get("live_photo_at_medicals") in (None, "Unknown", ""):
            applicant["live_photo_at_medicals"] = "No"

    # ── Face match (only when both sides are present) ─────────────
    if not (kyc_candidates or kyc_fallback) or not (live_candidates or live_fallback):
        applicant.setdefault("photo_matches_uid", "Unverified")
        applicant["photo_match"] = {
            "match_result": "skipped",
            "confidence": None,
            "requires_manual_review": False,
            "reasoning": (
                "No KYC photo on file."
                if not (kyc_candidates or kyc_fallback)
                else "No live photo on file."
            ),
            "kyc_files": [str(c) for c in kyc_candidates],
            "live_files": [str(c) for c in live_candidates],
        }
        return

    # Pick the first candidate from each side (keyword match preferred;
    # content-classification fallback only when keyword matching found
    # nothing at all for that side).
    kyc_ref = kyc_candidates[0] if kyc_candidates else None
    live_ref = live_candidates[0] if live_candidates else None
    kyc_bytes = _load_face_image_bytes(kyc_ref) if kyc_ref is not None else kyc_fallback[1]
    live_bytes = _load_face_image_bytes(live_ref) if live_ref is not None else live_fallback[1]
    kyc_ref_label = str(kyc_ref) if kyc_ref is not None else f"content-scan:{kyc_fallback[0]}"
    live_ref_label = str(live_ref) if live_ref is not None else f"content-scan:{live_fallback[0]}"
    kyc_ref, live_ref = kyc_ref_label, live_ref_label
    if not kyc_bytes or not live_bytes:
        applicant["photo_matches_uid"] = "Unverified"
        applicant["photo_match"] = {
            "match_result": "skipped",
            "confidence": None,
            "requires_manual_review": False,
            "reasoning": (
                "KYC image bytes unreadable." if not kyc_bytes else "Live image bytes unreadable."
            ),
            "kyc_file": str(kyc_ref),
            "live_file": str(live_ref),
        }
        return

    from underwriting_platform.engine.photo_match import compare_faces_gemini

    result = await compare_faces_gemini(kyc_bytes, live_bytes)
    result["kyc_file"] = str(kyc_ref)
    result["live_file"] = str(live_ref)

    applicant["photo_match"] = result
    mr = result.get("match_result")
    if mr == "match":
        applicant["photo_matches_uid"] = "Yes"
    elif mr == "no-match":
        applicant["photo_matches_uid"] = "No"
    elif mr in ("partial-match", "undetectable"):
        applicant["photo_matches_uid"] = "Unverified"
    else:  # skipped / unknown
        applicant.setdefault("photo_matches_uid", "Unverified")

    logger.info(
        "[id_verify] photo_match: %s (confidence=%s, kyc=%s, live=%s)",
        mr,
        result.get("confidence"),
        kyc_ref,
        live_ref,
    )


def _skip_result(reason: str) -> dict:
    return {
        "match_result": "skipped",
        "confidence": None,
        "requires_manual_review": False,
        "reasoning": reason,
    }


async def _resolve_photo_match_set(
    case_dir: Path, applicant: dict, doc_labels: dict[str, tuple[str, bytes]] | None = None
) -> None:
    """Three-way face match, all anchored on the proposal/CAF photo as the
    base image: base-vs-KYC, base-vs-Insta-PIWC,
    base-vs-medical-document-photo. Populates applicant.photo_match_set =
    {"vs_kyc": {...}, "vs_piwc": {...}, "vs_medical": {...}}, each shaped
    like engine.photo_match.compare_faces_gemini's own return dict. Every
    comparison degrades to "skipped" independently — a missing medical
    photo (the normal case for a financial-only product) doesn't block
    the KYC/PIWC comparisons.

    Keyword matching is tried first for every side; content-classification
    (see module docstring item 0) fills in only the sides keyword
    matching found nothing for. Multiple PERSON_PHOTO hits from the
    content scan are split base/PIWC rather than compared against
    themselves."""
    files = _scan_case_dir(case_dir)
    base_candidates = _filter(files, _BASE_PHOTO_KEYWORDS, _PHOTO_EXTS)
    kyc_candidates = _filter(files, _KYC_KEYWORDS, _KYC_EXTS)
    piwc_candidates = _filter(files, _LIVE_PHOTO_KEYWORDS, _PHOTO_EXTS)
    medical_candidates = _filter(files, _MEDICAL_PHOTO_KEYWORDS, _PHOTO_EXTS)

    # All PERSON_PHOTO hits from the content scan, in case both base and
    # PIWC need a fallback — pulled from different files, never the same
    # one compared against itself.
    person_photo_hits = (
        [(id_, b) for id_, (label, b) in (doc_labels or {}).items() if label == "PERSON_PHOTO"]
        if not (base_candidates and piwc_candidates)
        else []
    )

    base_fallback = None
    if not base_candidates and person_photo_hits:
        base_fallback = person_photo_hits[0]
    piwc_fallback = None
    if not piwc_candidates and person_photo_hits:
        remaining = [
            h for h in person_photo_hits if base_fallback is None or h[0] != base_fallback[0]
        ]
        if remaining:
            piwc_fallback = remaining[0]
    kyc_fallback = None if kyc_candidates else _doc_label_match(doc_labels, _KYC_LABEL_PRIORITY)

    result_set: dict = {}

    if not base_candidates and not base_fallback:
        skip = _skip_result("No proposal/base photo (RECENTPHOTOGRAPH) on file.")
        applicant["photo_match_set"] = {
            "vs_kyc": dict(skip),
            "vs_piwc": dict(skip),
            "vs_medical": dict(skip),
        }
        logger.info("[id_verify] photo_match_set: no base photo found — all skipped")
        return

    base_ref = base_candidates[0] if base_candidates else None
    base_bytes = _load_face_image_bytes(base_ref) if base_ref is not None else base_fallback[1]
    base_label = str(base_ref) if base_ref is not None else f"content-scan:{base_fallback[0]}"
    if not base_bytes:
        skip = _skip_result("Base photo file unreadable.")
        applicant["photo_match_set"] = {
            "vs_kyc": dict(skip),
            "vs_piwc": dict(skip),
            "vs_medical": dict(skip),
        }
        logger.info("[id_verify] photo_match_set: base photo unreadable (%s)", base_label)
        return

    from underwriting_platform.engine.photo_match import compare_faces_gemini

    for key, candidates, fallback, empty_reason in (
        ("vs_kyc", kyc_candidates, kyc_fallback, "No KYC photo on file."),
        ("vs_piwc", piwc_candidates, piwc_fallback, "No Insta-PIWC photo on file."),
        ("vs_medical", medical_candidates, None, "No medical-document photo on file."),
    ):
        other_ref = candidates[0] if candidates else None
        if other_ref is None and fallback is None:
            result_set[key] = _skip_result(empty_reason)
            continue
        other_bytes = _load_face_image_bytes(other_ref) if other_ref is not None else fallback[1]
        other_label = str(other_ref) if other_ref is not None else f"content-scan:{fallback[0]}"
        if not other_bytes:
            result_set[key] = _skip_result(f"{key} comparison image unreadable.")
            continue
        res = await compare_faces_gemini(base_bytes, other_bytes)
        res["base_file"] = base_label
        res["compare_file"] = other_label
        result_set[key] = res
        logger.info(
            "[id_verify] photo_match_set.%s: %s (confidence=%s, base=%s, vs=%s)",
            key,
            res.get("match_result"),
            res.get("confidence"),
            base_label,
            other_label,
        )

    applicant["photo_match_set"] = result_set
