"""Local document survey, configured extraction, reference joins and audit output.

Native text and local OCR drive content classification/routing. Domain mode uses
domain extraction agents; experimental source-once partitions one owning reader.
Carrier decisions remain in the deterministic engine. Unknown files are retained.
"""

from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import os
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Type

from underwriting_platform.llm import LLMClient, Document, prepare_doc
from underwriting_platform.llm.document_reuse import collapse_identical_documents, is_face_photo
from underwriting_platform.agents import (
    BaseAgent,
    IdentityAgent,
    IncomeAgent,
    AssetsLiabilitiesAgent,
    ComplianceAgent,
    BusinessSurrogateAgent,
    MedicalAgent,
    AgentResult,
)
from .reconciler import LLMReconciler, ReconciliationResult
from underwriting_platform.engine.routing import is_sbi_case

logger = logging.getLogger(__name__)

_SUPPORTED_DOC_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}


def _merge_field_values(a, b):
    """Merge one field's value across two batches of the same agent's
    output — None/empty on either side loses to the other's real value;
    two dicts merge key-by-key recursively; two lists concatenate with
    exact-duplicate items dropped; two different non-empty scalars keep
    `a` (the earlier, higher-priority batch per _batch_docs's own
    ordering — see _merge_agent_results)."""
    if a in (None, "", [], {}):
        return b
    if b in (None, "", [], {}):
        return a
    if isinstance(a, dict) and isinstance(b, dict):
        merged = dict(a)
        for k, v in b.items():
            merged[k] = _merge_field_values(merged[k], v) if k in merged else v
        return merged
    if isinstance(a, list) and isinstance(b, list):
        out = list(a)
        for item in b:
            if item not in out:
                out.append(item)
        return out
    return a


def _merge_agent_results(results: list[AgentResult]) -> AgentResult:
    """Combine the AgentResult from each doc-batch of the SAME agent run
    (see MasterOrchestrator._batch_docs) into the one result the rest of
    the pipeline expects — every batch's block/contradictions/docs_consumed
    is kept, none of it silently dropped the way truncating to a single
    capped batch would. Batches are passed in priority order (batch 1 =
    the agent's own highest-ranked documents), so a genuine conflict
    between batches keeps batch 1's value — the common case is one batch
    filling in a field the other left null, not two batches actively
    disagreeing."""
    if len(results) == 1:
        return results[0]
    merged_block: dict = {}
    docs_consumed: list[str] = []
    contradictions: list[dict] = []
    notes_parts: list[str] = []
    confidences: list[float] = []
    for i, r in enumerate(results):
        merged_block = _merge_field_values(merged_block, r.block) if merged_block else dict(r.block)
        for fn in r.docs_consumed:
            if fn not in docs_consumed:
                docs_consumed.append(fn)
        contradictions.extend(r.contradictions)
        if r.notes:
            notes_parts.append(f"[batch {i + 1}/{len(results)}] {r.notes}")
        confidences.append(r.confidence)
    return AgentResult(
        block=merged_block,
        confidence=(sum(confidences) / len(confidences)) if confidences else 0.0,
        notes=" ".join(notes_parts),
        contradictions=contradictions,
        docs_consumed=docs_consumed,
        field_sources={
            key: [s for r in results for s in r.field_sources.get(key, [])]
            for key in {k for r in results for k in r.field_sources}
        },
        raw_payload={"batches": [r.raw_payload for r in results]},
        source_findings=[finding for result in results for finding in result.source_findings],
        error=next((r.error for r in results if r.error), None),
        retryable=all(r.retryable for r in results if r.error),
    )


