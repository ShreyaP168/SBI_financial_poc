"""
pipeline/batch.py — run v9 over a batch of cases with bounded concurrency.

Usage:
    python -m underwriting_platform.pipeline.batch --source_root "<dir>" --output_dir output --concurrency 4 \
                             [--cases 24_C684869052 27_U040681175 ...]
"""

from __future__ import annotations
from underwriting_platform.configuration.paths import CONFIG_ROOT, OUTPUT_ROOT
import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

# Windows consoles default stdout to cp1252 when output is redirected /
# piped / logged, which crashes on the unicode glyphs in our progress
# prints (→ ✓ ✗ ↷ ⊘ ⓓ). Force UTF-8 so a logged batch run never dies on
# a print() before any case is processed.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from underwriting_platform.llm import build_client
from underwriting_platform.orchestrator import MasterOrchestrator
from underwriting_platform.engine import run_engine
from underwriting_platform.renderer.triage_sheet import render_triage_sheet


def _load_env() -> None:
    from underwriting_platform.configuration.secrets import load_secret_environment

    load_secret_environment()


def _load_rules() -> dict:
    return json.loads((CONFIG_ROOT / "uw_rules.json").read_text(encoding="utf-8"))


async def run_one(
    orch: MasterOrchestrator,
    case_dir: Path,
    case_id: str,
    out_dir: Path,
    rules: dict,
    proposed_sa: int = 0,
    emit_pdf: bool = True,
) -> dict:
    from underwriting_platform.infrastructure.delivery import extract_and_process

    t0 = time.time()
    outcome = await extract_and_process(
        orch,
        case_dir,
        case_id,
        out_dir,
        rules,
        proposed_sa=proposed_sa,
        emit_pdf=emit_pdf,
        engine_fn=run_engine,
        renderer_fn=render_triage_sheet,
    )
    outcome.update({"elapsed_s": round(time.time() - t0, 1), "case_dir": str(case_dir)})
    return outcome


async def main_async(args: argparse.Namespace) -> int:
    from underwriting_platform.infrastructure.files import atomic_json

    try:
        _load_env()
        rules = _load_rules()
        client = build_client()
        orch = MasterOrchestrator(client, rules=rules)
    except Exception:
        failure_root = Path(args.output_dir)
        failure_root.mkdir(parents=True, exist_ok=True)
        atomic_json(
            failure_root / "startup_failure.json",
            {"status": "INCOMPLETE", "delivery_allowed": False, "error_code": "STARTUP_FAILED"},
        )
        return 1

    source_root = Path(args.source_root)
    if not source_root.exists():
        raise FileNotFoundError(f"--source_root not found: {source_root}")

    # Discover cases. If --cases is given, use those. Else use every subdir.
    if args.cases:
        case_dirs = [(source_root / c) for c in args.cases]
    else:
        case_dirs = [d for d in source_root.iterdir() if d.is_dir()]

    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(args.concurrency)

    async def _worker(case_dir: Path):
        if not case_dir.exists():
            return {"case_id": case_dir.name, "status": "SKIP_NO_DIR", "elapsed_s": 0}
        # Strip leading "NN_" prefix from case_id when present (matches Policy_Data convention).
        case_id = case_dir.name
        if "_" in case_id and case_id.split("_", 1)[0].isdigit():
            case_id = case_id.split("_", 1)[1]
        async with sem:
            print(f"  [START] {case_dir.name} → case_id={case_id}", flush=True)
            r = await run_one(
                orch,
                case_dir,
                case_id,
                out_root,
                rules=rules,
                proposed_sa=args.proposed_sa,
                emit_pdf=not args.no_pdf,
            )
            status = r.get("status", "?")
            name = r.get("applicant_name") or "—"
            verd = r.get("verdict") or "—"
            db_tag = "  ⓓ" if r.get("db_pushed") else ("  ⓓ✗" if r.get("db_error") else "")
            print(
                f"  [DONE ] {case_id:14s} {status:6s} ({r['elapsed_s']:5.1f}s)  "
                f"verdict={verd:<20s}  name={name[:30]}{db_tag}",
                flush=True,
            )
            return r

    print(f"\n  Launching {len(case_dirs)} cases @ concurrency={args.concurrency}\n")
    t0 = time.time()
    results = await asyncio.gather(*[_worker(d) for d in case_dirs])
    total_s = round(time.time() - t0, 1)

    # Save batch summary
    summary_path = out_root / "batch_summary.json"
    qc_blocked = sum(1 for r in results if r.get("qc_blocked"))
    # Token usage — cumulative LLM consumption across all cases.
    try:
        token_usage = client.get_usage_summary()
    except Exception as e:
        token_usage = {"error": f"{type(e).__name__}: {e}"}
    summary = {
        "total_cases": len(results),
        "successful": sum(1 for r in results if r.get("exit_code", 1) == 0),
        "failed": sum(1 for r in results if r.get("exit_code", 1) != 0),
        "skipped": sum(1 for r in results if r.get("status") == "SKIP_NO_DIR"),
        "qc_blocked": qc_blocked,
        "total_elapsed_s": total_s,
        "token_usage": token_usage,
        "results": results,
    }
    summary_path.write_text(
        json.dumps(summary, indent=2, default=str, ensure_ascii=False), encoding="utf-8"
    )

    # Print final table
    print(f"\n  Batch complete in {total_s:.1f}s")
    print(
        f"  ✓ {summary['successful']}/{len(results)} succeeded   "
        f"✗ {summary['failed']} failed   ↷ {summary['skipped']} skipped"
        f"   ⊘ {qc_blocked} QC-blocked"
    )
    # Token usage roll-up
    tu = summary.get("token_usage") or {}
    if "error" not in tu:
        print(f"\n  LLM tokens (cumulative):")
        print(
            f"    calls={tu.get('call_count', 0):,}   "
            f"input={tu.get('input_tokens', 0):,}   "
            f"output={tu.get('output_tokens', 0):,}   "
            f"cache_create={tu.get('cache_creation_tokens', 0):,}   "
            f"cache_read={tu.get('cache_read_tokens', 0):,}   "
            f"TOTAL={tu.get('total_tokens', 0):,}"
        )
        for m, mu in (tu.get("by_model") or {}).items():
            print(
                f"      {m}: {mu['call_count']:,} calls · "
                f"in={mu['input_tokens']:,} out={mu['output_tokens']:,} "
                f"total={mu['total_tokens']:,}"
            )
    print(f"\n  Summary: {summary_path}\n")
    # Non-zero exit if any case was QC-blocked — CI picks this up.
    return 1 if any(r.get("exit_code", 1) != 0 for r in results) else 0


def main() -> int:
    p = argparse.ArgumentParser(description="UW v9 — batch IDP run")
    p.add_argument("--source_root", required=True, help="Folder containing per-case sub-folders")
    p.add_argument("--output_dir", default=str(OUTPUT_ROOT))
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--cases", nargs="*", help="Specific case folder names to run")
    p.add_argument(
        "--proposed_sa",
        type=int,
        default=0,
        help="Proposed SA in INR applied to every case in this batch",
    )
    p.add_argument(
        "--no-pdf",
        action="store_true",
        help="Skip PDF rendering (still writes case_json + decision_json)",
    )
    args = p.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
