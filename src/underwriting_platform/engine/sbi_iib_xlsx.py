"""
engine/sbi_iib_xlsx.py — read a structured IIB export workbook directly
(no vision/LLM extraction needed).

A later batch's IIB data arrived as a proper table (one row per prior
policy, columns PROP_POL_NO / DB_NO / STATUS / SUM_ASSURED / COMPANY_NAME
/ PRODUCT_TYPE / ...) rather than the screenshot-pair-per-case docx that
agents/bureau.py's vision pipeline was built for. This is already-
structured tabular data — same deterministic-join pattern as
engine/policy_data.py and engine/scores_xlsx.py, no LLM call needed.

Kept as a separate, additive loader (env var SBI_IIB_XLSX) rather than
touching agents/bureau.py — orchestrator/master.py tries both sources and
merges, so cases covered by the original docx-screenshot batch are
unaffected.

Path resolution:
  1. Explicit `path=` argument.
  2. `SBI_IIB_XLSX` environment variable.
  3. None — lookup() returns None for every case_id; the join no-ops.
"""

from __future__ import annotations
import logging
import os
from pathlib import Path
from typing import Optional

from .reference_cache import source_revision

_loaded_revisions = {}
logger = logging.getLogger(__name__)

_INDEX: Optional[dict[str, list[dict]]] = None
_LOADED_PATH: Optional[Path] = None


# sbi_smart_shield_rules.json's iib_gate disallow lists (client-sourced,
# from "Explanation on IIB and CIBIL Score rules.msg") use "Pending for
# Underwriting" — this export's own status string for the same thing is
# the abbreviated "Proposal received/Pending for U/w", which the gate's
# substring matcher (engine.sbi_financial._match_any) won't recognise as
# the same thing. Expanding the abbreviation here (data layer) rather
# than touching the shared matcher keeps every other case's gating
# untouched.
def _normalize_status(status) -> Optional[str]:
    s = str(status).strip() if status not in (None, "") else ""
    if not s or s == "-":
        return None
    sl = s.lower()
    if "pending for u/w" in sl or "pending for uw" in sl:
        return "Proposal Pending for Underwriting"
    return s


def _clean(v):
    if v in (None, "", "-"):
        return None
    return v


def _safe_int(v):
    try:
        if v in (None, "", "-"):
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _fmt_date(v) -> Optional[str]:
    if v in (None, "", "-"):
        return None
    try:
        return v.date().isoformat() if hasattr(v, "date") else v.isoformat()
    except Exception:
        return str(v)


def _resolve_path(path: Optional[str | Path] = None) -> Optional[Path]:
    if path:
        p = Path(path)
        return p if p.exists() else None
    env_p = os.environ.get("SBI_IIB_XLSX")
    if env_p:
        p = Path(env_p)
        return p if p.exists() else None
    return None


