"""
qc/blocklists.py — single source of truth for IP / vendor / internal-vocab
patterns that must never appear in a delivered case sheet.

These are project-AGNOSTIC: the same vendor names, model IDs, and
internal codenames need scrubbing whether the artifact is for TALIC,
IFL, or Kotak. Per-insurer additions (custom codename blocklists, etc.)
layer on top via brand_profile.json -> profile["redact"].

Both the renderer's write-time scrubber (renderer.pdf._sanitize_for_client)
and the delivery-time QC enforcer (qc.checks_ip) import these tables.
Defense in depth — scrubber cleans, QC enforces.
"""

from __future__ import annotations
import re


# A1 — LLM vendor tokens.
# Word-boundary match with _ treated as separator so e.g. "KY4" doesn't
# trip the "KY" prefix on any token, and "azure_claude" splits cleanly.
# Mapped target shows what a write-time scrubber should replace each
# with; QC uses the keys as detection patterns only. Per the
# DATAMATICS-LAYOUT design doc, "Primary" / "Secondary" are the
# chosen public terms (the architecture is dual-vendor; the rendered
# text never names the specific brands).
VENDOR_TOKENS: dict[str, str] = {
    "claude": "Primary",
    "anthropic": "Primary",
    "gemini": "Secondary",
    "openai": "Primary",
    "azure": "—",  # generic infra; QC may demote to WARN
    "google ai": "Secondary",
    "google_ai": "Secondary",
    "gpt-": "—",
    "mistral": "—",
    "llama": "—",
}

# A2 — Model version strings. Catches e.g. "claude-opus-4-7", "gemini-2.5-pro",
# "gpt-4-turbo-preview". The pattern matches the version-token tail so it
# fires on the form (regardless of which vendor prefix preceded).
MODEL_VERSION_RE: re.Pattern = re.compile(
    r"\b\d+\.\d+-(?:pro|opus|sonnet|haiku|turbo|flash|mini)\b",
    re.IGNORECASE,
)
MODEL_FULL_ID_RE: re.Pattern = re.compile(
    r"\b(?:claude-(?:opus|sonnet|haiku)-\d+(?:-\d+)?|"
    r"gemini-\d+\.\d+(?:-(?:pro|flash))?|"
    r"gpt-\d+(?:\.\d+)?(?:-(?:turbo|preview))?)"
    r"\b",
    re.IGNORECASE,
)

# A3 — SDK / library tells.
SDK_TELLS: list[str] = [
    "anthropic",
    "langchain",
    "supabase",
    "reportlab",
    "pdfplumber",
    "pymupdf",
    "fitz",
    "openpyxl",
]

# A4 — API endpoint patterns.
API_ENDPOINT_RE: re.Pattern = re.compile(
    r"\b(?:anthropic\.com|googleapis\.com|"
    r"openai\.com|services\.ai\.azure\.com|"
    r"[a-z0-9-]+\.azure\.com)\b",
    re.IGNORECASE,
)

# A5 — Stack-trace residue. Conservative — only fires on tokens
# that should never appear in a user-facing case sheet.
STACK_TRACE_RE: re.Pattern = re.compile(
    r"(?:Traceback \(most recent call last\)|"
    r"^\s*File \"[^\"]+\", line \d+|"
    r"<built-in [a-z]+>)",
    re.MULTILINE,
)

# B1 — Pipeline agent labels (the `_agent` suffix is itself a tell;
# we want "the compliance review" not "the compliance agent" — except
# the public terms "primary agent" / "secondary agent" which are fine).
AGENT_LABEL_RE: re.Pattern = re.compile(
    r"\b(?:medical|identity|compliance|income|assets|surrogate|"
    r"business_insurance|credit_protect)_(?:agent|gemini|claude|openai)\b",
    re.IGNORECASE,
)

# B2 — Internal status codes (the v9 reconciler emits these; the
# renderer's _sanitize_for_client already strips them but we ENFORCE
# here so a regression slips a finding through).
INTERNAL_STATUS_CODES: list[str] = [
    "STP_OK",
    "STP_BLOCK",
    "STP_CONDITIONAL",
]
INTERNAL_STATUS_RE: re.Pattern = re.compile(r"\b(?:STP_OK|STP_BLOCK|STP_CONDITIONAL)\b")

