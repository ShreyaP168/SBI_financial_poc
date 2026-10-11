"""
qc/case_sheet.py — pre-delivery QC orchestrator.

Runs *after* renderer.triage_sheet.render_triage_sheet() has written a PDF. Reads
the rendered PDF text + the source decision JSON, runs every check
module under qc/checks_*.py, aggregates findings into a QCReport, and
returns it. The pipeline writes the report to disk and (if any BLOCK
findings fired) flags the batch for a non-zero exit code.

Design notes:
- The check registry is intentionally a flat list of callables that
  each return list[QCFinding]. Adding a check class is "add a function,
  add it to the list". No class hierarchy needed.
- Every check is wrapped in a try/except — a crashing check emits a
  QC_INFRASTRUCTURE_FAILURE finding rather than tanking the report.
- PDF text extraction uses pdfplumber (already a dependency via
  llm/pdf_utils.py). The extracted text is computed once and passed to
  every check that needs it.
- The brand profile is loaded once and threaded into every check; per
  -insurer behaviour lives entirely in JSON, not code.

Entry points:
- validate_case_sheet(decision, pdf_path, profile) -> QCReport
- load_profile(path) -> dict
"""

from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Any, Callable

from .types import QCFinding, QCReport

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Profile loader
# ---------------------------------------------------------------------------

_DEFAULT_PROFILE = {
    "project_slug": "default",
    "engine_version": "v9",
    "insurer": {"canonical": "—", "permitted_forms": []},
    "brand": {"system_name": "TruAI UW System", "operator": "Datamatics"},
    "currency": {"symbol": "₹", "code": "INR", "permitted_forms": ["₹", "INR"]},
    "date_format": "DD-Mon-YYYY",
    "vocabulary": {},
    "redact": {"vendor_tokens": [], "internal_codenames": [], "internal_vocab": []},
    "verdict_terminology": {},
}


def load_profile(path: str | Path | None, *, required: bool = False) -> dict:
    """Load a brand profile JSON; fall back to defaults if missing."""
    if path is None:
        if required:
            raise ValueError("A delivery QC profile is required")
        logger.info("underwriting_platform.qc.load_profile: no path supplied; using defaults")
        return dict(_DEFAULT_PROFILE)
    p = Path(path)
    if not p.exists():
        if required:
            raise FileNotFoundError("Required QC profile unavailable")
        logger.warning("underwriting_platform.qc.load_profile: %s not found; using defaults", p)
        return dict(_DEFAULT_PROFILE)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        if required:
            raise ValueError("Required QC profile invalid") from e
        logger.warning(
            "underwriting_platform.qc.load_profile: failed to parse %s (%s); defaults", p, e
        )
        return dict(_DEFAULT_PROFILE)
    # Merge over defaults so absent keys still resolve cleanly.
    if required and (
        not isinstance(data, dict)
        or not data.get("project_slug")
        or not isinstance(data.get("insurer"), dict)
        or not data["insurer"].get("canonical")
    ):
        raise ValueError("Required QC profile does not meet its schema")
    merged = dict(_DEFAULT_PROFILE)
    merged.update(data)
    return merged


# ---------------------------------------------------------------------------
# PDF text extraction (one-shot per case)
# ---------------------------------------------------------------------------


def _extract_pdf_text(pdf_path: str | Path) -> tuple[list[str], int]:
    """Return (per_page_text, page_count). On any error returns ([], 0).

    The QC layout checks need the per-page text; the IP / PII / brand
    checks just concatenate. Extraction happens once; results are
    passed to every check that asks for them.
    """
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        logger.warning("qc: pdfplumber not installed; PDF text checks degraded")
        return [], 0
    p = Path(pdf_path)
    if not p.exists():
        logger.warning("qc: PDF not found at %s", p)
        return [], 0
    try:
        pages: list[str] = []
        with pdfplumber.open(str(p)) as pdf:
            for page in pdf.pages:
                pages.append(page.extract_text() or "")
        return pages, len(pages)
    except Exception as e:
        logger.warning("qc: pdfplumber failed on %s (%s); degraded", p, e)
        return [], 0


# ---------------------------------------------------------------------------
# Check registry
# ---------------------------------------------------------------------------

# Each check is a callable: (ctx: dict) -> list[QCFinding]
# ctx carries: decision, pages, full_text, profile, pdf_path, decision_path
CheckFn = Callable[[dict], list[QCFinding]]

