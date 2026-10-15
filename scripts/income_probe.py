import glob
import json
import pickle
import sys
from pathlib import Path

from underwriting_platform.extraction.local_fields import extract_local
from underwriting_platform.llm.pdf_utils import prepare_doc
from underwriting_platform.ocr.classification import survey_documents

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "output" / "_probe_cache"


def documents(case):
    cache = CACHE / f"{case}.pkl"
    if cache.exists():
        return pickle.loads(cache.read_bytes())
    docs = [prepare_doc(p) for p in sorted(glob.glob(str(ROOT / "Input" / "Financial Evaluation" / case / "*")))]
    CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(pickle.dumps(docs))
    return docs


def probe(case):
    docs = documents(case)
    survey = survey_documents(docs, [])
    kinds = {
        (row["filename"], page["page"]): (row["kind"], page["label"])
        for row in survey["files"]
        for page in row["pages"]
    }
    block, sources, findings, _ = extract_local(docs, kinds)
    income = block["income"]
    print("=====", case)
    print(
        "primary:", income.get("primary_source"),
        "| annual:", income.get("annual_income"),
        "| slips:", [(s["month_year"], s["gross_salary"]) for s in income.get("salary_slips", [])],
        "| itr:", [(y["assessment_year"], y["earned_total"], y.get("total_income")) for y in income.get("itr_years", [])],
        "| credits:", len(income.get("bank_salary_credits", [])),
    )
    app = block["applicant"]
    print("applicant:", {k: app.get(k) for k in ("applicant_name", "age", "gender", "education_level", "declared_annual_income", "business_nature")})
    print("findings:", findings)
    return block, sources


if __name__ == "__main__":
    for case in sys.argv[1:]:
        probe(case)
