"""Verify _cap_docs prefers required-test medical evidence over discharge / generic docs.

Reproduces the C312315043 hand-off bug: with 14 medical-relevant docs and an 8-doc cap,
the pre-fix scorer left every doc tied at 0 (medical keyword list had only
['vmer','bmi','vendor medical']) and fell back to first-N. That dropped the three
TPA-conducted reports (TMT, MER, Blood) — exactly the files needed to flip
documents_present.tmt/mer/cbc/hba1c → True.

This test scores the same 14 filenames against the new cap and asserts the
TPA reports survive plus the scout-flagged generic 'Others document.pdf'
(which carries the ECG image).
"""
from __future__ import annotations
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from underwriting_platform.orchestrator.master import MasterOrchestrator  # noqa: E402


@dataclass
class _Doc:
    filename: str


# The 14 files the medical agent saw for C312315043 after the ZIP-extraction fix.
MEDICAL_DOCS = [_Doc(fn) for fn in [
    "Income Proof - AML Guideline (1).pdf",
    "Others document.pdf",                        # payslips variant
    "Video MER Transcript.pdf",
    "Doctor Certificate 2025.pdf",
    "Hernia Surgery Discharge certificate.pdf",
    "Bombay Hospital Discharge summary 1986.pdf",
    "Bombay Hospital Discharge summary 1986 Back Side.pdf",
    "2D echo 2025.pdf",
    "Employment Salary Slip.pdf",
    "Employment Salary Slip (1).pdf",
    "Others document (1).pdf",                    # ECG image variant (different content, same basename pattern)
    "TMT reports done by TPA.pdf",
    "Medical Examination Report done by TPA.pdf",
    "Blood reports done by TPA.pdf",
]]


def main() -> int:
    # The scout would have flagged 'Others document.pdf' (the ECG image one).
    # Even though both Others variants share a basename, the boost is filename-keyed,
    # so both pass — the medical agent then classifies each by content.
    scout_hits = {"Others document.pdf"}

    orch = MasterOrchestrator.__new__(MasterOrchestrator)  # bypass __init__ (no LLM needed)
    capped = orch._cap_docs("medical", MEDICAL_DOCS, max_count=8, scout_hits=scout_hits)
    kept = [d.filename for d in capped]

    print(f"Cap kept {len(kept)} of {len(MEDICAL_DOCS)} docs:")
    for fn in kept:
        print(f"  - {fn}")
    print()

    # The required-test files the gating grid scores against — these MUST survive.
    must_survive = [
        "TMT reports done by TPA.pdf",
        "Medical Examination Report done by TPA.pdf",
        "Blood reports done by TPA.pdf",
        "Video MER Transcript.pdf",
        "Others document.pdf",   # ECG image (scout-flagged)
    ]
    missing = [f for f in must_survive if f not in kept]
    if missing:
        print(f"FAIL — these files were dropped by the cap: {missing}")
        return 1

    # The Bombay Hospital duplicate (back side) and one of the Others variants
    # are reasonable casualties — they're either redundant or unranked.
    print("PASS — all required-test files + scout-flagged ECG file survived the cap.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
