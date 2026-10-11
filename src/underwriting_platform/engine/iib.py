"""
engine/iib.py — IIB (Insurance Information Bureau) / prior-policy DB join.

Datamatics LAYOUT row 31 requires an "IIB findings" section listing
*Details of All existing Inforce, Postpone, Decline, Void Policies and
policies applied simultaneously but pending for Underwriting*.

Today TALIC's pipeline has NO IIB feed — that's the
[[project-iib-history-gap]] memory note. This module is a SCHEMA STUB
+ LOADER that wires the contract end-to-end so the day the feed lands,
populating the JSON file is the only change needed.

Path resolution:
  1. Explicit `path=` argument to load_iib_data
  2. `IIB_DATA_JSON` environment variable
  3. None — returns empty index; orchestrator skips the join

Per-case lookup keyed by PAN (preferred — unique cross-insurer) or
PLCY_NUM (TALIC-only). Multi-policy results returned as a list.

Schema for `config/iib_sample.json`:
{
  "_comment": "...",
  "_source": "IIB / Insurance Information Bureau export YYYY-MM-DD",
  "policies": [
    {
      "pan":            "ABCDE1234F",
      "insured_name":   "Full Name",
      "insurer":        "ICICI Pru / HDFC Life / etc.",
      "policy_number":  "...",
      "plan_type":      "Term / ULIP / Endowment",
      "sum_assured":    50000000,
      "status":         "Inforce | Postpone | Decline | Void | Pending UW",
      "date_of_issue":  "2024-03-15",
      "premium_paying": true,
      "rated_up":       false
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


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class IIBPolicyRecord:
    pan: str = ""
    insured_name: str = ""
    insurer: str = ""
    policy_number: str = ""
    plan_type: str = ""
    sum_assured: int = 0
    status: str = ""
    date_of_issue: str = ""
    premium_paying: bool = False
    rated_up: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class IIBLookupResult:
    matched_by: str = ""  # "pan" | "policy_number" | "none"
    policies: list[IIBPolicyRecord] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "matched_by": self.matched_by,
            "policies": [p.to_dict() for p in self.policies],
            "summary": self.summary,
        }

    @property
    def has_data(self) -> bool:
        return bool(self.policies)


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------

_INDEX_BY_PAN: dict[str, list[IIBPolicyRecord]] | None = None
_INDEX_BY_POLICY: dict[str, IIBPolicyRecord] | None = None
_LOADED_PATH: Path | None = None


def _resolve_path(path: Optional[str | Path]) -> Optional[Path]:
    if path:
        p = Path(path)
        return p if p.exists() else None
    env_p = os.environ.get("IIB_DATA_JSON")
    if env_p:
        p = Path(env_p)
        return p if p.exists() else None
    return None


def load_iib_data(path: Optional[str | Path] = None) -> tuple[dict, dict]:
    """Load + index the IIB JSON. Returns (by_pan, by_policy)."""
    global _INDEX_BY_PAN, _INDEX_BY_POLICY, _LOADED_PATH
    resolved = _resolve_path(path)
    if resolved is None:
        logger.info("[iib] no path resolved — IIB lookup disabled (project-iib-history-gap)")
        _INDEX_BY_PAN, _INDEX_BY_POLICY = {}, {}
        _LOADED_PATH = None
        return _INDEX_BY_PAN, _INDEX_BY_POLICY
    revision = source_revision(resolved)
    if (
        _INDEX_BY_PAN is not None
        and _INDEX_BY_POLICY is not None
        and _LOADED_PATH == resolved
        and _loaded_revisions.get("_LOADED_PATH") == revision
    ):
        return _INDEX_BY_PAN, _INDEX_BY_POLICY

    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[iib] failed to load %s: %s", resolved, e)
        _INDEX_BY_PAN, _INDEX_BY_POLICY = {}, {}
        _LOADED_PATH = None
        return _INDEX_BY_PAN, _INDEX_BY_POLICY

    by_pan: dict[str, list[IIBPolicyRecord]] = defaultdict(list)
    by_policy: dict[str, IIBPolicyRecord] = {}

    for entry in data.get("policies") or []:
        rec = IIBPolicyRecord(
            pan=str(entry.get("pan") or "").upper().strip(),
            insured_name=str(entry.get("insured_name") or "").strip(),
            insurer=str(entry.get("insurer") or "").strip(),
            policy_number=str(entry.get("policy_number") or "").strip(),
            plan_type=str(entry.get("plan_type") or "").strip(),
            sum_assured=int(entry.get("sum_assured") or 0),
            status=str(entry.get("status") or "").strip(),
            date_of_issue=str(entry.get("date_of_issue") or "").strip(),
            premium_paying=bool(entry.get("premium_paying")),
            rated_up=bool(entry.get("rated_up")),
        )
        if rec.pan:
            by_pan[rec.pan].append(rec)
        if rec.policy_number:
            by_policy[rec.policy_number] = rec

    _INDEX_BY_PAN = dict(by_pan)
    _INDEX_BY_POLICY = by_policy
    _LOADED_PATH = resolved
    _loaded_revisions["_LOADED_PATH"] = revision
    logger.info(
        "[iib] indexed %d unique PANs / %d policies from %s",
        len(_INDEX_BY_PAN),
        len(_INDEX_BY_POLICY),
        resolved.name,
    )
    return _INDEX_BY_PAN, _INDEX_BY_POLICY


def lookup_iib(
    pan: str | None = None, policy_number: str | None = None, path: Optional[str | Path] = None
) -> IIBLookupResult:
    """Return IIB findings for an applicant.

    Match priority: PAN (cross-insurer, unique) > policy_number.
    Summary aggregates: count by status, total sum_assured across all
    policies, count of decline / postpone / void history.
    """
    by_pan, by_policy = load_iib_data(path)
    result = IIBLookupResult()

    policies: list[IIBPolicyRecord] = []
    if pan:
        p = str(pan).upper().strip()
        if p in by_pan:
            policies = by_pan[p]
            result.matched_by = "pan"
    if not policies and policy_number:
        pn = str(policy_number).strip()
        if pn in by_policy:
            policies = [by_policy[pn]]
            result.matched_by = "policy_number"
    if not policies:
        result.matched_by = "none"
        result.summary = {
            "total_policies": 0,
            "total_sum_assured": 0,
            "by_status": {},
            "data_available": bool(by_pan or by_policy),
        }
        return result

    result.policies = policies
    by_status: dict[str, int] = defaultdict(int)
    total_sa = 0
    for p in policies:
        by_status[p.status or "Unknown"] += 1
        total_sa += p.sum_assured
    result.summary = {
        "total_policies": len(policies),
        "total_sum_assured": total_sa,
        "by_status": dict(by_status),
        "decline_count": by_status.get("Decline", 0),
        "postpone_count": by_status.get("Postpone", 0),
        "void_count": by_status.get("Void", 0),
        "inforce_count": by_status.get("Inforce", 0),
        "data_available": True,
    }
    return result
