"""Single-case extraction, deterministic underwriting, PDF/QC and delivery CLI."""

from __future__ import annotations
from underwriting_platform.configuration.paths import CONFIG_ROOT, OUTPUT_ROOT
import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# Windows consoles default stdout to cp1252 when output is redirected /
# piped / logged, which crashes on the unicode glyphs in our progress
# prints (→ ✓ etc.). Force UTF-8 so a logged run never dies on a print().
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from underwriting_platform.llm import build_client
from underwriting_platform.orchestrator import MasterOrchestrator
from underwriting_platform.engine import run_engine
from underwriting_platform.renderer import render_triage_sheet


def _load_env() -> None:
    from underwriting_platform.configuration.secrets import load_secret_environment

    load_secret_environment()


def _load_rules() -> dict:
    rules_path = CONFIG_ROOT / "uw_rules.json"
    return json.loads(rules_path.read_text(encoding="utf-8"))


async def main_async(args: argparse.Namespace) -> int:
    from underwriting_platform.infrastructure.delivery import extract_and_process
    from underwriting_platform.infrastructure.files import atomic_json

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    try:
        _load_env()
        rules = _load_rules()
        client = build_client()
        orch = MasterOrchestrator(client, rules=rules)
    except Exception:
        atomic_json(
            output / "startup_failure.json",
            {"status": "INCOMPLETE", "delivery_allowed": False, "error_code": "STARTUP_FAILED"},
        )
        return 1
    try:
        outcome = await extract_and_process(
            orch,
            Path(args.case_dir),
            args.case_id,
            output,
            rules,
            proposed_sa=args.proposed_sa,
            emit_pdf=not args.no_pdf and not args.no_engine,
            engine_fn=run_engine,
            renderer_fn=render_triage_sheet,
            extract_only=args.no_engine,
        )
        print(
            f"{args.case_id}: {outcome['status']}; delivery_allowed={outcome['delivery_allowed']}"
        )
        return outcome["exit_code"]
    except Exception:
        atomic_json(
            output / "processing_failure.json",
            {"status": "INCOMPLETE", "delivery_allowed": False, "error_code": "PROCESSING_FAILED"},
        )
        return 1
    finally:
        sdk = getattr(client, "_anthropic", None)
        if sdk is not None:
            await sdk.close()


def main() -> int:
    p = argparse.ArgumentParser(description="UW v9 — agentic IDP")
    p.add_argument("--case_dir", required=True, help="Folder containing case PDFs")
    p.add_argument("--case_id", required=True, help="Case identifier (used in output paths)")
    p.add_argument(
        "--output_dir",
        default=str(OUTPUT_ROOT),
        help="Where to write case_json + report",
    )
    p.add_argument(
        "--proposed_sa",
        type=int,
        default=0,
        help="Proposed SA in INR (drives Phase-6 max-SA + verdict gating)",
    )
    p.add_argument(
        "--no-engine", action="store_true", help="Skip Phase-6 engine + PDF (extraction only)"
    )
    p.add_argument("--no-pdf", action="store_true", help="Run engine but skip PDF rendering")
    p.add_argument("--verbose", "-v", action="store_true", help="Enable DEBUG logging")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
