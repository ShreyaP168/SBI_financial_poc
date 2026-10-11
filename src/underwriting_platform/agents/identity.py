"""
agents/identity.py — IDENTITY agent.

Reads CAF + identity proofs + address proofs and returns the applicant block.
"""

from __future__ import annotations
import logging

from underwriting_platform.llm import Document
from .base import AgentResult, BaseAgent

logger = logging.getLogger(__name__)


class IdentityAgent(BaseAgent):
    AGENT_NAME = "identity"
    OUTPUT_BLOCK = "applicant"
    PROMPT_FILE = "identity.txt"

    async def extract(
        self,
        documents: list[Document],
        shared_context: dict | None = None,
        rules: dict | None = None,
    ) -> AgentResult:
        if not documents:
            return AgentResult(
                block={"applicant": {}},
                confidence=0.0,
                notes="No documents supplied to identity agent.",
                docs_consumed=[],
            )

        prompt = self._load_prompt()
        blocks = self._build_blocks(
            documents,
            header=(
                "You will now receive the documents. Extract per the schema in your "
                "system prompt. Return only the JSON object."
            ),
        )
        try:
            data = await self._extract_json(prompt, blocks)
        except Exception as e:
            logger.error("Identity agent LLM call failed: %s", e)
            return AgentResult(
                block={"applicant": {}},
                confidence=0.0,
                notes=f"LLM error: {getattr(e, 'code', type(e).__name__)}",
                error=getattr(e, "code", type(e).__name__),
                retryable=getattr(e, "retryable", False),
                docs_consumed=[d.filename for d in documents],
            )

        applicant = (data.get("applicant") or {}) if isinstance(data, dict) else {}
        afm = (data.get("anti_fraud_markers") or {}) if isinstance(data, dict) else {}
        meta = data.get("agent_meta") or {}
        return AgentResult(
            block={"applicant": applicant, "anti_fraud_markers": afm},
            field_sources=meta.get("field_sources") or {},
            raw_payload=getattr(self, "_raw_payload", data),
            source_findings=meta.get("source_findings") or [],
            confidence=float(meta.get("confidence") or 0.0),
            notes=str(meta.get("notes") or ""),
            contradictions=list(meta.get("contradictions") or []),
            docs_consumed=list(meta.get("docs_consumed") or [d.filename for d in documents]),
        )
