"""
qc/checks_brand.py — C1–C7: brand & naming consistency.

Asserts that the same brand string is used uniformly across the
rendered case sheet — insurer name (e.g. always "Tata AIA Life
Insurance"), system brand (always "TruAI UW System", never "Tru AI"),
operator name, currency symbol, and the project's chosen vocabulary
for ambiguous terms (CAF vs Common Application Form, KY4 vs Live
Photo).

All thresholds come from config/brand_profile.json — no insurer-
specific terminology is baked into this module.
"""

from __future__ import annotations
import re

from .case_sheet import register_check
from .types import QCFinding


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _word_pat(needle: str) -> re.Pattern:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])", re.IGNORECASE)


def _locate(pages: list[str], match_text: str) -> str:
    for i, page_text in enumerate(pages, start=1):
        if match_text in page_text:
            return f"PDF p{i}"
    return "—"


# ---------------------------------------------------------------------------
# C1 — Insurer name uniformity
# ---------------------------------------------------------------------------


def check_insurer_name(ctx: dict) -> list[QCFinding]:
    profile: dict = ctx["profile"]
    pages: list[str] = ctx["pages"]
    text: str = ctx["full_text"]
    out: list[QCFinding] = []

    insurer = profile.get("insurer") or {}
    permitted = [p for p in (insurer.get("permitted_forms") or []) if p]
    canonical = insurer.get("canonical") or ""
    if not permitted:
        return out

    # Build a regex that finds anything that LOOKS like the insurer
    # name but doesn't exactly match a permitted form. We catch only
    # the common typo class: split / hyphenated / extra-space variants
    # of the canonical's distinguishing word.
    # E.g. canonical "Tata AIA" — look for "Tata-AIA", "TataAIA", "Tata Aia".
    if canonical:
        # Variants: lowercased canonical with non-alphanum collapsed
        core = re.sub(r"\s+", "", canonical).lower()
        if len(core) >= 6:
            # Hunt for fuzzy variants of the canonical's first word + middle
            head = canonical.split()[0]  # e.g. "Tata"
            variant_pat = re.compile(rf"\b{re.escape(head)}\s*[-_/]\s*[A-Z][A-Za-z]+\b")
            for m in variant_pat.finditer(text):
                hit = m.group(0)
                if hit in permitted:
                    continue
                out.append(
                    QCFinding(
                        code="INSURER_NAME_TYPO",
                        category="brand",
                        severity="WARN",
                        location=_locate(pages, hit),
                        snippet=hit,
                        rule="underwriting_platform.qc.checks_brand.check_insurer_name",
                        remediation=(f"Use one of the permitted insurer-name forms: {permitted}."),
                    )
                )
    return out


# ---------------------------------------------------------------------------
# C2 — System brand uniformity
# ---------------------------------------------------------------------------


def check_system_brand(ctx: dict) -> list[QCFinding]:
    profile: dict = ctx["profile"]
    pages: list[str] = ctx["pages"]
    text: str = ctx["full_text"]
    out: list[QCFinding] = []

    brand = (profile.get("brand") or {}).get("system_name") or ""
    if not brand:
        return out

    # Look for malformed variants — typical typos:
    #   "TruAI" canonical -> "TRU AI", "Tru AI", "Tru-AI", "Tru_AI"
    head = re.sub(r"[^A-Za-z]", "", brand.split()[0]) if brand.split() else brand
    if not head:
        return out
    variant_pat = re.compile(
        rf"\b{head[:3]}[-_ ]+{head[3:]}\b",
        re.IGNORECASE,
    )
    for m in variant_pat.finditer(text):
        hit = m.group(0)
        if hit == brand or hit in brand:
            continue
        out.append(
            QCFinding(
                code="SYSTEM_BRAND_TYPO",
                category="brand",
                severity="WARN",
                location=_locate(pages, hit),
                snippet=hit,
                rule="underwriting_platform.qc.checks_brand.check_system_brand",
                remediation=f"Canonical brand is {brand!r}; remove typo variants.",
            )
        )
    return out


# ---------------------------------------------------------------------------
# C3 — Operator name presence
# ---------------------------------------------------------------------------


