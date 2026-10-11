import base64
import io

from PIL import Image

from underwriting_platform.agents import AgentResult
from underwriting_platform.agents.compliance import ComplianceAgent
from underwriting_platform.llm.document_reuse import collapse_identical_documents, is_face_photo
from underwriting_platform.llm.pdf_utils import Document


def encoded(color):
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def test_content_duplicates_collapse_but_changed_or_unrendered_pages_survive():
    first = Document("first.pdf", "same", [encoded("white")], 1)
    copy = Document("resubmitted.pdf", "same", [encoded("white")], 1)
    changed = Document("changed.pdf", "same", [encoded("black")], 1)
    partial = Document("partial.pdf", "same", [encoded("white")], 2)
    kept, aliases = collapse_identical_documents([first, copy, changed, partial])
    assert [d.filename for d in kept] == ["first.pdf", "changed.pdf", "partial.pdf"]
    assert aliases == {"resubmitted.pdf": "first.pdf"}


def test_face_photos_excluded_even_when_survey_kind_unknown():
    assert is_face_photo(Document("R15_RECENTPHOTOGRAPH_2.jpg", ""), "unknown")
    assert is_face_photo(Document("PIVCPHOTO.pdf", ""), "unknown")
    assert not is_face_photo(Document("proposal.pdf", ""), "proposal_form")


async def test_compliance_uses_upstream_evidence_without_rereading_id():
    record = {"document": "id.pdf", "page": 1, "raw_value": "Example"}

    class Client:
        async def extract_json(self, system, blocks, **kwargs):
            assert kwargs["max_tokens"] == 5000
            assert any("UPSTREAM EXTRACTION EVIDENCE" in b.get("text", "") for b in blocks)
            assert not any(b["type"] == "image" for b in blocks)
            return {
                "compliance": {"kyc_status": "Partial"},
                "agent_meta": {
                    "field_sources": {"compliance.kyc_status": [record]},
                    "confidence": 1,
                },
            }

    result = await ComplianceAgent(Client()).extract(
        [],
        shared_context={
            "identity": {
                "block": {"applicant": {"name": "Example"}},
                "field_sources": {"applicant.name": [record]},
            }
        },
    )
    assert result.field_sources["compliance.kyc_status"] == [record]
    assert not result.source_findings


async def test_compliance_rejects_fabricated_upstream_page():
    class Client:
        async def extract_json(self, *args, **kwargs):
            return {
                "compliance": {},
                "agent_meta": {
                    "field_sources": {
                        "compliance.kyc_status": [
                            {"document": "id.pdf", "page": 2, "raw_value": "Example"}
                        ]
                    }
                },
            }

    result = await ComplianceAgent(Client()).extract(
        [],
        shared_context={
            "identity": {
                "field_sources": {
                    "applicant.name": [{"document": "id.pdf", "page": 1, "raw_value": "Example"}]
                }
            }
        },
    )
    assert result.source_findings[0]["code"] == "INVALID_SOURCE_LOCATION"


async def test_orchestrator_compliance_reuses_owner_outputs(tmp_path, monkeypatch):
    import underwriting_platform.orchestrator.master as master

    documents = [
        Document(name, "Applicant evidence " * 100, page_count=1)
        for name in ("id.pdf", "itr.pdf", "proposal.pdf", "RECENTPHOTOGRAPH.jpg")
    ]
    monkeypatch.setattr(
        master, "_collect_case_files", lambda *a: [tmp_path / d.filename for d in documents]
    )
    monkeypatch.setattr(
        master, "prepare_doc", lambda path: next(d for d in documents if d.filename == path.name)
    )
    calls = {}

    class Identity:
        OUTPUT_BLOCK = "applicant"

        def __init__(self, *args):
            pass

        async def extract(self, docs, **kwargs):
            calls["identity"] = [d.filename for d in docs]
            return AgentResult({"applicant": {"age": 35, "sum_assured": 1000000}}, confidence=1)

    class Income(Identity):
        OUTPUT_BLOCK = "income"

        async def extract(self, docs, **kwargs):
            calls["income"] = [d.filename for d in docs]
            return AgentResult({"income": {"annual_income": 1000000}}, confidence=1)

    class Compliance(Identity):
        OUTPUT_BLOCK = "compliance"

        async def extract(self, docs, shared_context=None, **kwargs):
            calls["compliance"] = [d.filename for d in docs]
            assert shared_context["identity"]["block"]["applicant"]["age"] == 35
            assert shared_context["income"]["block"]["income"]["annual_income"] == 1000000
            return AgentResult({"compliance": {}}, confidence=1)

    for name, cls in (("identity", Identity), ("income", Income), ("compliance", Compliance)):
        monkeypatch.setitem(master._AGENT_REGISTRY, name, cls)
    orchestrator = master.MasterOrchestrator(object(), rules={}, reconcile=False)

    async def survey(*args):
        return {
            "files": [
                {
                    "filename": d.filename,
                    "kind": kind,
                    "relevance": ["identity", "income", "compliance"],
                }
                for d, kind in zip(
                    documents, ("id_proof", "itr", "proposal_form", "other"), strict=True
                )
            ],
            "agents_to_dispatch": ["identity", "income", "compliance"],
        }

    monkeypatch.setattr(orchestrator, "_survey", survey)
    await orchestrator.run(tmp_path, "SYNTHETIC")
    assert calls["compliance"] == ["proposal.pdf"]
    assert "id.pdf" in calls["identity"] and "itr.pdf" in calls["income"]
    assert all("RECENTPHOTOGRAPH.jpg" not in names for names in calls.values())
