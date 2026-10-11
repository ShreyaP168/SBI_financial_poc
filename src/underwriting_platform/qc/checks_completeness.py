"""
qc/checks_completeness.py — F1–F6 readability + G1–G4 auditability.

Readability (F): placeholder leaks (None, null, undefined, NaN as
sentences), HTML entity leaks (&lt;, &amp;), Python literal leaks
([{, dict_keys(), raw repr output), truncated text, empty sections,
escape leaks (literal \\n, \\t).

Auditability (G): every calculation carries non-empty sources[] + rule;
every flag carries a rule field; verdict.decision_drivers is non-empty;
reconciler_notes is substantive (> 200 chars when a reconciler ran).
"""

from __future__ import annotations
import re

from .case_sheet import register_check
from .types import QCFinding


# ---------------------------------------------------------------------------
# F1 — Placeholder leaks in narrative prose
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:None|null|undefined|NaN|N/A)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def check_placeholder_leaks(ctx: dict) -> list[QCFinding]:
    """Look for 'None' / 'null' / 'NaN' inside narrative prose where a
    real value was expected. We scan flag descriptions, reconciler
    narratives, and risk-signal descriptions — these are the places
    where an f-string would have produced a literal 'None' if the
    upstream value was missing.
    """
    decision: dict = ctx["decision"]
    recon = decision.get("reconciliation") or {}
    out: list[QCFinding] = []

    fields: list[tuple[str, str]] = []
    for f in decision.get("flags") or []:
        fields.append((f"flags[{f.get('code', '?')}].description", str(f.get("description") or "")))
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
        # The token "N/A" inside a structured display is fine; we only
        # care about it inside what should be narrative sentences. Skip
        # rows that are obviously short labels.
        if len(value) < 20:
            continue
        for m in _PLACEHOLDER_RE.finditer(value):
            tok = m.group(0)
            # "None declared" / "Not applicable" — legitimate
            tail = value[m.end() : m.end() + 15].lower()
            if re.match(r"\s+(?:declared|applicable|of|are|is|were|was|have|has|found)\b", tail):
                continue
            out.append(
                QCFinding(
                    code="PLACEHOLDER_IN_PROSE",
                    category="readability",
                    severity="WARN",
                    location=path,
                    snippet=tok + value[m.end() : m.end() + 30],
                    rule="underwriting_platform.qc.checks_completeness._PLACEHOLDER_RE",
                    remediation=(
                        f"Literal {tok!r} reached the narrative — "
                        "upstream computation returned None / NaN."
                    ),
                )
            )
    return out


# ---------------------------------------------------------------------------
# F2 — HTML entity leaks
# ---------------------------------------------------------------------------

_HTML_ENTITY_RE = re.compile(r"&(?:lt|gt|amp|nbsp|quot|apos|#\d+);")