def _file_hash(path: Path) -> str:
    """Hash complete evidence; unreadable input must not be silently discarded."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _collect_case_files(case_dir: Path, tmp_dir: Path) -> list[Path]:
    """Return all processable PDF/image paths for a case.

    Walks ``case_dir``, extracts every ``*.zip`` into a per-zip subfolder
    of ``tmp_dir`` (avoids member-name collisions), then deduplicates the
    combined list by content hash. Loose files take precedence over ZIP
    members on collision.
    """
    seen: set[str] = set()
    collected: list[Path] = []

    def _add(p: Path) -> None:
        if p.suffix.lower() not in _SUPPORTED_DOC_EXTS:
            return
        digest = _file_hash(p)
        if not digest or digest in seen:
            return
        seen.add(digest)
        collected.append(p)

    for f in sorted(case_dir.iterdir()):
        if f.is_file() and f.suffix.lower() != ".zip":
            _add(f)

    n_zips = 0
    n_zip_members = 0
    for zpath in sorted(case_dir.glob("*.zip")):
        n_zips += 1
        zip_dest = tmp_dir / zpath.stem
        zip_dest.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zpath, "r") as z:
                for member in z.namelist():
                    if Path(member).suffix.lower() not in _SUPPORTED_DOC_EXTS:
                        continue
                    z.extract(member, zip_dest)
                    extracted = zip_dest / member
                    if extracted.is_file():
                        n_zip_members += 1
                        _add(extracted)
        except (zipfile.BadZipFile, OSError) as e:
            logger.warning("Failed to extract ZIP %s: %s", zpath.name, e)

    if n_zips:
        logger.info(
            "ZIP extraction: %d archive(s) → %d member(s); collected %d unique files (loose+zip after dedup)",
            n_zips,
            n_zip_members,
            len(collected),
        )
    return collected


# Registry — every available agent class keyed by AGENT_NAME.
_AGENT_REGISTRY: dict[str, Type[BaseAgent]] = {
    "identity": IdentityAgent,
    "income": IncomeAgent,
    "assets_liabilities": AssetsLiabilitiesAgent,
    "compliance": ComplianceAgent,
    "business_surrogate": BusinessSurrogateAgent,
    "medical": MedicalAgent,
}
# Aliases the survey prompt may emit:
_AGENT_REGISTRY["business_insurance"] = BusinessSurrogateAgent


@dataclass
class OrchestratorResult:
    case_json: dict
    extraction_report: dict
    survey: dict | None = None
    agent_outputs: dict = field(default_factory=dict)
    reconciliation: ReconciliationResult | None = None


class MasterOrchestrator:
    """Coordinates per-case extraction across domain agents."""

    def __init__(
        self,
        llm_client: LLMClient,
        rules: dict | None = None,
        reconcile: bool = True,
        checkpoint_root: Path | None = None,
    ):
        self.llm = llm_client
        self.checkpoint_root = checkpoint_root
        self.rules = rules or {}
        self.reconcile_enabled = reconcile
        self.reconciler = LLMReconciler(llm_client) if reconcile else None

        # Optional Gemini client for dual-vendor medical extraction.
        # Built once at orchestrator init from GEMINI_API_KEY; None if not set.
        from underwriting_platform.llm import build_gemini_client

        self.gemini = (
            build_gemini_client()
            if os.getenv("UW_DUAL_VENDOR_MEDICAL", "false").lower() == "true"
            else None
        )
        if self.gemini is not None:
            logger.info(
                "Dual-vendor medical enabled: Gemini client built (%s)",
                getattr(self.gemini, "model", "?"),
            )

    async def run(self, case_dir: str | Path, case_id: str) -> OrchestratorResult:
        from contextlib import nullcontext
        from underwriting_platform.llm.budget import case_scope, TokenBudgetExceeded

        cfg = getattr(self.llm, "cfg", None)
        scope = case_scope(cfg.token_budget_per_case) if cfg is not None else nullcontext(None)
        with scope as budget:
            from underwriting_platform.orchestrator.checkpoints import StageStore
            from underwriting_platform.configuration.extraction import cache_revision
            from underwriting_platform.configuration.paths import CACHE_ROOT, CONFIG_ROOT
            from underwriting_platform.infrastructure.files import fingerprint

            store = None
            from underwriting_platform.infrastructure.evidence import validate_intake_size

            validate_intake_size(Path(case_dir), CONFIG_ROOT)
            if cfg is not None or self.checkpoint_root is not None:
                try:
                    digest = hashlib.sha256(
                        (case_id + ":" + fingerprint(Path(case_dir))).encode()
                    ).hexdigest()
                    revision = cache_revision(digest, CONFIG_ROOT)
                    store = StageStore(
                        (self.checkpoint_root or CACHE_ROOT / "agent-stages") / revision
                    )
                    if budget is not None:
                        from underwriting_platform.orchestrator.budget_ledger import attach_budget

                        attach_budget(
                            budget,
                            self.checkpoint_root or CACHE_ROOT / "agent-stages",
                            case_id,
                            store,
                        )
                except FileNotFoundError:
                    store = None
                except Exception as error:
                    from underwriting_platform.application.errors import MissingCase

                    if not isinstance(error, MissingCase):
                        raise
            try:
                result = await self._run_case(case_dir, case_id, store)
            except TokenBudgetExceeded:
                result = OrchestratorResult(
                    case_json={
                        "case_id": case_id,
                        "routing": {"insurer": "SBI Life", "product": "Smart Shield Plus"},
                        "processing_errors": [
                            {
                                "stage": "extraction",
                                "type": "LLM_TOKEN_BUDGET_EXCEEDED",
                                "retryable": False,
                            }
                        ],
                    },
                    extraction_report={
                        "case_id": case_id,
                        "error_code": "LLM_TOKEN_BUDGET_EXCEEDED",
                    },
                )
            if budget is not None:
                result.case_json["_token_budget"] = budget.snapshot()
                result.extraction_report["token_budget"] = budget.snapshot()
                result.extraction_report["provider_usage"] = list(budget.usage)
            return result

    async def _run_case(self, case_dir: str | Path, case_id: str, store=None) -> OrchestratorResult:
        case_dir = Path(case_dir)
        if not case_dir.exists():
            raise FileNotFoundError(f"Case directory not found: {case_dir}")

        # 1. Load every PDF into Documents (extracts ZIP archives first;
        # tempdir auto-cleans once prepare_doc has consumed each file).
        documents: list[Document] = []
        with tempfile.TemporaryDirectory(prefix=f"uw_zip_{case_id}_") as zip_tmp:
            for pdf in _collect_case_files(case_dir, Path(zip_tmp)):
                documents.append(prepare_doc(pdf))
        logger.info("Loaded %d documents from %s", len(documents), case_dir)
        documents, duplicate_aliases = collapse_identical_documents(documents)

        if not documents:
            raise ValueError(f"No source documents available for case {case_id}")
        # 2. Survey
        survey = store.read("survey") if store is not None else None
        if survey is None:
            survey = await self._survey(documents)
            if store is not None and not str(survey.get("notes", "")).startswith("survey-error"):
                store.write("survey", survey)
        logger.info(
            "Survey: %d files classified, agents=%s, skip=%s",
            len(survey.get("files") or []),
            survey.get("agents_to_dispatch"),
            survey.get("skip_agents"),
        )

        financial_text_kinds = {
            "itr",
            "form_16",
            "form_26as",
            "ca_computation",
            "audited_financials",
            "provisional_financials",
            "bank_statement",
            "salary_slip",
            "investment_statement",
        }
        kinds = {row.get("filename"): row.get("kind") for row in survey.get("files") or []}
        for document in documents:
            # Native text remains sufficient unless layout carries declarations.
            document.force_vision = kinds.get(document.filename) in {
                "proposal",
                "proposal_form",
                "caf",
                "medical_questionnaire",
                "aml",
                "aml_summary",
                "aml_disclosure",
                "kyc_declaration",
                "declaration",
                "disclosure_form",
            }
            if kinds.get(document.filename) in {
                "pan",
                "pan_card",
                "aadhaar",
                "aadhar",
                "id_proof",
                "identity_proof",
            }:
                from underwriting_platform.llm.pdf_utils import survey_preview

                document.images_b64 = [survey_preview(image, 1000) for image in document.images_b64]

        # 3. Plan + Dispatch
        agents_to_run = [
            a for a in (survey.get("agents_to_dispatch") or []) if a in _AGENT_REGISTRY
        ]
        if not agents_to_run:
            logger.warning("No registered agents to dispatch; defaulting to identity only.")
            agents_to_run = ["identity"] if "identity" in _AGENT_REGISTRY else []

        for mandatory in ("identity", "income", "compliance"):
            if mandatory not in agents_to_run:
                agents_to_run.append(mandatory)
        agent_outputs: dict[str, Any] = {}
        # Per-agent doc filtering: use the survey's relevance map.
        relevance_map = self._build_relevance_map(survey, [d.filename for d in documents])

        # 3a. Medical scout — deterministic pass that catches medical
        # content buried inside files the survey misclassified ("Internal
        # Correspondence", "Others document", etc.). Two signals:
        # (a) keyword hits in extracted text, (b) image-only fallback when
        # the survey didn't mark anything explicitly medical.
        scouted = self._medical_scout(documents, survey)
        scout_hits_by_agent: dict[str, set[str]] = {}
        if scouted:
            if "medical" in _AGENT_REGISTRY and "medical" not in agents_to_run:
                logger.info(
                    "Medical scout: forcing medical agent to run "
                    "(%d file(s) carry medical keywords).",
                    len(scouted),
                )
                agents_to_run.append("medical")
            existing = set(relevance_map.get("medical") or [])
            for fn in scouted:
                if fn not in existing:
                    relevance_map.setdefault("medical", []).append(fn)
                    existing.add(fn)
            scout_hits_by_agent["medical"] = set(scouted)

        # 3b. Correspondence scout — DQ / clarification / query-response docs
        # carry UW-decisive facts (residence status, income / existing-cover
        # clarifications) the survey often drops. Force them to the agents
        # that consume those facts so the clarification is actually read.
        corr_docs = self._correspondence_scout(documents)
        if corr_docs:
            logger.info(
                "Correspondence scout: %d clarification/DQ doc(s) → identity/income/compliance.",
                len(corr_docs),
            )
            for ag in ("identity", "income", "compliance"):
                if ag not in _AGENT_REGISTRY:
                    continue
                if ag not in agents_to_run:
                    agents_to_run.append(ag)
                existing = set(relevance_map.get(ag) or [])
                for fn in corr_docs:
                    if fn not in existing:
                        relevance_map.setdefault(ag, []).append(fn)
                        existing.add(fn)
                scout_hits_by_agent.setdefault(ag, set()).update(corr_docs)

        async def _run_one(agent_name: str):
            agent_cls = _AGENT_REGISTRY[agent_name]
            agent = agent_cls(self.llm)
            # An explicitly empty plan means missing evidence, not permission to
            # resend the entire folder. Unclassified files remain eligible.
            classified = {row.get("filename") for row in survey.get("files") or []}
            relevant_files = list(relevance_map.get(agent_name) or [])
            relevant_files += [d.filename for d in documents if d.filename not in classified]
            relevant_docs = [
                d
                for d in documents
                if d.filename in relevant_files
                and not d.extraction_error
                and not is_face_photo(d, kinds.get(d.filename))
            ]
            shared_context = {}
            if agent_name == "compliance":
                # Share only evidence-backed upstream findings; declarations still
                # need direct reading because identity/income do not own them.
                shared_context = {
                    name: {
                        "block": result.block,
                        "field_sources": result.field_sources,
                        "source_findings": result.source_findings,
                        "error": result.error,
                    }
                    for name, result in agent_outputs.items()
                    if name in {"identity", "income"}
                }
                relevant_docs = [
                    d
                    for d in relevant_docs
                    if kinds.get(d.filename)
                    not in financial_text_kinds
                    | {"pan", "pan_card", "aadhaar", "aadhar", "id_proof", "identity_proof"}
                ]
            if not relevant_docs and not shared_context:
                return agent_name, AgentResult(
                    block={agent_cls.OUTPUT_BLOCK: {}},
                    error="MissingRelevantDocuments",
                    retryable=False,
                    notes="No relevant source evidence supplied; manual review required.",
                )
            # Split into priority-ordered batches of at most 8 docs each —
            # a single call can't take more than that and stay under
            # Anthropic's 100-per-request image ceiling (prepare_doc(
            # max_pages_image=12) → up to 12 images per doc, so 8 docs ≈ 96
            # images max) — and run one extraction call per batch instead
            # of truncating to the first batch and silently dropping the
            # rest. Every relevant document reaches extraction this way.
            doc_batches = self._batch_docs(
                agent_name,
                relevant_docs,
                max_count=8,
                scout_hits=scout_hits_by_agent.get(agent_name),
            )
            if not doc_batches and shared_context:
                doc_batches = [[]]
            try:
                batch_results = []
                for batch_index, batch in enumerate(doc_batches):
                    key = f"{agent_name}:{batch_index}"
                    cached = store.agent(key) if store is not None else None
                    try:
                        extracted = cached or await agent.extract(
                            batch, shared_context=shared_context, rules=self.rules
                        )
                    except Exception as error:
                        extracted = AgentResult(
                            block={agent_cls.OUTPUT_BLOCK: {}},
                            error=getattr(error, "code", type(error).__name__),
                            retryable=getattr(
                                error,
                                "retryable",
                                not isinstance(error, (ValueError, TypeError, FileNotFoundError)),
                            ),
                            notes="This batch failed; completed batches are retained.",
                        )
                    batch_results.append(extracted)
                    if store is not None and cached is None:
                        store.save_agent(key, extracted)
                    if extracted.error == "LLM_TOKEN_BUDGET_EXCEEDED":
                        break
                result = _merge_agent_results(batch_results)
                if len(doc_batches) > 1:
                    logger.info(
                        "[%s] %d docs split into %d batches, merged",
                        agent_name,
                        len(relevant_docs),
                        len(doc_batches),
                    )
                logger.info("[%s] confidence=%.2f", agent_name, result.confidence)
                return agent_name, result
            except Exception as e:
                logger.error("[%s] failed: %s", agent_name, e)

                return agent_name, AgentResult(
                    block={agent_cls.OUTPUT_BLOCK: {}},
                    confidence=0.0,
                    notes="Agent extraction failed; manual review required.",
                    error=type(e).__name__,
                    retryable=getattr(
                        e,
                        "retryable",
                        not isinstance(e, (ValueError, TypeError, FileNotFoundError)),
                    ),
                )

        mode = os.getenv("UW_EXTRACTION_MODE", "local_first")
        source_owner = None
        if mode == "local_first":
            from underwriting_platform.extraction.coordinator import extract_local_first

            owned, source_owner = await extract_local_first(self.llm, documents)
            for name in {
                "identity",
                "income",
                "compliance",
                "medical",
                "assets_liabilities",
                "business_surrogate",
            }:
                keys = (
                    ("applicant", "anti_fraud_markers")
                    if name == "identity"
                    else (
                        ("business_insurance", "credit_protect", "surrogate")
                        if name == "business_surrogate"
                        else (_AGENT_REGISTRY[name].OUTPUT_BLOCK,)
                    )
                )
                pending = [
                    path
                    for path in source_owner["unresolved"]
                    if any(path.startswith(key + ".") for key in keys)
                ]
                agent_outputs[name] = AgentResult(
                    block={key: owned.block.get(key, {}) for key in keys},
                    field_sources={
                        path: records
                        for path, records in owned.field_sources.items()
                        if any(path.startswith(key + ".") for key in keys)
                    },
                    source_findings=[
                        f
                        for f in owned.source_findings
                        if any(f.get("field", "").startswith(key + ".") for key in keys)
                    ],
                    docs_consumed=owned.docs_consumed,
                    confidence=owned.confidence,
                    error="LOCAL_EVIDENCE_REVIEW_REQUIRED" if pending else None,
                    retryable=False,
                )
        elif mode == "source_once":
            from underwriting_platform.agents.source_reader import SourceReader, source_batches

            domains = list(
                dict.fromkeys(
                    "business_surrogate" if name == "business_insurance" else name
                    for name in agents_to_run
                )
            )
            reader = SourceReader(self.llm, domains)
            selected = [
                d
                for d in documents
                if not d.extraction_error and not is_face_photo(d, kinds.get(d.filename))
            ]
            owned_results = []
            for index, batch in enumerate(source_batches(selected)):
                key = f"source_once:{index}"
                cached = store.agent(key) if store is not None else None
                try:
                    result = cached or await reader.extract(batch, rules=self.rules)
                except Exception as error:
                    result = AgentResult(
                        block={},
                        error=getattr(error, "code", type(error).__name__),
                        retryable=getattr(
                            error,
                            "retryable",
                            not isinstance(error, (ValueError, TypeError, FileNotFoundError)),
                        ),
                        notes="Source extraction failed; completed batches are retained.",
                        docs_consumed=[d.filename for d in batch],
                    )
                owned_results.append(result)
                if store is not None and cached is None:
                    store.save_agent(key, result)
                if result.error == "LLM_TOKEN_BUDGET_EXCEEDED":
                    break
            owned = (
                _merge_agent_results(owned_results)
                if owned_results
                else AgentResult(
                    block={},
                    error="MissingRelevantDocuments",
                    retryable=False,
                    notes="No usable source documents supplied.",
                )
            )
            source_owner = {
                "mode": mode,
                "documents_consumed": owned.docs_consumed,
                "batches": len(owned_results),
                "raw_payload": owned.raw_payload,
            }
            for name in domains:
                output = _AGENT_REGISTRY[name].OUTPUT_BLOCK
                keys = (
                    ("applicant", "anti_fraud_markers")
                    if name == "identity"
                    else ("business_insurance", "credit_protect", "surrogate")
                    if name == "business_surrogate"
                    else (output,)
                )
                error = owned.error
                if name in {"identity", "income", "compliance"} and not isinstance(
                    owned.block.get(output), dict
                ):
                    error = error or "MissingCanonicalBlock"
                agent_outputs[name] = AgentResult(
                    block={k: owned.block[k] for k in keys if k in owned.block},
                    confidence=owned.confidence,
                    notes=owned.notes,
                    contradictions=owned.contradictions,
                    docs_consumed=owned.docs_consumed,
                    field_sources={
                        k: v
                        for k, v in owned.field_sources.items()
                        if any(k.startswith(prefix + ".") for prefix in keys)
                    },
                    source_findings=owned.source_findings,
                    error=error,
                    retryable=owned.retryable if error else False,
                )
        elif mode == "domain":
            results = await asyncio.gather(
                *[_run_one(name) for name in agents_to_run if name != "compliance"]
            )
            for name, result in results:
                agent_outputs[name] = result
            if "compliance" in agents_to_run:
                name, result = await _run_one("compliance")
                agent_outputs[name] = result
        else:
            raise ValueError("Unsupported extraction mode")

        # 4a. Deterministic merge — last writer wins per top-level block
        case_json = self._reconcile(case_id, agent_outputs)
        case_json["_document_classification"] = {
            "provider": survey.get("provider"),
            "classifier_version": survey.get("classifier_version"),
            "provider_tokens": 0,
            "files": [
                {"filename": row["filename"], "pages": row.get("pages", [])}
                for row in survey.get("files", [])
            ],
        }
        case_json["_extraction_mode"] = mode
        case_json["field_sources"] = {
            key: [s for r in agent_outputs.values() for s in r.field_sources.get(key, [])]
            for key in {k for r in agent_outputs.values() for k in r.field_sources}
        }
        case_json["_processing_stages"] = {"extraction": "completed"}
        case_json["extraction_findings"] = [
            {"agent": name, **finding}
            for name, result in agent_outputs.items()
            for finding in result.source_findings
        ]
        app = case_json.get("applicant") or {}
        if mode == "source_once":
            from underwriting_platform.agents.native_business import reconcile_native_business

            case_json["_native_business_adjustments"] = reconcile_native_business(
                case_json, documents, kinds
            )
        app["declared_occupation"] = app.get("occupation")
        app["declared_business_nature"] = app.get("business_nature")
        # This deployed pipeline is explicitly SBI Smart Shield Plus. The rule path
        # remains SBI even when its bureau join is missing or fails.
        case_json["routing"] = {"insurer": "SBI Life", "product": "Smart Shield Plus"}
        case_json["processing_errors"] = [
            {"stage": name, "type": result.error, "retryable": result.retryable}
            for name, result in agent_outputs.items()
            if result.error
        ]
        if any(doc.extraction_error for doc in documents):
            case_json["processing_errors"].append(
                {"stage": "document_preparation", "type": "UnreadableDocument", "retryable": False}
            )

        # 4a.0.0. Policy_Data.xlsx join — system-of-record nominee details
        # (and a few internal-compare fields). The xlsx is keyed by
        # PLCY_NUM = case_id. Skipped silently when POLICY_DATA_XLSX is
        # unset or the case isn't in the sheet — the agents already do
        # best-effort PDF extraction for the same fields.
        try:
            from underwriting_platform.engine.policy_data import lookup as _policy_lookup

            pd_row = _policy_lookup(case_id)
            if pd_row:
                case_json["nominee"] = {
                    "nominee_name": pd_row.get("nominee_name"),
                    "nominee_relation": pd_row.get("nominee_relation"),
                    "source": pd_row.get("_source_xlsx"),
                    "_internal_compare": {
                        "insured_income": pd_row.get("insured_income"),
                        "sum_assured": pd_row.get("sum_assured"),
                        "insured_name": pd_row.get("insured_name"),
                        "app_status": pd_row.get("app_status"),
                    },
                }
                # Full policy-system view — surfaced for the renderer's
                # Policy System Data section, the rider-opted HLV gate,
                # the ACM bureau-scores read, and the issued/postponed
                # ground-truth signal we benchmark our verdict against.
                case_json["policy_data"] = pd_row
                # HHW derivation — when the xlsx doesn't carry the flag
                # (403_POC_cases.xlsx case), set it from the applicant's
                # occupation per client guidance 2026-05-26. Identity
                # agent has already run by this point.
                if pd_row.get("hhw_flag") in (None, ""):
                    applicant_occ = (
                        ((case_json.get("applicant") or {}).get("occupation") or "").strip().lower()
                    )
                    is_homemaker = applicant_occ in ("homemaker", "housewife", "house wife")
                    pd_row["hhw_flag"] = "Y" if is_homemaker else "N"
                    pd_row["hhw_flag_source"] = (
                        "derived from applicant.occupation"
                        if is_homemaker
                        else "defaulted N (occupation not Homemaker)"
                    )
                logger.info(
                    "Policy_Data join: nominee=%r relation=%r rider=%r app_status=%r hhw=%r",
                    pd_row.get("nominee_name"),
                    pd_row.get("nominee_relation"),
                    pd_row.get("health_rider_flag"),
                    pd_row.get("app_status"),
                    pd_row.get("hhw_flag"),
                )
        except Exception as e:
            logger.warning("Policy_Data join failed: %s", e)

        # 4a.0.1b. SBI Life bureau join (Financial UW POC) — CIBIL score,
        # DRC category, Profile Score (Scores.xlsx, already-structured —
        # engine/scores_xlsx.py, no LLM needed) and IIB prior-policy rows
        # (IIB screenshots, vision-extracted live by agents.BureauAgent —
        # no copying into Input_Docs, reads the docx directly). Falls back
        # to the static config/sbi_bureau_data.json snapshot (hand-verified
        # for the first 2 cases) if either live source is unavailable —
        # e.g. env vars unset. No-ops entirely for any case with neither
        # source — zero impact on the TATA AIA / BALIC flow.
        try:
            from underwriting_platform.engine.scores_xlsx import (
                lookup_scores,
                lookup_scores_tabular,
            )
            from underwriting_platform.engine.sbi_bureau import (
                lookup_bureau as _lookup_static_bureau,
            )
            from underwriting_platform.engine.sbi_iib_xlsx import lookup_iib_xlsx

            # scores_xlsx has two sibling formats depending on which batch a
            # case came from: one-sheet-per-case (original) or one row per
            # case in a single tabular sheet (SBI_SCORES_XLSX_TABULAR, a
            # later batch). A case_id is only ever in one of them, so
            # trying both and taking whichever hits is safe.
            scores_rec = lookup_scores(case_id) or lookup_scores_tabular(case_id)

            static_rec = _lookup_static_bureau(case_id)
            bureau_rec: dict = dict(static_rec) if static_rec else {}
            if scores_rec:
                bureau_rec.update(scores_rec)

            # IIB similarly has two sibling sources: the original docx of
            # bureau screenshots (vision-extracted, agents.BureauAgent) and
            # a later batch's already-structured xlsx export
            # (engine.sbi_iib_xlsx, SBI_IIB_XLSX — deterministic, no LLM
            # call). Prefer the structured source when this case is in it
            # — skips an unnecessary vision call entirely for that case.
            iib_xlsx_policies = lookup_iib_xlsx(case_id)
            if iib_xlsx_policies is not None:
                bureau_rec["iib_policies"] = iib_xlsx_policies
                bureau_rec["_iib_extraction_notes"] = (
                    "Structured IIB xlsx export (engine.sbi_iib_xlsx) — deterministic read, no vision extraction."
                )
                bureau_rec["_iib_extraction_confidence"] = 1.0
                live_iib_source = True
            elif mode != "local_first":
                from underwriting_platform.agents import BureauAgent

                bureau_agent = BureauAgent(self.llm)
                bureau_result = await bureau_agent.extract_for_case(case_id)
                iib_block = bureau_result.block.get("sbi_bureau")
                live_iib_source = bool(iib_block and iib_block.get("iib_policies") is not None)
                if live_iib_source:
                    bureau_rec["iib_policies"] = iib_block["iib_policies"]
                    bureau_rec["_iib_extraction_notes"] = iib_block.get("extraction_notes")
                    bureau_rec["_iib_extraction_confidence"] = bureau_result.confidence

            if bureau_rec:
                case_json["sbi_bureau"] = bureau_rec
                logger.info(
                    "SBI bureau join: case=%s cibil=%s drc=%s profile=%s iib_policies=%s (live=%s)",
                    case_id,
                    bureau_rec.get("cibil_score"),
                    bureau_rec.get("drc_category"),
                    bureau_rec.get("profile_score"),
                    len(bureau_rec.get("iib_policies") or []),
                    bool(scores_rec or live_iib_source),
                )
        except Exception as e:
            logger.warning("SBI bureau join failed: %s", e)

        # 4a.0.1. IIB / prior-policy join — Datamatics LAYOUT row 31.
        # Loader is a stub today (project-iib-history-gap); when feed
        # lands, set IIB_DATA_JSON env to the export path and the join
        # populates case_json["iib"] automatically. Skipped entirely when
        # the SBI bureau join above already populated sbi_bureau — running
        # this TATA-only stub against an SBI case is always a no-op
        # (config/iib_sample.json is empty) and just adds log noise.
        if not is_sbi_case(case_json):
            try:
                from underwriting_platform.engine.iib import lookup_iib

                applicant_pan = (case_json.get("applicant") or {}).get("pan")
                iib_result = lookup_iib(pan=applicant_pan, policy_number=case_id)
                case_json["iib"] = iib_result.to_dict()
                if iib_result.has_data:
                    logger.info(
                        "IIB join: matched_by=%s · %d policies · total_sa=%s",
                        iib_result.matched_by,
                        len(iib_result.policies),
                        iib_result.summary.get("total_sum_assured"),
                    )
            except Exception as e:
                logger.warning("IIB join failed: %s", e)

        # 4a.0.2. Alpha Policy join — Datamatics LAYOUT rows 20/36/53.
        # TALIC-only prior-policy ledger. Loader is a stub; when ledger
        # lands, set ALPHA_POLICY_JSON env var. Falls back to the
        # legacy HHW link signal from Policy_Data.xlsx. Same skip-for-SBI
        # reasoning as the IIB join above — this is a TATA-only stub.
        if not is_sbi_case(case_json):
            try:
                from underwriting_platform.engine.alpha_policy import lookup_alpha

                applicant_pan = (case_json.get("applicant") or {}).get("pan")
                hhw_link = (case_json.get("policy_data") or {}).get("hhw_plcy_num")
                alpha_result = lookup_alpha(pan=applicant_pan, hhw_plcy_num=hhw_link)
                case_json["alpha_policy"] = alpha_result.to_dict()
                if alpha_result.has_data:
                    logger.info(
                        "Alpha Policy join: matched_by=%s · %d policies",
                        alpha_result.matched_by,
                        len(alpha_result.policies),
                    )
            except Exception as e:
                logger.warning("Alpha Policy join failed: %s", e)

        # 4a.0.5. Dual-vendor medical pass — when GEMINI_API_KEY is set,
        # run the medical agent again with a secondary-vendor client over
        # the same docs. Park the secondary block at
        # `case_json["medical_secondary"]` (vendor-neutral key name — the
        # reconciler emits field paths organically from this dump, so
        # using a vendor-bearing key like 'medical_gemini' would leak the
        # vendor identity into the reconciler's prose. See RULES § 15.6).
        # The field-level diff is at `case_json["medical"]["dual_vendor_diff"]`.
        # The engine flag stage turns disagreements into
        # MEDICAL_DUAL_VENDOR_DISAGREEMENT flags so UW sees both reads.
        if "medical" in agent_outputs and self.gemini is not None and not is_sbi_case(case_json):
            try:
                from underwriting_platform.agents import MedicalAgent as _MedicalAgent

                medical_docs = [
                    d
                    for d in documents
                    if d.filename
                    in (relevance_map.get("medical") or [d.filename for d in documents])
                ]
                if not medical_docs:
                    medical_docs = documents
                medical_doc_batches = self._batch_docs(
                    "medical",
                    medical_docs,
                    max_count=8,
                    scout_hits=scout_hits_by_agent.get("medical"),
                )
                gemini_agent = _MedicalAgent(self.gemini)
                gemini_batch_results = [
                    await gemini_agent.extract(batch, shared_context={}, rules=self.rules)
                    for batch in medical_doc_batches
                ]
                gemini_result = _merge_agent_results(gemini_batch_results)
                logger.info(
                    "[medical/gemini] confidence=%.2f",
                    gemini_result.confidence,
                )
                # Park the secondary block under a vendor-neutral key so
                # reconciler-emitted field paths never name the vendor.
                gem_block = (gemini_result.block or {}).get("medical") or {}
                case_json["medical_secondary"] = {
                    **gem_block,
                    "_agent_meta": {
                        "vendor": "secondary",  # vendor name kept in
                        # diff dict only (JSON
                        # provenance, not prose)
                        "model": getattr(self.gemini, "model", "?"),
                        "confidence": gemini_result.confidence,
                        "notes": gemini_result.notes,
                        "docs_consumed": gemini_result.docs_consumed,
                    },
                }
                # Field-level diff vs primary block. Pass vendor-neutral
                # labels — the diff dict is consumed by the engine flag
                # stage which uses these as the description prefix.
                from underwriting_platform.engine.medical_dual_vendor import compare_medical_blocks

                diff = compare_medical_blocks(
                    primary=(case_json.get("medical") or {}),
                    secondary=gem_block,
                    primary_vendor="primary",
                    secondary_vendor="secondary",
                )
                case_json.setdefault("medical", {})["dual_vendor_diff"] = diff
                logger.info("Dual-vendor medical: %s", diff.get("summary"))
            except Exception as e:
                logger.warning("Dual-vendor medical pass failed: %s", e)

        # 4a.1. Focused handwritten-section re-OCR — each registered
        # section (vitals, family history, …) gets a focused locate +
        # 600 DPI crop + Gemini read with a constrained schema. Results
        # are parked at `medical.<section>_reocr` for the engine flag
        # stage to compare against the primary medical agent's read.
        if "medical" in agent_outputs and not is_sbi_case(case_json):
            try:
                from underwriting_platform.engine.handwritten_reocr import (
                    reocr_all_sections_for_case,
                )

                reocr_results = await reocr_all_sections_for_case(
                    case_dir,
                    self.llm,
                    debug_dir=getattr(self, "_reocr_debug_dir", None),
                )
                medical_block = case_json.setdefault("medical", {})
                for key, result in reocr_results.items():
                    medical_block[f"{key}_reocr"] = result
                    rv = result.get("values") or {}
                    logger.info(
                        "Re-OCR [%s]: %s p%s — values=%s",
                        key,
                        result.get("source_pdf"),
                        result.get("source_page"),
                        {k: rv.get(k) for k in list(rv.keys())[:6]},
                    )
                # Per-cell M/F isolation pass for the family-history table.
                # Both vendors flip M↔F when shown both checkboxes in the
                # same crop (UAT case C308394654). The standard TALIC MER
                # layout lets us isolate each checkbox into its own tiny
                # crop so the model has no neighbour to confuse it with.
                fh = medical_block.get("family_history_reocr")
                if fh:
                    try:
                        from underwriting_platform.engine.family_checkbox import (
                            refine_family_history_with_checkbox_isolation,
                        )

                        await refine_family_history_with_checkbox_isolation(
                            fh,
                            case_dir=case_dir,
                            debug_dir=getattr(self, "_reocr_debug_dir", None),
                        )
                    except Exception as e:
                        logger.warning("Family-history checkbox isolation failed: %s", e)
            except Exception as e:
                logger.warning("Handwritten section re-OCR failed: %s", e)

        # 4b. Phase 5: LLM reconciler — cross-agent contradiction + risk-signal pass
        recon: ReconciliationResult | None = None
        if mode == "domain" and self.reconciler is not None and len(agent_outputs) >= 2:
            try:
                from dataclasses import asdict

                recon_key = (
                    "reconciliation:"
                    + hashlib.sha256(
                        json.dumps(case_json, sort_keys=True, default=str).encode()
                    ).hexdigest()
                )
                saved_recon = store.read(recon_key) if store is not None else None
                recon = (
                    ReconciliationResult(**saved_recon)
                    if saved_recon is not None
                    else await self.reconciler.reconcile(case_json, agent_outputs)
                )
                if store is not None and not recon.error:
                    store.write(recon_key, asdict(recon))
                case_json["reconciliation"] = recon.to_block()
                logger.info(
                    "Reconciler: %d contradictions, %d risk signals, stp=%s, conf=%.2f",
                    len(recon.contradictions),
                    len(recon.risk_signals),
                    recon.stp_recommendation,
                    recon.overall_confidence,
                )
            except Exception as e:
                logger.error("Reconciler unexpected failure: %s", e)
                recon = ReconciliationResult(error=f"reconciler crashed: {e}")
                case_json["reconciliation"] = recon.to_block()

        if recon is not None and recon.error:
            case_json["processing_errors"].append(
                {
                    "stage": "reconciliation",
                    "type": "ReconciliationError",
                    "retryable": recon.retryable,
                }
            )
        if recon is not None and not recon.error:
            # The reconciler has no original documents. Suggestions are advisory,
            # never replacements for extracted facts or their source evidence.
            for path, value in recon.field_overrides.items():
                case_json["reconciliation"].setdefault("profile_override_review", []).append(
                    {
                        "field": path,
                        "assessed_candidate": value,
                        "action": "Preserve extracted fact; source-backed human review required.",
                    }
                )
            case_json["reconciliation"]["field_overrides_applied"] = False
        # 5. Build extraction report
        extraction_report = {
            "local_ocr": {
                "provider_tokens": 0,
                "documents": {d.filename: d.ocr_pages for d in documents if d.ocr_pages},
                "pages_completed": sum(
                    r.get("status") == "completed" for d in documents for r in d.ocr_pages
                ),
                "pages_failed": sum(
                    r.get("status") in {"failed", "render_limit"}
                    for d in documents
                    for r in d.ocr_pages
                ),
            },
            "source_owner": source_owner,
            "duplicate_aliases": duplicate_aliases,
            "case_id": case_id,
            "survey": survey,
            "agents": {
                name: {
                    "confidence": result.confidence,
                    "notes": result.notes,
                    "contradictions": result.contradictions,
                    "docs_consumed": result.docs_consumed,
                    "error": result.error,
                    "field_sources": result.field_sources,
                    "raw_payload": result.raw_payload,
                    "source_findings": result.source_findings,
                }
                for name, result in agent_outputs.items()
            },
            "reconciliation": (recon.to_block() if recon else None),
        }

        return OrchestratorResult(
            case_json=case_json,
            extraction_report=extraction_report,
            survey=survey,
            agent_outputs=agent_outputs,
            reconciliation=recon,
        )

    async def _survey(self, documents: list[Document]) -> dict:
        """Route native/local-OCR evidence without any model classification call."""
        from underwriting_platform.ocr.classification import survey_documents

        return survey_documents(
            documents, [name for name in _AGENT_REGISTRY if name != "business_insurance"]
        )

    # Heuristic filename hints used when the survey misses or over-includes.
    # These are NOT a classifier — they're a payload-cap tiebreaker.
    _AGENT_KEYWORDS: dict[str, list[str]] = {
        "identity": [
            "application form",
            "common application",
            "customer information",
            "proposal",
            "cif",
            "aadhaar",
            "pan",
            "passport",
            "voter",
            "driving",
            "id_proof",
            "address proof",
        ],
        "income": [
            "salary slip",
            "salary",
            "payslip",
            "pay slip",
            "form 16",
            "form_16",
            "appointment letter",
            "offer letter",
            "joining",
            "income proof",
            "itr",
            "form 26as",
            "form_26",
            "ca computation",
            "audited",
            "p&l",
            "profit",
            "balance sheet",
            "financial statement",
        ],
        "assets_liabilities": [
            "bank statement",
            "passbook",
            "property",
            "sale deed",
            "loan",
            "emi",
            "cibil",
            "investment",
            "mf",
            "equity",
            "credit card",
            "fd",
            "fixed deposit",
        ],
        "compliance": [
            "aml",
            "sanction",
            "pep",
            "kyc",
            "id_proof",
            "aadhaar",
            "pan",
            "common application",
            "customer information",
            "beneficial",
            "declaration",
            "passport",
        ],
        "medical": [
            "bmi",
            "vendor medical",
            "ecg",
            "echo",
            "2d echo",
            "doctor",
            "discharge",
            "hospital",
            "cardiologist",
            "fitness certificate",
            "lab report",
            "pathology",
            "radiology",
            "surgery",
            "hernia",
            "diabetes",
            "hypertension",
            "x-ray",
            "ultrasound",
            "scan",
            "biopsy",
            "consultation",
            "physician",
        ],
        "business_insurance": [
            "partnership",
            "keyman",
            "huf",
            "shareholding",
            "audited",
            "balance sheet",
            "p&l",
            "moa",
            "aoa",
            "trust deed",
            "board resolution",
            "company",
        ],
        "business_surrogate": [
            "partnership",
            "keyman",
            "huf",
            "shareholding",
            "audited",
            "balance sheet",
            "p&l",
            "trust deed",
            "board resolution",
            "vehicle",
            "rc book",
            "idv",
            "loan sanction",
            "disbursement",
            "sip",
            "credit card",
            "investment statement",
        ],
    }

    # Tier-1 keywords for the cap's tiebreaker — match the names of the
    # required medical tests in the gating grid. A file whose name signals
    # one of these (e.g. "TMT reports done by TPA.pdf") gets weighted ×3
    # so it beats general medical content (discharge summary, doctor cert)
    # when the per-agent doc cap forces a choice.
    # The Financial Evaluation batch (2026-09-29) names its proposal form
    # "<case_id>_P01.pdf" — an abbreviation none of the identity/income/
    # compliance regular keywords above recognise (they match on the word
    # "proposal", not "p01"). A case with >8 identity-relevant files (very
    # common — individual PAN/Aadhaar/photo/PIVC images routinely add up
    # to a dozen+) could then have the proposal form itself — the single
    # richest source for sum assured, premium, product, nominees, marital
    # status, education, and prior-decline disclosure — lose the doc cap
    # to a pile of small ID-photo scans that happen to match "pan"/
    # "aadhaar" in the regular list, even though none of them carry that
    # data at all. Found via case SYNTHETIC_CASE (2026-10-01): the proposal
    # form was present and correctly flagged "identity"-relevant by the
    # survey, but never reached the identity/compliance agents — every
    # plan field came back null. "p01" is kept alongside "proposal" so
    # this still works for a differently-named batch too.
    _AGENT_PRIORITY_KEYWORDS: dict[str, list[str]] = {
        "medical": [
            "tmt",
            "mer",
            "medical examination",
            "bpb",
            "blood pressure",
            "cbc",
            "blood report",
            "blood test",
            "hba1c",
            "msu",
            "midstream urine",
            "urine cotinine",
            "cotinine",
            "vmer",
        ],
        "identity": ["p01", "proposal", "caf", "common proposal form"],
        "income": ["p01", "proposal", "caf", "common proposal form"],
        "compliance": ["p01", "proposal", "caf", "common proposal form"],
    }

    # Keywords that strongly imply a document carries financial-UW-relevant
    # medical content. We require at least 2 distinct hits in a doc's text
    # before forcing it into the medical agent's queue, to avoid false
    # positives from incidental mentions ("alcohol declaration" on a CAF).
    _MEDICAL_KEYWORDS: tuple[str, ...] = (
        # Vitals + body
        "height",
        "weight",
        "bmi",
        "blood pressure",
        "pulse",
        # Hematology / Pathology
        "haemoglobin",
        "hemoglobin",
        "rbc",
        "wbc",
        "platelet",
        "esr",
        "lipid profile",
        "cholesterol",
        "ldl",
        "hdl",
        "triglyceride",
        # Liver
        "sgpt",
        "sgot",
        "alt ",
        "ast ",
        "bilirubin",
        "alkaline phosphatase",
        # Kidney
        "creatinine",
        "bun ",
        "blood urea",
        # Metabolic + serology
        "hba1c",
        "glycated",
        "rbs ",
        "fbs ",
        "fasting blood",
        "hbsag",
        "hepatitis",
        "anti hcv",
        "anti-hcv",
        "hiv",
        # Urine + cotinine
        "urine examination",
        "urine analysis",
        "urine albumin",
        "specific gravity",
        "ketone",
        "cotinine",
        # Cardiac
        "ecg",
        "electrocardiogram",
        "tmt",
        "treadmill",
        "echocardiogram",
        # Habits / fraud markers
        "smoker",
        "smoking",
        "cigarette",
        "tobacco",
        "bidi",
        "hookah",
        "alcohol",
        "whisky",
        "beer",
        "drink",
        # Common surgical + chronic history phrasing
        "surgery",
        "surgical",
        "operated",
        "hernio",
        "covid",
        "diabetes",
        "hypertension",
        "dyslipidemia",
        "thyroid",
        # MER / VMER markers
        "medical examination report",
        "video mer",
        "vmer",
        "tele medical",
        "tele-medical",
    )

    # Survey kinds that explicitly mean "medical" — if any file is tagged
    # with one of these we trust the survey and don't scout further.
    _MEDICAL_KINDS: tuple[str, ...] = (
        "mer",
        "vmer",
        "lab_report",
        "urine_analysis",
        "ecg_report",
        "tmt_report",
        "hba1c_report",
        "medical_report",
    )

    # Survey kinds that are HARD non-medical (pure single-purpose docs that
    # never come bundled with medical scans). The image-only scout skips
    # these even when no explicit medical kind was found. Everything else
    # — including proposal_form, caf, kyc_declaration, corporate_doc — is
    # fair game because in real cases medical reports routinely arrive
    # bundled inside generically-named PDFs.
    _NON_MEDICAL_KINDS: tuple[str, ...] = (
        "salary_slip",
        "form_16",
        "form_26as",
        "ca_computation",
        "bank_statement",
        "id_proof",
        "address_proof",
        "vehicle_rc",
        "credit_card_statement",
        "investment_statement",
        "cibil_report",
    )

    # Underwriting-correspondence filename hints: discrepancy queries (DQ),
    # clarifications, query responses. These PDFs carry UW-decisive facts
    # (residence status, income / existing-cover clarifications) that the
    # survey routinely classifies as irrelevant and drops from every agent.
    _CORRESPONDENCE_HINTS = ("clarif", "correspond", "discrepan")

    def _correspondence_scout(self, documents: list[Document]) -> list[str]:
        """Return filenames that look like UW correspondence (DQ /
        clarification / query response). These are routed to the identity /
        income / compliance agents so the clarified facts inform extraction
        (e.g. NRI part-year residence explaining a low India ITR income)."""
        out: list[str] = []
        for d in documents:
            name = (d.filename or "").lower()
            if not name:
                continue
            toks = name.replace(".", " ").replace("-", " ").replace("_", " ").split()
            if (
                any(h in name for h in self._CORRESPONDENCE_HINTS)
                or "dq" in toks
                or "query" in toks
            ):
                out.append(d.filename)
        return out

    def _medical_scout(self, documents: list[Document], survey: dict | None = None) -> list[str]:
        """Return filenames likely to carry medical content that the survey
        missed. Two signals:

        1. **Text keyword scan** — ≥2 distinct medical keywords in extracted
           text. Catches typed medical reports buried inside misnamed files.
        2. **Image-only fallback** — when the survey did NOT classify any
           file as a medical kind, every file that has no extractable text
           AND isn't a definite non-medical kind (id proof, salary slip,
           bank statement, etc.) becomes a medical-scout candidate. This
           covers the very common case where MER / lab reports are scanned
           images dropped into "Internal Correspondence.pdf".
        """
        out: list[str] = []
        survey_files = (survey or {}).get("files") or []
        kinds_by_fn: dict[str, str] = {
            f.get("filename"): (f.get("kind") or "").lower()
            for f in survey_files
            if f.get("filename")
        }

        # Signal 1 — keyword text scan
        for doc in documents:
            text = (doc.text or "").lower()
            if not text:
                continue
            hit = 0
            for kw in self._MEDICAL_KEYWORDS:
                if kw in text:
                    hit += 1
                    if hit >= 2:
                        out.append(doc.filename)
                        break

        # Signal 2 — image-only fallback when survey didn't mark anything
        # as a medical kind. Pull every doc that's got images, no text,
        # AND wasn't classified as a definite non-medical kind.
        survey_has_medical = any(k in self._MEDICAL_KINDS for k in kinds_by_fn.values())
        if not survey_has_medical:
            for doc in documents:
                if doc.filename in out:
                    continue
                text_len = len((doc.text or "").strip())
                kind = kinds_by_fn.get(doc.filename, "")
                # Image-only doc not classified as a definite non-medical
                if text_len < 500 and doc.images_b64 and kind not in self._NON_MEDICAL_KINDS:
                    out.append(doc.filename)

        return out

    @staticmethod
    def _compact(s: str) -> str:
        """Strip spaces/underscores/hyphens/periods so filename keyword
        matching is separator-agnostic — "P_01.pdf", "P-01.pdf", "P 01.pdf"
        and "P01.pdf" all compact to "p01pdf". Different case batches use
        different separator conventions for the same document (e.g. the
        4CYL... batch uses "P_01.pdf" where most others use "P01.pdf") — a
        literal "p01" keyword silently misses the underscored variant,
        which is exactly how a case's own proposal form can lose the
        per-agent doc cap to files that don't carry its data at all. See
        _AGENT_PRIORITY_KEYWORDS's own comment for the original version of
        this problem (a different naming batch, fixed by adding a second
        literal keyword) — compacting both sides here generalises that fix
        instead of requiring a new literal per batch.
        """
        for ch in (" ", "_", "-", "."):
            s = s.replace(ch, "")
        return s

    def _score_docs(self, agent_name: str, docs: list, scout_hits: set[str] | None = None) -> list:
        """Rank every doc for this agent, highest-value first — same
        priority/regular-keyword + scout-bonus scoring _cap_docs always
        used, just returning the full ranked list instead of truncating
        it. _cap_docs and _batch_docs both build on this."""
        priority = [self._compact(kw) for kw in self._AGENT_PRIORITY_KEYWORDS.get(agent_name, [])]
        regular = [self._compact(kw) for kw in self._AGENT_KEYWORDS.get(agent_name, [])]
        scout_hits = scout_hits or set()
        scored = []
        for d in docs:
            fnl = self._compact(d.filename.lower())
            # Two-key sort:
            #   1. priority_hits — count of priority keyword matches in the
            #      filename. Files with ANY priority hit (e.g. "TMT reports
            #      done by TPA.pdf") are unconditionally ranked above files
            #      with zero priority hits, regardless of how many regular /
            #      scout signals the latter accumulated. Required-test names
            #      are the strongest signal we have for "the underwriter
            #      cares about this file" and must not be displaced by a
            #      scout that picked up incidental medical keywords in a CAF.
            #   2. total_score — within each priority tier, rank by the
            #      remaining heuristics so the highest-value docs lead the
            #      first batch when the cap forces more than one.
            priority_hits = sum(1 for kw in priority if kw in fnl)
            regular_hits = sum(1 for kw in regular if kw in fnl)
            scout_bonus = 5 if d.filename in scout_hits else 0
            total_score = (3 * priority_hits) + regular_hits + scout_bonus
            scored.append((priority_hits, total_score, d))
        # Highest priority hits first, then highest total score.
        scored.sort(key=lambda t: (-t[0], -t[1]))
        return [d for _, _, d in scored]

    def _cap_docs(
        self, agent_name: str, docs: list, max_count: int, scout_hits: set[str] | None = None
    ) -> list:
        if len(docs) <= max_count:
            return docs
        return self._score_docs(agent_name, docs, scout_hits)[:max_count]

    def _batch_docs(
        self, agent_name: str, docs: list, max_count: int, scout_hits: set[str] | None = None
    ) -> list[list]:
        """Split every relevant doc into priority-ordered groups of at most
        max_count, instead of _cap_docs's silent truncation to one group —
        every document the survey marked relevant for this agent reaches
        extraction, just across more than one LLM call when there are more
        than max_count of them (Anthropic's 100-image-per-request ceiling
        is the reason a single call can't just take all of them at once —
        see the max_count=8 callers' own comment). Highest-ranked docs
        (per _score_docs) lead the first batch."""
        if len(docs) <= max_count:
            return [docs]
        ranked = self._score_docs(agent_name, docs, scout_hits)
        return [ranked[i : i + max_count] for i in range(0, len(ranked), max_count)]

    def _build_relevance_map(self, survey: dict, all_filenames: list[str]) -> dict[str, list[str]]:
        """Invert the survey's file->relevance map into agent->filenames."""
        out: dict[str, list[str]] = {}
        for entry in survey.get("files") or []:
            fn = entry.get("filename")
            if not fn:
                continue
            for agent_name in entry.get("relevance") or []:
                out.setdefault(agent_name, []).append(fn)
        return out

    def _reconcile(self, case_id: str, agent_outputs: dict) -> dict:
        """Phase 0: deterministic merge of agent blocks into one case_json.

        Phase 5+ will replace this with an LLM reconciler that resolves
        cross-agent contradictions explicitly.
        """
        case_json: dict[str, Any] = {"case_id": case_id}
        for agent_name, result in agent_outputs.items():
            for k, v in (result.block or {}).items():
                # Last writer wins; future: merge with LLM-resolved contradictions.
                case_json[k] = v
        return case_json
