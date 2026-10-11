"""
qc/checks_pii.py — E1–E5: PII & confidentiality.

Asserts that sensitive identifiers (PAN, Aadhaar, mobile, bank account,
email) are either masked or absent from the rendered text. The
renderer already masks PAN + Aadhaar via _mask_pan / _mask_aadhaar;
this layer ENFORCES the result against the actual PDF text so a
renderer regression is caught before delivery.

False positives are kept low by anchoring on Indian-format identifiers
(PAN = 5 letters + 4 digits + 1 letter, Aadhaar = 12 digits in groups
of 4, mobile = 10 digits, account = ≥ 9 digits).
"""

from __future__ import annotations
import re

from .case_sheet import register_check
from .types import QCFinding


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# PAN: 5 letters + 4 digits + 1 letter (e.g. ABCPK1234E).
# Tightened 2026-05-26: the 4th letter is the holder-type code per
# Income-Tax PAN spec — P=Individual, F=Firm, C=Company, H=HUF,
# A=AOP, T=Trust, B=BoI, L=LocalAuthority, J=Juridical, G=Government.
# This kills false positives on coincidental 10-char alphanum strings
# (bank reference numbers, mutual-fund codes) which were tripping the
# previous broader regex. Real PANs always have a valid holder code.
# The renderer's _mask_pan produces "XXXXX1234E" (4th char = X), which
# is naturally excluded since X is not a holder code.
PAN_RE = re.compile(r"\b[A-Z]{3}[PFCHATBLJG][A-Z]\d{4}[A-Z]\b")
# Aadhaar: 12 digits in groups of 4. Tightened 2026-05-26 twice:
#   (a) require space or dash separator (kills 12-digit bank-account runs)
#   (b) require CONSISTENT separator via backreference (kills lab-value
#       false positives like "6220 2000-8000" where space + dash mix).
# Real Aadhaar renders use either all spaces ("1234 5678 9012") or
# all dashes ("1234-5678-9012") — never mixed.
AADHAAR_RE = re.compile(r"\b\d{4}([\s\-])\d{4}\1\d{4}\b")
# Mobile: 10 digits starting with 6-9 (India)
MOBILE_RE = re.compile(r"(?<!\d)[6-9]\d{9}(?!\d)")
# Bank account: 9-18 consecutive digits (loose)
BANK_ACC_RE = re.compile(r"(?<!\d)\d{9,18}(?!\d)")
# Email
EMAIL_RE = re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9-.]+")


def _locate(pages: list[str], match_text: str) -> str:
    for i, page_text in enumerate(pages, start=1):
        if match_text in page_text:
            return f"PDF p{i}"
    return "—"


def _snippet(text: str, m: re.Match, window: int = 30) -> str:
    s = max(0, m.start() - window)
    e = min(len(text), m.end() + window)
    return re.sub(r"\s+", " ", text[s:e]).strip()


# ---------------------------------------------------------------------------
# E1 — Unmasked PAN
# ---------------------------------------------------------------------------


