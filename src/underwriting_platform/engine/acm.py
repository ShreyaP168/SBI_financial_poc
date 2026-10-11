"""
engine/acm.py — Adverse Claims Model (ACM) Action Matrix.

Per Term Underwriting Guidelines for UW V5 - Feb 26, page 6:
the ACM band (Preferred / Standard / High / Critical) drives a 5-action
checklist that the underwriter must satisfy before the case can clear.

This module:
  - Resolves the ACM band from applicant.acm_band  (external IIB feed —
    if absent we default to "Pending" so the underwriter can see the gap).
  - Computes the 5 actions and the required documentation per band.
  - Honours channel exemptions (CPR not required for Websales / PB / etc.).
  - Honours product exemptions (Saral Jeevan Bima / ILP-non-term /
    Rural / SSR 11x/15x — ACM not applicable).
  - Returns a structured ACMResult that the renderer surfaces as a
    table.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict


_ACM_BAND_RANK = {
    "PREFERRED": 0,
    "STANDARD": 1,
    "HIGH": 2,
    "CRITICAL": 3,
    "PENDING": -1,
    "N/A": -2,
    "": -2,
}


# Channels exempt from CPR (per page 6 footnote)
_CPR_EXEMPT_CHANNELS = {
    "WEBSALES",
    "WEB SALES",
    "WEB-SALES",
    "DIGITAL PARTNERS",
    "DIGITAL",
    "TDL",
    "PHONEPE",
    "FRONZO SOLUTION",
    "INDMONEY",
    "POLICY BAZAAR",
    "POLICYBAZAAR",
    "PB",
    # Note: "PB PARTNERS" is NOT exempt per the footnote.
}

# Products that exempt the case from ACM entirely
_ACM_EXEMPT_PRODUCT_CODES = {
    "SARAL_JEEVAN",
    "SSR_11X",
    "SSR_15X",
}


@dataclass
class ACMAction:
    code: str  # action_1 ... action_5
    title: str
    required: str  # "Yes" | "No" | "Mandatory" | text
    notes: str = ""


@dataclass
class ACMResult:
    band: str  # PREFERRED / STANDARD / HIGH / CRITICAL / PENDING / N/A
    band_source: str  # "applicant.acm_band" / "default" / "exempt"
    applicable: bool
    exempt_reason: str = ""
    channel: str = ""
    cpr_exempt: bool = False
    cpr_exempt_reason: str = ""
    product_code: str = ""
    actions: list[ACMAction] = field(default_factory=list)
    function: str = "underwriting_platform.engine.acm.compute_acm"

    def to_dict(self) -> dict:
        return {
            "band": self.band,
            "band_source": self.band_source,
            "applicable": self.applicable,
            "exempt_reason": self.exempt_reason,
            "channel": self.channel,
            "cpr_exempt": self.cpr_exempt,
            "cpr_exempt_reason": self.cpr_exempt_reason,
            "product_code": self.product_code,
            "actions": [asdict(a) for a in self.actions],
            "function": self.function,
        }


def _resolve_band(case: dict) -> tuple[str, str]:
    applicant = case.get("applicant") or {}
    band = (applicant.get("acm_band") or "").strip().upper()
    if band in _ACM_BAND_RANK and band not in ("PENDING", "N/A", ""):
        return band, "applicant.acm_band"
    return "PENDING", "default — IIB feed not on file"


def _resolve_channel(case: dict) -> tuple[str, bool, str]:
    applicant = case.get("applicant") or {}
    channel = (applicant.get("channel") or applicant.get("agent_channel") or "").strip().upper()
    if not channel:
        return "", False, ""
    if channel in _CPR_EXEMPT_CHANNELS:
        return channel, True, f"Channel '{channel}' exempt from CPR per ACM footnote."
    return channel, False, ""


def _resolve_product_exempt(case: dict) -> tuple[bool, str, str]:
    applicant = case.get("applicant") or {}
    code = (applicant.get("product_code") or "").strip().upper()
    if code in _ACM_EXEMPT_PRODUCT_CODES:
        return True, f"Product {code} is exempt from ACM matrix.", code
    return False, "", code


def compute_acm(case: dict) -> ACMResult:
    """Compute the ACM Action Matrix for this case."""
    exempt, exempt_reason, product_code = _resolve_product_exempt(case)
    if exempt:
        return ACMResult(
            band="N/A",
            band_source="exempt",
            applicable=False,
            exempt_reason=exempt_reason,
            product_code=product_code,
            actions=[],
        )

    band, band_source = _resolve_band(case)
    channel, cpr_exempt, cpr_reason = _resolve_channel(case)

    # Build the 5 actions per page-6 table
    actions: list[ACMAction] = []

    # Action 1 — Income proof submission
    is_high_or_critical = band in ("HIGH", "CRITICAL")
    actions.append(
        ACMAction(
            code="action_1",
            title="Income proof submission",
            required="Mandatory for all sum assured",
            notes=(
                "Salaried: latest Form 16 OR bank statement with salary credit "
                "(latest 3 months / 1 month proof beyond salary slip from top "
                "companies). Self-Employed / Business Owner: ITR with COI "
                "mandatory for last 3 years. [No cover-bridging on surrogate]"
                + (
                    " — band requires standard income proof regardless of SA."
                    if is_high_or_critical
                    else ""
                )
            ),
        )
    )

    # Action 2 — RVMER (Regular Video MER)
    actions.append(
        ACMAction(
            code="action_2",
            title="RVMER (Regular Video MER)",
            required=("Yes" if band == "CRITICAL" else "No"),
            notes=(
                "Required for Critical band only."
                if band != "CRITICAL"
                else "Mandatory — Critical-band cases require RVMER irrespective of SA."
            ),
        )
    )

    # Action 3 — Detailed Sales Report from Branch Manager (CPR)
    if band == "PREFERRED" or band == "STANDARD":
        a3_required = "No"
        a3_notes = "Not required at Preferred / Standard band."
    elif cpr_exempt:
        a3_required = "Exempt"
        a3_notes = cpr_reason
    else:
        a3_required = "Yes (CPR)"
        a3_notes = (
            "Detailed Channel Performance Report (CPR) from Branch Manager "
            "required at High / Critical band. Channel exemption applies "
            "to Websales / Digital Partners / TDL / PhonePe / Fronzo / "
            "IndMoney / Policy Bazaar (excludes PB Partners)."
        )
    actions.append(
        ACMAction(
            code="action_3",
            title="Detailed Sales Report from Branch Manager",
            required=a3_required,
            notes=a3_notes,
        )
    )

    # Action 4 — Tele/Video MER
    if band == "PREFERRED" or band == "STANDARD":
        a4_required = "Existing EODB Tele/VMER applies"
        a4_notes = (
            "Standard EODB (Easy On-boarding for Doctor Booking) Tele "
            "or Video MER parameters apply."
        )
    else:  # High or Critical
        a4_required = "Full Grid Medicals (no Tele/Video)"
        a4_notes = "High / Critical band requires full grid medicals; Tele / Video MER not allowed."
    actions.append(
        ACMAction(
            code="action_4",
            title="Tele / Video MER",
            required=a4_required,
            notes=a4_notes,
        )
    )

    # Action 5 — Senior UW approval
    actions.append(
        ACMAction(
            code="action_5",
            title="Senior UW approval",
            required=("Yes" if band == "CRITICAL" else "No"),
            notes=(
                "Critical band requires Senior UW sign-off on the file."
                if band == "CRITICAL"
                else "Not required at this band."
            ),
        )
    )

    return ACMResult(
        band=band,
        band_source=band_source,
        applicable=True,
        channel=channel,
        cpr_exempt=cpr_exempt,
        cpr_exempt_reason=cpr_reason,
        product_code=product_code,
        actions=actions,
    )
