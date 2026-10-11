from support.extraction_fixture import SyntheticExtractor, synthetic_case

from underwriting_platform.application.worker import Worker
from underwriting_platform.application.workflow import Workflow
from underwriting_platform.configuration.settings import Settings
from underwriting_platform.contracts.v1 import (
    ArtifactResponse,
    CaseRequest,
    ExtractionRequest,
    ExtractionResponse,
)
from underwriting_platform.infrastructure.jobs import RelationalJobRepository
from underwriting_platform.infrastructure.kernel import KernelExtractor, KernelUnderwriter


async def test_auth_rejection_does_not_spend_generation_budget(monkeypatch):
    from types import SimpleNamespace

    import pytest

    from underwriting_platform.llm.budget import case_scope
    from underwriting_platform.llm.client import LLMClient, LLMConfig, LLMResponseRejected

    async def count(**kwargs):
        return SimpleNamespace(input_tokens=100)

    class Rejected(Exception):
        status_code = 401

    class Stream:
        async def __aenter__(self):
            raise Rejected("private response content")

        async def __aexit__(self, *args):
            pass

    client = LLMClient(LLMConfig("claude", "test", "dummy", max_tokens=100))
    monkeypatch.setattr(
        client,
        "_client",
        lambda: SimpleNamespace(
            messages=SimpleNamespace(count_tokens=count, stream=lambda **kwargs: Stream())
        ),
    )
    with case_scope(10000) as budget:
        with pytest.raises(LLMResponseRejected):
            await client.chat("prompt", [])
        assert budget.spent == 0
        assert budget.reserved == 0


def test_missing_income_is_resolvable_but_documented_zero_is_adverse():
    from underwriting_platform.engine.flags import flags_from_calculations
    from underwriting_platform.engine.types import Calculation

    missing = flags_from_calculations(
        {"selected_income": Calculation(name="selected_income", value=0, method="none")}, {}
    )
    verified = flags_from_calculations(
        {"selected_income": Calculation(name="selected_income", value=0, method="itr")}, {}
    )
    assert next(f for f in missing if f.code == "INCOME_NOT_ESTABLISHED").severity == "HIGH"
    assert next(f for f in verified if f.code == "INCOME_NOT_ESTABLISHED").severity == "CRITICAL"


def test_failed_agent_checkpoint_does_not_survive_recovered_configuration(tmp_path):
    from underwriting_platform.agents.base import AgentResult
    from underwriting_platform.orchestrator.checkpoints import StageStore

    store = StageStore(tmp_path)
    failed = AgentResult(block={}, error="MODEL_REQUEST_REJECTED", retryable=False)
    store.save_agent("source", failed)
    assert store.agent("source") is None
    store.write("source", vars(failed))
    assert store.agent("source") is None
    succeeded = AgentResult(block={"applicant": {"age": 35}})
    store.save_agent("source", succeeded)
    assert store.agent("source").block == succeeded.block


def test_bureau_document_reader_needs_no_optional_word_dependency(tmp_path):
    import zipfile

    from underwriting_platform.agents.bureau import extract_case_iib_images

    document = tmp_path / "bureau.docx"
    xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>CASE000001</w:t></w:r></w:p></w:body></w:document>'
    with zipfile.ZipFile(document, "w") as archive:
        archive.writestr("word/document.xml", xml)
        archive.writestr("word/media/image1.png", b"first")
        archive.writestr("word/media/image2.png", b"second")
    assert extract_case_iib_images("CASE000001", document) == [b"first", b"second"]


async def test_decision_checkpoint_contains_recomputed_completion(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'jobs.db'}")
    repository = RelationalJobRepository(settings.database_url)
    repository.initialize()
    job = repository.create(CaseRequest(case_id="A"), "completion")

    class Underwriter(KernelUnderwriter):
        async def decide(self, request):
            response = await super().decide(request)
            response.decision.pop("_completion", None)
            return response

    class Artifacts:
        async def render(self, request):
            return ArtifactResponse(
                case_id="A",
                artifact_id="A_artifact",
                qc_status="PASS",
                qc_summary={},
                download_path="/unused",
                delivery_allowed=True,
            )

    workflow = Workflow(repository, SyntheticExtractor(), Underwriter(settings), Artifacts())
    await Worker(repository, workflow, settings).run_once()
    summary = repository.get(job.job_id).checkpoints["decision"]["completion"]
    assert summary["decision_complete"] is True
    assert summary["policy_version"]


async def test_invalid_evidence_is_not_published_as_successful_cache(tmp_path, monkeypatch):
    settings = Settings(case_root=tmp_path / "cases", extraction_cache=tmp_path / "cache")
    folder = settings.case_root / "A"
    folder.mkdir(parents=True)
    (folder / "proposal.pdf").write_bytes(b"same source bytes")
    extractor = KernelExtractor(settings)
    calls = []

    async def extract(request, folder, digest):
        calls.append(1)
        case = synthetic_case("A")
        if len(calls) == 1:
            case["extraction_findings"] = [
                {"field": "applicant.age", "code": "INVALID_SOURCE_LOCATION"}
            ]
        return ExtractionResponse(
            case_id="A", input_fingerprint=digest, case_json=case, extraction_report={}
        )

    monkeypatch.setattr(extractor, "_extract_async", extract)
    await extractor.extract(ExtractionRequest(case_id="A"))
    assert all(
        path.name.endswith(".partial.json") for path in settings.extraction_cache.rglob("*.json")
    )
    await extractor.extract(ExtractionRequest(case_id="A"))
    await extractor.extract(ExtractionRequest(case_id="A"))
    assert len(calls) == 2
