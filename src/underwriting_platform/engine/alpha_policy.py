"""
engine/alpha_policy.py — Alpha Policy join (TALIC-only prior-policy ledger).

Datamatics LAYOUT rows 20, 36, 53 require an "Alpha Policy" section
listing TALIC's prior policies issued to the same applicant —
distinct from IIB (which is cross-insurer). Used to show: "Details of
Alpha Policy / Other insurance — Accepted Std / Rate Up — Reason".

Today TALIC does not expose an Alpha Policy ledger to our pipeline.
This module is a SCHEMA STUB + LOADER analogous to engine/iib.py — the
contract is wired end-to-end so the day the ledger lands, populating
the JSON file is the only change needed.

Source candidates (one-of):
  1. Explicit path argument
  2. `ALPHA_POLICY_JSON` env var
  3. Inferred from Policy_Data.xlsx HUSBAND_HOUSEWIFE_PLCY_NUM (legacy
     linked-policy field — coarse signal only)
  4. None — render placeholder

Schema for the JSON file (one entry per TALIC prior policy):
{
  "_comment": "TALIC prior-policy ledger — populate when feed lands.",
  "alpha_policies": [
    {
      "pan":              "ABCDE1234F",
      "policy_number":    "C123456789",
      "plan_name":        "Term Plus / Maha Raksha / etc.",
      "issue_date":       "2022-04-15",
      "sum_assured":      10000000,
      "underwriting_outcome": "Std / Rate Up + 25 EMR / Postpone / Decline",
      "rate_up_reason":   "BMI 31 / hypertension / diabetes / etc.",
      "status":           "Inforce | Lapsed | Surrendered | Matured"
    }
  ]
}
"""

from __future__ import annotations
import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from .reference_cache import source_revision

_loaded_revisions = {}
logger = logging.getLogger(__name__)


@dataclass
class AlphaPolicyRecord:
    pan: str = ""
    policy_number: str = ""
    plan_name: str = ""
    issue_date: str = ""
    sum_assured: int = 0
    underwriting_outcome: str = ""
    rate_up_reason: str = ""
    status: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AlphaPolicyLookupResult:
    matched_by: str = ""
    policies: list[AlphaPolicyRecord] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    data_available: bool = False

    def to_dict(self) -> dict:
        return {
            "matched_by": self.matched_by,
            "policies": [p.to_dict() for p in self.policies],
            "summary": self.summary,
            "data_available": self.data_available,
        }

    @property
    def has_data(self) -> bool:
        return bool(self.policies)


_INDEX_BY_PAN: dict[str, list[AlphaPolicyRecord]] | None = None
_LOADED_PATH: Path | None = None


def _resolve_path(path: Optional[str | Path]) -> Optional[Path]:
    if path:
        p = Path(path)
        return p if p.exists() else None
    env_p = os.environ.get("ALPHA_POLICY_JSON")
    if env_p:
        p = Path(env_p)
        return p if p.exists() else None
    return None


def load_alpha_data(path: Optional[str | Path] = None) -> dict[str, list[AlphaPolicyRecord]]:
    global _INDEX_BY_PAN, _LOADED_PATH
    resolved = _resolve_path(path)
    if resolved is None:
        logger.info("[alpha_policy] no path resolved — lookup disabled")
        _INDEX_BY_PAN = {}
        _LOADED_PATH = None
        return _INDEX_BY_PAN
    revision = source_revision(resolved)
    if (
        _INDEX_BY_PAN is not None
        and _LOADED_PATH == resolved
        and _loaded_revisions.get("_LOADED_PATH") == revision
    ):
        return _INDEX_BY_PAN

    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[alpha_policy] failed to load %s: %s", resolved, e)
        _INDEX_BY_PAN = {}
        _LOADED_PATH = None
        return _INDEX_BY_PAN

    by_pan: dict[str, list[AlphaPolicyRecord]] = defaultdict(list)
    for entry in data.get("alpha_policies") or []:
        rec = AlphaPolicyRecord(
            pan=str(entry.get("pan") or "").upper().strip(),
            policy_number=str(entry.get("policy_number") or "").strip(),
            plan_name=str(entry.get("plan_name") or "").strip(),
            issue_date=str(entry.get("issue_date") or "").strip(),
            sum_assured=int(entry.get("sum_assured") or 0),
            underwriting_outcome=str(entry.get("underwriting_outcome") or "").strip(),
            rate_up_reason=str(entry.get("rate_up_reason") or "").strip(),
            status=str(entry.get("status") or "").strip(),
        )
        if rec.pan:
            by_pan[rec.pan].append(rec)
    _INDEX_BY_PAN = dict(by_pan)
    _LOADED_PATH = resolved
    _loaded_revisions["_LOADED_PATH"] = revision
    logger.info("[alpha_policy] indexed %d unique PANs from %s", len(_INDEX_BY_PAN), resolved.name)
    return _INDEX_BY_PAN


def lookup_alpha(
    pan: str | None = None, hhw_plcy_num: str | None = None, path: Optional[str | Path] = None
) -> AlphaPolicyLookupResult:
    """Return Alpha Policy ledger entries for an applicant.

    Match priority: PAN (preferred) > HUSBAND_HOUSEWIFE_PLCY_NUM
    fallback (signals a linked TALIC policy exists even when the
    Alpha ledger isn't on file).
    """
    by_pan = load_alpha_data(path)
    result = AlphaPolicyLookupResult()
    result.data_available = bool(by_pan)

    if pan:
        p = pan.upper().strip()
        if p in by_pan:
            result.policies = by_pan[p]
            result.matched_by = "pan"

    if not result.policies and hhw_plcy_num:
        # No alpha ledger entry, but the legacy HHW field suggests a
        # linked TALIC policy. Synthesise a single-row marker so the
        # underwriter sees the signal.
        result.policies = [
            AlphaPolicyRecord(
                policy_number=str(hhw_plcy_num),
                plan_name="Linked policy (per Policy_Data HHW field)",
                status="Inferred — Alpha ledger not on file for this PAN",
            )
        ]
        result.matched_by = "hhw_link"

    if result.policies:
        total_sa = sum(p.sum_assured for p in result.policies)
        outcomes = defaultdict(int)
        for p in result.policies:
            outcomes[p.underwriting_outcome or "Unknown"] += 1
        result.summary = {
            "total_policies": len(result.policies),
            "total_sum_assured": total_sa,
            "by_outcome": dict(outcomes),
        }
    else:
        result.matched_by = "none"
        result.summary = {
            "total_policies": 0,
            "total_sum_assured": 0,
            "by_outcome": {},
        }
    return result
