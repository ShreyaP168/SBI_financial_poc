"""
engine/identity_verification.py — post-extraction step that fills in
deterministic identity-verification fields:

  0. **Content-based document classification** — client requirement:
     "Identify documents by their actual content and document type, not
     solely by exact filenames... Ensure Aadhaar cards and other relevant
     identity documents are detected even when their filenames differ
     from expected patterns." Every loose image file plus every
     low-text-density PDF page (a text-sparse page is the signature of a
     scanned photo/ID rather than a typed form) in the case folder is
     sent to Gemini vision in ONE batched call and labelled
     (AADHAAR_FRONT / AADHAAR_BACK / PAN_CARD / PASSPORT /
     DRIVING_LICENSE / PERSON_PHOTO / SIGNATURE / FORM_OR_TEXT /
     BLANK_OR_UNREADABLE / OTHER). Keyword filename matching (below)
     still runs first as the fast path; content classification is
     consulted whenever keyword matching comes up empty for a category,
     and backfills compliance.kyc_evidence when it finds a real ID photo
     the extraction agent's own pass missed entirely.
  1. **DC distance** — geocode applicant residence + diagnostic-centre
     address via Google Maps, compute Haversine km, populate
     `anti_fraud_markers.dc_distance_km` + `dc_distance_band`.
  2. **Live photo at medicals** — locate a "TPA photo identification" /
     "live photo" / "geotag" file in the case folder and set
     `applicant.live_photo_at_medicals` accordingly.
  3. **Photo match** — when both a KYC photo (Aadhaar / PAN / passport)
     and a live photo are on disk, run Gemini face-match and populate
     `applicant.photo_matches_uid` + `applicant.photo_match` (full result
     dict with confidence, reasoning, model, manual-review flag).

File-discovery is deterministic (keyword pass) with one shared Gemini
vision call for content classification, feeding both this module's own
KYC backfill and the face-match steps below — not a separate call per
file. Geocoding + face-match are the only other network calls.

Failures degrade gracefully — geocoding miss → `Unknown` band, no live
photo file → `live_photo_at_medicals = "No"`, GEMINI_API_KEY absent →
photo match `skipped`, classification call failure → falls back to
keyword-only matching exactly as before this feature existed. The
engine flag stage emits the corresponding `DC_DISTANCE_NOT_DETERMINED` /
`LIVE_PHOTO_MISSING` / `PHOTO_MATCH_*` flags from these field values.
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

_CLASSIFY_PROMPT = """You are classifying document images for an Indian life-insurance KYC workflow.
You will be shown several images, each preceded by its ID in the form "=== ID: <id> ===".

For EACH image, output one label from this exact set:
  AADHAAR_FRONT        - Aadhaar card front (face photo + 12-digit number + Govt of India emblem)
  AADHAAR_BACK         - Aadhaar card back (address + QR code, no face)
  PAN_CARD             - Income Tax PAN card
  PASSPORT             - passport document
  DRIVING_LICENSE      - driving license card
  PERSON_PHOTO         - a standalone photo of a person's face/headshot, NOT on an ID card (e.g. passport-style photo submitted on its own, or a photo pasted into a form)
  SIGNATURE            - signature specimen only
  FORM_OR_TEXT         - a form, letter, declaration, financial statement, or other text-heavy page with no prominent face photo
  BLANK_OR_UNREADABLE  - blank, solid-colour, fully corrupted/unreadable, or pure noise
  OTHER                - anything else (logo only, QR code only, stamp, etc.)

Respond with STRICT JSON ONLY: a JSON array of objects {"id": "<the id>", "label": "<ONE label from the set above>"}.
No prose, no markdown fences, no explanation. One object per image, in the order shown."""


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
                    low_text_pages = [
                        i
                        for i, page in enumerate(pdf.pages)
                        if len((page.extract_text() or "").strip()) < _CLASSIFY_TEXT_DENSITY_MAX
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


async def classify_case_documents(case_dir: Path) -> dict[str, tuple[str, bytes]]:
    """Returns {id: (label, image_bytes)} for every candidate in the case
    folder — one batched Gemini vision call, not one per file. Empty dict
    (not an exception) on any failure — GEMINI_API_KEY absent, API error,
    unparseable response — so callers fall back to keyword matching
    exactly as if this feature didn't run."""
    import os

    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return {}
    candidates = _gather_classification_candidates(case_dir)
    if not candidates:
        return {}
    try:
        from google import genai
        from google.genai import types as genai_types

        parts: list = [
            _CLASSIFY_PROMPT
            + f"\nReturn exactly {len(candidates)} rows; include every supplied ID exactly once."
        ]
        for id_str, img_bytes in candidates:
            parts.append(f"=== ID: {id_str} ===")
            parts.append(genai_types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))

        from underwriting_platform.llm.gemini_budget import generate

        client = genai.Client(api_key=api_key)
        response = await generate(
            client,
            model=os.getenv("GEMINI_DOCUMENT_CLASSIFIER_MODEL", "gemini-2.5-flash"),
            contents=parts,
            max_output_tokens=8192,
            json_output=True,
            json_schema={
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "id": {"type": "STRING"},
                        "label": {
                            "type": "STRING",
                            "enum": [
                                "AADHAAR_FRONT",
                                "AADHAAR_BACK",
                                "PAN_CARD",
                                "PASSPORT",
                                "DRIVING_LICENSE",
                                "PERSON_PHOTO",
                                "SIGNATURE",
                                "FORM_OR_TEXT",
                                "BLANK_OR_UNREADABLE",
                                "OTHER",
                            ],
                        },
                    },
                    "required": ["id", "label"],
                },
            },
        )
        raw = response.text or ""
    except Exception as e:
        logger.warning("[doc_classify] Gemini batch classification failed: %s", e)
        raise RuntimeError("Identity document classification dependency failed") from e

    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
        if text.lower().startswith("json"):
            text = text[4:].lstrip("\n")
    try:
        parsed = json.loads(text)
    except Exception as e:
        logger.warning("[doc_classify] could not parse classification response: %s", e)
        raise ValueError("Identity document classification response invalid") from e

    bytes_by_id = dict(candidates)
    if not isinstance(parsed, list):
        raise ValueError("Identity classification must return a complete array")
    result: dict[str, tuple[str, bytes]] = {}
    for row in parsed if isinstance(parsed, list) else []:
        try:
            id_str, label = row["id"], row["label"]
        except (TypeError, KeyError):
            raise ValueError("Malformed identity classification row")
        if id_str in bytes_by_id:
            result[id_str] = (label, bytes_by_id[id_str])
    if len(result) != len(bytes_by_id):
        raise ValueError("Identity classification omitted required documents")
    logger.info(
        "[doc_classify] classified %d/%d candidates in %s",
        len(result),
        len(candidates),
        case_dir.name,
    )
    return result


