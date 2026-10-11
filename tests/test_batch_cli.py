"""The real CLI namespace contains no removed analytics arguments."""

from types import SimpleNamespace

from underwriting_platform.pipeline import batch


async def test_batch_cli_starts_with_current_parser_arguments(tmp_path, monkeypatch):
    source = tmp_path / "cases"
    (source / "CASE").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(batch, "_load_env", lambda: None)
    monkeypatch.setattr(batch, "_load_rules", lambda: {})
    monkeypatch.setattr(
        batch, "build_client", lambda: SimpleNamespace(get_usage_summary=lambda: {})
    )
    monkeypatch.setattr(batch, "MasterOrchestrator", lambda *args, **kwargs: object())

    async def run_one(orch, folder, case_id, output, **kwargs):
        calls.append(case_id)
        return {
            "case_id": case_id,
            "status": "OK",
            "verdict": "DECLINE",
            "elapsed_s": 0,
            "exit_code": 0,
            "delivery_allowed": True,
        }

    monkeypatch.setattr(batch, "run_one", run_one)
    args = SimpleNamespace(
        source_root=source,
        output_dir=tmp_path / "output",
        concurrency=1,
        cases=None,
        proposed_sa=0,
        no_pdf=False,
    )
    assert await batch.main_async(args) == 0
    assert calls == ["CASE"]
    assert (args.output_dir / "batch_summary.json").is_file()
