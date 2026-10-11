"""Boundary, durability and fail-closed behavior of the service architecture."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
from pydantic import ValidationError
from support.extraction_fixture import SyntheticExtractor, synthetic_case

from underwriting_platform.application.errors import (
    DependencyUnavailable,
    IdempotencyConflict,
    LeaseLost,
)
from underwriting_platform.application.worker import Worker
from underwriting_platform.application.workflow import Workflow
from underwriting_platform.configuration.settings import Settings
from underwriting_platform.contracts.v1 import (
    ArtifactResponse,
    CaseRequest,
    DecisionRequest,
    DecisionResponse,
    ExtractionResponse,
    JobStatus,
)
from underwriting_platform.infrastructure.http import HttpStages
from underwriting_platform.infrastructure.jobs import RelationalJobRepository
from underwriting_platform.infrastructure.kernel import (
    KernelArtifacts,
    KernelExtractor,
    KernelUnderwriter,
)
from underwriting_platform.services import artifacts, extraction, gateway, underwriting


@pytest.fixture
def settings(tmp_path):
    return Settings(
        service_token="test-service-token",
        gateway_token="test-client-token",
        database_url=f"sqlite:///{tmp_path / 'jobs.sqlite3'}",
        artifact_root=tmp_path / "artifacts",
        extraction_cache=tmp_path / "cache",
        case_root=tmp_path,
    )


@pytest.fixture
def repository(settings):
    repository = RelationalJobRepository(settings.database_url)
    repository.initialize()
    return repository


@pytest.mark.parametrize("case_id", ["../secret", "a/b", "a\\b", "", "a" * 65])
def test_case_paths_are_not_api_input(case_id):
    with pytest.raises(ValidationError):
        CaseRequest(case_id=case_id)


def test_contract_versions_and_identity_are_enforced():
    with pytest.raises(ValidationError):
        CaseRequest(case_id="A", schema_version="2.0")
    with pytest.raises(ValidationError):
        DecisionRequest(case_id="A", case_json={"case_id": "B"})
    with pytest.raises(ValidationError):
        DecisionResponse(case_id="A", decision={"case_id": "A", "verdict": {"label": "YES"}})


def test_idempotency_survives_repository_restart(repository, settings):
    first = repository.create(CaseRequest(case_id="A"), "repeat")
    second = RelationalJobRepository(settings.database_url).create(
        CaseRequest(case_id="A"), "repeat"
    )
    assert first.job_id == second.job_id
    with pytest.raises(IdempotencyConflict):
        repository.create(CaseRequest(case_id="B"), "repeat")


def test_parallel_workers_do_not_claim_same_job(repository):
    for index in range(8):
        repository.create(CaseRequest(case_id=f"C{index}"), f"key{index}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(lambda index: repository.claim(str(index), 60, 3), range(8)))
    assert len({job.job_id for job in jobs}) == 8
    assert repository.claim("extra", 60, 3) is None


def test_expired_lease_reclaimed_and_old_owner_cannot_commit(repository, monkeypatch):
    import underwriting_platform.infrastructure.jobs as jobs_module

    now = 1000.0
    monkeypatch.setattr(jobs_module.time, "time", lambda: now)
    repository.create(CaseRequest(case_id="A"), "lease")
    job = repository.claim("old", 10, 3)
    now = 1011.0
    resumed = repository.claim("new", 10, 3)
    assert resumed.job_id == job.job_id
    assert resumed.attempts == 2
    with pytest.raises(LeaseLost):
        repository.checkpoint(job.job_id, "old", "decision", {})


class DecisionStub:
    def __init__(self, label="INCOMPLETE"):
        self.label = label
        self.calls = 0

    async def decide(self, request):
        self.calls += 1
        from underwriting_platform.application.completion import POLICY_VERSION
        from underwriting_platform.engine import run_engine

        case = synthetic_case(request.case_id)
        decision = run_engine(case, {}, 10000000)
        decision["_normalized_input"] = case
        decision["verdict"] = {"label": self.label}
        decision["_completion"] = {
            "decision_complete": self.label != "INCOMPLETE",
            "issues": [],
            "policy_version": POLICY_VERSION,
        }
        return DecisionResponse(
            case_id=request.case_id,
            decision=decision,
        )


class ArtifactStub:
    def __init__(self, status="PASS", fail_once=False):
        self.status = status
        self.fail_once = fail_once
        self.calls = 0

    async def render(self, request):
        self.calls += 1
        if self.fail_once and self.calls == 1:
            raise DependencyUnavailable()
        return ArtifactResponse(
            case_id=request.case_id,
            artifact_id=f"{request.case_id}_artifact",
            qc_status=self.status,
            qc_summary={},
            download_path="/unused",
        )


@pytest.mark.parametrize(
    "verdict,qc,status",
    [
        ("INCOMPLETE", "PASS", JobStatus.INCOMPLETE),
        ("STANDARD_ISSUANCE", "BLOCK", JobStatus.INCOMPLETE),
        ("DECLINE", "PASS", JobStatus.SUCCEEDED),
    ],
)
async def test_workflow_completion_does_not_mean_issuance(
    repository, settings, verdict, qc, status
):
    job = repository.create(CaseRequest(case_id="A"), "workflow")
    workflow = Workflow(repository, SyntheticExtractor(), DecisionStub(verdict), ArtifactStub(qc))
    assert await Worker(repository, workflow, settings).run_once()
    completed = repository.get(job.job_id)
    assert completed.status == status
    assert completed.result.verdict == ("INCOMPLETE" if status == JobStatus.INCOMPLETE else verdict)


async def test_retry_resumes_checkpoints_with_new_worker(repository, settings):
    job = repository.create(CaseRequest(case_id="A"), "resume")
    decision = DecisionStub("DECLINE")
    artifact = ArtifactStub(fail_once=True)
    workflow = Workflow(repository, SyntheticExtractor(), decision, artifact)
    await Worker(repository, workflow, settings).run_once()
    assert repository.get(job.job_id).status == JobStatus.QUEUED
    restarted_repository = RelationalJobRepository(settings.database_url)
    await Worker(
        restarted_repository,
        Workflow(restarted_repository, SyntheticExtractor(), decision, artifact),
        settings,
    ).run_once()
    assert repository.get(job.job_id).status == JobStatus.SUCCEEDED
    assert decision.calls == 1
    assert artifact.calls == 2


async def test_unhandled_stage_exception_cannot_issue(repository, settings):
    class BrokenExtractor:
        async def extract(self, request):
            raise RuntimeError("private financial data")

    job = repository.create(CaseRequest(case_id="A"), "broken")
    worker = Worker(
        repository,
        Workflow(repository, BrokenExtractor(), DecisionStub(), ArtifactStub()),
        settings,
    )
    await worker.run_once()
    failed = repository.get(job.job_id)
    assert failed.status == JobStatus.FAILED
    assert failed.result is None
    assert failed.error_code == "PROCESSING_FAILED"


async def test_retry_budget_is_bounded(repository, settings):
    class Unavailable:
        async def extract(self, request):
            raise DependencyUnavailable()

    job = repository.create(CaseRequest(case_id="A"), "bounded")
    worker = Worker(
        repository, Workflow(repository, Unavailable(), DecisionStub(), ArtifactStub()), settings
    )
    for _ in range(3):
        assert await worker.run_once()
    assert not await worker.run_once()
    assert repository.get(job.job_id).status == JobStatus.FAILED
    assert repository.get(job.job_id).attempts == 3


async def test_cross_case_stage_response_is_blocked(repository, settings):
    class WrongExtractor:
        async def extract(self, request):
            return ExtractionResponse(
                case_id="OTHER",
                input_fingerprint="test",
                case_json={"case_id": "OTHER"},
                extraction_report={},
            )

    job = repository.create(CaseRequest(case_id="A"), "wrong")
    decision = DecisionStub()
    await Worker(
        repository, Workflow(repository, WrongExtractor(), decision, ArtifactStub()), settings
    ).run_once()
    assert repository.get(job.job_id).status == JobStatus.FAILED
    assert decision.calls == 0


async def test_auth_validation_and_idempotency_http(repository, settings):
    app = gateway.build_app(settings, repository)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            assert (await client.post("/v1/runs", json={"case_id": "A"})).status_code == 401
            headers = {
                "Authorization": f"Bearer {settings.gateway_token}",
                "Idempotency-Key": "same",
            }
            first = await client.post("/v1/runs", json={"case_id": "A"}, headers=headers)
            second = await client.post("/v1/runs", json={"case_id": "A"}, headers=headers)
            assert first.status_code == 202
            assert first.json()["job_id"] == second.json()["job_id"]
            assert (
                await client.post("/v1/runs", json={"case_id": "B"}, headers=headers)
            ).status_code == 409
            invalid = await client.post("/v1/runs", json={"case_id": "../private"}, headers=headers)
            assert invalid.status_code == 422
            assert "../private" not in invalid.text
            assert invalid.headers["X-Request-ID"]


async def test_empty_auth_configuration_fails_closed(settings):
    app = underwriting.build_app(replace(settings, service_token=""))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://uw"
    ) as client:
        assert (await client.get("/health/ready")).status_code == 503
        assert (
            await client.post(
                "/v1/decisions", json={"case_id": "A", "case_json": synthetic_case("A")}
            )
        ).status_code == 503


async def test_real_kernel_through_http_and_artifact_delivery(repository, settings):
    apps = {
        "extraction": extraction.build_app(settings, SyntheticExtractor()),
        "underwriting": underwriting.build_app(settings, KernelUnderwriter(settings)),
        "artifacts": artifacts.build_app(settings, KernelArtifacts(settings)),
    }
    routed_settings = replace(
        settings,
        extraction_url="http://extraction",
        underwriting_url="http://underwriting",
        artifacts_url="http://artifacts",
    )
    mounts = {f"http://{name}": httpx.ASGITransport(app=app) for name, app in apps.items()}
    async with httpx.AsyncClient(mounts=mounts) as service_client:
        stages = HttpStages(routed_settings, service_client)
        job = repository.create(CaseRequest(case_id="SYNTHETIC", proposed_sa=10000000), "real")
        await Worker(
            repository, Workflow(repository, stages, stages, stages), routed_settings
        ).run_once()
        completed = repository.get(job.job_id)
        assert completed.status == JobStatus.SUCCEEDED
        assert completed.result.verdict == "DECLINE"
        app = gateway.build_app(routed_settings, repository, service_client)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client:
                response = await client.get(
                    f"/v1/runs/{job.job_id}/case-sheet",
                    headers={"Authorization": f"Bearer {settings.gateway_token}"},
                )
                assert response.status_code == 200
                assert response.content.startswith(b"%PDF")


async def test_qc_blocked_artifact_download_is_denied(settings):
    folder = settings.artifact_root / "A_artifact"
    folder.mkdir(parents=True)
    metadata = ArtifactResponse(
        case_id="A",
        artifact_id="A_artifact",
        qc_status="BLOCK",
        qc_summary={},
        download_path="/unused",
    )
    (folder / "manifest.json").write_text(metadata.model_dump_json(), encoding="utf8")
    app = artifacts.build_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://artifacts"
    ) as client:
        response = await client.get(
            "/v1/artifacts/A_artifact/case-sheet",
            headers={"Authorization": f"Bearer {settings.service_token}"},
        )
        assert response.status_code == 409


async def test_malformed_dependency_response_is_not_a_decision(settings):
    async def malformed(request):
        return httpx.Response(
            200, json={"case_id": "A", "decision": {"verdict": {"label": "STANDARD_ISSUANCE"}}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(malformed)) as client:
        stages = HttpStages(settings, client)
        with pytest.raises(DependencyUnavailable):
            await stages.decide(DecisionRequest(case_id="A", case_json={"case_id": "A"}))


async def test_service_exception_details_are_redacted(settings):
    class BrokenUnderwriter:
        async def decide(self, request):
            raise RuntimeError("private-financial-data-and-provider-secret")

    app = underwriting.build_app(settings, BrokenUnderwriter())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://uw"
    ) as client:
        response = await client.post(
            "/v1/decisions",
            json={"case_id": "A", "case_json": {"case_id": "A"}},
            headers={"Authorization": f"Bearer {settings.service_token}"},
        )
        assert response.status_code == 500
        assert "private-financial" not in response.text
        assert "STANDARD_ISSUANCE" not in response.text
        assert response.json()["request_id"] == response.headers["X-Request-ID"]


async def test_invalid_rules_are_not_ready_or_issuable(settings, tmp_path):
    (tmp_path / "sbi_smart_shield_rules.json").write_text("{}", encoding="utf8")
    (tmp_path / "uw_rules.json").write_text("{}", encoding="utf8")
    app = underwriting.build_app(replace(settings, config_root=tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://uw"
    ) as client:
        assert (await client.get("/health/ready")).status_code == 503
        response = await client.post(
            "/v1/decisions",
            json={"case_id": "A", "case_json": synthetic_case("A")},
            headers={"Authorization": f"Bearer {settings.service_token}"},
        )
        assert response.status_code == 200
        assert response.json()["decision"]["verdict"]["label"] == "INCOMPLETE"


async def test_lease_heartbeat_protects_a_long_job(repository, settings):
    from types import SimpleNamespace

    class SlowExtractor(SyntheticExtractor):
        async def extract(self, request):
            await asyncio.sleep(0.3)
            return await super().extract(request)

    job = repository.create(CaseRequest(case_id="A"), "heartbeat")
    worker_settings = SimpleNamespace(lease_seconds=0.15, max_attempts=3, poll_seconds=0.01)
    worker = Worker(
        repository,
        Workflow(repository, SlowExtractor(), DecisionStub("DECLINE"), ArtifactStub()),
        worker_settings,
    )
    await worker.run_once()
    assert repository.get(job.job_id).status == JobStatus.SUCCEEDED
    assert repository.get(job.job_id).attempts == 1


async def test_extraction_cache_changes_with_evidence_rules_reference_and_model(
    settings, tmp_path, monkeypatch
):
    import dotenv

    import underwriting_platform.infrastructure.kernel as kernel_module
    from underwriting_platform.contracts.v1 import ExtractionRequest

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(kernel_module, "REFERENCE_ROOT", tmp_path / "references")
    for key in ("SBI_SCORES_XLSX", "SBI_SCORES_XLSX_TABULAR", "SBI_IIB_XLSX"):
        monkeypatch.delenv(key, raising=False)
    folder = settings.case_root / "A"
    folder.mkdir()
    document = folder / "synthetic.pdf"
    document.write_bytes(b"synthetic-document-version-1")
    config = tmp_path / "config"
    config.mkdir()
    rules = config / "uw_rules.json"
    rules.write_text("{}", encoding="utf8")
    calls = []

    async def fake_extract(request, folder, digest):
        calls.append(digest)
        return ExtractionResponse(
            case_id="A",
            input_fingerprint=digest,
            case_json={"case_id": "A"},
            extraction_report={"synthetic": True},
        )

    extractor = KernelExtractor(replace(settings, config_root=config))
    monkeypatch.setattr(extractor, "_extract_async", fake_extract)
    request = ExtractionRequest(case_id="A")
    await extractor.extract(request)
    await extractor.extract(request)
    assert len(calls) == 1
    document.write_bytes(b"synthetic-document-version-2")
    await extractor.extract(request)
    rules.write_text('{"changed": true}', encoding="utf8")
    await extractor.extract(request)
    reference = tmp_path / "bureau.xlsx"
    reference.write_bytes(b"changed-reference")
    monkeypatch.setenv("SBI_IIB_XLSX", str(reference))
    await extractor.extract(request)
    monkeypatch.setenv("AZURE_CLAUDE_DEPLOYMENT", "different-test-model")
    await extractor.extract(request)
    assert len(calls) == 5
