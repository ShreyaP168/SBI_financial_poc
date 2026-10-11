"""Shared delivery boundary for CLI, batch and service canonical cases."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path

from underwriting_platform.application.completion import assess_decision, delivery_outcome
from underwriting_platform.configuration.paths import CONFIG_ROOT
from underwriting_platform.engine import run_engine
from underwriting_platform.infrastructure.files import atomic_json, scoped_path
from underwriting_platform.infrastructure.reproducibility import retain_inputs
from underwriting_platform.qc import load_profile, validate_case_sheet
from underwriting_platform.renderer import render_triage_sheet


def process_case(
    case: dict,
    rules: dict,
    output_root: Path,
    *,
    proposed_sa: int = 0,
    emit_pdf: bool = True,
    raw_input=None,
    extraction_report=None,
    failures=None,
    provider=None,
    engine_fn=None,
    renderer_fn=None,
    qc_fn=None,
    extract_only=False,
    stage_callback=None,
    source_directory=None,
    config_root=CONFIG_ROOT,
    folder_override=None,
    external_directory=None,
) -> dict:
    case = copy.deepcopy(case)
    cid = case["case_id"]
    folder = (
        Path(folder_override)
        if folder_override is not None
        else scoped_path(Path(output_root), cid)
    )
    folder.mkdir(parents=True, exist_ok=True)
    # A prior successful delivery marker must not survive a failed rerun.
    atomic_json(folder / "delivery.json", {"delivery_allowed": False, "status": "PROCESSING"})
    (folder / f"{cid}_case_sheet.pdf").unlink(missing_ok=True)
    (folder / f"{cid}_delivered_decision.json").unlink(missing_ok=True)
    failed = list(failures or [])
    decision = {
        "case_id": cid,
        "applicant": case.get("applicant") or {},
        "verdict": {"label": "INCOMPLETE", "decision_drivers": ["Mandatory processing incomplete"]},
        "gating": [],
        "calculations": {},
        "processing_errors": [],
    }
    qc_data = None
    try:
        retain_inputs(
            folder,
            case,
            raw_input=raw_input,
            extraction_report=extraction_report,
            rules=rules,
            provider=provider,
            source_directory=source_directory,
            config_root=config_root,
            external_directory=external_directory,
        )
    except Exception:
        failed.append("INPUT_RETENTION_FAILED")
    atomic_json(folder / f"{cid}_case.json", case)
    atomic_json(folder / f"{cid}_extraction_report.json", extraction_report or {})
    if extract_only:
        outcome = {
            "case_id": cid,
            "status": "EXTRACTION_ONLY",
            "delivery_allowed": False,
            "decision_complete": False,
            "exit_code": 1 if failed or case.get("processing_errors") else 0,
        }
        atomic_json(folder / "delivery.json", outcome)
        return outcome
    if stage_callback:
        stage_callback("engine")
    try:
        sa = int(
            proposed_sa
            or (case.get("applicant") or {}).get("sum_assured")
            or (case.get("policy_data") or {}).get("sum_assured")
            or 0
        )
        decision = (engine_fn or run_engine)(copy.deepcopy(case), rules, proposed_sa=sa)
    except Exception:
        failed.append("ENGINE_FAILED")
    try:
        completion = assess_decision(case, decision)
    except Exception:
        failed.append("COMPLETENESS_CHECK_FAILED")
        completion = {"decision_complete": False, "issues": ["COMPLETENESS_CHECK_FAILED"]}
    decision["_completion"] = completion
    # Retained decision is diagnostic until delivery.json allows its release.
    atomic_json(folder / f"{cid}_decision.json", decision)
    if emit_pdf:
        if stage_callback:
            stage_callback("rendering")
        try:
            display = copy.deepcopy(decision)
            if failed or not completion["decision_complete"]:
                display["verdict"] = {
                    "label": "INCOMPLETE",
                    "decision_drivers": ["Decision delivery blocked; manual review required"],
                }
            (renderer_fn or render_triage_sheet)(display, folder / f"{cid}_case_sheet.pdf")
        except Exception:
            failed.append("RENDERING_FAILED")
        if "RENDERING_FAILED" not in failed:
            if stage_callback:
                stage_callback("qc")
            try:
                profile = load_profile(Path(config_root) / "brand_profile_sbi.json", required=True)
                report = (qc_fn or validate_case_sheet)(
                    decision,
                    folder / f"{cid}_case_sheet.pdf",
                    profile,
                    decision_path=str(folder / f"{cid}_decision.json"),
                )
                qc_data = report.to_dict()
            except Exception:
                failed.append("QC_INFRASTRUCTURE_FAILED")
        if qc_data is None:
            qc_data = {
                "status": "BLOCK",
                "findings": [{"code": "QC_INFRASTRUCTURE_FAILURE", "severity": "BLOCK"}],
            }
        qc_data["decision_complete"] = completion["decision_complete"]
        atomic_json(folder / f"{cid}_qc_report.json", qc_data)
    outcome = delivery_outcome(completion, qc_data, failed, pdf_required=emit_pdf)
    outcome["infrastructure_failed"] = bool(
        failed
        or case.get("processing_errors")
        or decision.get("processing_errors")
        or any(
            f.get("code") == "QC_INFRASTRUCTURE_FAILURE"
            for f in (qc_data or {}).get("findings", [])
        )
    )
    outcome.update(
        {
            "case_id": cid,
            "verdict": decision["verdict"]["label"]
            if outcome["delivery_allowed"]
            else "INCOMPLETE",
            "diagnostic_verdict": (decision.get("_diagnostic_verdict") or decision["verdict"])[
                "label"
            ],
            "qc_status": (qc_data or {}).get("status"),
            "qc_blocked": bool(qc_data and qc_data["status"] == "BLOCK"),
        }
    )
    decision["_delivery"] = outcome
    atomic_json(folder / f"{cid}_diagnostic_decision.json", decision)
    published = copy.deepcopy(decision)
    if not outcome["delivery_allowed"]:
        published["verdict"] = {
            "label": "INCOMPLETE",
            "decision_drivers": [
                "Decision delivery blocked; inspect outcome and diagnostic decision"
            ],
        }
    atomic_json(folder / f"{cid}_decision.json", published)
    if outcome["delivery_allowed"]:
        atomic_json(folder / f"{cid}_delivered_decision.json", published)
    atomic_json(folder / "delivery.json", outcome)
    atomic_json(folder / f"{cid}_outcome.json", outcome)
    return outcome


async def _extract_and_process(
    orch,
    case_dir: Path,
    case_id: str,
    output_root: Path,
    rules: dict,
    *,
    proposed_sa=0,
    emit_pdf=True,
    engine_fn=None,
    renderer_fn=None,
    extract_only=False,
):
    failed = []
    case = {"case_id": case_id, "processing_errors": []}
    report = {}
    try:
        result = await orch.run(case_dir, case_id)
        case, report = result.case_json, result.extraction_report
        if case.get("case_id") != case_id:
            raise ValueError("Cross-case extraction response")
        case.setdefault("_processing_stages", {})["extraction"] = "completed"
    except Exception:
        failed.append("EXTRACTION_FAILED")
        case = {"case_id": case_id, "processing_errors": [{"stage": "extraction"}]}
    if not failed and not (
        case.get("_extraction_mode") == "local_first" and case.get("processing_errors")
    ):
        try:
            from underwriting_platform.engine.identity_verification import run_identity_verification

            await run_identity_verification(case, Path(case_dir))
            case.setdefault("_processing_stages", {})["identity"] = "completed"
        except Exception:
            failed.append("IDENTITY_FAILED")
            case.setdefault("processing_errors", []).append({"stage": "identity"})
    from underwriting_platform.llm.budget import current_budget

    budget = current_budget.get()
    if budget is not None:
        case["_token_budget"] = budget.snapshot()
        report["token_budget"] = budget.snapshot()
        report["provider_usage"] = list(budget.usage)
    provider = {}
    cfg = getattr(getattr(orch, "llm", None), "cfg", None)
    if cfg:
        provider = {
            "backend": cfg.backend,
            "model_or_deployment": cfg.model,
            "api_version": cfg.api_version,
        }
    return await asyncio.to_thread(
        process_case,
        case,
        rules,
        Path(output_root),
        proposed_sa=proposed_sa,
        emit_pdf=emit_pdf,
        raw_input={
            "source_documents": report.get("documents", []),
            "agent_outputs": report.get("agents", {}),
        },
        extraction_report=report,
        failures=failed,
        provider=provider,
        engine_fn=engine_fn,
        renderer_fn=renderer_fn,
        extract_only=extract_only,
        source_directory=Path(case_dir),
    )


async def extract_and_process(orch, case_dir, case_id, output_root, rules, **options):
    from contextlib import nullcontext

    from underwriting_platform.llm.budget import case_scope

    cfg = getattr(getattr(orch, "llm", None), "cfg", None)
    scope = case_scope(cfg.token_budget_per_case) if cfg is not None else nullcontext(None)
    with scope:
        return await _extract_and_process(orch, case_dir, case_id, output_root, rules, **options)
