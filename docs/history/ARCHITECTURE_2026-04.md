# Historical April 2026 design

This is historical design material, not the implemented SBI architecture.

**Insurer:** TATA AIA Life Insurance
**Date:** 2026-04-27
**Status:** Phase 0 — foundational design; orchestrator + IDENTITY agent live; remaining agents in plan

## 1. Why v9 exists

v8.4 has a 4-layer pipeline (file_router → classifier → extractor → normaliser → builder → engine → renderer). Each layer is a brittle, narrow contract. Symptoms observed in production runs:

- Filename-pattern classifier misroutes Common Application Forms to `bank_statement`, group-scheme application forms to `appointment_letter`, audited financials to `firm_financial` instead of `audited_pl` / `audited_balance_sheet`.
- Per-doc-type LLM extraction prompts return malformed JSON, miss fields, can't reason across documents.
- Normaliser drops mismatched payloads silently.
- Each new doc type requires a 5-step ritual (schema + classifier patterns + extraction prompt + normaliser + builder hook) and STILL loses to legacy patterns in real cases.

Meanwhile, dropping the same PDFs into Claude.ai produces clean extraction in one pass — **because Claude.ai reads everything holistically with full context**, not through a brittle pipeline.

**v9's thesis:** stop pretending IDP is a pipeline. Treat it as orchestrated reasoning. Master agent + domain sub-agents. One thinking layer, structured outputs, canonical case JSON.

## 2. The new pipeline

```
case folder (PDFs)
        ↓
┌──────────────────────────────────────────────────────────────┐
│            MASTER ORCHESTRATOR                               │
│  1. Survey: lightweight scan of all PDFs (one cheap call)    │
│  2. Plan: which domain agents to dispatch + which docs each  │
│       gets + what to share between them                      │
│  3. Dispatch: parallel agent calls with curated context      │
│  4. Reconcile: resolve cross-agent contradictions            │
│  5. Emit: canonical case JSON + extraction report            │
└────────────────────┬─────────────────────────────────────────┘
                     │
   ┌─────────────────┼──────────┬──────────────┬──────────────┐
   ▼                 ▼          ▼              ▼              ▼
┌────────┐    ┌──────────┐  ┌──────────┐  ┌─────────┐  ┌────────────┐
│IDENTITY│    │ INCOME   │  │ ASSETS+  │  │COMPLIA- │  │ BUSINESS/  │
│        │    │          │  │ LIABIL.  │  │NCE      │  │ SURROGATE  │
│applica-│    │primary + │  │property, │  │KYC, AML,│  │EE, Keyman, │
│nt block│    │alternate │  │bank, EMI │  │disclosure│  │CP, SEP     │
│        │    │sources   │  │          │  │         │  │            │
└────────┘    └──────────┘  └──────────┘  └─────────┘  └────────────┘
                                ↓
┌──────────────────────────────────────────────────────────────┐
│            ENGINE (calculator → gating → flags → verdict)    │
│   Unchanged from v8.4 — consumes canonical case JSON         │
└──────────────────────────────────────────────────────────────┘
                                ↓
┌──────────────────────────────────────────────────────────────┐
│            RENDERER (pdf_gen/template_v2/)                   │
│   Unchanged from v8.4 — produces v3 case sheet PDF           │
└──────────────────────────────────────────────────────────────┘
```

## 3. Canonical case JSON contract

The master orchestrator's output (and engine's input) is a JSON document with a fixed top-level shape. Domain agents fill specific blocks; the master reconciles + assembles.

