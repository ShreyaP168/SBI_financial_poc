import copy
import json

import pytest
from support.extraction_fixture import synthetic_case

from underwriting_platform.agents.source_reader import SourceReader, source_batches
from underwriting_platform.configuration.paths import CONFIG_ROOT
from underwriting_platform.engine import run_engine
from underwriting_platform.llm.pdf_utils import Document


@pytest.mark.parametrize("trade_field", ["employer_name", "business_nature"])
async def test_single_source_read_retains_trade_provenance_and_decline_gate(trade_field):
    case = synthetic_case("SOURCE")
    case["applicant"].update(age=35, occupation="Self-Employed Business")
    case["applicant"][trade_field] = "Kirana store"
    sources = {
        "applicant." + key: [{"document": "synthetic-proposal.pdf", "page": 1, "raw_value": value}]
        for key, value in case["applicant"].items()
    }
    sources["income.itr_years"] = [
        {"document": "synthetic-itr.pdf", "page": 1, "raw_value": case["income"]["itr_years"]}
    ]
    calls = []

    class Provider:
        async def extract_json(self, system, blocks, **kwargs):
            calls.append(blocks)
            text = "\n".join(b.get("text", "") for b in blocks)
            assert text.count("=== DOCUMENT: synthetic-proposal.pdf") == 1
            assert text.count("=== DOCUMENT: synthetic-itr.pdf") == 1
            assert kwargs["max_tokens"] == 16000
            payload = copy.deepcopy(case)
            payload["agent_meta"] = {"confidence": 1, "field_sources": sources}
            return payload

    documents = [
        Document("synthetic-proposal.pdf", "Kirana store", page_count=1),
        Document("synthetic-itr.pdf", "Income evidence", page_count=1),
    ]
    result = await SourceReader(Provider(), ["identity", "income", "compliance"]).extract(documents)
    case.update(result.block)
    case["field_sources"] = result.field_sources
    rules = json.loads((CONFIG_ROOT / "uw_rules.json").read_text(encoding="utf8"))
    decision = run_engine(case, rules, 10000000)
    gate = next(g for g in decision["gating"] if g["criterion"] == "sbi_occupation_income_proof")
    assert gate["status"] == "FAIL"
    assert "disallowed_petty_business_occupations" in gate["rule"]
    assert any(s.get("raw_value") == "Kirana store" for s in gate["sources"])
    assert decision["verdict"]["label"] == "DECLINE"
    assert len(calls) == 1 and not result.source_findings


def test_source_packing_uses_actual_images_and_preserves_each_document_once():
    documents = [Document(str(i), "Evidence", ["image"] * 12, 12) for i in range(10)]
    batches = list(source_batches(documents, max_images=80))
    assert len(batches) == 2
    assert [d.filename for batch in batches for d in batch] == [d.filename for d in documents]
    assert all(sum(len(d.images_b64) for d in batch) <= 80 for batch in batches)


async def test_orchestrator_partitions_one_read_without_duplicate_raw_payload(
    tmp_path, monkeypatch
):
    import underwriting_platform.orchestrator.master as master

    monkeypatch.setenv("UW_EXTRACTION_MODE", "source_once")
    documents = [
        Document("synthetic-proposal.pdf", "Applicant evidence", page_count=1),
        Document("synthetic-itr.pdf", "Income evidence", page_count=1),
    ]
    monkeypatch.setattr(
        master, "_collect_case_files", lambda *a: [tmp_path / d.filename for d in documents]
    )
    monkeypatch.setattr(
        master, "prepare_doc", lambda path: next(d for d in documents if d.filename == path.name)
    )
    calls = []

    class Provider:
        async def extract_json(self, *args, **kwargs):
            calls.append(1)
            payload = synthetic_case("SOURCE")
            payload["agent_meta"] = {"confidence": 1, "field_sources": payload.pop("field_sources")}
            return payload

    orchestrator = master.MasterOrchestrator(Provider(), rules={}, reconcile=True)

    async def unexpected_reconciler(*args, **kwargs):
        pytest.fail("Source views must not invoke a paid independent-agent reconciler")

    monkeypatch.setattr(orchestrator.reconciler, "reconcile", unexpected_reconciler)

    async def survey(*args):
        return {
            "files": [{"filename": d.filename, "kind": "other"} for d in documents],
            "agents_to_dispatch": ["identity", "income", "compliance"],
        }

    monkeypatch.setattr(orchestrator, "_survey", survey)
    result = await orchestrator.run(tmp_path, "SOURCE")
    assert len(calls) == 1
    assert result.case_json["applicant"]["age"] == 66
    assert result.case_json["income"]["annual_income"] == 2000000
    assert result.extraction_report["source_owner"]["documents_consumed"] == [
        d.filename for d in documents
    ]
    assert all(not agent.raw_payload for agent in result.agent_outputs.values())
    assert result.extraction_report["source_owner"]["raw_payload"]["applicant"]["age"] == 66
