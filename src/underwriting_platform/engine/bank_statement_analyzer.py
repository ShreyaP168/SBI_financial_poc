"""
engine/bank_statement_analyzer.py — Perfios-style structured analysis
over the income agent's `bank_transactions[]` extraction.

Datamatics LAYOUT Fin #3/#4/#5 (C304418178, C280127198, C278819094):
bank statement evidence must produce DETAILED CALCULATIONS (monthly
aggregates, recurring credits, EMI list, bounce list, counterparty
concentration) — not just a narrative "salary credits received" line.

Architecture: pure-Python deterministic layer on top of the LLM's
transaction extraction. The agent emits `bank_transactions[]`; this
module ingests that list and produces structured signals the engine +
renderer + QC can consume. No LLM cost on the analytical layer.

Outputs (consumed by renderer.pdf + qc.checks_coherence):
  monthly_summary[]    — credits/debits/avg-balance per (account, month)
  detected_emis[]      — recurring debit groups identified as loan EMIs
  bounces[]            — txns matching cheque-return / penalty keywords
  recurring_credits[]  — counterparty-grouped recurring inflows
  cheque_series_gaps   — issued cheque numbers with gaps (anti-fraud)
  counterparty_top     — top 5 credit counterparties + their share
  totals               — 12M credit, 12M debit, 12M bounce count + sum

The agent's `bank_salary_credits[]` stays separate (it's the agent's
*opinion* of which credits are salary); this module looks at ALL
transactions and emits its own *structured signals*. The income
calculator can use either or both.
"""

from __future__ import annotations
import logging
import re
from collections import defaultdict, Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pattern banks (deterministic categorisation)
# ---------------------------------------------------------------------------

# Description-keyword matches (case-insensitive). Higher specificity first.
_BOUNCE_PATTERNS = re.compile(
    r"\b(?:cheque return|chq return|return-micr|i/w chq return|"
    r"insufficient funds|ecs return|nach return|ach return|"
    r"bounce charge|bounce penal)\b",
    re.IGNORECASE,
)

_EMI_PATTERNS = re.compile(
    r"\b(?:emi|loan(?:[-_ ]repay|[-_ ]inst)?|home loan|auto loan|"
    r"car loan|personal loan|education loan|mortgage|"
    r"ach[-_ ]?(?:cr|debit|d)?[-_ ]?\w*loan)\b",
    re.IGNORECASE,
)

_SALARY_PATTERNS = re.compile(
    r"\b(?:salary|sal\.?|payroll|stipend|monthly compensation)\b",
    re.IGNORECASE,
)

_UTILITY_PATTERNS = re.compile(
    r"\b(?:electricity|water bill|gas bill|broadband|telecom|"
    r"bsnl|airtel|jio|vi[- ]?postpaid|mahanagar gas|adani gas|"
    r"bescom|mseb|tata power|reliance energy|bill[-_ ]pay)\b",
    re.IGNORECASE,
)

