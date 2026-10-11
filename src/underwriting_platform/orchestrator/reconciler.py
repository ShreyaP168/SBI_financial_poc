"""
orchestrator/reconciler.py — Phase 5 LLM-driven cross-agent reconciler.

After the master orchestrator's deterministic merge, this module runs ONE
additional LLM call that looks at the unified case_json + each agent's
self-reported notes/contradictions and emits:

  - contradictions[]   : facts that disagree across agents, with winner picked
  - risk_signals[]     : cross-agent insights no single agent could see alone
  - field_overrides    : { dotted_path -> winning_value } for the engine layer
  - overall_confidence : 0.0–1.0 — case-level coherence score
  - stp_recommendation : STP_OK | STP_CONDITIONAL | STP_BLOCK
  - reconciler_notes   : human summary

Failure mode: if the LLM call fails, returns an empty reconciliation block
(deterministic merge already produced a usable case_json — never block on this).
"""

from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from underwriting_platform.llm import LLMClient

logger = logging.getLogger(__name__)


@dataclass
class ReconciliationResult:
    contradictions: list[dict] = field(default_factory=list)
    risk_signals: list[dict] = field(default_factory=list)
    field_overrides: dict = field(default_factory=dict)
    overall_confidence: float = 0.0
    stp_recommendation: str = "STP_OK"
    reconciler_notes: str = ""
    financial_recommendation: str = ""
    medical_recommendation: str = ""
    overall_recommendation: str = ""
    retryable: bool = True
    error: str | None = None

    def to_block(self) -> dict:
        return {
            "contradictions": self.contradictions,
            "risk_signals": self.risk_signals,
            "field_overrides": self.field_overrides,
            "overall_confidence": self.overall_confidence,
            "stp_recommendation": self.stp_recommendation,
            "reconciler_notes": self.reconciler_notes,
            "financial_recommendation": self.financial_recommendation,
            "medical_recommendation": self.medical_recommendation,
            "overall_recommendation": self.overall_recommendation,
            "error": self.error,
        }


class LLMReconciler:
    PROMPT_FILE = "reconciler.txt"

    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client

    def _load_prompt(self) -> str:
        prompt_path = Path(__file__).parent.parent / "agents" / "prompts" / self.PROMPT_FILE
        return prompt_path.read_text(encoding="utf-8")

    async def reconcile(
        self,
        case_json: dict,
        agent_outputs: dict,
    ) -> ReconciliationResult:
        """Run the LLM reconciler over a merged case_json + per-agent metadata.

        agent_outputs maps agent_name -> AgentResult — we forward the
        confidence + notes + contradictions to give the LLM context.
        """
        system = self._load_prompt() + (
            "\nRESPONSE SIZE CONTRACT: return compact JSON within 6,000 characters. "
            "At most 5 material contradictions and 5 risk signals, most severe first; "
            "mention any additional concerns briefly in reconciler_notes. Each explanation "
            "and reasoning field must be at most 160 characters. Each recommendation and "
            "reconciler_notes must be at most 300 characters. Do not reproduce document text "
            "or itemise every normal finding. field_overrides must be {}: suggestions cannot "
            "replace extracted facts. Domains from source_once are views of one extraction, "
            "not independent corroborating model opinions. Never issue a policy verdict."
        )

        # Build a single text block with merged case_json + per-agent metadata.
        # The reconciler only needs JSON — no images.
        per_agent_meta = {
            name: {
                "confidence": ar.confidence,
                "notes": ar.notes,
                "contradictions": ar.contradictions,
                "source_documents_count": len(ar.docs_consumed),
            }
            for name, ar in agent_outputs.items()
        }

        payload = {
            "merged_case_json": case_json,
            "per_agent_meta": per_agent_meta,
            "source_documents": sorted(
                {fn for ar in agent_outputs.values() for fn in ar.docs_consumed}
            ),
        }
        payload_text = json.dumps(payload, separators=(",", ":"), default=str, ensure_ascii=False)

        # Truncate if obscenely large (>200KB) to avoid hitting token limits.
        if len(payload_text) > 200_000:
            logger.warning("Reconciler payload large (%d chars); truncating.", len(payload_text))
            payload_text = payload_text[:200_000] + "\n[TRUNCATED]"

        blocks: list[dict] = [
            {
                "type": "text",
                "text": "Below is the merged case_json (output of all 5 agents) plus "
                "per-agent metadata. Reconcile per the system prompt and return "
                "ONLY the JSON object.",
            },
            {"type": "text", "text": payload_text},
        ]

        try:
            from underwriting_platform.llm.telemetry import request_stage

            token = request_stage.set("reconciliation")
            try:
                data = await self.llm.extract_json(system, blocks, max_tokens=4000)
            finally:
                request_stage.reset(token)
        except Exception as e:
            logger.error("Reconciler LLM call failed: %s", e)
            return ReconciliationResult(
                error="ReconciliationUnavailable", retryable=getattr(e, "retryable", True)
            )

        if not isinstance(data, dict):
            return ReconciliationResult(
                error=f"Reconciler returned non-dict: {type(data).__name__}"
            )

        return ReconciliationResult(
            contradictions=list(data.get("contradictions") or []),
            risk_signals=list(data.get("risk_signals") or []),
            field_overrides=dict(data.get("field_overrides") or {}),
            overall_confidence=float(data.get("overall_confidence") or 0.0),
            stp_recommendation=str(data.get("stp_recommendation") or "STP_OK"),
            reconciler_notes=str(data.get("reconciler_notes") or ""),
            financial_recommendation=str(data.get("financial_recommendation") or ""),
            medical_recommendation=str(data.get("medical_recommendation") or ""),
            overall_recommendation=str(data.get("overall_recommendation") or ""),
        )
