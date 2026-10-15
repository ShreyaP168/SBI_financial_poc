"""Evidence-based confidence for every extracted field, with optional comparison to verified values."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
from datetime import date
from pathlib import Path

from underwriting_platform.configuration.paths import RUNTIME_ROOT
from underwriting_platform.extraction.documents import iso_date, names_compatible

FIELDS = (
    "applicant.applicant_name",
    "applicant.dob",
    "applicant.age",
    "applicant.gender",
    "applicant.pan",
    "applicant.occupation",
    "applicant.education_level",
    "applicant.residential_status",
    "applicant.sum_assured",
    "applicant.declared_annual_income",
    "applicant.employer_name",
    "applicant.business_nature",
    "applicant.nominees",
    "applicant.riders",
    "income.primary_source",
    "income.annual_income",
    "compliance.existing_cover_declared",
)
SOURCE_ALIASES = {
    "income.primary_source": ("income.itr_years", "income.annual_income"),
    "compliance.existing_cover_declared": ("compliance.existing_cover_declared",),
}
NEUTRAL_LABELS = {"FORM_OR_TEXT", "CONTINUATION", "OTHER"}
GENDERS = {"Male", "Female", "Other", "Transgender"}


class NoCloud:
    async def extract_json(self, *args, **kwargs):
        raise RuntimeError("Cloud calls are disabled; pass --fallback to allow them")


def value_at(case, path):
    value = case
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def reference_date(case):
    raw = (case.get("applicant") or {}).get("submission_date")
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return date.today()


def calendar_age(dob, today):
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def _positive_int(value, case):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _dob(value, case):
    try:
        born = date.fromisoformat(str(value))
    except ValueError:
        return False
    return 18 <= calendar_age(born, reference_date(case)) <= 80


def _age(value, case):
    if not isinstance(value, int) or not 18 <= value <= 80:
        return False
    try:
        born = date.fromisoformat(str(value_at(case, "applicant.dob")))
    except ValueError:
        return True
    return abs(value - calendar_age(born, reference_date(case))) <= 1


def _text(value, case):
    return isinstance(value, str) and bool(value.strip())


VALIDATORS = {
    "applicant.applicant_name": lambda v, c: bool(re.fullmatch(r"[A-Za-z][A-Za-z .'-]{2,}", str(v))),
    "applicant.dob": _dob,
    "applicant.age": _age,
    "applicant.gender": lambda v, c: v in GENDERS,
    "applicant.pan": lambda v, c: bool(re.fullmatch(r"[A-Z]{3}[PFCHATBLJG][A-Z]\d{4}[A-Z]", str(v))),
    "applicant.occupation": _text,
    "applicant.education_level": _text,
    "applicant.residential_status": _text,
    "applicant.sum_assured": _positive_int,
    "applicant.declared_annual_income": _positive_int,
    "income.annual_income": lambda v, c: isinstance(v, int) and v >= 0,
    "applicant.nominees": lambda v, c: isinstance(v, list)
    and bool(v)
    and sum(n.get("share_pct") or 0 for n in v) == 100,
}


def signature(source, kinds):
    kind, label = kinds.get((source["document"], source["page"]), ("", ""))
    return label if label and label not in NEUTRAL_LABELS else kind or source["document"]


def page_confidence(ocr, source):
    pages = ocr.get(source["document"]) or []
    page = next((p for p in pages if p.get("page") == source["page"]), None)
    values = [
        line.get("mean_confidence") if line.get("mean_confidence") is not None else line["confidence"]
        for line in (page or {}).get("lines", [])
    ]
    return statistics.mean(values) if values else None


def field_report(case, report, path):
    kinds = {
        (row["filename"], page["page"]): (page["kind"], page["label"])
        for row in (case.get("_document_classification") or {}).get("files", [])
        for page in row.get("pages", [])
    }
    ocr = (report.get("local_ocr") or {}).get("documents") or {}
    sources = []
    for alias in SOURCE_ALIASES.get(path, (path,)):
        sources += (case.get("field_sources") or {}).get(alias, [])
    value = value_at(case, path)
    conflicted = any(
        finding.get("field") == path and "CONFLICT" in finding.get("code", "")
        for finding in case.get("extraction_findings") or []
    )
    if conflicted:
        return {"field": path, "value": value, "status": "conflict", "confidence": 0.0, "sources": []}
    if value in (None, "", [], {}):
        return {"field": path, "value": None, "status": "missing", "confidence": 0.0, "sources": []}
    signatures = {signature(source, kinds) for source in sources}
    base = {0: 0.5, 1: 0.6, 2: 0.85}.get(len(signatures), 0.95)
    scores = [page_confidence(ocr, source) for source in sources]
    ocr_factor = max((1.0 if score is None else min(score, 1.0) for score in scores), default=1.0)
    confidence = base * ocr_factor
    validator = VALIDATORS.get(path)
    valid = validator(value, case) if validator else None
    if valid is False:
        confidence = min(confidence, 0.3)
    return {
        "field": path,
        "value": value,
        "status": "corroborated" if len(signatures) >= 2 else "single_source",
        "confidence": round(confidence, 2),
        "independent_documents": sorted(signatures),
        "ocr_confidence": None if ocr_factor == 1.0 else round(ocr_factor, 2),
        "format_check": {None: "n/a", True: "pass", False: "FAIL"}[valid],
        "sources": [f"{s['document']} p{s['page']}" for s in sources][:6],
    }


def _canonical(value):
    return re.sub(r"[^a-z0-9 ]", " ", str(value).casefold()).split()


def matches(path, got, expected):
    if got in (None, "", []):
        return False
    if path == "applicant.nominees":
        names = {" ".join(_canonical(n.get("name", ""))) for n in got}
        wanted = expected if isinstance(expected, list) else [expected]
        return names == {" ".join(_canonical(w.get("name", w) if isinstance(w, dict) else w)) for w in wanted}
    if isinstance(got, int) and not isinstance(got, bool):
        try:
            return got == int(str(expected).replace(",", ""))
        except ValueError:
            return False
    if path == "applicant.dob":
        try:
            return got == iso_date(str(expected))
        except ValueError:
            return False
    if path == "applicant.applicant_name":
        return names_compatible(str(got), str(expected)) and _canonical(got)[0] == _canonical(expected)[0]
    return _canonical(got) == _canonical(expected)


def compare_to_truth(rows, truth):
    results = []
    for row in rows:
        if row["field"] in truth:
            results.append(
                {
                    "field": row["field"],
                    "expected": truth[row["field"]],
                    "extracted": row["value"],
                    "correct": matches(row["field"], row["value"], truth[row["field"]]),
                }
            )
    correct = sum(r["correct"] for r in results)
    return {
        "compared": len(results),
        "correct": correct,
        "accuracy": round(correct / len(results), 3) if results else None,
        "mismatches": [r for r in results if not r["correct"]],
    }


def summarize(rows):
    extracted = [r for r in rows if r["status"] in {"corroborated", "single_source"}]
    return {
        "fields_total": len(rows),
        "extracted": len(extracted),
        "corroborated": sum(r["status"] == "corroborated" for r in rows),
        "single_source": sum(r["status"] == "single_source" for r in rows),
        "conflicting": sum(r["status"] == "conflict" for r in rows),
        "missing": sum(r["status"] == "missing" for r in rows),
        "format_check_failures": sum(r.get("format_check") == "FAIL" for r in rows),
        "estimated_correct": round(sum(r["confidence"] for r in extracted), 1),
        "estimated_accuracy_of_extracted": round(
            sum(r["confidence"] for r in extracted) / len(extracted), 3
        )
        if extracted
        else None,
    }


async def extract(args):
    os.environ["UW_EXTRACTION_MODE"] = "local_first"
    os.environ["UW_TARGETED_LLM_FALLBACK"] = "true" if args.fallback else "false"
    if args.ocr:
        os.environ["UW_LOCAL_OCR_PROVIDER"] = args.ocr
    if args.fallback:
        from underwriting_platform.pipeline.run import _load_env

        _load_env()
        from underwriting_platform.llm import build_client

        client = build_client()
    else:
        os.environ.pop("GEMINI_API_KEY", None)
        client = NoCloud()
    from underwriting_platform.orchestrator import MasterOrchestrator

    orchestrator = MasterOrchestrator(client, rules={}, reconcile=False)
    orchestrator.gemini = None
    return await orchestrator.run(Path(args.case_dir), args.case_id)


def render(rows, summary, comparison):
    lines = [f"{'FIELD':38} {'STATUS':13} {'CONF':5} {'CHECK':5}  VALUE"]
    for row in rows:
        value = json.dumps(row["value"], ensure_ascii=False, default=str)
        lines.append(
            f"{row['field']:38} {row['status']:13} {row['confidence']:<5} "
            f"{row.get('format_check', ''):5}  {value[:70]}"
        )
    lines += ["", json.dumps(summary, indent=2)]
    if comparison:
        lines += ["", "AGAINST VERIFIED VALUES:", json.dumps(comparison, indent=2, default=str)]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Extraction confidence report for one case")
    parser.add_argument("--case_dir", required=True)
    parser.add_argument("--case_id", required=True)
    parser.add_argument("--output_dir", default=None)
    parser.add_argument("--truth", default=None, help="JSON of verified values: {field: value}")
    parser.add_argument("--ocr", default=None, choices=["auto", "rapidocr", "tesseract"])
    parser.add_argument("--fallback", action="store_true", help="allow the bounded Azure call")
    args = parser.parse_args()
    result = asyncio.run(extract(args))
    rows = [field_report(result.case_json, result.extraction_report, path) for path in FIELDS]
    summary = summarize(rows)
    comparison = None
    if args.truth:
        comparison = compare_to_truth(rows, json.loads(Path(args.truth).read_text(encoding="utf8")))
    output = Path(args.output_dir) if args.output_dir else RUNTIME_ROOT / "accuracy" / args.case_id
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{args.case_id}_accuracy.json").write_text(
        json.dumps(
            {"summary": summary, "fields": rows, "against_verified": comparison},
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf8",
    )
    template = {r["field"]: r["value"] for r in rows if r["value"] not in (None, "", [], {})}
    (output / f"{args.case_id}_truth_template.json").write_text(
        json.dumps(template, indent=2, ensure_ascii=False, default=str), encoding="utf8"
    )
    print(render(rows, summary, comparison))
    print(f"\nWrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
