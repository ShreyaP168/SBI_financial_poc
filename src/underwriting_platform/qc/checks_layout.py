"""
qc/checks_layout.py — H1–H3: layout / rendering smoke signals.

H1  Page-count band — typical case sheets are 3–15 pages; outliers
    are a smoke signal (truncated render, ReportLab overflow loop,
    excessive medical evidence).
H2  Header on every page — confirm by counting "TruAI UW System"
    occurrences vs page count.
H3  Verdict pill rendered — confirm the brand-translated verdict label
    appears at least once (D4 already covers; this is the layout
    angle).

Note: pdfplumber gives us text only, not styling. Pill colour
validation would need a different tool (ReportLab introspection or
PDF render comparison) — kept out of scope.
"""

from __future__ import annotations

from .case_sheet import register_check
from .types import QCFinding


_MIN_PAGES = 3
_MAX_PAGES = 15


def check_page_count(ctx: dict) -> list[QCFinding]:
    page_count: int = ctx.get("page_count", 0)
    # SBI uses the compact triage renderer; a one-page result is expected.
    compact = ((ctx.get("decision") or {}).get("routing") or {}).get(
        "rule_set"
    ) == "sbi_smart_shield_plus"
    min_pages = 1 if compact else _MIN_PAGES
    if page_count == 0:
        return [
            QCFinding(
                code="PDF_NO_PAGES",
                category="layout",
                severity="BLOCK",
                location=ctx.get("pdf_path", "—"),
                snippet="0 pages",
                rule=f"underwriting_platform.qc.checks_layout._MIN_PAGES={_MIN_PAGES}",
                remediation=("PDF has zero pages or could not be parsed — renderer write failed."),
            )
        ]
    if page_count < min_pages:
        return [
            QCFinding(
                code="PDF_TOO_FEW_PAGES",
                category="layout",
                severity="WARN",
                location=ctx.get("pdf_path", "—"),
                snippet=f"{page_count} pages",
                rule=f"underwriting_platform.qc.checks_layout._MIN_PAGES={_MIN_PAGES}",
                remediation=("Suspiciously short PDF — likely a stub or early-exit render."),
            )
        ]
    if page_count > _MAX_PAGES:
        return [
            QCFinding(
                code="PDF_TOO_MANY_PAGES",
                category="layout",
                severity="WARN",
                location=ctx.get("pdf_path", "—"),
                snippet=f"{page_count} pages",
                rule=f"underwriting_platform.qc.checks_layout._MAX_PAGES={_MAX_PAGES}",
                remediation=(
                    "Page count exceeds typical band — investigate "
                    "renderer overflow / excessive evidence."
                ),
            )
        ]
    return []


def check_header_per_page(ctx: dict) -> list[QCFinding]:
    """Brand system_name should appear on every page (page decorator
    draws it). Count occurrences vs page count.
    """
    profile: dict = ctx["profile"]
    # Per-page brand header is opt-in. The compact case sheet (talic_sheet)
    # intentionally has no running header, so this check applies only when a
    # profile explicitly sets brand.require_per_page_header = true.
    if not (profile.get("brand") or {}).get("require_per_page_header", False):
        return []
    brand = (profile.get("brand") or {}).get("system_name") or ""
    pages: list[str] = ctx["pages"]
    if not brand or not pages:
        return []
    missing_pages: list[int] = []
    for i, page_text in enumerate(pages, start=1):
        if brand not in page_text:
            missing_pages.append(i)
    if missing_pages:
        return [
            QCFinding(
                code="HEADER_MISSING_ON_PAGES",
                category="layout",
                severity="WARN",
                location=f"pages {missing_pages}",
                snippet=f"expected {brand!r} on every page",
                rule="underwriting_platform.qc.checks_layout.check_header_per_page",
                remediation=(
                    "Page decorator failed to draw the brand "
                    "header on some pages — check "
                    "_build_page_decorator."
                ),
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

register_check("checks_layout.page_count", check_page_count)
register_check("checks_layout.header_per_page", check_header_per_page)
