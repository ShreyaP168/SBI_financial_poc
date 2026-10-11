"""Cache, logging, contracts, leases and paid-work retry regressions."""

import asyncio
import json
import logging
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from support.extraction_fixture import synthetic_case

from underwriting_platform.application.completion import assess_decision
from underwriting_platform.application.context import request_id
from underwriting_platform.application.worker import Worker
from underwriting_platform.configuration.extraction import EXTERNAL_SOURCES
from underwriting_platform.configuration.settings import Settings
from underwriting_platform.contracts.v1 import (
    ArtifactResponse,
    CaseRequest,
    DecisionRequest,
    DecisionResponse,
    ExtractionRequest,
    ExtractionResponse,
    JobStatus,
    WorkflowResult,
)
from underwriting_platform.engine import run_engine
from underwriting_platform.infrastructure.jobs import RelationalJobRepository
from underwriting_platform.infrastructure.kernel import KernelArtifacts, KernelExtractor
from underwriting_platform.llm.client import LLMClient, LLMConfig, LLMResponseRejected
from underwriting_platform.orchestrator.master import _file_hash
from underwriting_platform.services.gateway import build_app


async def test_configuration_referenced_template_invalidates_cache(tmp_path, monkeypatch):
    from underwriting_platform.configuration.extraction import cache_revision

    config = tmp_path / "config"
    config.mkdir()
    template = tmp_path / "template.pdf"
    template.write_bytes(b"template version one")
    (config / "atlas.json").write_text(json.dumps({"_template_pdf": str(template)}))
    first = cache_revision("same-PDFs", config)
    template.write_bytes(b"template version two")
    assert cache_revision("same-PDFs", config) != first


async def test_service_delivery_retains_frozen_sources_and_fingerprint(tmp_path, monkeypatch):
    from underwriting_platform.infrastructure.kernel import KernelUnderwriter

    settings = local_settings(tmp_path)
    folder = settings.case_root / "A"
    folder.mkdir(parents=True)
    source = folder / "proposal.pdf"
    source.write_bytes(b"frozen source version one")
    extractor = KernelExtractor(settings)

    async def extract(request, folder, digest):
        return ExtractionResponse(
            case_id="A",
            input_fingerprint=digest,
            case_json=synthetic_case("A"),
            extraction_report={"agents": {"raw_payload": "synthetic private evidence"}},
        )

    monkeypatch.setattr(extractor, "_extract_async", extract)
    extracted = await extractor.extract(ExtractionRequest(case_id="A"))
    source.write_bytes(b"changed after extraction")
    decision = await KernelUnderwriter(settings).decide(
        DecisionRequest(case_id="A", case_json=extracted.case_json)
    )
    artifact = await KernelArtifacts(settings).render(decision)
    output = settings.artifact_root / artifact.artifact_id
    manifest = json.loads((output / "reproduction/latest.json").read_text())
    assert manifest["input_fingerprint"] == extracted.input_fingerprint
    retained = output / manifest["bundle_path"] / "documents/proposal.pdf"
    assert retained.read_bytes() == b"frozen source version one"


def test_application_layer_has_no_kernel_or_infrastructure_imports():
    import ast
    from pathlib import Path

    import underwriting_platform

    root = Path(underwriting_platform.__file__).parent / "application"
    for source in root.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf8"))):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(
                    (
                        "underwriting_platform.engine",
                        "underwriting_platform.infrastructure",
                        "underwriting_platform.llm",
                    )
                )


def local_settings(tmp_path):
    return Settings(
        case_root=tmp_path / "cases",
        extraction_cache=tmp_path / "cache",
        artifact_root=tmp_path / "artifacts",
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        service_token="synthetic-service",
        gateway_token="synthetic-client",
    )


async def test_changed_policy_workbook_refreshes_process_local_join_index(tmp_path, monkeypatch):
    import openpyxl

    settings = local_settings(tmp_path)
    folder = settings.case_root / "A"
    folder.mkdir(parents=True)
    (folder / "proposal.pdf").write_bytes(b"unchanged proposal")
    workbook_path = tmp_path / "policy.xlsx"
    monkeypatch.setenv("POLICY_DATA_XLSX", str(workbook_path))

    def save(name):
        workbook = openpyxl.Workbook()
        workbook.active.append(["PLCY_NUM", "NOMINEE_NAME"])
        workbook.active.append(["A", name])
        workbook.save(workbook_path)
        workbook.close()

    extractor = KernelExtractor(settings)

    async def extract(request, folder, digest):
        from underwriting_platform.engine.policy_data import lookup

        case = synthetic_case("A")
        case["nominee"] = lookup("A")
        return ExtractionResponse(
            case_id="A", input_fingerprint=digest, case_json=case, extraction_report={}
        )

    monkeypatch.setattr(extractor, "_extract_async", extract)
    save("Synthetic Nominee One")
    first = await extractor.extract(ExtractionRequest(case_id="A"))
    save("Synthetic Nominee Two")
    second = await extractor.extract(ExtractionRequest(case_id="A"))
    assert first.case_json["nominee"]["nominee_name"] == "Synthetic Nominee One"
    assert second.case_json["nominee"]["nominee_name"] == "Synthetic Nominee Two"


