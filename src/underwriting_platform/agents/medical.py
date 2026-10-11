"""
agents/medical.py — MEDICAL agent.

Extracts the FUW-relevant medical surface from MER, VMER, lab reports, urine
analysis, ECG, TMT, and any other medical PDFs in the case folder. The
output is a single `medical` block consumed downstream by the engine
(BMI band, cotinine fraud check, document-checklist gating).

Mirrors the extraction surface used by the Datamatics medical_uw_backend:
  vitals, habits, lab_results[] with reference ranges + status, ECG/TMT,
  fraud_signals (Cotinine vs declared smoker), family_history (with
  the "form-checkbox false-positive" rule), personal_medical_history,
  documents_present checklist.
"""

from __future__ import annotations
import logging

from underwriting_platform.llm import Document
from .base import AgentResult, BaseAgent

logger = logging.getLogger(__name__)


class MedicalAgent(BaseAgent):
    AGENT_NAME = "medical"
    OUTPUT_BLOCK = "medical"
    PROMPT_FILE = "medical.txt"

    async def extract(
        self,
        documents: list[Document],
        shared_context: dict | None = None,
        rules: dict | None = None,
    ) -> AgentResult:
        if not documents:
            return AgentResult(
                block={
                    "medical": {
                        "vitals": {},
                        "habits_declared": {},
                        "lab_results": [],
                        "ecg": {},
                        "tmt": {},
                        "fraud_signals": {
                            "urine_cotinine_ng_ml": None,
                            "cotinine_threshold": 10,
                            "cotinine_smoker_mismatch": False,
                            "declared_smoker": "Unknown",
                            "fraud_severity": "none",
                            "notes": "No medical documents on file.",
                        },
                        "family_history": [],
                        "personal_medical_history": [],
                        "documents_present": {},
                        "extraction_notes": "No medical documents supplied.",
                    }
                },
                confidence=0.0,
                notes="No documents supplied to medical agent.",
                docs_consumed=[],
            )

        prompt = self._load_prompt()
        ctx_block_text = ""
        if shared_context:
            age = shared_context.get("age")
            sa = shared_context.get("sum_assured")
            smoker = shared_context.get("declared_smoker")
            ctx_bits = []
            if age is not None:
                ctx_bits.append(f"applicant age = {age}")
            if sa:
                ctx_bits.append(f"proposed Sum Assured = ₹{int(sa):,}")
            if smoker is not None:
                ctx_bits.append(f"declared smoker (CAF) = {smoker}")
            if ctx_bits:
                ctx_block_text = (
                    "CONTEXT FROM POLICY DATA: "
                    + "; ".join(ctx_bits)
                    + ". Use the smoker declaration to drive cotinine "
                    "fraud-signal logic.\n\n"
                )

        blocks = self._build_blocks(
            documents,
            header=(
                ctx_block_text + "Medical documents follow. Apply the rules in "
                "your system prompt and return only the JSON object."
            ),
        )
        try:
            data = await self._extract_json(prompt, blocks)
        except Exception as e:
            logger.error("Medical agent LLM call failed: %s", e)
            return AgentResult(
                block={"medical": {}},
                confidence=0.0,
                notes=f"LLM error: {getattr(e, 'code', type(e).__name__)}",
                error=getattr(e, "code", type(e).__name__),
                retryable=getattr(e, "retryable", False),
                docs_consumed=[d.filename for d in documents],
            )

        block = (data.get("medical") or {}) if isinstance(data, dict) else {}
        meta = data.get("agent_meta") or block.get("agent_meta") or {}

        return AgentResult(
            block={"medical": block},
            field_sources=meta.get("field_sources") or {},
            raw_payload=getattr(self, "_raw_payload", data),
            source_findings=meta.get("source_findings") or [],
            confidence=float(meta.get("confidence") or 0.0),
            notes=str(meta.get("notes") or ""),
            contradictions=list(meta.get("contradictions") or []),
            docs_consumed=list(meta.get("docs_consumed") or [d.filename for d in documents]),
        )
