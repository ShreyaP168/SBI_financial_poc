import json
import re
import sys
from pathlib import Path

import fitz

SHEETS = Path(
    r"C:\Users\Shreya.poojary\Desktop\project\Profiling and Financial UW POC\lifeinsurance-poc-repo\uw_engine_v9\outputs\corrected_7_cases_2026-10-14"
)
TRUTH = Path(__file__).resolve().parents[1] / "output" / "truth"


def lines_of(path):
    text = fitz.open(path)[0].get_text()
    return [line.strip() for line in text.splitlines() if line.strip()]


def after(lines, label):
    for i, line in enumerate(lines):
        if line == label and i + 1 < len(lines):
            return lines[i + 1]
    return None


def rupees(raw):
    if raw is None:
        return None
    match = re.search(r"[\d,]{4,}", raw)
    return int(match[0].replace(",", "")) if match else None


def parse(path):
    lines = lines_of(path)
    case = path.stem.replace("_case_sheet", "")
    joined = "\n".join(lines)
    info = {"case": case}
    for line in lines:
        if line.startswith("LA "):
            info["name"] = line[3:].strip()
        elif line.startswith("AGE "):
            match = re.match(r"AGE (\d+)\s+([MF])", line)
            if match:
                info["age"] = int(match[1])
                info["gender"] = {"M": "Male", "F": "Female"}[match[2]]
        elif line.startswith("OCC "):
            info["occupation_text"] = line[4:].strip()
        elif line.startswith("EDU "):
            info["education"] = line[4:].strip()
        elif line.startswith("SA ") and rupees(line):
            info.setdefault("sum_assured", rupees(line))
    decision = re.search(r"DECISION\s*\n\s*([A-Z][A-Z \-]+)\n", joined)
    info["decision"] = decision[1].strip() if decision else None
    verified = re.search(r"Verified Annual Income[^\n]*\n(?:[^\n]*\n){0,3}?\s*([\d,]{6,})", joined)
    info["verified_income"] = rupees(verified[1]) if verified else None
    declared = re.search(r"Declared Annual Income\s*\n\s*(?:Rs\.?\s*)?([\d,]{5,})", joined)
    info["declared_income"] = rupees(declared[1]) if declared else None
    cibil = re.search(r"CIBIL Score\s*\n\s*(\d{3})", joined)
    info["cibil"] = int(cibil[1]) if cibil else None
    reason = re.search(r"DECISION\s*\n[A-Z \-]+\n(.+?)\n(?:HIGH RISK|MEDIUM RISK|LOW RISK|OVERALL)", joined, re.S)
    info["reason"] = " ".join(reason[1].split()) if reason else None
    requirements = re.search(r"REQUIREMENTS TO RAISE\s*\n(.+?)\n(?:DECISION|HIGH RISK)", joined, re.S)
    info["requirements"] = [r for r in requirements[1].splitlines() if r.strip()][:8] if requirements else []
    return info


def main():
    TRUTH.mkdir(parents=True, exist_ok=True)
    rows = [parse(p) for p in sorted(SHEETS.glob("*_case_sheet.pdf"))]
    for row in rows:
        (TRUTH / f"{row['case']}_sheet.json").write_text(
            json.dumps(row, indent=2, ensure_ascii=False), encoding="utf8"
        )
        print(json.dumps({k: v for k, v in row.items() if k != "requirements"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