@pytest.mark.parametrize("source_key", EXTERNAL_SOURCES)
async def test_every_external_source_invalidates_cached_joins(tmp_path, monkeypatch, source_key):
    settings = local_settings(tmp_path)
    folder = settings.case_root / "A"
    folder.mkdir(parents=True)
    (folder / "proposal.pdf").write_bytes(b"same applicant PDFs")
    for key in EXTERNAL_SOURCES:
        monkeypatch.delenv(key, raising=False)
    import underwriting_platform.infrastructure.kernel as kernel

    monkeypatch.setattr(kernel, "REFERENCE_ROOT", tmp_path / "empty-references")
    source = tmp_path / "reference.xlsx"
    source.write_bytes(b"source version one")
    monkeypatch.setenv(source_key, str(source))
    calls = []
    extractor = KernelExtractor(settings)

    async def extract(request, folder, digest):
        calls.append(source.read_bytes() if source.is_file() else b"MISSING")
        return ExtractionResponse(
            case_id="A",
            input_fingerprint=digest,
            case_json=synthetic_case("A"),
            extraction_report={},
        )

    monkeypatch.setattr(extractor, "_extract_async", extract)
    await extractor.extract(ExtractionRequest(case_id="A"))
    await extractor.extract(ExtractionRequest(case_id="A"))
    assert len(calls) == 1
    source.write_bytes(b"source version two")
    await extractor.extract(ExtractionRequest(case_id="A"))
    assert len(calls) == 2
    source.unlink()
    await extractor.extract(ExtractionRequest(case_id="A"))
    assert len(calls) == 3


async def test_failed_extraction_not_cached_and_retry_recovers(tmp_path, monkeypatch):
    settings = local_settings(tmp_path)
    folder = settings.case_root / "A"
    folder.mkdir(parents=True)
    (folder / "proposal.pdf").write_bytes(b"same source")
    extractor = KernelExtractor(settings)
    calls = []

    async def extract(request, folder, digest):
        calls.append(1)
        case = synthetic_case("A")
        if len(calls) == 1:
            case["processing_errors"] = [{"stage": "identity", "retryable": True}]
        return ExtractionResponse(
            case_id="A", input_fingerprint=digest, case_json=case, extraction_report={}
        )

    monkeypatch.setattr(extractor, "_extract_async", extract)
    partial = await extractor.extract(ExtractionRequest(case_id="A"))
    assert partial.case_json["processing_errors"]
    assert all(
        path.name.endswith(".partial.json") for path in settings.extraction_cache.rglob("*.json")
    )
    await extractor.extract(ExtractionRequest(case_id="A"))
    await extractor.extract(ExtractionRequest(case_id="A"))
    assert len(calls) == 2


def test_parse_failure_logs_only_safe_metadata(caplog):
    token = request_id.set("47d62be9-ea55-44e5-97eb-7831a47b6981")
    try:
        with caplog.at_level(logging.ERROR), pytest.raises(ValueError):
            LLMClient._parse_tolerant("Applicant Alice Example PAN ABCDE1234F income 1200000")
    finally:
        request_id.reset(token)
    assert all(word not in caplog.text for word in ("Alice", "ABCDE1234F", "1200000"))
    assert caplog.records[-1].error_code == "MODEL_JSON_INVALID"
    assert caplog.records[-1].request_id == "47d62be9-ea55-44e5-97eb-7831a47b6981"


@pytest.mark.parametrize("status", [400, 401, 403, 422])
async def test_deterministic_provider_errors_not_retried_or_logged(monkeypatch, caplog, status):
    calls = []

    class Stream:
        async def __aenter__(self):
            calls.append(1)
            error = RuntimeError("Alice Example PAN ABCDE1234F")
            error.status_code = status
            raise error

        async def __aexit__(self, *args):
            pass

    client = LLMClient(LLMConfig("claude", "fixture", "not-a-real-key", max_retries=5))
    monkeypatch.setattr(
        client,
        "_client",
        lambda: SimpleNamespace(messages=SimpleNamespace(stream=lambda **kwargs: Stream())),
    )
    with caplog.at_level(logging.ERROR), pytest.raises(LLMResponseRejected):
        await client.chat("schema", [])
    assert len(calls) == 1
    assert "Alice" not in caplog.text and "ABCDE1234F" not in caplog.text


@pytest.mark.parametrize(
    "field,value", [("age", "66"), ("age", True), ("sum_assured", []), ("occupation", {})]
)
async def test_malformed_critical_case_fields_return_incomplete(tmp_path, field, value):
    from underwriting_platform.infrastructure.kernel import KernelUnderwriter

    case = synthetic_case("A")
    case["applicant"][field] = value
    response = await KernelUnderwriter(local_settings(tmp_path)).decide(
        DecisionRequest(case_id="A", case_json=case)
    )
    assert response.decision["verdict"]["label"] == "INCOMPLETE"
    assert not response.decision["_completion"]["decision_complete"]