def check_operator_name(ctx: dict) -> list[QCFinding]:
    """If brand.operator is set in the profile, confirm it appears at
    least once in the rendered text (typically the footer)."""
    profile: dict = ctx["profile"]
    text: str = ctx["full_text"]
    operator = (profile.get("brand") or {}).get("operator") or ""
    if not operator:
        return []
    if operator in text:
        return []
    return [
        QCFinding(
            code="OPERATOR_NAME_MISSING",
            category="brand",
            severity="INFO",
            location="footer (expected)",
            snippet="",
            rule="underwriting_platform.qc.checks_brand.check_operator_name",
            remediation=f"Operator string {operator!r} not found in rendered text.",
        )
    ]


# ---------------------------------------------------------------------------
# C6 — Currency formatting
# ---------------------------------------------------------------------------


def check_currency_uniformity(ctx: dict) -> list[QCFinding]:
    """Catch currency forms outside the permitted list — e.g. literal
    'Rs.' when profile permits only '₹' + 'INR'."""
    profile: dict = ctx["profile"]
    pages: list[str] = ctx["pages"]
    text: str = ctx["full_text"]
    out: list[QCFinding] = []

    permitted = set((profile.get("currency") or {}).get("permitted_forms") or [])
    if not permitted:
        return out

    # Disallowed-form regex covers common Indian English variants.
    disallowed_patterns = [
        (r"\bRs\.?\b", "Rs."),
        (r"\bRupees?\b", "Rupees"),
        (r"\bINR\b", "INR"),
        (r"₹", "₹"),
    ]
    for pat, label in disallowed_patterns:
        if label in permitted:
            continue
        rx = re.compile(pat, re.IGNORECASE if label != "₹" else 0)
        for m in rx.finditer(text):
            out.append(
                QCFinding(
                    code="CURRENCY_FORM_INCONSISTENT",
                    category="brand",
                    severity="INFO",
                    location=_locate(pages, m.group(0)),
                    snippet=m.group(0),
                    rule=f"underwriting_platform.qc.checks_brand.check_currency_uniformity ({label})",
                    remediation=(f"Use only the permitted currency forms: {sorted(permitted)}."),
                )
            )
    return out


# ---------------------------------------------------------------------------
# C7 — Date format
# ---------------------------------------------------------------------------

# Common date-format detectors (loose; mainly looking for obvious mixes)
_DATE_PATTERNS = {
    "DD-Mon-YYYY": re.compile(
        r"\b\d{2}-(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-\d{4}\b"
    ),
    "DD/MM/YYYY": re.compile(r"\b\d{2}/\d{2}/\d{4}\b"),
    "MM/DD/YYYY": re.compile(r"\b(?:0[1-9]|1[0-2])/\d{2}/\d{4}\b"),
    "YYYY-MM-DD": re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    "DD-MM-YYYY": re.compile(r"\b\d{2}-\d{2}-\d{4}\b"),
}


def check_date_format(ctx: dict) -> list[QCFinding]:
    """If profile.date_format is set, flag rendered dates that match
    OTHER formats — the underwriter shouldn't see a mix."""
    profile: dict = ctx["profile"]
    pages: list[str] = ctx["pages"]
    text: str = ctx["full_text"]
    target = profile.get("date_format") or ""
    if not target or target not in _DATE_PATTERNS:
        return []
    out: list[QCFinding] = []
    for fmt, pat in _DATE_PATTERNS.items():
        if fmt == target:
            continue
        for m in pat.finditer(text):
            # Skip obvious false positives: things that look like
            # ISO dates inside structured cells (DOB display etc.) —
            # these are common in TALIC's renderer today. We emit at
            # INFO severity so they show up in the report without
            # poisoning batch exit.
            out.append(
                QCFinding(
                    code="DATE_FORMAT_MIXED",
                    category="brand",
                    severity="INFO",
                    location=_locate(pages, m.group(0)),
                    snippet=m.group(0),
                    rule=f"underwriting_platform.qc.checks_brand.check_date_format (target={target!r}, found={fmt!r})",
                    remediation=(f"Normalise all renderer date displays to {target!r}."),
                )
            )
    return out


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

register_check("checks_brand.insurer_name", check_insurer_name)
register_check("checks_brand.system_brand", check_system_brand)
register_check("checks_brand.operator_name", check_operator_name)
register_check("checks_brand.currency_form", check_currency_uniformity)
register_check("checks_brand.date_format", check_date_format)
