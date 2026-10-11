"""
agents/business_surrogate.py — BUSINESS + SURROGATE agent.

Conditional dispatch — the master orchestrator only invokes this agent when
audited financials, partnership/keyman docs, surrogate income docs, or
Credit Protect loan paperwork is present.

Three sub-domains:
  - business_insurance (Keyman / Partnership / EE / HUF)
  - credit_protect    (loan-linked term cover, ≤105% cover-to-loan)
  - surrogate         (Car IDV / Investments / SIP / CC / Home Loan)

Returns three top-level case_json blocks: business_insurance, credit_protect,
surrogate.
"""

from __future__ import annotations
import logging

from underwriting_platform.llm import Document
from .base import AgentResult, BaseAgent

logger = logging.getLogger(__name__)


class BusinessSurrogateAgent(BaseAgent):
    AGENT_NAME = "business_surrogate"
    OUTPUT_BLOCK = "business_surrogate"  # composite — emits three blocks
    PROMPT_FILE = "business_surrogate.txt"

    async def extract(
        self,
        documents: list[Document],
        shared_context: dict | None = None,
        rules: dict | None = None,
    ) -> AgentResult:
        if not documents:
            return AgentResult(
                block={
                    "business_insurance": {"applicable": False},
                    "credit_protect": {"applicable": False},
                    "surrogate": {"applicable": False},
                },
                confidence=0.0,
                notes="No documents supplied to business_surrogate agent.",
                docs_consumed=[],
            )

        prompt = self._load_prompt()
        ctx_block_text = ""
        if shared_context and shared_context.get("product_type"):
            ctx_block_text = (
                f"CONTEXT: proposed product is {shared_context['product_type']}. "
                f"Look for Credit Protect specifics if applicable.\n\n"
            )

        blocks = self._build_blocks(
            documents,
            header=(
                ctx_block_text + "Documents follow. Set applicable=false for "
                "any sub-domain with no relevant docs. Return only the JSON object."
            ),
        )
        try:
            data = await self._extract_json(prompt, blocks)
        except Exception as e:
            logger.error("Business+Surrogate agent LLM call failed: %s", e)
            return AgentResult(
                block={
                    "business_insurance": {"applicable": False},
                    "credit_protect": {"applicable": False},
                    "surrogate": {"applicable": False},
                },
                confidence=0.0,
                notes=f"LLM error: {getattr(e, 'code', type(e).__name__)}",
                error=getattr(e, "code", type(e).__name__),
                retryable=getattr(e, "retryable", False),
                docs_consumed=[d.filename for d in documents],
            )

        # The prompt asks for THREE top-level blocks. Pull each.
        bi = (data.get("business_insurance") or {}) if isinstance(data, dict) else {}
        cp = (data.get("credit_protect") or {}) if isinstance(data, dict) else {}
        sg = (data.get("surrogate") or {}) if isinstance(data, dict) else {}
        meta = data.get("agent_meta") or {}

        return AgentResult(
            block={
                "business_insurance": bi,
                "credit_protect": cp,
                "surrogate": sg,
            },
            field_sources=meta.get("field_sources") or {},
            raw_payload=getattr(self, "_raw_payload", data),
            source_findings=meta.get("source_findings") or [],
            confidence=float(meta.get("confidence") or 0.0),
            notes=str(meta.get("notes") or ""),
            contradictions=list(meta.get("contradictions") or []),
            docs_consumed=list(meta.get("docs_consumed") or [d.filename for d in documents]),
        )