@pytest.mark.parametrize(
    "defect",
    [
        "missing_gate",
        "bad_status",
        "missing_calculation",
        "bad_evidence",
        "bad_sources",
        "contradictory_verdict",
        "mismatched_age",
    ],
)
def test_decision_boundary_rejects_schema_and_audit_drift(defect):
    case = synthetic_case("A")
    decision = run_engine(case, {}, 10000000)
    decision["_normalized_input"] = case
    if defect == "missing_gate":
        decision["gating"] = [
            g for g in decision["gating"] if g["criterion"] != "sbi_qualification"
        ]
    elif defect == "bad_status":
        decision["gating"][0]["status"] = "MAYBE"
    elif defect == "missing_calculation":
        del decision["calculations"]["selected_income"]
    elif defect == "bad_evidence":
        case["field_sources"]["applicant.age"][0]["page"] = True
    elif defect == "bad_sources":
        decision["gating"][0]["sources"] = "source.pdf"
    elif defect == "mismatched_age":
        decision["calculations"]["age"]["value"] += 1
    else:
        decision["verdict"]["label"] = "STANDARD_ISSUANCE"
    response = DecisionResponse(case_id="A", decision=decision)
    # Parsing is non-mutating. Policy assessment is an explicit application step.
    assert response.decision["gating"] == decision["gating"]
    assert not assess_decision(case, response.decision)["decision_complete"]


async def test_required_idempotency_and_retry_after_lost_response(tmp_path):
    settings = local_settings(tmp_path)
    repo = RelationalJobRepository(settings.database_url, max_pending=1)
    repo.initialize()
    app = build_app(settings, repo)
    headers = {"Authorization": "Bearer synthetic-client"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        assert (
            await client.post("/v1/runs", json={"case_id": "A"}, headers=headers)
        ).status_code == 422
        headers["Idempotency-Key"] = "stable-client-request"
        first = await client.post("/v1/runs", json={"case_id": "A"}, headers=headers)
        retry = await client.post("/v1/runs", json={"case_id": "A"}, headers=headers)
        assert first.status_code == retry.status_code == 202
        assert first.json()["job_id"] == retry.json()["job_id"]
        assert (
            await client.post("/v1/runs", json={"case_id": "B"}, headers=headers)
        ).status_code == 409
        headers["Idempotency-Key"] = "new-request"
        assert (
            await client.post("/v1/runs", json={"case_id": "B"}, headers=headers)
        ).status_code == 429
        metrics = await client.get("/v1/operations/queue", headers=headers)
        assert metrics.json()["pending"] == 1


async def test_heartbeat_database_blip_requeues_instead_of_terminal_failure(tmp_path, monkeypatch):
    settings = replace(local_settings(tmp_path), lease_seconds=6)
    repo = RelationalJobRepository(settings.database_url)
    repo.initialize()
    job = repo.create(CaseRequest(case_id="A"), "heartbeat")
    original = repo.renew
    calls = []

    def renew(*args):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("temporary DB outage")
        return original(*args)

    monkeypatch.setattr(repo, "renew", renew)

    class Workflow:
        delay = 3

        async def execute(self, job, owner):
            await asyncio.sleep(self.delay)
            result = WorkflowResult(
                case_id="A",
                verdict="DECLINE",
                artifact=ArtifactResponse(
                    case_id="A",
                    artifact_id="A_test",
                    qc_status="PASS",
                    qc_summary={},
                    download_path="/unused",
                ),
            )
            repo.finish(job.job_id, owner, JobStatus.SUCCEEDED, result)

    workflow = Workflow()
    worker = Worker(repo, workflow, settings)
    await worker.run_once()
    assert repo.get(job.job_id).status == JobStatus.QUEUED
    workflow.delay = 0.01
    await worker.run_once()
    assert repo.get(job.job_id).status == JobStatus.SUCCEEDED
    assert repo.get(job.job_id).attempts == 2


def test_full_document_hash_does_not_drop_equal_prefixes(tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    a.write_bytes(b"x" * 65536 + b"Applicant A")
    b.write_bytes(b"x" * 65536 + b"Applicant B")
    assert _file_hash(a) != _file_hash(b)


def test_checkpoint_metadata_is_small_and_payloads_only_hydrated_on_claim(tmp_path):
    repo = RelationalJobRepository(local_settings(tmp_path).database_url)
    repo.initialize()
    job = repo.create(CaseRequest(case_id="A"), "payload")
    repo.claim("one", 60, 3)
    sensitive = "Synthetic applicant information " * 1000
    repo.checkpoint(job.job_id, "one", "extraction", {"case_json": {"private": sensitive}})
    status = repo.get(job.job_id)
    assert sensitive not in json.dumps(status.checkpoints)
    assert len(json.dumps(status.checkpoints)) < 500
    repo.fail(job.job_id, "one", "retry", True)
    resumed = repo.claim("two", 60, 3)
    assert resumed.checkpoints["extraction"]["case_json"]["private"] == sensitive


def test_production_configuration_is_blocked_pending_security_prerequisites():
    with pytest.raises(ValueError, match="Production deployment is blocked"):
        Settings(deployment_mode="production")
