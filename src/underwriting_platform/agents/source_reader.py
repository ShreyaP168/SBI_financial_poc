"""One evidence reader owns each document; domains consume its canonical blocks."""

import re
from pathlib import Path

from .base import AgentResult, BaseAgent


class SourceReader(BaseAgent):
    AGENT_NAME = "source_once"
    OUTPUT_BLOCK = "applicant"
    OWNED_BLOCKS = (
        "applicant",
        "anti_fraud_markers",
        "income",
        "compliance",
        "medical",
        "assets_liabilities",
        "business_insurance",
        "credit_protect",
        "surrogate",
    )
    PROMPTS = {
        "identity": "identity.txt",
        "income": "income.txt",
        "compliance": "compliance.txt",
        "medical": "medical.txt",
        "assets_liabilities": "assets_liabilities.txt",
        "business_surrogate": "business_surrogate.txt",
    }

    def __init__(self, llm_client, domains):
        super().__init__(llm_client)
        self.domains = tuple(
            dict.fromkeys(
                "business_surrogate" if name == "business_insurance" else name for name in domains
            )
        )

    def _load_prompt(self):
        from underwriting_platform.application.completion import (
            CORE_FIELDS,
            CONDITIONAL_EVIDENCE_FIELDS,
        )

        schemas = []
        for name in self.domains:
            path = Path(__file__).parent / "prompts" / self.PROMPTS[name]
            prompt = path.read_text(encoding="utf-8")
            matches = re.findall(r"```(?:json)?\s*(.*?)```", prompt, re.S)
            if not matches:
                raise ValueError("Source reader requires a versioned domain schema")
            # Preserve domain interpretation instructions, not just the output shape.
            schemas.append(f"{name.upper()} DOMAIN INSTRUCTIONS:\n{prompt}")
        return (
            "Required decision fields MUST each have their own agent_meta.field_sources entry: "
            + ", ".join(CORE_FIELDS + CONDITIONAL_EVIDENCE_FIELDS)
            + ". Cite each known value separately, including gender, education and residential "
            "status. If a required fact is not documented, leave it null, never infer it.\n"
            + "You are the sole source-evidence reader for an insurance case. Read each supplied "
            "document once and extract all its applicable domain facts into ONE JSON object. "
            "Combine the following canonical shapes; emit only ONE agent_meta, never a wrapper "
            "or separate per-domain replies. These shapes describe data, not predetermined values. "
            "Use original evidence, not filenames or schema examples, to determine values. "
            "Do not issue underwriting decisions or fabricate proof. Missing evidence stays "
            "null/unknown; absent AML/medical findings never imply clearance. "
            "Preserve raw occupation, business name and business-nature statements separately; "
            "do not infer trade from an address or replace declarations with guesses. "
            "For financials, distinguish income from turnover, tax and sum assured; retain each "
            "assessment year and documented income source. Do not double count repeated copies "
            "of the same assessment year. Identify the documented primary income source; if "
            "income cannot be established leave annual_income null rather than guessing. "
            "For ITR, emit all documented itr_years with assessment_year, gross_total_income, "
            "earned_total and exact name_on_document. Earned heads include salary, business/"
            "profession, firm remuneration, firm interest on capital, firm profit share and "
            "pension; exclude passive interest, rental income, dividends and capital gains. "
            "earned_total cannot exceed that year's gross_total_income. Set is_salaried from "
            "the actual filing; annual_income is the latest earned_total, never a pre-average. "
            "The engine computes the eligible year average. Parse Indian lakh/crore commas "
            "without changing magnitude. On salary slips use gross earnings before deductions, "
            "not net pay or employer CTC; retain dated salary_slips and bank credits as separate "
            "evidence. Transcribe DOB and proposal/policy dates for deterministic entry-age "
            "calculation; preserve any conflicting declared age rather than overwriting it. "
            "Keep all nominee allocations and disclosed policy exposures. Cross-document "
            "conflicts belong in agent_meta.contradictions and must not be silently resolved. "
            "Describe KYC status only from available evidence; do not equate missing screening "
            "with a clear screening result. Read declarations and medical answers in full. "
            "Return compact JSON with short evidence excerpts and complete critical citations.\n\n"
            + "\n\n".join(schemas)
        )

    async def extract(self, documents, shared_context=None, rules=None):
        blocks = self._build_blocks(documents)
        data = await self._extract_json(self._load_prompt(), blocks, max_tokens=16000)
        meta = data.get("agent_meta") or {}
        return AgentResult(
            block={key: data[key] for key in self.OWNED_BLOCKS if key in data},
            confidence=float(meta.get("confidence") or 0),
            notes=str(meta.get("notes") or ""),
            contradictions=meta.get("contradictions") or [],
            docs_consumed=[d.filename for d in documents],
            field_sources=meta.get("field_sources") or {},
            source_findings=meta.get("source_findings") or [],
            raw_payload=getattr(self, "_raw_payload", data),
        )


def source_batches(documents, max_images=80, max_text_chars=150000):
    """Pack unique source documents by actual transmitted size, never by eight files."""
    batch, images, characters = [], 0, 0
    for document in documents:
        blocks = document.to_anthropic_blocks()
        image_count = sum(b["type"] == "image" for b in blocks)
        text_count = sum(len(b.get("text", "")) for b in blocks if b["type"] == "text")
        if batch and (
            images + image_count > max_images or characters + text_count > max_text_chars
        ):
            yield batch
            batch, images, characters = [], 0, 0
        batch.append(document)
        images += image_count
        characters += text_count
    if batch:
        yield batch
