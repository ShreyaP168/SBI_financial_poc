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


async def test_legacy_domain_setting_uses_one_local_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("UW_EXTRACTION_MODE", "domain")
    import underwriting_platform.extraction.coordinator as coordinator
    import underwriting_platform.orchestrator.master as master

    document = Document("proposal.pdf", "Applicant Age: 35\nSum Assured: 1000000")
    monkeypatch.setattr(master, "_collect_case_files", lambda *a: [tmp_path / document.filename])
    monkeypatch.setattr(master, "prepare_doc", lambda *a: document)
    calls = []

    async def shared_reader(client, documents):
        calls.append([d.filename for d in documents])
        return AgentResult(
            {
                "applicant": {"age": 35, "sum_assured": 1000000},
                "income": {"annual_income": 1000000},
                "compliance": {},
            },
            confidence=1,
        ), {"unresolved": []}

    monkeypatch.setattr(coordinator, "extract_local_first", shared_reader)
    orchestrator = master.MasterOrchestrator(object(), rules={}, reconcile=False)

    async def survey(*a):
        return {"files": [], "agents_to_dispatch": ["identity", "income", "compliance"]}

    monkeypatch.setattr(orchestrator, "_survey", survey)
    result = await orchestrator.run(tmp_path, "SYNTHETIC")
    assert calls == [["proposal.pdf"]]
    assert result.case_json["_extraction_mode"] == "local_first"
    assert result.case_json["applicant"]["age"] == 35
    assert result.case_json["income"]["annual_income"] == 1000000
