from pathlib import Path

import httpx

from underwriting_platform.agents.source_reader import SourceReader
from underwriting_platform.configuration.extraction import cache_revision
from underwriting_platform.configuration.settings import Settings
from underwriting_platform.infrastructure.jobs import RelationalJobRepository
from underwriting_platform.services import artifacts, gateway


async def test_public_status_and_idempotent_retry_hide_diagnostics(tmp_path):
    from support.extraction_fixture import SyntheticExtractor

    from underwriting_platform.application.worker import Worker
    from underwriting_platform.application.workflow import Workflow
    from underwriting_platform.contracts.v1 import CaseRequest
    from underwriting_platform.infrastructure.kernel import KernelArtifacts, KernelUnderwriter

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        artifact_root=tmp_path / "artifacts",
        gateway_token="client",
    )
    repository = RelationalJobRepository(settings.database_url)
    repository.initialize()
    job = repository.create(CaseRequest(case_id="A"), "review")
    workflow = Workflow(
        repository, SyntheticExtractor(), KernelUnderwriter(settings), KernelArtifacts(settings)
    )
    await Worker(repository, workflow, settings).run_once()
    app = gateway.build_app(settings, repository)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = {"Authorization": "Bearer client", "Idempotency-Key": "review"}
        responses = [
            await client.get(f"/v1/runs/{job.job_id}", headers=headers),
            await client.post("/v1/runs", json={"case_id": "A"}, headers=headers),
        ]
        for response in responses:
            assert response.status_code in {200, 202}
            artifact = response.json()["result"]["artifact"]
            assert artifact["diagnostic_verdict"] is None
            assert artifact["diagnostic_download_path"] is None


def test_source_reader_retains_every_selected_domain_instruction():
    reader = SourceReader(object(), ["identity", "income", "medical", "compliance"])
    prompt = reader._load_prompt()
    directory = Path(__file__).parents[1] / "src/underwriting_platform/agents/prompts"
    for name in reader.domains:
        assert (directory / reader.PROMPTS[name]).read_text(encoding="utf8") in prompt


def test_budget_limit_change_preserves_extraction_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_TOKEN_BUDGET_PER_CASE", "200000")
    first = cache_revision("same-source", tmp_path)
    monkeypatch.setenv("LLM_TOKEN_BUDGET_PER_CASE", "250000")
    assert cache_revision("same-source", tmp_path) == first


async def test_diagnostic_routes_are_removed_even_with_valid_credentials(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        gateway_token="client",
        service_token="service",
    )
    repository = RelationalJobRepository(settings.database_url)
    repository.initialize()
    for app, path, token in (
        (
            gateway.build_app(settings, repository),
            "/v1/runs/00000000-0000-0000-0000-000000000000/diagnostic-case-sheet",
            "client",
        ),
        (
            artifacts.build_app(settings),
            "/v1/artifacts/A_artifact/diagnostic-case-sheet",
            "service",
        ),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(path, headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 404
