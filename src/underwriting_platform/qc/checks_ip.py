"""
qc/checks_ip.py — A1–A5 + B1–B6 enforcement against the rendered PDF.

This module
ENFORCES at delivery-time. A finding here means a vendor / SDK / model
ID / internal-status / agent-label / function-path / project-codename /
JSON-key string survived all sanitisation and reached the client-facing
artifact. Severity is BLOCK for the IP / vendor-stack leaks (A-class)
and WARN for the internal-vocab leaks (B-class) — internal-vocab
violations are stylistic, not contractual.
"""

from __future__ import annotations
import re

from .blocklists import (
    AGENT_LABEL_RE,
    API_ENDPOINT_RE,
    ARCH_SLANG,
    COMMON_CODENAMES,
    FUNCTION_PATH_RE,
    INTERNAL_STATUS_RE,
    JSON_KEY_RE,
    MODEL_FULL_ID_RE,
    MODEL_VERSION_RE,
    SDK_TELLS,
    STACK_TRACE_RE,
    VENDOR_TOKENS,
    vendor_token_pattern,
)
from .case_sheet import register_check
from .types import QCFinding


# ---------------------------------------------------------------------------
# Helpers — locate the offending page + emit a narrow snippet
# ---------------------------------------------------------------------------


def _locate(pages: list[str], match_text: str) -> str:
    """Return 'PDF p<N>' for the first page containing match_text, else '—'."""
    for i, page_text in enumerate(pages, start=1):
        if match_text in page_text:
            return f"PDF p{i}"
    return "—"