def check_html_entity_leaks(ctx: dict) -> list[QCFinding]:
    pages: list[str] = ctx["pages"]
    text: str = ctx["full_text"]
    out: list[QCFinding] = []
    for m in _HTML_ENTITY_RE.finditer(text):
        hit = m.group(0)
        # Locate
        loc = "—"
        for i, p in enumerate(pages, 1):
            if hit in p:
                loc = f"PDF p{i}"
                break
        out.append(
            QCFinding(
                code="HTML_ENTITY_LEAK",
                category="readability",
                severity="WARN",
                location=loc,
                snippet=hit,
                rule="underwriting_platform.qc.checks_completeness._HTML_ENTITY_RE",
                remediation=(
                    "Renderer over-escaped a string; unescape "
                    "before rendering or check _html_escape usage."
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# F3 — Python literal leaks
# ---------------------------------------------------------------------------

_PY_LITERAL_RE = re.compile(
    r"(?:dict_keys\(\[|odict_items\(|defaultdict\(|"
    r"(?<![A-Za-z0-9_])\[\{(?:[^}]*?\:)|"
    r"(?<![A-Za-z0-9_])\}\](?![A-Za-z0-9_]))"
)


def check_python_literals(ctx: dict) -> list[QCFinding]:
    """`dict_keys([...])`, `defaultdict(...)`, raw `[{...}]` repr."""
    pages: list[str] = ctx["pages"]
    text: str = ctx["full_text"]
    out: list[QCFinding] = []
    for m in _PY_LITERAL_RE.finditer(text):
        hit = m.group(0)
        loc = "—"
        for i, p in enumerate(pages, 1):
            if hit in p:
                loc = f"PDF p{i}"
                break
        out.append(
            QCFinding(
                code="PYTHON_LITERAL_LEAK",
                category="readability",
                severity="WARN",
                location=loc,
                snippet=hit,
                rule="underwriting_platform.qc.checks_completeness._PY_LITERAL_RE",
                remediation=(
                    "A Python repr() output reached the rendered "
                    "PDF — convert via human-readable formatter."
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# F4 — Mid-word ellipsis truncation
# ---------------------------------------------------------------------------

_TRUNC_RE = re.compile(r"\b[A-Za-z]{2,}…(?:\s|$)")


def check_truncations(ctx: dict) -> list[QCFinding]:
    """Cell text ending with `…` after a word fragment — usually a
    too-aggressive truncation. We allow `…` at clause boundaries
    (after punctuation, after whitespace) but flag mid-word."""
    pages: list[str] = ctx["pages"]
    text: str = ctx["full_text"]
    out: list[QCFinding] = []
    seen: set[str] = set()
    for m in _TRUNC_RE.finditer(text):
        hit = m.group(0).strip()
        if hit in seen:
            continue
        seen.add(hit)
        loc = "—"
        for i, p in enumerate(pages, 1):
            if hit in p:
                loc = f"PDF p{i}"
                break
        out.append(
            QCFinding(
                code="MID_WORD_TRUNCATION",
                category="readability",
                severity="INFO",
                location=loc,
                snippet=hit,
                rule="underwriting_platform.qc.checks_completeness._TRUNC_RE",
                remediation=(
                    "Cell content truncated mid-word; widen the "
                    "column or shorten the source string."
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# F6 — Escape leaks (literal \n, \t)
# ---------------------------------------------------------------------------

_ESCAPE_RE = re.compile(r"(?:\\n|\\t|\\r)\b")


def check_escape_leaks(ctx: dict) -> list[QCFinding]:
    decision: dict = ctx["decision"]
    recon = decision.get("reconciliation") or {}
    out: list[QCFinding] = []
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
        for m in _ESCAPE_RE.finditer(value):
            out.append(
                QCFinding(
                    code="ESCAPE_SEQUENCE_LEAK",
                    category="readability",
                    severity="WARN",
                    location=path,
                    snippet=m.group(0),
                    rule="underwriting_platform.qc.checks_completeness._ESCAPE_RE",
                    remediation=(
                        "Literal escape sequence reached the prose — decode before rendering."
                    ),
                )
            )
    return out


# ---------------------------------------------------------------------------
# G1 — Calculation provenance
# ---------------------------------------------------------------------------


def check_calc_provenance(ctx: dict) -> list[QCFinding]:
    decision: dict = ctx["decision"]
    calcs = decision.get("calculations") or {}
    out: list[QCFinding] = []
    for name, calc in calcs.items():
        # Sources can legitimately be empty for engine-internal calcs
        # (e.g. lookups that consume no agent output) — those should
        # still carry a rule field. We flag the AND case: no sources
        # AND no rule = hollow provenance.
        srcs = calc.get("sources") or []
        rule = (calc.get("rule") or "").strip()
        if not rule or (not srcs and calc.get("source_not_applicable") is not True):
            out.append(
                QCFinding(
                    code="CALC_NO_PROVENANCE",
                    category="auditability",
                    severity="WARN",
                    location=f"calculations.{name}",
                    snippet=f"{name} = {calc.get('value')}",
                    rule="underwriting_platform.qc.checks_completeness.check_calc_provenance",
                    remediation=(
                        "Add source evidence and a rule citation "
                        "so the underwriter can audit this value."
                    ),
                )
            )
    return out


# ---------------------------------------------------------------------------
# G2 — Flag rule reference
# ---------------------------------------------------------------------------


def check_flag_rule(ctx: dict) -> list[QCFinding]:
    decision: dict = ctx["decision"]
    out: list[QCFinding] = []
    for f in decision.get("flags") or []:
        rule = (f.get("rule") or "").strip()
        if not rule:
            out.append(
                QCFinding(
                    code="FLAG_NO_RULE",
                    category="auditability",
                    severity="WARN",
                    location=f"flags[{f.get('code', '?')}]",
                    snippet=str(f.get("code") or "—"),
                    rule="underwriting_platform.qc.checks_completeness.check_flag_rule",
                    remediation=(
                        "Every flag must carry a rule reference "
                        "(engine/flags.py module path or rule slug)."
                    ),
                )
            )
    return out


# ---------------------------------------------------------------------------
# G3 — Verdict drivers
# ---------------------------------------------------------------------------


def check_verdict_drivers(ctx: dict) -> list[QCFinding]:
    decision: dict = ctx["decision"]
    drivers = (decision.get("verdict") or {}).get("decision_drivers") or []
    if not drivers:
        return [
            QCFinding(
                code="VERDICT_NO_DRIVERS",
                category="auditability",
                severity="BLOCK",
                location="verdict.decision_drivers",
                snippet="(empty)",
                rule="underwriting_platform.qc.checks_completeness.check_verdict_drivers",
                remediation=(
                    "Verdict carries no decision drivers — the "
                    "underwriter has no narrative basis for the "
                    "label."
                ),
            )
        ]
    return []


# ---------------------------------------------------------------------------
# G4 — Reconciler narrative substance
# ---------------------------------------------------------------------------


def check_reconciler_narrative(ctx: dict) -> list[QCFinding]:
    decision: dict = ctx["decision"]
    recon = decision.get("reconciliation") or {}
    if not recon:
        return []  # reconciler didn't run; legitimate
    notes = (recon.get("reconciler_notes") or "").strip()
    if recon and len(notes) < 200:
        return [
            QCFinding(
                code="RECONCILER_NARRATIVE_THIN",
                category="auditability",
                severity="INFO",
                location="reconciliation.reconciler_notes",
                snippet=f"{len(notes)} chars",
                rule="underwriting_platform.qc.checks_completeness.check_reconciler_narrative (>=200 chars)",
                remediation=(
                    "Reconciler notes are under 200 chars — the "
                    "reconciler may have errored or returned a "
                    "stub."
                ),
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

register_check("checks_completeness.placeholder", check_placeholder_leaks)
register_check("checks_completeness.html_entities", check_html_entity_leaks)
register_check("checks_completeness.python_literals", check_python_literals)
register_check("checks_completeness.truncations", check_truncations)
register_check("checks_completeness.escape_leaks", check_escape_leaks)
register_check("checks_completeness.calc_provenance", check_calc_provenance)


def check_decision_completion(ctx: dict) -> list[QCFinding]:
    completion = ctx["decision"].get("_completion")
    if completion is not None and not completion.get("decision_complete"):
        return [
            QCFinding(
                code="DECISION_INCOMPLETE",
                category="audit",
                severity="BLOCK",
                location="decision._completion",
                snippet="Required decision evidence/gates or mandatory stages are incomplete.",
                rule="underwriting_platform.application.completion.assess_decision",
                remediation="Resolve recorded completion issues; PDF quality alone cannot authorize delivery.",
            )
        ]
    return []


register_check("checks_completeness.decision_completion", check_decision_completion)
register_check("checks_completeness.flag_rule", check_flag_rule)
register_check("checks_completeness.verdict_drivers", check_verdict_drivers)
register_check("checks_completeness.reconciler", check_reconciler_narrative)
