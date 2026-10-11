"""
qc/types.py — dataclasses for pre-delivery QC findings on rendered case sheets.

Follows the same shape as `engine.types.Calculation` / `Flag` / `GatingItem`
— every artifact carries enough provenance that the underwriter, ops
engineer, or downstream CI can act on it without re-reading the code.

A QCFinding describes ONE rule violation found while validating a
rendered case sheet against its source decision JSON. A QCReport rolls
N findings into a single delivery-gate verdict (PASS / WARN / BLOCK).

The QC layer is project-agnostic; per-insurer differences are
expressed via the brand profile (config/brand_profile.json) loaded by
qc.case_sheet.load_profile().
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Literal


Severity = Literal["INFO", "WARN", "BLOCK"]
Status = Literal["PASS", "WARN", "BLOCK"]


@dataclass
class QCFinding:
    """One QC rule violation against a rendered case sheet."""

    code: str  # short slug e.g. "VENDOR_TOKEN_LEAK"
    category: str  # "ip_leak" | "brand" | "coherence" | "pii" | …
    severity: Severity  # INFO | WARN | BLOCK
    location: str = ""  # "PDF p3, flags table, VITALS_REOCR_DISAGREEMENT"
    snippet: str = ""  # offending text excerpt (already sanitized)
    rule: str = ""  # rule reference e.g. "underwriting_platform.qc.checks_ip.vendor_tokens.gemini"
    remediation: str = ""  # one-line fix hint for the engineer

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class QCReport:
    """Aggregated QC outcome for one case sheet."""

    case_id: str
    pdf_path: str
    decision_path: str
    profile: str  # project_slug from brand_profile.json
    checked_at: str = ""  # ISO-8601 UTC timestamp
    status: Status = "PASS"
    findings: list[QCFinding] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.checked_at:
            self.checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def add(self, finding: QCFinding) -> None:
        """Append a finding and bump status if the severity is worse."""
        self.findings.append(finding)
        # BLOCK > WARN > PASS — a single BLOCK poisons the report.
        if finding.severity == "BLOCK":
            self.status = "BLOCK"
        elif finding.severity == "WARN" and self.status == "PASS":
            self.status = "WARN"

    @property
    def summary(self) -> dict[str, int]:
        out = {"BLOCK": 0, "WARN": 0, "INFO": 0}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "checked_at": self.checked_at,
            "profile": self.profile,
            "status": self.status,
            "findings": [f.to_dict() for f in self.findings],
            "summary": self.summary,
            "pdf_path": self.pdf_path,
            "decision_path": self.decision_path,
            "decision_complete": getattr(self, "decision_complete", False),
        }
