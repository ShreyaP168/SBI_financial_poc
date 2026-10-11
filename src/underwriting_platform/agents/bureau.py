"""
agents/bureau.py — SBI IIB/CIBIL bureau screenshot extraction.

Reads "IIB, prev insurance.docx" DIRECTLY at its configured location
(SBI_IIB_DOCX env var) — no copying into Input_Docs. The doc bundles all
cases together: a case_id paragraph label followed by 2 screenshot images
(IIB Details + SBI's own ASP "Details For LA"), repeated per case, in the
same order as the case folders. This module locates the image pair for
one case_id and feeds it to the same vision-LLM pipeline every other
agent uses.

Not dispatched via the master orchestrator's document survey (these
images aren't part of any case folder) — called directly as a
deterministic-location join step, same point in orchestrator/master.py
where the policy_data / iib / alpha_policy xlsx joins already happen.
CIBIL score / DRC category / Profile Score come from Scores.xlsx
(engine/scores_xlsx.py) separately — that part is already-structured
tabular data and doesn't need vision extraction.
"""

from __future__ import annotations
import io
import logging
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from underwriting_platform.llm import Document
from .base import AgentResult, BaseAgent

logger = logging.getLogger(__name__)

_CASE_ID_RE = re.compile(r"^[0-9A-Z]{8,12}$")


def _resolve_docx_path(path: Optional[str | Path] = None) -> Optional[Path]:
    if path:
        p = Path(path)
        return p if p.exists() else None
    env_p = os.environ.get("SBI_IIB_DOCX")
    if env_p:
        p = Path(env_p)
        return p if p.exists() else None
    return None


def _png_to_jpeg_b64(png_bytes: bytes) -> str:
    """Convert PNG screenshot bytes to base64 JPEG — Document.to_anthropic_blocks
    declares media_type image/jpeg, and the rest of the pipeline's images
    are always JPEG."""
    import base64
    from PIL import Image

    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def extract_case_iib_images(
    case_id: str, docx_path: Optional[str | Path] = None
) -> Optional[list[bytes]]:
    """Return the [IIB Details PNG, ASP Details-For-LA PNG] pair for one
    case, read straight out of the bundled docx in document order.

    Mapping logic: the docx repeats <case_id paragraph> x2, <image>, <image>
    for each case, in the same sequence as the Input_Docs case folders.
    We pair the Nth unique case-id label with the Nth image pair. If the
    image count isn't a clean multiple of the case count, we bail out
    (return None) rather than guess — a wrong case/image pairing here
    would silently misattribute someone else's bureau history.
    """
    resolved = _resolve_docx_path(docx_path)
    if resolved is None:
        return None
    try:
        with zipfile.ZipFile(resolved) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
        namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs = [
            "".join(node.text or "" for node in paragraph.findall(".//w:t", namespaces))
            for paragraph in root.findall("./w:body/w:p", namespaces)
        ]
    except Exception as e:
        logger.warning("[bureau] failed to open %s: %s", resolved, e)
        return None

    case_order: list[str] = []
    for paragraph in paragraphs:
        t = paragraph.strip()
        if t and _CASE_ID_RE.match(t) and (not case_order or case_order[-1] != t):
            case_order.append(t)
    # Dedup while preserving first-seen order (each case_id appears twice).
    seen: set[str] = set()
    unique_case_order: list[str] = []
    for c in case_order:
        if c not in seen:
            seen.add(c)
            unique_case_order.append(c)

    if case_id not in unique_case_order:
        return None

    try:
        with zipfile.ZipFile(resolved) as z:
            media_names = sorted(
                [n for n in z.namelist() if n.startswith("word/media/")],
                key=lambda n: (
                    int(re.search(r"(\d+)", Path(n).stem).group(1))
                    if re.search(r"(\d+)", Path(n).stem)
                    else 0
                ),
            )
            media_bytes = [z.read(n) for n in media_names]
    except Exception as e:
        logger.warning("[bureau] failed to read media from %s: %s", resolved, e)
        return None

    n_cases = len(unique_case_order)
    if n_cases == 0 or len(media_bytes) % n_cases != 0:
        logger.warning(
            "[bureau] image count %d not divisible by case count %d — refusing to guess pairing",
            len(media_bytes),
            n_cases,
        )
        return None
    images_per_case = len(media_bytes) // n_cases
    idx = unique_case_order.index(case_id)
    start = idx * images_per_case
    return media_bytes[start : start + images_per_case]


class BureauAgent(BaseAgent):
    """Extracts IIB prior-policy records from the bureau screenshot pair
    for one case. CIBIL/DRC/Profile Score are NOT this agent's job — those
    come from Scores.xlsx (engine/scores_xlsx.py), already-structured data
    that doesn't need vision extraction."""

    AGENT_NAME = "bureau"
    OUTPUT_BLOCK = "sbi_bureau"
    PROMPT_FILE = "bureau.txt"

    async def extract_for_case(self, case_id: str, docx_path: Optional[str] = None) -> AgentResult:
        png_list = extract_case_iib_images(case_id, docx_path)
        if not png_list:
            return AgentResult(
                block={"sbi_bureau": None},
                confidence=0.0,
                notes=f"No IIB bureau screenshots found for case {case_id} "
                f"(SBI_IIB_DOCX unset, file missing, or case not in the doc).",
                docs_consumed=[],
            )
        try:
            images_b64 = [_png_to_jpeg_b64(p) for p in png_list]
        except Exception as e:
            return AgentResult(
                block={"sbi_bureau": None},
                confidence=0.0,
                notes=f"Failed to decode bureau screenshots for {case_id}: {e}",
                docs_consumed=[],
            )
        synthetic_doc = Document(
            filename=f"IIB_bureau_screenshots_{case_id}",
            text="",
            images_b64=images_b64,
            page_count=len(images_b64),
        )
        prompt = self._load_prompt()
        blocks = self._build_blocks(
            [synthetic_doc],
            header=f"Bureau screenshots for case {case_id}. Return only the JSON object.",
        )
        try:
            data = await self._extract_json(prompt, blocks)
        except Exception as e:
            logger.error("Bureau agent LLM call failed: %s", e)
            return AgentResult(
                block={"sbi_bureau": None},
                confidence=0.0,
                notes=f"LLM error: {getattr(e, 'code', type(e).__name__)}",
                error=getattr(e, "code", type(e).__name__),
                retryable=getattr(e, "retryable", False),
                docs_consumed=[synthetic_doc.filename],
            )
        bureau_block = (data.get("sbi_bureau") or {}) if isinstance(data, dict) else {}
        meta = data.get("agent_meta") or {}
        return AgentResult(
            block={"sbi_bureau": bureau_block},
            field_sources=meta.get("field_sources") or {},
            raw_payload=getattr(self, "_raw_payload", data),
            source_findings=meta.get("source_findings") or [],
            confidence=float(meta.get("confidence") or 0.0),
            notes=str(meta.get("notes") or ""),
            contradictions=list(meta.get("contradictions") or []),
            docs_consumed=[synthetic_doc.filename],
        )

    async def extract(self, documents, shared_context=None, rules=None) -> AgentResult:
        # Not used — this agent is invoked via extract_for_case() directly
        # from the orchestrator's join step, not the document-survey
        # dispatch (these images aren't in any case folder).
        raise NotImplementedError("BureauAgent uses extract_for_case(case_id), not extract().")