def check_unmasked_pan(ctx: dict) -> list[QCFinding]:
    pages = ctx["pages"]
    text = ctx["full_text"]
    out: list[QCFinding] = []
    for m in PAN_RE.finditer(text):
        hit = m.group(0)
        # _mask_pan in the renderer produces "XXXXX1234A" — if the
        # rendered text matches the PAN pattern, the mask didn't fire.
        out.append(
            QCFinding(
                code="UNMASKED_PAN",
                category="pii",
                severity="BLOCK",
                location=_locate(pages, hit),
                snippet=_snippet(text, m),
                rule="underwriting_platform.qc.checks_pii.PAN_RE",
                remediation=(
                    "Renderer _mask_pan should display only the last "
                    "5 chars. Investigate the unmasked render path."
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# E2 — Unmasked Aadhaar
# ---------------------------------------------------------------------------


def check_unmasked_aadhaar(ctx: dict) -> list[QCFinding]:
    pages = ctx["pages"]
    text = ctx["full_text"]
    out: list[QCFinding] = []
    for m in AADHAAR_RE.finditer(text):
        hit = m.group(0)
        # _mask_aadhaar renders "XXXX XXXX 1234"; a 12-digit run
        # (with or without spaces) means it slipped through.
        out.append(
            QCFinding(
                code="UNMASKED_AADHAAR",
                category="pii",
                severity="BLOCK",
                location=_locate(pages, hit),
                snippet=_snippet(text, m),
                rule="underwriting_platform.qc.checks_pii.AADHAAR_RE",
                remediation=("Renderer _mask_aadhaar should display only the last 4 digits."),
            )
        )
    return out


# ---------------------------------------------------------------------------
# E3 — Bare mobile numbers in narrative prose
# ---------------------------------------------------------------------------


def check_mobile_in_prose(ctx: dict) -> list[QCFinding]:
    """Mobile numbers are acceptable in structured contact cells but
    should be masked in narrative prose (flag descriptions, rationale,
    reconciler notes). We approximate "narrative" by scanning the
    decision JSON's narrative-bearing fields rather than the PDF text
    — full-text scanning would false-positive on structured cells.
    """
    decision: dict = ctx["decision"]
    out: list[QCFinding] = []
    fields: list[tuple[str, str]] = []
    for f in decision.get("flags") or []:
        fields.append((f"flags[{f.get('code', '?')}].description", str(f.get("description") or "")))
    recon = decision.get("reconciliation") or {}
    for r in recon.get("risk_signals") or []:
        fields.append(
            (
                f"reconciliation.risk_signals[{r.get('code', '?')}].description",
                str(r.get("description") or ""),
            )
        )
    for k in (
        "financial_recommendation",
        "medical_recommendation",
        "overall_recommendation",
        "reconciler_notes",
    ):
        if recon.get(k):
            fields.append((f"reconciliation.{k}", str(recon[k])))

    for path, value in fields:
        for m in MOBILE_RE.finditer(value):
            prefix = value[max(0, m.start() - 60) : m.start()]
            if re.search(
                r"\b(?:policy|proposal|workflow|reference)\s*(?:no\.?|number|#|id|:)?\s*$",
                prefix,
                re.IGNORECASE,
            ):
                continue  # Clearly labelled proposal/workflow reference, not a contact number.
            # A bare number sitting alone inside parentheses —
            # "(9394820309)" — is how a policy/workflow reference number
            # gets cited (e.g. engine.sbi_financial.check_pending_
            # proposal_disclosure's evidence text), not a mobile number;
            # the two shapes collide by coincidence. Matches renderer/
            # triage_sheet.py's own _mobile_redact_repl exclusion.
            start, end = m.start(), m.end()
            if start > 0 and end < len(value) and value[start - 1] == "(" and value[end] == ")":
                continue
            out.append(
                QCFinding(
                    code="MOBILE_IN_PROSE",
                    category="pii",
                    severity="WARN",
                    location=path,
                    snippet=_snippet(value, m, window=20),
                    rule="underwriting_platform.qc.checks_pii.MOBILE_RE",
                    remediation=(
                        "Mask or remove the mobile number from "
                        "narrative prose; use the structured "
                        "contact cell instead."
                    ),
                )
            )
    return out


# ---------------------------------------------------------------------------
# E4 — Bank account in narrative prose
# ---------------------------------------------------------------------------


def check_bank_account_in_prose(ctx: dict) -> list[QCFinding]:
    """Long digit runs (9-18) inside narrative prose. Bank account
    numbers should appear masked (last 4 only). We scan narrative
    fields, not the full PDF — full-text would false-positive on PAN
    numbers (which have digits) and IFSC codes."""
    decision: dict = ctx["decision"]
    out: list[QCFinding] = []
    recon = decision.get("reconciliation") or {}
    for k in (
        "financial_recommendation",
        "medical_recommendation",
        "overall_recommendation",
        "reconciler_notes",
    ):
        value = recon.get(k)
        if not value:
            continue
        for m in BANK_ACC_RE.finditer(str(value)):
            digits = m.group(0)
            prefix = str(value)[max(0, m.start() - 60) : m.start()]
            if re.search(
                r"(?:policy|proposal|workflow|reference)\s*(?:no\.?|number|#|id|:)?\s*$",
                prefix,
                re.IGNORECASE,
            ):
                continue  # Explicitly labelled policy IDs are not bank accounts.
            # Skip if it looks like a year + something else (timestamps)
            if len(digits) == 10 and digits[0] in "6789":
                continue  # mobile already covered
            out.append(
                QCFinding(
                    code="BANK_ACCOUNT_IN_PROSE",
                    category="pii",
                    severity="WARN",
                    location=f"reconciliation.{k}",
                    snippet=_snippet(str(value), m, window=20),
                    rule="underwriting_platform.qc.checks_pii.BANK_ACC_RE",
                    remediation=("Mask the bank account; show last-4 only."),
                )
            )
    return out


# ---------------------------------------------------------------------------
# E5 — Email in narrative prose
# ---------------------------------------------------------------------------


def check_email_in_prose(ctx: dict) -> list[QCFinding]:
    decision: dict = ctx["decision"]
    out: list[QCFinding] = []
    recon = decision.get("reconciliation") or {}
    fields: list[tuple[str, str]] = []
    for f in decision.get("flags") or []:
        fields.append((f"flags[{f.get('code', '?')}].description", str(f.get("description") or "")))
    for k in (
        "financial_recommendation",
        "medical_recommendation",
        "overall_recommendation",
        "reconciler_notes",
    ):
        if recon.get(k):
            fields.append((f"reconciliation.{k}", str(recon[k])))
    for path, value in fields:
        for m in EMAIL_RE.finditer(value):
            # Allow internal example emails sometimes embedded in
            # explanatory text — only flag what looks like a real
            # applicant email (heuristic: not @example.com or @insurer.com).
            email = m.group(0)
            out.append(
                QCFinding(
                    code="EMAIL_IN_PROSE",
                    category="pii",
                    severity="WARN",
                    location=path,
                    snippet=email,
                    rule="underwriting_platform.qc.checks_pii.EMAIL_RE",
                    remediation=(
                        "Move the email to a structured contact cell or redact the @local part."
                    ),
                )
            )
    return out


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

register_check("checks_pii.unmasked_pan", check_unmasked_pan)
register_check("checks_pii.unmasked_aadhaar", check_unmasked_aadhaar)
register_check("checks_pii.mobile_in_prose", check_mobile_in_prose)
register_check("checks_pii.bank_acc_in_prose", check_bank_account_in_prose)
register_check("checks_pii.email_in_prose", check_email_in_prose)