def _snippet(text: str, match: re.Match, window: int = 40) -> str:
    """A ±40-char window around the regex match, with leading/trailing
    whitespace collapsed for readability."""
    start = max(0, match.start() - window)
    end = min(len(text), match.end() + window)
    s = text[start:end].replace("\n", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return f"...{s}..." if start > 0 or end < len(text) else s


# ---------------------------------------------------------------------------
# A1 — Vendor tokens
# ---------------------------------------------------------------------------


def check_vendor_tokens(ctx: dict) -> list[QCFinding]:
    pages: list[str] = ctx["pages"]
    text: str = ctx["full_text"]
    out: list[QCFinding] = []
    pat = vendor_token_pattern()
    for m in pat.finditer(text):
        token = m.group(1)
        # 'azure' / 'gpt-' / 'llama' / 'mistral' are demoted to WARN
        # because they're often dependency / runtime tells rather than
        # confidential vendor identity. Anthropic / Claude / Gemini /
        # OpenAI are BLOCK-grade.
        key = token.lower()
        severity = (
            "BLOCK"
            if key in {"claude", "anthropic", "gemini", "openai", "google ai", "google_ai"}
            else "WARN"
        )
        out.append(
            QCFinding(
                code="VENDOR_TOKEN_LEAK",
                category="ip_leak",
                severity=severity,
                location=_locate(pages, token),
                snippet=_snippet(text, m),
                rule=f"underwriting_platform.qc.blocklists.VENDOR_TOKENS[{key!r}]",
                remediation=(
                    "Mask at the engine layer via "
                    "the active renderer or drop the "
                    "raw vendor identifier from the source string."
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# A2 — Model version strings
# ---------------------------------------------------------------------------


def check_model_versions(ctx: dict) -> list[QCFinding]:
    pages: list[str] = ctx["pages"]
    text: str = ctx["full_text"]
    out: list[QCFinding] = []
    for pattern, label in ((MODEL_VERSION_RE, "version-tail"), (MODEL_FULL_ID_RE, "full-id")):
        for m in pattern.finditer(text):
            hit = m.group(0)
            out.append(
                QCFinding(
                    code="MODEL_ID_LEAK",
                    category="ip_leak",
                    severity="BLOCK",
                    location=_locate(pages, hit),
                    snippet=_snippet(text, m),
                    rule=f"underwriting_platform.qc.blocklists.{label}",
                    remediation=(
                        "Strip model identifiers before serialising; "
                        "they should never reach the case sheet."
                    ),
                )
            )
    return out


# ---------------------------------------------------------------------------
# A3 — SDK / library tells
# ---------------------------------------------------------------------------


def check_sdk_tells(ctx: dict) -> list[QCFinding]:
    pages: list[str] = ctx["pages"]
    text: str = ctx["full_text"]
    out: list[QCFinding] = []
    for sdk in SDK_TELLS:
        pat = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(sdk)}(?![A-Za-z0-9_])", re.IGNORECASE)
        for m in pat.finditer(text):
            out.append(
                QCFinding(
                    code="SDK_NAME_LEAK",
                    category="ip_leak",
                    severity="WARN",
                    location=_locate(pages, sdk),
                    snippet=_snippet(text, m),
                    rule=f"underwriting_platform.qc.blocklists.SDK_TELLS[{sdk!r}]",
                    remediation=(
                        "Replace the SDK / library name with a "
                        "generic description (e.g. 'PDF extractor')."
                    ),
                )
            )
    return out


# ---------------------------------------------------------------------------
# A4 — API endpoints
# ---------------------------------------------------------------------------


def check_api_endpoints(ctx: dict) -> list[QCFinding]:
    pages: list[str] = ctx["pages"]
    text: str = ctx["full_text"]
    out: list[QCFinding] = []
    for m in API_ENDPOINT_RE.finditer(text):
        hit = m.group(0)
        out.append(
            QCFinding(
                code="API_ENDPOINT_LEAK",
                category="ip_leak",
                severity="BLOCK",
                location=_locate(pages, hit),
                snippet=_snippet(text, m),
                rule="underwriting_platform.qc.blocklists.API_ENDPOINT_RE",
                remediation="Endpoints should never appear in user-facing prose.",
            )
        )
    return out


# ---------------------------------------------------------------------------
# A5 — Stack-trace residue
# ---------------------------------------------------------------------------


def check_stack_traces(ctx: dict) -> list[QCFinding]:
    pages: list[str] = ctx["pages"]
    text: str = ctx["full_text"]
    out: list[QCFinding] = []
    for m in STACK_TRACE_RE.finditer(text):
        hit = m.group(0)
        out.append(
            QCFinding(
                code="STACK_TRACE_RESIDUE",
                category="ip_leak",
                severity="BLOCK",
                location=_locate(pages, hit[:40]),
                snippet=_snippet(text, m),
                rule="underwriting_platform.qc.blocklists.STACK_TRACE_RE",
                remediation=(
                    "An exception traceback leaked into the rendered "
                    "PDF — investigate which agent / renderer path "
                    "swallowed an error into a string."
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# B1 — Agent labels
# ---------------------------------------------------------------------------


def check_agent_labels(ctx: dict) -> list[QCFinding]:
    pages: list[str] = ctx["pages"]
    text: str = ctx["full_text"]
    out: list[QCFinding] = []
    for m in AGENT_LABEL_RE.finditer(text):
        hit = m.group(0)
        out.append(
            QCFinding(
                code="AGENT_LABEL_LEAK",
                category="internal_vocab",
                severity="WARN",
                location=_locate(pages, hit),
                snippet=_snippet(text, m),
                rule="underwriting_platform.qc.blocklists.AGENT_LABEL_RE",
                remediation=(
                    "Replace the agent slug with a human term "
                    "(e.g. 'compliance review' instead of "
                    "'compliance_agent')."
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# B2 — Internal status codes
# ---------------------------------------------------------------------------


def check_internal_status_codes(ctx: dict) -> list[QCFinding]:
    pages: list[str] = ctx["pages"]
    text: str = ctx["full_text"]
    out: list[QCFinding] = []
    for m in INTERNAL_STATUS_RE.finditer(text):
        hit = m.group(0)
        out.append(
            QCFinding(
                code="INTERNAL_STATUS_LEAK",
                category="internal_vocab",
                severity="WARN",
                location=_locate(pages, hit),
                snippet=_snippet(text, m),
                rule="underwriting_platform.qc.blocklists.INTERNAL_STATUS_RE",
                remediation=(
                    "Internal STP code leaked despite the renderer "
                    "scrubber — re-check _sanitize_for_client coverage."
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# B3 — Architectural slang
# ---------------------------------------------------------------------------


def check_arch_slang(ctx: dict) -> list[QCFinding]:
    pages: list[str] = ctx["pages"]
    text: str = ctx["full_text"]
    out: list[QCFinding] = []
    for phrase in ARCH_SLANG:
        pat = re.compile(re.escape(phrase), re.IGNORECASE)
        for m in pat.finditer(text):
            out.append(
                QCFinding(
                    code="ARCH_SLANG_LEAK",
                    category="internal_vocab",
                    severity="WARN",
                    location=_locate(pages, phrase),
                    snippet=_snippet(text, m),
                    rule=f"underwriting_platform.qc.blocklists.ARCH_SLANG[{phrase!r}]",
                    remediation=("Reword the prose to avoid the dual-vendor architecture hint."),
                )
            )
    return out


# ---------------------------------------------------------------------------
# B4 — Function paths in prose
# ---------------------------------------------------------------------------


def check_function_paths(ctx: dict) -> list[QCFinding]:
    """B4: dotted Python paths in narrative prose.

    Allowed inside structured provenance cells (rule field, function
    field) where they're correct citations; flagged when they appear
    inside flag/risk/contradiction *descriptions* or reconciler prose.
    We approximate this by scanning narrative-bearing fields in the
    decision JSON rather than the rendered PDF text — the PDF doesn't
    preserve cell semantics.
    """
    out: list[QCFinding] = []
    decision: dict = ctx["decision"]

    narrative_fields: list[tuple[str, str]] = []
    for f in decision.get("flags") or []:
        narrative_fields.append(
            (f"flags[{f.get('code', '?')}].description", str(f.get("description") or ""))
        )
    recon = decision.get("reconciliation") or {}
    for r in recon.get("risk_signals") or []:
        narrative_fields.append(
            (
                f"reconciliation.risk_signals[{r.get('code', '?')}].description",
                str(r.get("description") or ""),
            )
        )
    for c in recon.get("contradictions") or []:
        narrative_fields.append(
            (
                f"reconciliation.contradictions[{c.get('field', '?')}].reasoning",
                str(c.get("reasoning") or ""),
            )
        )
    for k in (
        "financial_recommendation",
        "medical_recommendation",
        "overall_recommendation",
        "reconciler_notes",
    ):
        if recon.get(k):
            narrative_fields.append((f"reconciliation.{k}", str(recon[k])))

    for path, value in narrative_fields:
        for m in FUNCTION_PATH_RE.finditer(value):
            out.append(
                QCFinding(
                    code="FUNCTION_PATH_IN_PROSE",
                    category="internal_vocab",
                    severity="WARN",
                    location=path,
                    snippet=_snippet(value, m, window=25),
                    rule="underwriting_platform.qc.blocklists.FUNCTION_PATH_RE",
                    remediation=(
                        "Move the dotted path into a structured cell "
                        "(rule / function field) or strip it from "
                        "the prose."
                    ),
                )
            )
    return out


# ---------------------------------------------------------------------------
# B5 — Project codenames
# ---------------------------------------------------------------------------


def check_project_codenames(ctx: dict) -> list[QCFinding]:
    pages: list[str] = ctx["pages"]
    text: str = ctx["full_text"]
    profile: dict = ctx["profile"]
    out: list[QCFinding] = []

    # Common defaults + per-project additions
    extras = (profile.get("redact") or {}).get("internal_codenames") or []
    codenames = list(COMMON_CODENAMES) + list(extras)
    for cn in codenames:
        # Anchor on word boundaries so "v9" doesn't match inside "v9-ifl"
        pat = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(cn)}(?![A-Za-z0-9_])", re.IGNORECASE)
        for m in pat.finditer(text):
            out.append(
                QCFinding(
                    code="CODENAME_LEAK",
                    category="internal_vocab",
                    severity="WARN",
                    location=_locate(pages, cn),
                    snippet=_snippet(text, m),
                    rule=f"underwriting_platform.qc.blocklists.COMMON_CODENAMES or profile.redact.internal_codenames",
                    remediation=("Remove the internal codename from the user-facing artifact."),
                )
            )
    return out


# ---------------------------------------------------------------------------
# B6 — JSON key paths in narrative prose
# ---------------------------------------------------------------------------


def check_json_key_leaks(ctx: dict) -> list[QCFinding]:
    out: list[QCFinding] = []
    decision: dict = ctx["decision"]
    recon = decision.get("reconciliation") or {}

    narrative_fields: list[tuple[str, str]] = []
    for f in decision.get("flags") or []:
        narrative_fields.append(
            (f"flags[{f.get('code', '?')}].description", str(f.get("description") or ""))
        )
    for r in recon.get("risk_signals") or []:
        narrative_fields.append(
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
            narrative_fields.append((f"reconciliation.{k}", str(recon[k])))

    for path, value in narrative_fields:
        for m in JSON_KEY_RE.finditer(value):
            out.append(
                QCFinding(
                    code="JSON_KEY_IN_PROSE",
                    category="internal_vocab",
                    severity="INFO",
                    location=path,
                    snippet=_snippet(value, m, window=25),
                    rule="underwriting_platform.qc.blocklists.JSON_KEY_RE",
                    remediation=(
                        "Reference the field by its human label "
                        "rather than its canonical JSON path."
                    ),
                )
            )
    return out


# ---------------------------------------------------------------------------
# Register all checks with the orchestrator
# ---------------------------------------------------------------------------

register_check("checks_ip.vendor_tokens", check_vendor_tokens)
register_check("checks_ip.model_versions", check_model_versions)
register_check("checks_ip.sdk_tells", check_sdk_tells)
register_check("checks_ip.api_endpoints", check_api_endpoints)
register_check("checks_ip.stack_traces", check_stack_traces)
register_check("checks_ip.agent_labels", check_agent_labels)
register_check("checks_ip.internal_status_codes", check_internal_status_codes)
register_check("checks_ip.arch_slang", check_arch_slang)
register_check("checks_ip.function_paths", check_function_paths)
register_check("checks_ip.project_codenames", check_project_codenames)
register_check("checks_ip.json_key_leaks", check_json_key_leaks)