# B3 — Architectural slang. "primary vendor" / "secondary vendor" /
# "Model A" / "Model B" all hint at the dual-vendor architecture in a
# way that should be hidden from the client-facing artifact. The
# renderer translates "Claude"/"Gemini" -> "Vendor A"/"Vendor B" today;
# the QC layer also rejects "primary vendor" / "dual-vendor" / etc.
ARCH_SLANG: list[str] = [
    "primary vendor",
    "secondary vendor",
    "dual-vendor",
    "dual vendor",
    "Model A",
    "Model B",
    "vendor=",
]

# B4 — Function-path leaks. Dotted Python paths in PROSE text (not in
# explicit provenance/source cells where they're correct).
# Pattern: lowercase module names followed by .function_name, but not
# preceded by ' / quote (so "underwriting_platform.engine.flags.foo" in a sentence triggers,
# but a structured rule citation like `'underwriting_platform.engine.foo.bar'` doesn't).
FUNCTION_PATH_RE: re.Pattern = re.compile(
    r"(?<![\"'`])\b(?:engine|agents|orchestrator|pipeline|qc)\."
    r"[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*\b(?![\"'`])"
)

# B5 — Project codename leaks. These are insurer-agnostic; per-project
# additions come from brand_profile.json -> profile["redact"]["internal_codenames"].
COMMON_CODENAMES: list[str] = [
    "v9",
    "v8.4",
    "v8.2",
    "v8",
    "Phase 8b",
    "Phase 8a",
    "Phase 7",
    "TATA scaffold",
]

# B6 — JSON-key leaks in narrative prose. Detect dotted keys with the
# v9 canonical shape: top-level block . sub_key . sub_sub_key.
# Allowed inside structured cells like `field_path`; flagged when it
# appears INSIDE narrative prose.
# We use a heuristic: 3+ identifier segments joined by dots, all
# lowercase, inside a Paragraph-style sentence.
JSON_KEY_RE: re.Pattern = re.compile(
    r"\b(?:applicant|income|assets_liabilities|compliance|medical|"
    r"reconciliation|policy_data|extraction_blocks|verdict|calculations)\."
    r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)+"
    r"\b"
)


# ---------------------------------------------------------------------------
# Helpers for the write-time renderer + the delivery-time QC
# ---------------------------------------------------------------------------


def vendor_token_pattern() -> re.Pattern:
    """Compiled regex matching any A1 vendor token as a whole word.

    Boundaries: alphanumeric only — `_` is treated as a *separator*, so
    "azure_claude" matches "claude" cleanly. This is what we want: the
    underlying string still leaks the vendor identity, even if it's
    snake_cased. The classic KY4 false-positive is prevented by the
    alphanumeric boundary (KY4 ends with a digit, no vendor token
    starts with a digit).
    """
    return re.compile(
        r"(?<![A-Za-z0-9])(" + "|".join(re.escape(k) for k in VENDOR_TOKENS) + r")(?![A-Za-z0-9])",
        re.IGNORECASE,
    )


def mask_vendors(text: str) -> str:
    """Used by renderer.pdf._sanitize_for_client to scrub at write-time.

    Replaces any A1 vendor token with its mapped neutral label. QC
    enforces post-write that no vendor token survived.
    """
    if not text:
        return text or ""
    pat = vendor_token_pattern()

    def _repl(m: "re.Match[str]") -> str:
        return VENDOR_TOKENS.get(m.group(1).lower(), "—")

    return pat.sub(_repl, text)


def mask_model_ids(text: str) -> str:
    """Symmetric counterpart of mask_vendors() — strips model version
    strings ("claude-opus-4-7", "gemini-2.5-pro", "gpt-4-turbo") AND
    bare version tails ("2.5-pro", "opus-4-6").

    Why: `mask_vendors` substitutes the vendor token only. A leaked
    "gemini-2.5-pro" becomes "Secondary-2.5-pro" — still uniquely
    fingerprints Gemini. This helper kills the trailing version
    fingerprint so the rendered text is vendor-agnostic.

    Order: call AFTER mask_vendors so the full-id regex still matches
    intact strings, then the leftover version-tail regex sweeps the
    rest. Both produce the neutral token "model X" / "vX".
    """
    if not text:
        return text or ""
    text = MODEL_FULL_ID_RE.sub("model X", text)
    text = MODEL_VERSION_RE.sub("vX", text)
    return text
