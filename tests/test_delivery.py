"""Delivery safety and source-document underwriting regression tests."""

import copy
import json
from types import SimpleNamespace

import pytest
from support.extraction_fixture import synthetic_case

from underwriting_platform.application.completion import assess_decision, delivery_outcome
from underwriting_platform.configuration.paths import CONFIG_ROOT
from underwriting_platform.engine import identity_verification, run_engine
from underwriting_platform.infrastructure import delivery as case_processing
from underwriting_platform.infrastructure.reproducibility import retain_inputs
from underwriting_platform.pipeline import batch, run
from underwriting_platform.qc import case_sheet
from underwriting_platform.qc.checks_completeness import check_calc_provenance

RULES = json.loads((CONFIG_ROOT / "uw_rules.json").read_text(encoding="utf8"))


@pytest.mark.parametrize(
    "field,path",
    [
        ("Business Name", "applicant.employer_name"),
        ("Nature of Business", "applicant.business_nature"),
    ],
)
async def test_source_pdf_identity_facts_reach_deterministic_gate(tmp_path, field, path):
    from reportlab.pdfgen import canvas

    from underwriting_platform.agents import IdentityAgent
    from underwriting_platform.llm.pdf_utils import prepare_doc

    fields = {
        "applicant_name": "Document Applicant",
        "age": 35,
        "gender": "Male",
        "occupation": "Self-Employed Business",
        "education_level": "Graduate",
        "residential_status": "Resident",
        "sum_assured": 10000000,
        path.split(".")[-1]: "Kirana store",
    }
    pdf = tmp_path / "source-proposal.pdf"
    drawing = canvas.Canvas(str(pdf))
    for index, (key, value) in enumerate(fields.items()):
        drawing.drawString(40, 780 - index * 22, f"{key}: {value}")
    drawing.save()
    document = prepare_doc(pdf)

    class ScriptedProvider:
        async def extract_json(self, system, blocks, **kwargs):
            # Controlled provider output grounded in the real prepared PDF.
            text = "\n".join(b.get("text", "") for b in blocks)
            for key, value in fields.items():
                assert f"{key}: {value}" in text
            return {
                "applicant": fields,
                "agent_meta": {
                    "confidence": 1.0,
                    "field_sources": {
                        "applicant." + key: [
                            {"document": document.filename, "page": 1, "raw_value": value}
                        ]
                        for key, value in fields.items()
                    },
                },
            }

    extracted = await IdentityAgent(ScriptedProvider()).extract([document])
    case = synthetic_case("DOCUMENT")
    case["applicant"].update(extracted.block["applicant"])
    case["field_sources"].update(extracted.field_sources)
    result = case_processing.process_case(
        case,
        RULES,
        tmp_path / "out",
        raw_input={"prepared_text": document.text},
        extraction_report={"provider": "scripted test provider"},
        source_directory=tmp_path,
    )
    decision = json.loads(
        (tmp_path / "out/DOCUMENT/DOCUMENT_decision.json").read_text(encoding="utf8")
    )
    gate = next(g for g in decision["gating"] if g["criterion"] == "sbi_occupation_income_proof")
    assert gate["status"] == "FAIL"
    assert "disallowed_petty_business_occupations" in gate["rule"]
    assert any(
        s.get("document") == "source-proposal.pdf"
        and s.get("page") == 1
        and s.get("raw_value") == "Kirana store"
        for s in gate["sources"]
    )
    assert result["verdict"] == "DECLINE" and result["delivery_allowed"]


def test_failed_rerun_cannot_leave_a_previous_deliverable(tmp_path):
    case = synthetic_case("STALE")
    successful = case_processing.process_case(case, RULES, tmp_path)
    assert successful["delivery_allowed"]

    def broken(*args, **kwargs):
        raise RuntimeError("renderer unavailable")

    failed = case_processing.process_case(case, RULES, tmp_path, renderer_fn=broken)
    assert not failed["delivery_allowed"]
    assert not (tmp_path / "STALE/STALE_delivered_decision.json").exists()
    assert not (tmp_path / "STALE/STALE_case_sheet.pdf").exists()
    decision = json.loads((tmp_path / "STALE/STALE_decision.json").read_text())
    assert decision["verdict"]["label"] == "INCOMPLETE"


@pytest.mark.parametrize("stage", ["engine", "rendering", "qc", "profile"])
def test_mandatory_failures_block_delivery_and_exit_nonzero(tmp_path, monkeypatch, stage):
    def failure(*args, **kwargs):
        raise RuntimeError("injected mandatory failure")

    options = {}
    if stage == "engine":
        options["engine_fn"] = failure
    elif stage == "rendering":
        options["renderer_fn"] = failure
    elif stage == "qc":
        options["qc_fn"] = failure
    else:
        monkeypatch.setattr(case_processing, "load_profile", failure)
    result = case_processing.process_case(synthetic_case("FAIL"), RULES, tmp_path, **options)
    assert result["exit_code"] != 0
    assert not result["delivery_allowed"]
    assert result["status"] == "INCOMPLETE"


def test_qc_check_exception_and_missing_registry_fail_closed(tmp_path, monkeypatch):
    from underwriting_platform.renderer import render_triage_sheet

    case = synthetic_case("QC")
    decision = run_engine(case, RULES, 10000000)
    pdf = tmp_path / "underwriting_platform.qc.pdf"
    render_triage_sheet(decision, pdf)

    def broken(ctx):
        raise RuntimeError("check error")

    monkeypatch.setattr(case_sheet, "_CHECKS", [("broken", broken)])
    monkeypatch.setattr(case_sheet, "_import_check_modules", lambda: None, raising=False)
    report = case_sheet.validate_case_sheet(decision, pdf, {})
    assert report.status == "BLOCK"
    assert any(f.code == "QC_INFRASTRUCTURE_FAILURE" for f in report.findings)


