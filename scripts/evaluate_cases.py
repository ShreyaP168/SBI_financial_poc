import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "Input" / "Financial Evaluation"
OUTPUT = ROOT / "output"
TRUTH = OUTPUT / "truth"
SECRETS = ROOT.parent / ".local-secrets" / "uw.env"
CASES = (
    "4CCZ525812",
    "4CCZ525814",
    "4CCZ526214",
    "4CCZ526688",
    "4CPB014070",
    "4CYL218379",
    "4CYL294940",
)
EDUCATION = (
    ("post", "Postgraduate"),
    ("professional", "Professional Degree"),
    ("graduate", "Graduate"),
    ("12", "HSC-12th"),
    ("hsc", "HSC-12th"),
    ("below", "Below SSC"),
    ("illiterate", "Below SSC"),
    ("10", "SSC-10th"),
    ("ssc", "SSC-10th"),
)


def run_case(case):
    env = {**os.environ, "UW_DEV_ENV_FILE": str(SECRETS), "UW_EXTRACTION_MODE": "local_first"}
    env["UW_TARGETED_LLM_FALLBACK"] = "true"
    started = time.time()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "underwriting_platform.pipeline.run",
            "--case_dir",
            str(INPUT / case),
            "--case_id",
            case,
        ],
        env=env,
        cwd=ROOT,
        capture_output=True,
        timeout=3600,
    )
    return round(time.time() - started)


def education_class(raw):
    text = str(raw or "").casefold()
    return next((label for key, label in EDUCATION if key in text), None)


def canonical(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def verdict_label(outcome, decision):
    label = (decision.get("verdict") or {}).get("label")
    if label in {None, "INCOMPLETE", "ERROR"}:
        label = outcome.get("diagnostic_verdict") or label
    return str(label or "").replace("_", " ").upper()


def compare(case):
    truth = json.loads((TRUTH / f"{case}_sheet.json").read_text(encoding="utf8"))
    folder = OUTPUT / case
    outcome = json.loads((folder / f"{case}_outcome.json").read_text(encoding="utf8"))
    decision = json.loads((folder / f"{case}_decision.json").read_text(encoding="utf8"))
    extraction = json.loads((folder / f"{case}_extraction_report.json").read_text(encoding="utf8"))
    applicant = (decision.get("applicant") or {}) or {}
    calculations = decision.get("calculations") or {}
    got = {
        "name": applicant.get("applicant_name"),
        "age": applicant.get("age"),
        "gender": applicant.get("gender"),
        "education": education_class(applicant.get("education_level")),
        "sum_assured": decision.get("proposed_sa") or applicant.get("sum_assured"),
        "verified_income": (calculations.get("selected_income") or {}).get("value"),
        "declared_income": applicant.get("declared_annual_income"),
    }
    want = {
        "name": truth.get("name"),
        "age": truth.get("age"),
        "gender": truth.get("gender"),
        "education": education_class(truth.get("education")),
        "sum_assured": truth.get("sum_assured"),
        "verified_income": truth.get("verified_income"),
        "declared_income": truth.get("declared_income"),
    }
    rows = []
    for field, expected in want.items():
        if expected is None:
            continue
        actual = got[field]
        if field == "name":
            ok = canonical(actual) == canonical(expected)
        elif field in {"verified_income", "declared_income", "sum_assured", "age"}:
            ok = actual is not None and abs(int(actual) - int(expected)) <= max(1, int(expected) // 1000)
        else:
            ok = actual == expected
        rows.append({"field": field, "expected": expected, "got": actual, "ok": ok})
    ours = verdict_label(outcome, decision)
    return {
        "case": case,
        "sheet_decision": truth["decision"],
        "our_decision": ours,
        "decision_match": ours == truth["decision"],
        "sheet_reason": truth.get("reason"),
        "status": outcome.get("status"),
        "delivery_allowed": outcome.get("delivery_allowed"),
        "blockers": outcome.get("blocking_reasons"),
        "fields": rows,
        "fields_correct": sum(r["ok"] for r in rows),
        "fields_compared": len(rows),
        "tokens": (extraction.get("token_budget") or {}).get("spent"),
        "files": len(extraction.get("survey", {}).get("files", [])),
        "other_documents": sum(f.get("kind") == "other" for f in extraction.get("survey", {}).get("files", [])),
    }


def main():
    only = sys.argv[1:] or CASES
    results = []
    for case in only:
        seconds = run_case(case)
        try:
            result = compare(case)
            result["seconds"] = seconds
        except FileNotFoundError:
            result = {"case": case, "error": "no outputs written", "seconds": seconds}
        results.append(result)
        print(json.dumps({k: v for k, v in result.items() if k not in {"fields", "blockers"}}), flush=True)
    folder = OUTPUT / "evaluation"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "summary.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