# Priority order matches SBI's own accepted-OVD list (Aadhaar / Passport /
# Driving Licence) from the real requirements-letter correspondence seen
# across these cases, PAN last (PAN is mandatory but not itself a
# standalone OVD for address proof).
_KYC_LABEL_PRIORITY = ("AADHAAR_FRONT", "PASSPORT", "DRIVING_LICENSE", "PAN_CARD", "AADHAAR_BACK")


def _backfill_kyc_from_content_scan(
    case_json: dict, doc_labels: dict[str, tuple[str, bytes]]
) -> None:
    """When content classification found a real ID photo that the
    compliance agent's own extraction pass missed, correct
    compliance.kyc_evidence / kyc_status to match reality instead of
    leaving a stale "Partial — no OVD" status that the actual documents
    contradict. Only ever upgrades (False -> True); never removes a
    True the agent already set, and never touches CKYC/address-proof
    fields this scan has no opinion on."""
    if not doc_labels:
        return
    compliance = case_json.setdefault("compliance", {})
    kyc_evidence = compliance.setdefault("kyc_evidence", {})
    found_labels = {label for label, _ in doc_labels.values()}

    _FIELD_FOR_LABEL = {
        "AADHAAR_FRONT": "aadhaar_present",
        "AADHAAR_BACK": "aadhaar_present",
        "PAN_CARD": "pan_present",
        "PASSPORT": "passport_present",
        "DRIVING_LICENSE": "driving_licence",
    }
    upgraded = []
    for label, field in _FIELD_FOR_LABEL.items():
        if label in found_labels and not kyc_evidence.get(field):
            kyc_evidence[field] = True
            upgraded.append(label)

    if upgraded:
        best_id = next((id_ for id_, (label, _) in doc_labels.items() if label in upgraded), None)
        note = (kyc_evidence.get("_note") or "").strip()
        addendum = (
            f"Content-based scan found a legible {', '.join(sorted(set(upgraded)))} "
            f"(file: {best_id}) that filename-based matching missed — "
            f"kyc_evidence corrected to match the actual document set."
        )
        kyc_evidence["_note"] = (note + " " + addendum).strip() if note else addendum
        logger.info(
            "[id_verify] KYC backfill from content scan: upgraded %s via %s", upgraded, best_id
        )

    # Re-derive kyc_status from the full, CURRENT set of evidence
    # sub-fields — run unconditionally, not just when this scan itself
    # found something new, so a case already fully complete from the
    # original extraction (nothing for this scan to "upgrade") still gets
    # its stale "Partial" status corrected. Only flips to "Verified" if
    # EVERY piece this product requires is present (any photo ID + PAN +
    # address proof), not just whatever this scan touched. A real
    # remaining gap (e.g. CKYC still blank) is a separate issue this
    # function has no opinion on and correctly leaves alone —
    # engine.gating.check_kyc still reports "Partial" for that reason.
    photo_id_present = any(
        kyc_evidence.get(k) for k in ("aadhaar_present", "passport_present", "driving_licence")
    )
    all_present = (
        photo_id_present
        and kyc_evidence.get("pan_present")
        and kyc_evidence.get("address_proof_present")
    )
    if all_present and compliance.get("kyc_status") != "Verified":
        compliance["kyc_status"] = "Verified"
        logger.info(
            "[id_verify] KYC status recomputed to Verified (photo ID + PAN + address proof all present)"
        )

    # applicant.photo_present is a separate field from kyc_evidence above
    # (extraction-agent-set, "is there a photo of the applicant anywhere
    # in the document set" — independent of whether that photo could be
    # matched against anything). The agent's own _survey() dispatch step
    # can miss a standalone photo filed under a generic name the same way
    # it misses KYC documents. Only ever upgrades Unknown/falsy -> Yes.
    applicant = case_json.setdefault("applicant", {})
    if not applicant.get("photo_present") or applicant.get("photo_present") == "Unknown":
        person_photo_ids = [label for label, _ in doc_labels.values() if label == "PERSON_PHOTO"]
        if found_labels & {"AADHAAR_FRONT", "PASSPORT", "DRIVING_LICENSE", "PERSON_PHOTO"}:
            applicant["photo_present"] = "Yes"
            logger.info(
                "[id_verify] photo_present backfilled to Yes from content scan (%d person/ID-photo hit(s))",
                len(person_photo_ids) or 1,
            )


# ── Public entry point ───────────────────────────────────────────


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
        doc_labels = await classify_case_documents(case_dir)
        _backfill_kyc_from_content_scan(case_json, doc_labels)
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