_CASH_PATTERNS = re.compile(
    r"\b(?:atm[-_ ]?withdrawal|cash[-_ ]withdraw|cash dep|"
    r"branch[-_ ]?cash|atm[-_ ]wdl)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


@dataclass
class MonthSummary:
    year_month: str  # "2024-09"
    credit_count: int = 0
    credit_total: int = 0
    debit_count: int = 0
    debit_total: int = 0
    avg_balance: float = 0.0
    closing_balance: float = 0.0
    bounce_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EMIRecord:
    counterparty: str
    avg_amount: int = 0
    count: int = 0
    months_seen: list[str] = field(default_factory=list)
    sample_desc: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BounceRecord:
    date: str
    description: str
    amount: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RecurringCreditRecord:
    counterparty: str
    avg_amount: int = 0
    count: int = 0
    months_seen: list[str] = field(default_factory=list)
    is_salary_like: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BankAnalysisResult:
    txn_count: int = 0
    months_covered: list[str] = field(default_factory=list)
    monthly_summary: list[MonthSummary] = field(default_factory=list)
    detected_emis: list[EMIRecord] = field(default_factory=list)
    bounces: list[BounceRecord] = field(default_factory=list)
    bounce_total: int = 0
    recurring_credits: list[RecurringCreditRecord] = field(default_factory=list)
    counterparty_top: list[dict] = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "txn_count": self.txn_count,
            "months_covered": self.months_covered,
            "monthly_summary": [m.to_dict() for m in self.monthly_summary],
            "detected_emis": [e.to_dict() for e in self.detected_emis],
            "bounces": [b.to_dict() for b in self.bounces],
            "bounce_total": self.bounce_total,
            "recurring_credits": [r.to_dict() for r in self.recurring_credits],
            "counterparty_top": self.counterparty_top,
            "totals": self.totals,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _yearmonth(d: datetime | None) -> str:
    return d.strftime("%Y-%m") if d else "—"


# Payment-channel / transfer-type tokens that are NOT counterparty names.
_TXN_PREFIXES = {
    "INF",
    "INFT",
    "NEFT",
    "RTGS",
    "IMPS",
    "UPI",
    "ACH",
    "CHQ",
    "CHEQUE",
    "CMS",
    "MMT",
    "ECS",
    "NACH",
    "BIL",
    "TPT",
    "ATOM",
    "POS",
    "ATM",
    "EBA",
    "MB",
    "IB",
    "FT",
    "CR",
    "DR",
    "BY",
    "TO",
    "REF",
    "TRF",
}
# Trailing company suffixes / truncation artifacts — stripped so that the same
# employer written as "WHATS ON INDIA MEDIA P", "...PRIVATE", "...PVT LTD" all
# collapse to one counterparty.
_CO_SUFFIX_RE = re.compile(
    r"\b(?:PRIVATE\s+LIMITED|PVT\.?\s*LTD\.?|PRIVATE|PVT|LIMITED|LTD|LLP|"
    r"AND|AN|A|P)\b\.?\s*$",
    re.I,
)
_SALARY_NAME_RE = re.compile(r"\bSAL(?:ARY)?\b", re.I)


def _counterparty(description: str) -> str:
    """Extract a clean counterparty name from an Indian bank narration.

    Splits on / and -, drops the transfer-type token (INF/INFT/NEFT/UPI/…),
    reference numbers, and VPAs, then takes the longest alphabetic segment as
    the party. Salary credits with no payer name collapse to "SALARY"; trailing
    company suffixes (PRIVATE/PVT LTD/…) and truncation tails are removed so the
    same employer merges into one counterparty.
    """
    if not description:
        return "—"
    cands = []
    for tok in re.split(r"[\/\-]", description):
        t = tok.strip()
        if not t or "@" in t:  # blank / VPA
            continue
        if t.upper() in _TXN_PREFIXES:  # transfer-type token
            continue
        digits = sum(c.isdigit() for c in t)
        letters = sum(c.isalpha() for c in t)
        if t.isdigit() or digits >= 6 or letters < 3:  # reference / junk
            continue
        cands.append(t)
    if not cands:
        # All-channel/reference narration (e.g. a CMS-routed EMI) — fall back
        # to the first token so the row keeps a label instead of "—".
        first = next((t.strip() for t in re.split(r"[\/\-]", description) if t.strip()), "")
        return first.upper()[:40] or "—"
    # The beneficiary/remitter name is conventionally the LAST meaningful
    # segment in NEFT/INF/IMPS narrations (after the channel + ref + purpose).
    best = cands[-1]
    name = re.sub(r"\s+", " ", best).strip().upper()
    if _SALARY_NAME_RE.search(name):  # "SALARY AUG 2025" → SALARY
        return "SALARY"
    prev = None
    while name and name != prev:  # strip stacked suffixes
        prev = name
        name = _CO_SUFFIX_RE.sub("", name).strip()
    return (name or best.upper())[:40]


def _amount(t: dict) -> int:
    a = t.get("amount")
    try:
        return int(float(a or 0))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def analyze(transactions: list[dict]) -> BankAnalysisResult:
    """Run Perfios-style analysis over the income agent's transaction
    extraction. Pure function — no I/O, no LLM.
    """
    res = BankAnalysisResult()
    if not transactions:
        res.notes.append("No transactions extracted by the income agent.")
        return res

    res.txn_count = len(transactions)

    # Per-month aggregates
    by_month: dict[str, MonthSummary] = {}
    last_balance_in_month: dict[str, float] = {}
    month_balances: dict[str, list[float]] = defaultdict(list)

    # Bounce list
    bounce_list: list[BounceRecord] = []

    # For EMI / recurring detection: bucket by (sign, counterparty)
    debit_buckets: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    credit_buckets: dict[str, list[tuple[str, int, str]]] = defaultdict(list)

    for t in transactions:
        amt = _amount(t)
        if amt == 0:
            continue
        desc = str(t.get("description") or "")
        date_obj = _parse_date(t.get("date"))
        ym = _yearmonth(date_obj)
        m = by_month.setdefault(ym, MonthSummary(year_month=ym))
        if amt > 0:
            m.credit_count += 1
            m.credit_total += amt
        else:
            m.debit_count += 1
            m.debit_total += -amt
        # Update closing/avg balance trackers
        bal = t.get("balance")
        try:
            bal_v = float(bal)
            m.closing_balance = bal_v
            last_balance_in_month[ym] = bal_v
            month_balances[ym].append(bal_v)
        except (TypeError, ValueError):
            pass

        # Bounce detection
        if _BOUNCE_PATTERNS.search(desc):
            m.bounce_count += 1
            bounce_list.append(
                BounceRecord(
                    date=t.get("date") or "—",
                    description=desc[:80],
                    amount=abs(amt),
                )
            )
            continue  # don't double-count as EMI/utility

        # Counterparty bucketing for recurrence detection
        cp = _counterparty(desc)
        if amt < 0:
            debit_buckets[cp].append((ym, -amt, desc))
        else:
            credit_buckets[cp].append((ym, amt, desc))

    res.monthly_summary = sorted(by_month.values(), key=lambda x: x.year_month)
    res.months_covered = [m.year_month for m in res.monthly_summary]
    res.bounces = bounce_list
    res.bounce_total = sum(b.amount for b in bounce_list)

    # EMI detection: a debit-counterparty is an EMI if
    #   - description matches EMI/loan keyword OR
    #   - amount is stable across ≥ 3 months (variance < 20% of mean)
    for cp, entries in debit_buckets.items():
        if len(entries) < 3:
            continue
        amounts = [e[1] for e in entries]
        mean = sum(amounts) / len(amounts)
        if mean <= 0:
            continue
        # Distinct months
        months = sorted({e[0] for e in entries})
        if len(months) < 3:
            continue
        # Description-keyword match short-circuits
        sample_desc = entries[0][2]
        kw_match = any(_EMI_PATTERNS.search(e[2]) for e in entries)
        # Variance stability
        var = sum((a - mean) ** 2 for a in amounts) / len(amounts)
        stable = (var**0.5) / max(mean, 1) < 0.20
        if kw_match or stable:
            res.detected_emis.append(
                EMIRecord(
                    counterparty=cp,
                    avg_amount=int(mean),
                    count=len(entries),
                    months_seen=months,
                    sample_desc=sample_desc[:80],
                )
            )

    # Recurring credits: similar logic. Tag salary-like when ≥ 3 months
    # cadence + amount stable + matches salary keyword OR avg amount in
    # the typical salary band.
    for cp, entries in credit_buckets.items():
        if len(entries) < 3:
            continue
        amounts = [e[1] for e in entries]
        mean = sum(amounts) / len(amounts)
        months = sorted({e[0] for e in entries})
        if len(months) < 3:
            continue
        var = sum((a - mean) ** 2 for a in amounts) / len(amounts)
        stable = (var**0.5) / max(mean, 1) < 0.20
        kw_match = any(_SALARY_PATTERNS.search(e[2]) for e in entries)
        if not (kw_match or stable):
            continue
        salary_like = kw_match or (10_000 <= mean <= 5_000_000)
        res.recurring_credits.append(
            RecurringCreditRecord(
                counterparty=cp,
                avg_amount=int(mean),
                count=len(entries),
                months_seen=months,
                is_salary_like=salary_like,
            )
        )

    # Counterparty top 5 by credit volume
    cp_totals = Counter()
    for cp, entries in credit_buckets.items():
        cp_totals[cp] = sum(e[1] for e in entries)
    grand_total = sum(cp_totals.values()) or 1
    res.counterparty_top = [
        {
            "counterparty": cp,
            "credit_total": amt,
            "share_pct": round(amt / grand_total * 100, 1),
        }
        for cp, amt in cp_totals.most_common(5)
    ]

    # Per-month average balance (mean of intra-month running balances)
    for m in res.monthly_summary:
        bals = month_balances.get(m.year_month) or []
        if bals:
            m.avg_balance = round(sum(bals) / len(bals), 2)

    # 12-month roll-ups + Perfios-style financial-health metrics
    months = max(1, len(res.months_covered))
    credit_total = sum(m.credit_total for m in res.monthly_summary)
    debit_total = sum(m.debit_total for m in res.monthly_summary)
    abb_vals = [m.avg_balance for m in res.monthly_summary if m.avg_balance]
    abb = (sum(abb_vals) / len(abb_vals)) if abb_vals else 0.0  # Average Bank Balance
    total_emi = sum(e.avg_amount for e in res.detected_emis)
    res.totals = {
        "credit_count": sum(m.credit_count for m in res.monthly_summary),
        "credit_total": credit_total,
        "debit_count": sum(m.debit_count for m in res.monthly_summary),
        "debit_total": debit_total,
        "bounce_count": len(bounce_list),
        "bounce_total": res.bounce_total,
        "months_with_data": len(res.months_covered),
        # Financial-health metrics (Perfios Summary parity)
        "avg_bank_balance": int(abb),
        "avg_monthly_credit": int(credit_total / months),
        "avg_monthly_debit": int(debit_total / months),
        "net_surplus": int(credit_total - debit_total),
        "savings_rate_pct": (
            round((credit_total - debit_total) / credit_total * 100, 1) if credit_total else 0.0
        ),
        "total_monthly_emi": int(total_emi),
        "emi_to_balance_pct": (round(total_emi / abb * 100, 1) if abb else None),
        "balance_covers_emi_x": (round(abb / total_emi, 1) if total_emi else None),
        "closing_balance_latest": (
            int(res.monthly_summary[-1].closing_balance) if res.monthly_summary else 0
        ),
    }
    if res.totals["bounce_count"] >= 3:
        res.notes.append(
            f"BOUNCE_RATE_HIGH: {res.totals['bounce_count']} bounces over "
            f"{res.totals['months_with_data']} months — refer to UW."
        )
    return res


def attach_to_case(case_json: dict) -> dict | None:
    """Run analyze() against case_json.income.bank_transactions and
    attach the result at case_json['bank_analysis']. Returns the
    BankAnalysisResult dict or None if no transactions were extracted.
    Idempotent — safe to call multiple times.
    """
    income = case_json.get("income") or {}
    txns = income.get("bank_transactions") or []
    if not txns:
        return None
    result = analyze(txns)
    case_json["bank_analysis"] = result.to_dict()
    return result.to_dict()
