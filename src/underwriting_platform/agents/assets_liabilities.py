"""
agents/assets_liabilities.py — ASSETS+LIABILITIES agent.

Reads bank statements, property docs, loan letters, investment statements,
credit card statements. Returns wealth + obligations block that feeds FOIR
and debt-coverage ratio downstream.
"""

from __future__ import annotations
import logging

from underwriting_platform.llm import Document
from .base import AgentResult, BaseAgent

logger = logging.getLogger(__name__)


class AssetsLiabilitiesAgent(BaseAgent):
    AGENT_NAME = "assets_liabilities"
    OUTPUT_BLOCK = "assets_liabilities"
    PROMPT_FILE = "assets_liabilities.txt"

    async def extract(
        self,
        documents: list[Document],
        shared_context: dict | None = None,
        rules: dict | None = None,
    ) -> AgentResult:
        if not documents:
            return AgentResult(
                block={
                    "assets_liabilities": {
                        "liquid_assets": None,
                        "fixed_assets_total": None,
                        "liabilities_total": None,
                        "monthly_emi_total": None,
                        "bank_statements": [],
                        "loans": [],
                        "property_records": [],
                        "investments": [],
                    }
                },
                confidence=0.0,
                notes="No documents supplied to assets_liabilities agent.",
                docs_consumed=[],
            )

        prompt = self._load_prompt()
        blocks = self._build_blocks(
            documents,
            header=(
                "Documents follow. Apply the rules in your system prompt and "
                "return only the JSON object."
            ),
        )
        try:
            data = await self._extract_json(prompt, blocks)
        except Exception as e:
            logger.error("Assets+Liabilities agent LLM call failed: %s", e)
            return AgentResult(
                block={"assets_liabilities": {}},
                confidence=0.0,
                notes=f"LLM error: {getattr(e, 'code', type(e).__name__)}",
                error=getattr(e, "code", type(e).__name__),
                retryable=getattr(e, "retryable", False),
                docs_consumed=[d.filename for d in documents],
            )

        block = (data.get("assets_liabilities") or {}) if isinstance(data, dict) else {}
        meta = data.get("agent_meta") or {}
        return AgentResult(
            block={"assets_liabilities": block},
            field_sources=meta.get("field_sources") or {},
            raw_payload=getattr(self, "_raw_payload", data),
            source_findings=meta.get("source_findings") or [],
            confidence=float(meta.get("confidence") or 0.0),
            notes=str(meta.get("notes") or ""),
            contradictions=list(meta.get("contradictions") or []),
            docs_consumed=list(meta.get("docs_consumed") or [d.filename for d in documents]),
        )