```json
{
  "case_id": "C684869052",
  "applicant": {
    "applicant_name": "Aditya Firake",
    "pan": "...",
    "dob": "1991-05-13",
    "age": 34,
    "gender": "M",
    "marital_status": "Married",
    "dependents": 1,
    "occupation": "Salaried",
    "education_level": "Graduate",
    "residential_status": "Resident",
    "employer_name": "...",
    "employee_id": "...",
    "address": "...",
    "aadhaar": "..."
  },
  "system_data": {
    "policy_number": "...",
    "plan_name": "...",
    "plan_sub_type": "Term",
    "policy_term": 36,
    "prem_pay_term": 10,
    "sum_assured": 30000000,
    "annual_premium": 66723,
    "insured_income": 3500000,
    "submission_date": "2025-09-10",
    "channel": "Policy Bazaar",
    "channel_detail": "...",
    "ecm_score": "STANDARD",
    "aureus_risk_score": "B4",
    "sar": 30000000, "fsar": 40000000, "tsar": 30000000, "msar": 30000000
  },
  "geography": {
    "country_of_residence": "India",
    "pin_code": "...",
    "negative_pin_flag": false
  },
  "income": {
    "primary_source": "salary_slip|form_16|itr|appointment_letter|bank_credits|declared_only",
    "is_salaried": true,           // ITR path switch: true = ITR-1 salary-only (latest year);
                                   // false = ITR-3/4 business/professional (avg of latest 2 years)
    "annual_income": 3500000,      // for ITR: agent emits latest-year earned; engine recomputes
                                   // eligibility per Term UW V5 p20 (see engine.calculator.compute_selected_income)
    "monthly_gross": 291666,
    "income_sources": ["..."],
    "itr_years": [{"assessment_year": "2024-25", "gross_total_income": ..., "earned_total": ...}, ...],
    "salary_slips": [...],
    "form_16_records": [...],
    "appointment_letters": [...],
    "bank_salary_credits": [...],
    "declared_only": false,
    "trend": "Rising|Stable|Declining|Volatile",
    "variability_cv": 0.16,
    "growth_yoy": 0.12,
    "extraction_notes": "..."
  },
  "assets_liabilities": {
    "liquid_assets": null,
    "fixed_assets_total": null,
    "liabilities_total": null,
    "bank_statements": [...],
    "property_records": [...],
    "loans": [...],
    "extraction_notes": "..."
  },
  "compliance": {
    "kyc_status": "Verified|Partial|Pending",
    "aml_status": "Compliant|Concerns|Non-Compliant",
    "concerns": [...],
    "id_proofs": [...],
    "aml_summary": {...},
    "extraction_notes": "..."
  },
  "medical": {
    "bmi_value": null,
    "bmi_band": null,
    "vmer_date": null,
    "extraction_notes": "..."
  },
  "business_insurance": {  // populated only when applicable
    "type": "Keyman|Partnership|Employer-Employee|HUF",
    "shareholding_pct": null,
    "audited_financials_3yr": [...],
    "extraction_notes": "..."
  },
  "raw_documents": [
    {"file": "...", "agent_owner": "income", "extraction_confidence": 0.92}
  ],
  "agent_reports": {
    "identity": {"status": "OK", "confidence": 0.95, "notes": "..."},
    "income":   {"status": "OK", "confidence": 0.78, "notes": "primary source: salary slip"},
    ...
  },
  "contradictions": [
    {"agents": ["income", "assets_liabilities"], "field": "monthly_gross", "values": [...]}
  ]
}
```

The renderer's existing `field_map.py` reads from these paths verbatim — **no renderer or engine changes needed**.

## 4. Agent contracts

SBI profile eligibility retains `applicant.declared_occupation` and
`applicant.declared_business_nature` from the identity extraction before
reconciliation. Explicit declared activities cannot be erased by an inferred
ITR income activity. Conflicting candidates remain under
`reconciliation.profile_override_review` for audit; income proof is evaluated
separately. A named disallowed declared trade still blocks eligibility even
when another document describes an additional/different income activity.

Every agent inherits from `agents.base.BaseAgent` and implements:

```python
class BaseAgent:
    AGENT_NAME: str         # e.g. "identity"
    OUTPUT_BLOCK: str       # which top-level case_json key it owns
    REQUIRED_DOCS: list[str]   # doc-type hints (advisory, not enforced)

    async def extract(
        self,
        documents: list[Document],   # full doc objects (text + images)
        shared_context: dict,        # findings from earlier agents
        rules: dict,                 # uw_rules.json
    ) -> AgentResult:
        ...
```

`AgentResult`:
```python
@dataclass
class AgentResult:
    block: dict              # the JSON block (e.g., applicant {...})
    confidence: float        # 0.0–1.0 — how sure the agent is
    notes: str               # human-readable reasoning summary
    contradictions: list     # detected inconsistencies WITHIN this domain
    docs_consumed: list[str] # filenames the agent actually used
```

## 5. Master orchestrator phases

### 5.1 Survey

A single LLM call that:
- Receives every PDF in the case folder (text + low-res images)
- Returns a JSON manifest classifying each PDF (best-effort, free-form, no schema constraint)
- Surfaces meta-observations ("this case has no salary slip but has Form 16 + 6 months bank")

```json
{
  "files": [
    {"file": "Common Application Form.pdf", "kind": "proposal", "relevance": ["identity", "system_data"]},
    {"file": "General Income Proofs.pdf", "kind": "bundle", "relevance": ["income"], "subdocs": ["salary_slip", "bank_statement"]}
  ],
  "applicant_signal": "salaried_resident",
  "missing_likely": ["ITR (no fiscal docs found)"]
}
```

### 5.2 Plan

A second LLM call that takes the survey + canonical schema + business rules, and produces an agent dispatch plan:

```json
{
  "agents_to_dispatch": ["identity", "income", "assets_liabilities", "compliance"],
  "skip": ["business_insurance"],   // not applicable
  "doc_assignments": {
    "identity": ["Common Application Form.pdf"],
    "income": ["General Income Proofs.pdf", "Bank statement.pdf"],
    "assets_liabilities": ["Bank statement.pdf"],
    "compliance": ["Common Application Form.pdf"]
  },
  "shared_context_seed": {
    "applicant_likely_salaried": true,
    "no_itr_expected": true
  }
}
```

### 5.3 Dispatch

