"""Failures preserve useful diagnostics while delivery remains blocked."""

import copy
import json

import httpx
from support.extraction_fixture import SyntheticExtractor, synthetic_case

from underwriting_platform.agents import IdentityAgent
from underwriting_platform.application.errors import ProcessingRejected
from underwriting_platform.application.worker import Worker
from underwriting_platform.application.workflow import Workflow
from underwriting_platform.configuration.settings import Settings
from underwriting_platform.contracts.v1 import (
    CaseRequest,
    DecisionRequest,
    DecisionResponse,
    JobStatus,
)
from underwriting_platform.engine import run_engine
from underwriting_platform.infrastructure.http import HttpStages
from underwriting_platform.infrastructure.jobs import RelationalJobRepository
from underwriting_platform.infrastructure.kernel import KernelArtifacts, KernelUnderwriter
from underwriting_platform.llm import Document


async def test_bad_citation_keeps_good_facts_and_marks_manual_review():
    class Provider:
        async def extract_json(self, *args, **kwargs):
            return {
                "applicant": {"age": 35, "occupation": "Kirana store"},
                "agent_meta": {
                    "confidence": 1,
                    "field_sources": {
                        "applicant.age": [{"document": "proposal.pdf", "page": 9, "raw_value": 35}],
                        "applicant.occupation": [
                            {"document": "proposal.pdf", "page": 1, "raw_value": "Kirana store"}
                        ],
                    },
                },
            }

    result = await IdentityAgent(Provider()).extract(
        [Document("proposal.pdf", "source", page_count=1)]
    )
    assert result.block["applicant"]["age"] == 35
    assert result.field_sources["applicant.occupation"]
    assert not result.field_sources["applicant.age"]
    assert result.source_findings[0]["retryable"] is False
    assert result.raw_payload["agent_meta"]["field_sources"]["applicant.age"][0]["page"] == 9


def test_contract_parsing_never_overwrites_diagnostic_verdict_or_gates():
    case = synthetic_case("A")
    decision = run_engine(copy.deepcopy(case), {}, 10000000)
    decision["gating"][0]["status"] = "MALFORMED"
    original = copy.deepcopy(decision)
    response = DecisionResponse(case_id="A", decision=decision)
    assert response.decision == original
    assert DecisionResponse.model_validate_json(response.model_dump_json()).decision == original


async def test_engine_exception_produces_incomplete_job_and_diagnostic_pdf(tmp_path, monkeypatch):
    import underwriting_platform.engine.run as engine_module

    def broken(*args, **kwargs):
        raise RuntimeError("injected engine failure")

    monkeypatch.setattr(engine_module, "_run_engine", broken)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}", artifact_root=tmp_path / "artifacts"
    )
    repository = RelationalJobRepository(settings.database_url)
    repository.initialize()
    job = repository.create(CaseRequest(case_id="A"), "engine")
    workflow = Workflow(
        repository, SyntheticExtractor(), KernelUnderwriter(settings), KernelArtifacts(settings)
    )
    await Worker(repository, workflow, settings).run_once()
    completed = repository.get(job.job_id)
    assert completed.status == JobStatus.INCOMPLETE
    assert completed.attempts == 1
    folder = settings.artifact_root / completed.result.artifact.artifact_id
    assert (folder / "case-sheet.pdf").is_file()
    assert completed.result.artifact.delivery_allowed is False
    assert json.loads((folder / "delivery.json").read_text())["delivery_allowed"] is False
    assert completed.result.artifact.diagnostic_download_path is None


async def test_http_preserves_processing_rejected_code():
    def rejected(request):
        return httpx.Response(422, json={"code": "PROCESSING_REJECTED"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(rejected)) as client:
        stages = HttpStages(Settings(), client)
        try:
            await stages.decide(DecisionRequest(case_id="A", case_json={"case_id": "A"}))
        except ProcessingRejected as error:
            assert error.code == "PROCESSING_REJECTED"
        else:
            raise AssertionError("Error code lost")
