import copy
from dataclasses import replace

import fitz
import httpx
import pytest
from support.extraction_fixture import SyntheticExtractor

from underwriting_platform.application.errors import EvidenceTooLarge
from underwriting_platform.application.retry_policy import retryable_dependency
from underwriting_platform.application.worker import Worker
from underwriting_platform.application.workflow import Workflow
from underwriting_platform.configuration.settings import Settings
from underwriting_platform.contracts.v1 import CaseRequest, JobStatus
from underwriting_platform.infrastructure.jobs import RelationalJobRepository
from underwriting_platform.infrastructure.kernel import (
    KernelArtifacts,
    KernelExtractor,
    KernelUnderwriter,
)
from underwriting_platform.llm.budget import TokenBudgetExceeded, case_scope
from underwriting_platform.orchestrator.budget_ledger import attach_budget
from underwriting_platform.orchestrator.checkpoints import StageStore
from underwriting_platform.services import artifacts, gateway


async def test_case_budget_survives_revision_changes_and_migrates_legacy_spend(tmp_path):
    for revision, spend in (("old-code", 60), ("new-prompt", 30)):
        store = StageStore(tmp_path / revision)
        store.write("survey", {"files": [{"filename": "CASE_A_proposal.pdf"}]})
        store.write("budget", {"spent": spend, "reserved": 0})
    with case_scope(100) as budget:
        attach_budget(budget, tmp_path, "CASE_A")
        assert budget.spent == 90
        reserved = await budget.reserve(10)
        await budget.finish(reserved, 10)
    with case_scope(100) as budget:
        attach_budget(budget, tmp_path, "CASE_A", StageStore(tmp_path / "another-code-change"))
        assert budget.spent == 100
        with pytest.raises(TokenBudgetExceeded):
            await budget.reserve(1)


async def test_oversized_intake_never_starts_paid_extraction(tmp_path, monkeypatch):
    import underwriting_platform.infrastructure.evidence as evidence

    settings = Settings(case_root=tmp_path / "cases", extraction_cache=tmp_path / "cache")
    folder = settings.case_root / "A"
    folder.mkdir(parents=True)
    (folder / "scan.pdf").write_bytes(b"too much evidence")
    monkeypatch.setattr(evidence, "MAX_EVIDENCE_BYTES", 4)
    extractor = KernelExtractor(settings)
    monkeypatch.setattr(
        extractor, "_extract_async", lambda *args: pytest.fail("Paid extraction started")
    )
    from underwriting_platform.contracts.v1 import ExtractionRequest

    with pytest.raises(EvidenceTooLarge):
        await extractor.extract(ExtractionRequest(case_id="A"))


def test_uncited_business_checkpoint_is_rejected_and_cited_repair_is_reusable(tmp_path):
    from underwriting_platform.agents.base import AgentResult

    store = StageStore(tmp_path)
    bad = AgentResult(block={"applicant": {"employer_name": "Finance"}})
    store.write("identity:0", vars(bad))
    assert store.agent("identity:0") is None
    repaired = copy.deepcopy(bad)
    repaired.field_sources = {
        "applicant.employer_name": [{"document": "proposal.pdf", "page": 1, "raw_value": "Finance"}]
    }
    store.save_agent("identity:0", repaired)
    assert store.agent("identity:0").block == repaired.block


def test_wrapped_identity_timeout_is_retryable_but_budget_failure_is_not():
    try:
        try:
            raise TimeoutError()
        except TimeoutError as error:
            raise RuntimeError("safe wrapper") from error
    except RuntimeError as error:
        assert retryable_dependency(error)
    assert not retryable_dependency(TokenBudgetExceeded())
    assert not retryable_dependency(ValueError("invalid evidence"))


async def test_retryable_extraction_renders_only_the_final_attempt(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'jobs.db'}", max_attempts=3)
    repository = RelationalJobRepository(settings.database_url)
    repository.initialize()
    job = repository.create(CaseRequest(case_id="A"), "retry")

    class Partial(SyntheticExtractor):
        async def extract(self, request):
            response = await super().extract(request)
            response.case_json["processing_errors"] = [
                {"stage": "identity", "type": "TimeoutError", "retryable": True}
            ]
            return response

    calls = []

    class Artifacts(KernelArtifacts):
        async def render(self, request):
            calls.append(1)
            return await super().render(request)

    workflow = Workflow(
        repository,
        Partial(),
        KernelUnderwriter(settings),
        Artifacts(replace(settings, artifact_root=tmp_path / "artifacts")),
    )
    worker = Worker(repository, workflow, settings)
    for _ in range(3):
        await worker.run_once()
    assert len(calls) == 1
    assert repository.get(job.job_id).status == JobStatus.INCOMPLETE


async def test_blocked_sheet_requires_reviewer_credential_and_is_watermarked(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        artifact_root=tmp_path / "artifacts",
        gateway_token="client",
        service_token="service",
        reviewer_token="reviewer",
    )
    repository = RelationalJobRepository(settings.database_url)
    repository.initialize()
    job = repository.create(CaseRequest(case_id="A"), "review")

    class Incomplete(SyntheticExtractor):
        async def extract(self, request):
            response = await super().extract(request)
            response.case_json["field_sources"].pop("applicant.occupation")
            return response

    workflow = Workflow(
        repository, Incomplete(), KernelUnderwriter(settings), KernelArtifacts(settings)
    )
    await Worker(repository, workflow, settings).run_once()
    result = repository.get(job.job_id)
    assert result.status == JobStatus.INCOMPLETE
    artifact_app = artifacts.build_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=artifact_app), base_url="http://artifacts"
    ) as internal:
        app = gateway.build_app(settings, repository, internal)
        app.state.download_client = internal
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            path = f"/v1/runs/{job.job_id}/review-case-sheet"
            assert (
                await client.get(path, headers={"Authorization": "Bearer client"})
            ).status_code == 401
            response = await client.get(path, headers={"Authorization": "Bearer reviewer"})
            assert response.status_code == 200
            assert response.headers["x-uw-review-only"] == "true"
            with fitz.open(stream=response.content, filetype="pdf") as pdf:
                assert "REVIEW ONLY" in "".join(page.get_text() for page in pdf)
            assert (
                await client.get(
                    f"/v1/runs/{job.job_id}/case-sheet", headers={"Authorization": "Bearer client"}
                )
            ).status_code == 409