Domain agents run in **parallel** (asyncio.gather). Each gets:
- The doc subset from the plan
- The full doc set (so it can read more if needed)
- A snapshot of `shared_context` (filled by master after each batch)
- The `rules` dict

### 5.4 Reconcile

After all agents return, the master compares overlapping fields (e.g., income agent's monthly_gross vs assets_liabilities' bank credits). One LLM call with all agent outputs + the canonical schema, asks: "are there contradictions? if so, which value is more trustworthy and why?"

Output: final consolidated `case_json` + `contradictions` list.

### 5.5 Emit

Write `case.json` (canonical) + `extraction_report.json` (per-agent reasoning + confidence + contradictions). Hand off to engine.

## 6. Engine + renderer integration

The engine (`engine/`) is a **clean port** of v8.4's calculator + gating + flags + verdict + derived_metrics + dispatcher logic. No new business rules. Phase 1 derived_metrics (3-multiplier HLV, profile classifier, BMI band, 7 ratios, declared_only fallback) all preserved.

The renderer (`renderer/`) is a **direct copy** of v8.4's `pdf_gen/template_v2/` — including all 28 universal sections, 9 conditional sections, field_map (255 fields), §28 UW-prose narrative, ₹-glyph-aware font registration, N/A vs PENDING labelling.

Both downstream layers consume the canonical case JSON unchanged.

## 7. LLM client

`llm/client.py` provides an `Anthropic` (or `AnthropicFoundry` for Azure) wrapper with:
- Async + retries + rate limiting (port from v8.4 `utils/rate_limiter.py`)
- A `multimodal()` method that takes (system_prompt, list of doc objects, optional schema)
- A `tool_use()` method for agent-style flows (planned-but-not-required for Phase 0)

The agent prompts live in `agents/prompts/*.txt`. Prompts are short (LLM does the work), structured, and include the canonical-schema fragment for whichever block the agent owns.

## 8. Phasing

| Phase | Deliverable | Status |
|---|---|---|
| 0 | Project scaffold, ARCHITECTURE.md, LLM client, BaseAgent, orchestrator skeleton, IDENTITY agent, end-to-end POC on one case | **In progress** (this session) |
| 1 | INCOME agent (the most complex; cascades across slip / ITR / Form 16 / bank / declared) | Next |
| 2 | ASSETS+LIABILITIES agent | Next |
| 3 | COMPLIANCE agent | Next |
| 4 | BUSINESS+SURROGATE agent (conditional invocations) | Next |
| 5 | Reconciler with cross-agent contradiction detection | Next |
| 6 | Engine + renderer integration; run all 15 cases; compare to v8.4 outputs | Next |
| 7 | Replace v8.4 IDP layer with v9 in production; v8.4 engine + renderer continue serving | Done |

## 9. Cost ceiling (per case)

Target: **≤ $1.00 per case** at Anthropic Sonnet 4.6 rates.

Breakdown:
- Survey: ~10K tokens input × 1 call ≈ $0.05
- Plan: ~5K tokens × 1 call ≈ $0.03
- 5 domain agents in parallel: ~30K tokens each input × 5 calls ≈ $0.40
- Reconciler: ~20K tokens × 1 call ≈ $0.10
- Engine + renderer: deterministic, ~$0.00 LLM cost
- **Total: ~$0.55/case** in steady state

Compared to v8.4: ~$0.10/case. So 5x cost increase. Acceptable in exchange for ~10x reduction in iteration overhead.

## 10. What v9 explicitly does NOT have

- A document classifier (the master agent decides via reasoning, not regex)
- Per-doc-type extraction prompts (each agent reads any doc it needs)
- A normaliser dispatcher (each agent normalises its own output to the canonical schema)
- External integrations (Bureau, ASKme, CKYC, AML, TruAI, PhonePe, Vahan, HR system) — same as v8.4 v3 final state
- Rule-engine complexity (kept simple; rules dict consumed by agents but not interpreted as a DSL)

## 11. Validation strategy

Phase 0 success = orchestrator + IDENTITY agent runs end-to-end on case 28 (`U105232867`) and emits a `case.json` whose `applicant` block matches what v8.4 produced (or is provably better — e.g., catches the `Common Application Form` content that v8.4 misrouted).

Phase 6 success = all 15 cases in `output_itr1_15/` (from v8.4) re-render via v9 with field-population counts ≥ v8.4's, zero crashes.

## 12. Open questions parked for next session

- **Tool use vs single-shot prompts**: Anthropic SDK supports tool_use loops. Right now agents are single-shot. May upgrade if reasoning quality demands it.
- **Multi-page PDF handling**: v8.4 converts PDFs to images for the vision pass. v9 reuses the same `prepare_doc()` utility — reasonable for now, but token cost grows linearly with pages.
- **Caching**: system prompts now request ephemeral provider caching. Measure actual cache usage rather than assuming savings; see [token controls](TOKEN_USAGE.md).
- **Streaming reconciliation**: agents currently return then master reconciles. Could stream partial results to master for live observation. Not needed for Phase 0.
