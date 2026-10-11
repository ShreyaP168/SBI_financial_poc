"""
qc/ — pre-delivery QC for rendered case sheets.

Public entry points:
    validate_case_sheet(decision, pdf_path, profile) -> QCReport
    load_profile(path) -> dict

See qc/case_sheet.py for the orchestrator and qc/checks_*.py for the
individual check modules. Per-insurer behaviour is driven by
config/brand_profile.json — there's no per-project code branch.
"""

from .case_sheet import load_profile, validate_case_sheet, register_check
from .types import QCFinding, QCReport, Severity, Status

__all__ = [
    "QCFinding",
    "QCReport",
    "Severity",
    "Status",
    "load_profile",
    "register_check",
    "validate_case_sheet",
]