def load_iib_xlsx(path: Optional[str | Path] = None) -> dict[str, list[dict]]:
    global _INDEX, _LOADED_PATH
    resolved = _resolve_path(path)
    if resolved is None:
        if _INDEX is None:
            logger.info("[sbi_iib_xlsx] no path resolved — SBI_IIB_XLSX join disabled")
        _INDEX = {}
        _LOADED_PATH = None
        return _INDEX
    revision = source_revision(resolved)
    if (
        _INDEX is not None
        and _LOADED_PATH == resolved
        and _loaded_revisions.get("_LOADED_PATH") == revision
    ):
        return _INDEX

    try:
        import openpyxl

        wb = openpyxl.load_workbook(resolved, data_only=True)
    except Exception as e:
        logger.warning("[sbi_iib_xlsx] failed to load %s: %s", resolved, e)
        _INDEX, _LOADED_PATH = {}, None
        return _INDEX

    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    try:
        header = [str(h).strip() if h is not None else "" for h in next(rows)]
    except StopIteration:
        _INDEX, _LOADED_PATH = {}, resolved
        return _INDEX
    col = {name: i for i, name in enumerate(header)}

    def get(row, name):
        i = col.get(name)
        return row[i] if i is not None and i < len(row) else None

    index: dict[str, list[dict]] = {}
    for row in rows:
        if not row or not row[0]:
            continue
        case_id = str(get(row, "PROP_POL_NO") or "").strip()
        if not case_id:
            continue
        raw_status = get(row, "STATUS")
        status = _normalize_status(raw_status)
        insurer = _clean(get(row, "COMPANY_NAME"))
        sa = _safe_int(get(row, "SUM_ASSURED"))
        product_type = _clean(get(row, "PRODUCT_TYPE"))
        dop = _fmt_date(get(row, "DP_DOC"))
        reasons = [
            r
            for r in (
                get(row, "RSN_FOR_DCLN"),
                get(row, "RSN_FOR_PSTPND"),
                get(row, "RSN_FOR_REPDTN"),
            )
            if _clean(r)
        ]
        note_bits = [
            f"IIB export row, matched via {get(row, 'MATCHING_PARAMETER') or 'unknown'}.",
            f"DOP {dop}." if dop else "",
            f"Last updated {_fmt_date(get(row, 'RCRD_LST_UPDTD'))}."
            if get(row, "RCRD_LST_UPDTD")
            else "",
        ]
        if raw_status and status and str(raw_status).strip() != status:
            note_bits.append(f"Source status text: '{raw_status}'.")
        if reasons:
            note_bits.append("Reason(s): " + "; ".join(str(r) for r in reasons) + ".")
        note = " ".join(b for b in note_bits if b)

        # entity_caution_status / intermediary_caution_status / is_negative_match:
        # raw IIB export columns (ENTITY_CAUTION_STS, INTERMEDIARY_CAUTION_STS,
        # IS_NGTV_MTCH) captured here for completeness, but deliberately NOT
        # evaluated by any gating rule yet — none of the 3 core SBI reference
        # documents (Smart Shield Plus V02.docx, the IIB/CIBIL rules email,
        # Protection Plans & Surrogate Income guidelines.docx) attach any
        # rule/threshold to these fields; they're raw bureau data with no
        # documented meaning for this product. Captured now (2026-10-08,
        # rule audit) so a rule can be written the moment SBI clarifies what
        # a caution status or negative match should trigger — capturing the
        # field is a data-completeness fix, not a decision yet. Zero effect
        # on any existing case: nothing currently reads these keys.
        policy = {
            "policy_or_wf_no": str(_clean(get(row, "DB_NO")) or ""),
            "insurer": insurer or "Company name is masked/not found",
            "status": status,
            "sum_assured_inr": sa,
            "product_type": product_type,
            # Adverse-insurer-decision detection (engine.sbi_financial,
            # engine.requirements) matches on both status and uw_decision;
            # this export has no separate decision column, so reuse the
            # (normalized) status — substring matching makes this safe.
            "uw_decision": status,
            "uw_decision_date": dop,
            "cancellation_sub_status": None,
            "entity_caution_status": _clean(get(row, "ENTITY_CAUTION_STS")),
            "intermediary_caution_status": _clean(get(row, "INTERMEDIARY_CAUTION_STS")),
            # Stored as the raw export value, NOT pre-interpreted as a
            # boolean "flag" — a quick look at sample export rows showed
            # 'Y' even on what otherwise reads as a routine record, so
            # whether 'Y' here actually means "negative match" (vs. e.g.
            # "yes, this record matched the search criteria") is unclear
            # without SBI confirming the column's intended semantics.
            "is_negative_match_raw": _clean(get(row, "IS_NGTV_MTCH")),
            "note": note,
        }
        index.setdefault(case_id, []).append(policy)

    _INDEX = index
    _LOADED_PATH = resolved
    _loaded_revisions["_LOADED_PATH"] = revision
    logger.info(
        "[sbi_iib_xlsx] indexed %d case(s), %d polic(y/ies) from %s",
        len(_INDEX),
        sum(len(v) for v in _INDEX.values()),
        resolved.name,
    )
    return _INDEX


def lookup_iib_xlsx(case_id: str, path: Optional[str | Path] = None) -> Optional[list[dict]]:
    """Return the list of prior-policy records for `case_id`, or None if
    the case isn't in this workbook at all (distinct from an empty list,
    which would mean the case IS in the workbook with zero matches)."""
    idx = load_iib_xlsx(path)
    return idx.get(str(case_id).strip())
