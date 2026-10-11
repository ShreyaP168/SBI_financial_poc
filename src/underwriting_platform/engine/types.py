"""
engine/types.py — provenance-bearing dataclasses for the v9 engine layer.

Every number that lands in the final decision_json is wrapped in a
Calculation dataclass that records:

  - value   : the result
  - unit    : "INR" / "%" / "ratio" / "years" / "count"
  - method  : short slug naming the technique used
  - function: fully-qualified Python callable that produced the result
              (e.g. "underwriting_platform.engine.calculator.compute_hlv")
  - formula : human-readable formula string
  - inputs  : { input_name : value } — what went into the formula
  - sources : list of {document, agent, field_path} pointers back to extraction
  - rule    : dotted path into uw_rules.json (e.g. "income_multiples.31-35 = 30")
  - note    : free-text caveat / interpretation

Gating items, flags, and the verdict carry the same provenance pattern so
the renderer can show the underwriter the audit trail end-to-end.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Source:
    document: str  # filename or human-readable doc descriptor
    agent: str  # which v9 agent extracted it
    field_path: str = ""  # dotted path into the agent's block (optional)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Calculation:
    name: str  # short slug e.g. "hlv_base"
    value: Any  # number, string, or null
    unit: str = ""  # "INR" | "%" | "ratio" | "years" | "count" | ""
    method: str = ""  # "salary_slip_3m_avg", "income_multiple_age_band", etc.
    function: str = ""  # "underwriting_platform.engine.calculator.compute_hlv"
    formula: str = ""  # "annual_income × age_multiplier"
    inputs: dict = field(default_factory=dict)
    sources: list[Source] = field(default_factory=list)
    rule: str = ""  # "uw_rules.income_multiples.31-35 = 30"
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "method": self.method,
            "function": self.function,
            "formula": self.formula,
            "inputs": self.inputs,
            "sources": [s.to_dict() for s in self.sources],
            "rule": self.rule,
            "note": self.note,
        }


GATING_STATUS = ("PASS", "WARN", "FAIL", "NA")


@dataclass
class GatingItem:
    criterion: str  # short id e.g. "income_documented"
    label: str  # human title
    status: str  # one of GATING_STATUS
    function: str = ""  # "underwriting_platform.engine.gating.check_income_documented"
    rule: str = ""  # rule reference
    evidence: str = ""  # one-line summary of why it passed/failed
    sources: list[Source] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "criterion": self.criterion,
            "label": self.label,
            "status": self.status,
            "function": self.function,
            "rule": self.rule,
            "evidence": self.evidence,
            "sources": [s.to_dict() for s in self.sources],
        }


FLAG_SEVERITY = ("CRITICAL", "HIGH", "MEDIUM", "LOW")


@dataclass
class Flag:
    code: str
    severity: str  # one of FLAG_SEVERITY
    title: str
    description: str
    function: str = ""  # which engine function raised it
    rule: str = ""
    sources: list[Source] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "description": self.description,
            "function": self.function,
            "rule": self.rule,
            "sources": [s.to_dict() for s in self.sources],
        }


# STANDARD_ISSUANCE / FURTHER_REQUIREMENT are SBI-case-only labels (see
# engine.verdict.make_verdict) matching SBI's own decision vocabulary —
# confirmed against the QC team's "Datamatics decision" column (2026-10-06),
# which has no literal "CONDITIONAL" category. STANDARD_ACCEPTANCE /
# CONDITIONAL remain the generic / TATA AIA flow's labels, unchanged.
VERDICT_LABELS = (
    "STANDARD_ACCEPTANCE",
    "CONDITIONAL",
    "STANDARD_ISSUANCE",
    "FURTHER_REQUIREMENT",
    "REFER",
    "DECLINE",
    "INCOMPLETE",
    "ERROR",
)


@dataclass
class Verdict:
    label: str  # one of VERDICT_LABELS
    confidence: float  # 0.0–1.0
    rationale: str  # human paragraph
    function: str = "underwriting_platform.engine.verdict.make_verdict"
    decision_drivers: list[str] = field(default_factory=list)  # short bullets
    # 14-cell Final Decision matrix per Datamatics LAYOUT row 71-77.
    # Combines: rate class (Smoker / NSR / Sub-Std-RU Smoker / Sub-Std-RU NSR /
    # Postpone / Decline) × cover-level (full / restricted / stretched, when
    # applicable). When the financial / medical assessment doesn't return a
    # rate-class decision (Refer / Conditional / Out of Scope), this is "—".
    final_decision_cell: str = ""

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "function": self.function,
            "decision_drivers": self.decision_drivers,
            "final_decision_cell": self.final_decision_cell,
        }
