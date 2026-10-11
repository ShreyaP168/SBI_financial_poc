"""
agents/income.py — INCOME agent.

Reads slip / Form 16 / appointment letter / ITR / Form 26AS / CA computation /
audited P&L / bank statements / AML summary. Picks primary income source per
the cascade in the prompt. Returns structured income block.
"""

from __future__ import annotations
import logging

from underwriting_platform.llm import Document
from .base import AgentResult, BaseAgent

logger = logging.getLogger(__name__)


class IncomeAgent(BaseAgent):
    AGENT_NAME = "income"
    OUTPUT_BLOCK = "income"
    PROMPT_FILE = "income.txt"

    async def extract(
        self,
        documents: list[Document],
        shared_context: dict | None = None,
        rules: dict | None = None,
    ) -> AgentResult:
        if not documents:
            return AgentResult(
                block={
                    "income": {
                        "primary_source": "declared_only",
                        "annual_income": None,
                        "monthly_gross": None,
                        "income_sources": [],
                        "declared_only": True,
                        "trend": "Unknown",
                    }
                },
                confidence=0.0,
                notes="No documents supplied to income agent.",
                docs_consumed=[],
            )

        prompt = self._load_prompt()
        # Optionally seed with system_data.insured_income if shared_context has it,
        # so the agent can do the gross-up reconciliation against bank credits.
        ctx_block_text = ""
        if shared_context:
            insured_income = shared_context.get("insured_income")
            if insured_income:
                ctx_block_text = (
                    f"CONTEXT FROM POLICY DATA: applicant's CAF-declared annual gross "
                    f"income is ₹{int(insured_income):,}. Use this for gross-up "
                    f"reconciliation when bank credits are the primary source.\n\n"
                )

        blocks = self._build_blocks(
            documents,
            header=(
                ctx_block_text + "Documents follow. Apply the cascade in your "
                "system prompt and return only the JSON object."
            ),
        )

        try:
            data = await self._extract_json(prompt, blocks)
        except Exception as e:
            logger.error("Income agent LLM call failed: %s", e)
            return AgentResult(
                block={"income": {"primary_source": "declared_only", "declared_only": True}},
                confidence=0.0,
                notes=f"LLM error: {getattr(e, 'code', type(e).__name__)}",
                error=getattr(e, "code", type(e).__name__),
                retryable=getattr(e, "retryable", False),
                docs_consumed=[d.filename for d in documents],
            )

        income_block = (data.get("income") or {}) if isinstance(data, dict) else {}
        meta = data.get("agent_meta") or {}

        return AgentResult(
            block={"income": income_block},
            field_sources=meta.get("field_sources") or {},
            raw_payload=getattr(self, "_raw_payload", data),
            source_findings=meta.get("source_findings") or [],
            confidence=float(meta.get("confidence") or 0.0),
            notes=str(meta.get("notes") or ""),
            contradictions=list(meta.get("contradictions") or []),
            docs_consumed=list(meta.get("docs_consumed") or [d.filename for d in documents]),
        )