def test_qc_pass_is_not_equivalent_to_decision_complete():
    result = delivery_outcome(
        {"decision_complete": False, "issues": ["FIELD_EVIDENCE:age"]}, {"status": "PASS"}, []
    )
    assert result["qc_detected_no_block"]
    assert not result["delivery_allowed"]
    assert result["exit_code"] == 1


@pytest.mark.parametrize("missing", ["field", "source", "gate", "citation"])
def test_required_facts_gates_and_citations_are_enforced(missing):
    case = synthetic_case("AUDIT")
    decision = run_engine(case, RULES, 10000000)
    assert assess_decision(case, decision)["decision_complete"]
    if missing == "field":
        del case["applicant"]["occupation"]
    elif missing == "source":
        del case["field_sources"]["applicant.occupation"]
    elif missing == "gate":
        decision["gating"] = [
            g for g in decision["gating"] if g["criterion"] != "sbi_qualification"
        ]
    else:
        next(g for g in decision["gating"] if g["criterion"] == "sbi_qualification")["rule"] = ""
    assert not assess_decision(case, decision)["decision_complete"]


def test_calculation_needs_both_source_and_rule():
    for calc in (
        {"sources": [{"document": "proof.pdf"}], "rule": ""},
        {"sources": [], "rule": "carrier.income"},
    ):
        assert check_calc_provenance({"decision": {"calculations": {"income": calc}}})


async def test_internal_identity_exception_reaches_fail_closed_engine(tmp_path, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("classifier unavailable")

    async def noop(*args, **kwargs):
        pass

    monkeypatch.setattr(identity_verification, "classify_case_documents", broken)
    monkeypatch.setattr(identity_verification, "_resolve_dc_distance", noop)
    monkeypatch.setattr(identity_verification, "_resolve_photo_match", noop)
    monkeypatch.setattr(identity_verification, "_resolve_photo_match_set", noop)
    case = synthetic_case("IDENTITY")
    await identity_verification.run_identity_verification(case, tmp_path)
    assert any(error["stage"] == "identity_classification" for error in case["processing_errors"])
    assert run_engine(case, RULES, 10000000)["verdict"]["label"] == "INCOMPLETE"


def test_unresolved_identity_blocks_standard_delivery_even_if_engine_label_is_standard():
    case = synthetic_case("IDENTITY")
    decision = run_engine(case, RULES, 10000000)
    decision["verdict"]["label"] = "STANDARD_ISSUANCE"
    assert "IDENTITY_REVIEW_REQUIRED" in assess_decision(case, decision)["issues"]


def test_inputs_and_assets_retained_across_reruns(tmp_path):
    original = synthetic_case("REPLAY")
    first = retain_inputs(
        tmp_path,
        original,
        raw_input={"source": "first"},
        rules=RULES,
        provider={"backend": "fixture", "model_or_deployment": "fixture-v1"},
    )
    changed = copy.deepcopy(original)
    changed["applicant"]["age"] = 40
    second = retain_inputs(tmp_path, changed, raw_input={"source": "second"}, rules=RULES)
    assert first["run_id"] != second["run_id"]
    first_input = json.loads(
        (tmp_path / first["bundle_path"] / "normalized_input.json").read_text()
    )
    assert first_input["applicant"]["age"] == 66
    assert first["provider"]["model_or_deployment"] == "fixture-v1"
    assert any("sbi_smart_shield_rules.json" in name for name in first["assets_sha256"])
    assert any("base.py" in name for name in first["assets_sha256"])
    assert any("brand_profile_sbi.json" in name for name in first["assets_sha256"])


@pytest.mark.parametrize("error", ["extraction", "identity", "engine", "qc"])
async def test_single_and_batch_share_failure_exit_policy(tmp_path, monkeypatch, error):
    class Orch:
        def __init__(self, *a, **kw):
            pass

        async def run(self, *args):
            if error == "extraction":
                raise RuntimeError("extraction error")
            return SimpleNamespace(case_json=synthetic_case("FAIL"), extraction_report={})

    async def identity(*args):
        if error == "identity":
            raise RuntimeError("identity error")

    def failure(*args, **kwargs):
        raise RuntimeError("mandatory error")

    monkeypatch.setattr(identity_verification, "run_identity_verification", identity)
    monkeypatch.setattr(run, "_load_env", lambda: None)
    monkeypatch.setattr(run, "build_client", lambda: object())
    monkeypatch.setattr(run, "MasterOrchestrator", Orch)
    if error == "engine":
        monkeypatch.setattr(run, "run_engine", failure)
        monkeypatch.setattr(batch, "run_engine", failure)
    if error == "qc":
        monkeypatch.setattr(case_processing, "validate_case_sheet", failure)
    args = SimpleNamespace(
        case_dir=tmp_path,
        case_id="FAIL",
        output_dir=tmp_path / "single",
        proposed_sa=10000000,
        no_pdf=False,
        no_engine=False,
        push_db=False,
    )
    assert await run.main_async(args) == 1
    summary = await batch.run_one(Orch(), tmp_path, "FAIL", tmp_path / "batch", RULES)
    assert summary["exit_code"] == 1 and summary["status"] == "INCOMPLETE"
