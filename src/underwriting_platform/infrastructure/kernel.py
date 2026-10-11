"""Adapters reuse the regression-tested kernel; no policy rules in HTTP handlers."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import time
from pathlib import Path

from underwriting_platform import __version__
from underwriting_platform.application.context import request_id
from underwriting_platform.application.errors import DependencyUnavailable, MissingCase
from underwriting_platform.configuration.paths import REFERENCE_ROOT
from underwriting_platform.configuration.settings import Settings
from underwriting_platform.contracts.v1 import (
    ArtifactResponse,
    DecisionRequest,
    DecisionResponse,
    ExtractionRequest,
    ExtractionResponse,
)
from underwriting_platform.infrastructure.files import (
    atomic_json,
    case_directory,
    fingerprint,
    native_path,
    scoped_path,
)

logger = logging.getLogger(__name__)


class KernelUnderwriter:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def decide(self, request: DecisionRequest) -> DecisionResponse:
        return await asyncio.to_thread(self._decide, request)

    def _decide(self, request: DecisionRequest) -> DecisionResponse:
        from underwriting_platform.engine import run_engine

        try:
            rules = json.loads(
                (self.settings.config_root / "uw_rules.json").read_text(encoding="utf8")
            )
            sbi_rules = json.loads(
                (self.settings.config_root / "sbi_smart_shield_rules.json").read_text(
                    encoding="utf8"
                )
            )
        except (OSError, ValueError) as exc:
            raise DependencyUnavailable() from exc
        case = copy.deepcopy(request.case_json)
        from underwriting_platform.contracts.canonical import case_issues

        issues = case_issues(case)
        if issues:
            case["_contract_issues"] = issues
            case["processing_errors"] = list(case.get("processing_errors") or []) + [
                {"stage": "case_contract", "type": "InvalidCaseSchema", "retryable": False}
            ]
        sa = request.proposed_sa
        if case.get("processing_errors"):
            sa = sa or 0
        elif sa is None:
            sa = int(
                (case.get("applicant") or {}).get("sum_assured")
                or (case.get("policy_data") or {}).get("sum_assured")
                or 0
            )
        from underwriting_platform.application.completion import assess_decision

        decision = run_engine(copy.deepcopy(case), rules, proposed_sa=sa, sbi_rules=sbi_rules)
        decision["_completion"] = assess_decision(case, decision)
        decision["_normalized_input"] = case
        return DecisionResponse(case_id=request.case_id, decision=decision)


class KernelExtractor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = asyncio.Lock()

    async def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        waiting = time.monotonic()
        async with self._lock:
            started = time.monotonic()
            try:
                return await asyncio.to_thread(self._extract, request)
            finally:
                logger.info(
                    "Stage timing",
                    extra={
                        "request_id": request_id.get(),
                        "stage": "extraction",
                        "wait_seconds": started - waiting,
                        "processing_seconds": time.monotonic() - started,
                    },
                )

    def _extract(self, request: ExtractionRequest) -> ExtractionResponse:
        from underwriting_platform.configuration.secrets import load_secret_environment

        load_secret_environment()
        defaults = {
            "SBI_SCORES_XLSX": "Scores.xlsx",
            "SBI_SCORES_XLSX_TABULAR": "Scores- DRC, CIBIL, Profile.xlsx",
            "SBI_IIB_XLSX": "IIB output first lot.xlsx",
            "SBI_BUREAU_DATA_JSON": "sbi_bureau_data.json",
        }
        for key, filename in defaults.items():
            candidate = REFERENCE_ROOT / filename
            if candidate.is_file() and not os.getenv(key):
                os.environ[key] = str(candidate)
        try:
            folder = case_directory(self.settings.case_root, request.case_id)
            digest = fingerprint(folder)
        except MissingCase:
            return ExtractionResponse(
                case_id=request.case_id,
                input_fingerprint="missing-source",
                case_json={
                    "case_id": request.case_id,
                    "routing": {"insurer": "SBI Life", "product": "Smart Shield Plus"},
                    "processing_errors": [
                        {
                            "stage": "source_documents",
                            "type": "MissingDocuments",
                            "retryable": False,
                        }
                    ],
                },
                extraction_report={"source_available": False},
            )
        from underwriting_platform.configuration.extraction import cache_revision

        revision = cache_revision(digest, self.settings.config_root)
        from underwriting_platform.infrastructure.evidence import validate_intake_size

        validate_intake_size(folder, self.settings.config_root)
        target = native_path(
            scoped_path(self.settings.extraction_cache, request.case_id) / f"{revision}.json"
        )
        if target.is_file():
            from underwriting_platform.contracts.evidence import uncited_applicant_fields

            cached = ExtractionResponse.model_validate_json(target.read_text(encoding="utf8"))
            if not (
                cached.case_json.get("processing_errors")
                or cached.case_json.get("extraction_findings")
                or cached.case_json.get("_contract_issues")
                or uncited_applicant_fields(cached.case_json)
            ):
                return cached
            target.unlink()
        response = asyncio.run(self._extract_async(request, folder, digest))
        from underwriting_platform.contracts.canonical import case_issues
        from underwriting_platform.contracts.evidence import uncited_applicant_fields

        issues = case_issues(response.case_json)
        if issues:
            response.case_json["_contract_issues"] = issues
            response.case_json.setdefault("processing_errors", []).append(
                {"stage": "case_contract", "type": "InvalidCaseSchema", "retryable": False}
            )
        errors = (
            response.case_json.get("processing_errors")
            or response.case_json.get("extraction_findings")
            or response.case_json.get("_contract_issues")
            or uncited_applicant_fields(response.case_json)
            or []
        )
        if (
            fingerprint(folder) != digest
            or cache_revision(digest, self.settings.config_root) != revision
        ):
            from underwriting_platform.application.errors import ExtractionFailed

            raise ExtractionFailed()
        response.case_json["_input_fingerprint"] = digest
        response.case_json["_extraction_revision"] = revision
        # Keep the extraction report once, outside canonical facts and queue JSON.
        from underwriting_platform.configuration.extraction import evidence_paths
        from underwriting_platform.infrastructure.reproducibility import retain_blob

        snapshot = target.with_suffix("") / "documents"
        snapshot.mkdir(parents=True, exist_ok=True)
        for source in folder.iterdir():
            if source.is_file() and source.suffix.lower() in {
                ".pdf",
                ".zip",
                ".jpg",
                ".jpeg",
                ".png",
                ".tif",
                ".tiff",
                ".webp",
            }:
                retain_blob(
                    source.read_bytes(),
                    snapshot / source.name,
                    self.settings.extraction_cache / ".objects",
                )
        for key, source in evidence_paths(self.settings.config_root).items():
            if source.is_file():
                safe_key = hashlib.sha256(key.encode()).hexdigest()
                retain_blob(
                    source.read_bytes(),
                    target.with_suffix("") / "references" / safe_key / source.name,
                    self.settings.extraction_cache / ".objects",
                )
        if fingerprint(snapshot) != digest:
            from underwriting_platform.application.errors import ExtractionFailed

            raise ExtractionFailed()
        if cache_revision(digest, self.settings.config_root) != revision:
            from underwriting_platform.application.errors import ExtractionFailed

            raise ExtractionFailed()
        # Partial responses retain diagnostics; they are never successful-cache hits.
        atomic_json(
            target.with_suffix(".partial.json") if errors else target,
            response.model_dump(mode="json"),
        )
        return response

    async def _extract_async(
        self, request: ExtractionRequest, folder: Path, digest: str
    ) -> ExtractionResponse:
        from underwriting_platform.engine.identity_verification import run_identity_verification
        from underwriting_platform.llm import build_client
        from underwriting_platform.orchestrator import MasterOrchestrator

        client = build_client()
        try:
            rules = json.loads(
                (self.settings.config_root / "uw_rules.json").read_text(encoding="utf8")
            )
            orchestrator = MasterOrchestrator(client, rules=rules)
            result = await orchestrator.run(folder, request.case_id)
            from underwriting_platform.llm.budget import case_scope

            prior = result.case_json.get("_token_budget") or {}
            with case_scope(
                client.cfg.token_budget_per_case, spent=prior.get("spent", 0)
            ) as budget:
                from underwriting_platform.configuration.paths import CACHE_ROOT
                from underwriting_platform.orchestrator.budget_ledger import attach_budget

                attach_budget(budget, CACHE_ROOT / "agent-stages", request.case_id)
                try:
                    await run_identity_verification(result.case_json, folder)
                    result.case_json.setdefault("_processing_stages", {})["identity"] = "completed"
                except Exception as exc:
                    from underwriting_platform.application.retry_policy import retryable_dependency

                    result.case_json.setdefault("processing_errors", []).append(
                        {
                            "stage": "identity_verification",
                            "type": type(exc).__name__,
                            "retryable": retryable_dependency(exc),
                        }
                    )
                result.case_json["_token_budget"] = budget.snapshot()
                result.extraction_report["token_budget"] = budget.snapshot()
                result.extraction_report.setdefault("provider_usage", []).extend(budget.usage)
            result.case_json["_provider_versions"] = {
                "backend": client.cfg.backend,
                "model_or_deployment": client.cfg.model,
                "api_version": client.cfg.api_version,
            }
            return ExtractionResponse(
                case_id=request.case_id,
                input_fingerprint=digest,
                case_json=result.case_json,
                extraction_report=result.extraction_report,
            )
        except FileNotFoundError as exc:
            raise MissingCase() from exc
        except Exception as exc:
            raise DependencyUnavailable() from exc
        finally:
            if client._anthropic is not None:
                await client._anthropic.close()


class KernelArtifacts:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = asyncio.Lock()

    async def render(self, request: DecisionResponse) -> ArtifactResponse:
        waiting = time.monotonic()
        async with self._lock:
            started = time.monotonic()
            try:
                return await asyncio.to_thread(self._render, request)
            finally:
                logger.info(
                    "Stage timing",
                    extra={
                        "request_id": request_id.get(),
                        "stage": "artifacts",
                        "wait_seconds": started - waiting,
                        "processing_seconds": time.monotonic() - started,
                    },
                )

    def _render(self, request: DecisionResponse) -> ArtifactResponse:
        from underwriting_platform.application.completion import assess_decision

        payload = copy.deepcopy(request.decision)
        payload["_completion"] = assess_decision(payload.get("_normalized_input") or {}, payload)
        request = DecisionResponse(case_id=request.case_id, decision=payload)

        content = json.dumps(request.decision, sort_keys=True, allow_nan=False, default=str)
        revision = hashlib.sha256(__version__.encode())
        revision.update(content.encode())
        revision.update((self.settings.config_root / "brand_profile_sbi.json").read_bytes())
        artifact_id = f"{request.case_id}_{revision.hexdigest()[:24]}"
        folder = scoped_path(self.settings.artifact_root, artifact_id, artifact=True)
        manifest = folder / "manifest.json"
        if manifest.is_file():
            return ArtifactResponse.model_validate_json(manifest.read_text(encoding="utf8"))
        from underwriting_platform.infrastructure.delivery import process_case

        case = request.decision.get("_normalized_input") or {
            "case_id": request.case_id,
            "_contract_issues": ["CASE_INPUT_MISSING"],
        }
        report = {}
        source_directory = None
        extraction_revision = case.get("_extraction_revision")
        if extraction_revision:
            if (
                not isinstance(extraction_revision, str)
                or len(extraction_revision) != 64
                or any(c not in "0123456789abcdef" for c in extraction_revision)
            ):
                raise DependencyUnavailable()
            from underwriting_platform.infrastructure.evidence import (
                export_evidence,
                receive_evidence,
            )

            cache_folder = scoped_path(self.settings.extraction_cache, request.case_id)
            if (cache_folder / f"{extraction_revision}.json").is_file() or (
                cache_folder / f"{extraction_revision}.partial.json"
            ).is_file():
                # Explicit local development can use the same host without a volume mount.
                evidence = export_evidence(
                    self.settings.extraction_cache, request.case_id, extraction_revision
                )
            else:
                import httpx

                response = httpx.get(
                    self.settings.extraction_url
                    + f"/v1/extractions/{request.case_id}/{extraction_revision}/evidence",
                    headers={"Authorization": f"Bearer {self.settings.service_token}"},
                    timeout=60,
                )
                if response.status_code != 200:
                    if response.status_code == 413:
                        from underwriting_platform.application.errors import EvidenceTooLarge

                        raise EvidenceTooLarge()
                    raise DependencyUnavailable()
                evidence = response.json()
            snapshot = folder / "frozen-evidence"
            report = receive_evidence(
                evidence,
                snapshot,
                request.case_id,
                extraction_revision,
                case.get("_input_fingerprint"),
            )
            source_directory = snapshot / "documents"
            if fingerprint(source_directory) != case.get("_input_fingerprint"):
                raise DependencyUnavailable()
        rules = json.loads((self.settings.config_root / "uw_rules.json").read_text(encoding="utf8"))
        outcome = process_case(
            case,
            rules,
            self.settings.artifact_root,
            engine_fn=lambda *args, **kwargs: request.decision,
            extraction_report=report,
            raw_input=report,
            provider=case.get("_provider_versions"),
            source_directory=source_directory,
            config_root=self.settings.config_root,
            folder_override=folder,
            external_directory=(snapshot / "references") if extraction_revision else None,
        )
        # Transport names alias outputs from the same CLI delivery boundary.
        cid = request.case_id
        atomic_json(
            folder / "decision.json",
            json.loads((folder / f"{cid}_decision.json").read_text(encoding="utf8")),
        )
        quality = json.loads((folder / f"{cid}_qc_report.json").read_text(encoding="utf8"))
        atomic_json(folder / "qc.json", quality)
        pdf = folder / f"{cid}_case_sheet.pdf"
        if pdf.is_file():
            pdf.replace(folder / "case-sheet.pdf")
        # A failed engine/extraction still has a diagnostic INCOMPLETE PDF.
        # Only unavailable rendering/QC infrastructure prevents artifact creation.
        if not (folder / "case-sheet.pdf").is_file() or any(
            f.get("code") == "QC_INFRASTRUCTURE_FAILURE" for f in quality.get("findings", [])
        ):
            raise DependencyUnavailable()
        response = ArtifactResponse(
            case_id=cid,
            artifact_id=artifact_id,
            qc_status=quality["status"],
            qc_summary=quality.get("summary") or {},
            download_path=f"/v1/artifacts/{artifact_id}/case-sheet",
            diagnostic_download_path=None,
            delivery_allowed=outcome["delivery_allowed"],
            diagnostic_verdict=outcome["diagnostic_verdict"],
        )
        atomic_json(manifest, response.model_dump(mode="json"))
        return response