_CHECKS: list[tuple[str, CheckFn]] = []


def register_check(name: str, fn: CheckFn) -> None:
    """Register a check function. Called by qc/checks_*.py at import time."""
    _CHECKS.append((name, fn))


# Lazy-import the check modules so that adding a new qc/checks_*.py is
# self-registering. Import side-effects populate _CHECKS via register_check.
def _load_checks() -> None:
    # Imports below are no-ops in this commit (modules don't exist yet);
    # each will be created in a follow-up commit.
    try:
        from . import checks_ip  # noqa: F401
    except ImportError:
        pass
    try:
        from . import checks_brand  # noqa: F401
    except ImportError:
        pass
    try:
        from . import checks_pii  # noqa: F401
    except ImportError:
        pass
    try:
        from . import checks_completeness  # noqa: F401
    except ImportError:
        pass
    try:
        from . import checks_coherence  # noqa: F401
    except ImportError:
        pass
    try:
        from . import checks_layout  # noqa: F401
    except ImportError:
        pass


_CHECKS_LOADED = False


def _ensure_loaded() -> None:
    global _CHECKS_LOADED
    if not _CHECKS_LOADED:
        _load_checks()
        _CHECKS_LOADED = True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def validate_case_sheet(
    decision: dict, pdf_path: str | Path, profile: dict, decision_path: str | Path = ""
) -> QCReport:
    """Run every registered QC check against `decision` + the rendered PDF.

    The decision dict is the v9 engine output (case_json after engine.run).
    pdf_path points to the file written by renderer.triage_sheet.render_triage_sheet.
    profile comes from underwriting_platform.qc.case_sheet.load_profile().

    A check that raises is caught and surfaced as a single
    QC_INFRASTRUCTURE_FAILURE finding (severity WARN) so one broken
    check doesn't tank the rest of the report.
    """
    _ensure_loaded()

    case_id = str(decision.get("case_id") or "—")
    pdf_path_str = str(pdf_path)
    decision_path_str = str(decision_path or "")
    project_slug = str(profile.get("project_slug") or "default")

    report = QCReport(
        case_id=case_id,
        pdf_path=pdf_path_str,
        decision_path=decision_path_str,
        profile=project_slug,
    )

    pages, page_count = _extract_pdf_text(pdf_path)
    report.decision_complete = bool((decision.get("_completion") or {}).get("decision_complete"))
    full_text = "\n".join(pages)
    if not page_count or not full_text.strip():
        report.add(
            QCFinding(
                code="QC_INFRASTRUCTURE_FAILURE",
                category="infra",
                severity="BLOCK",
                location="pdf_text_extraction",
                snippet="PDF text could not be verified.",
                rule="underwriting_platform.qc.case_sheet._extract_pdf_text",
                remediation="Restore PDF inspection before delivery.",
            )
        )

    ctx = {
        "decision": decision,
        "pages": pages,
        "page_count": page_count,
        "full_text": full_text,
        "profile": profile,
        "pdf_path": pdf_path_str,
        "decision_path": decision_path_str,
    }
    for prefix in (
        "checks_ip.",
        "checks_pii.",
        "checks_brand.",
        "checks_layout.",
        "checks_coherence.",
        "checks_completeness.",
    ):
        if not any(name.startswith(prefix) for name, _ in _CHECKS):
            report.add(
                QCFinding(
                    code="QC_INFRASTRUCTURE_FAILURE",
                    category="infra",
                    severity="BLOCK",
                    location=prefix,
                    snippet="Required QC checks are unavailable.",
                    rule="underwriting_platform.qc.case_sheet.registry",
                    remediation="Restore the required QC registry before delivery.",
                )
            )

    for name, fn in _CHECKS:
        try:
            findings = fn(ctx) or []
            for f in findings:
                report.add(f)
        except Exception as e:
            logger.exception("qc check %r crashed", name)
            report.add(
                QCFinding(
                    code="QC_INFRASTRUCTURE_FAILURE",
                    category="infra",
                    severity="BLOCK",
                    location=f"underwriting_platform.qc.{name}",
                    snippet=f"Check raised {type(e).__name__}: {e}",
                    rule=f"underwriting_platform.qc.case_sheet._run_check({name})",
                    remediation="Investigate the QC check implementation; "
                    "report is incomplete until fixed.",
                )
            )

    return report
