"""Recurring income credits from bank-statement tables, grouped by narration stem."""

import re
from collections import defaultdict
from datetime import datetime

from .financial import amount
from .tables import table_rows

INCOME_NARRATION = re.compile(
    r"salary|\bsal\b|payroll|wages|pension|\bpens\b|annuity|stipend|remuneration", re.I
)
TRANSFER_NARRATION = re.compile(r"\b(?:upi|imps|neft_?out|rtgs|atm|cash|self)\b", re.I)
DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d-%b-%Y")
MINIMUM_CREDITS = 3


def _squash(value):
    return re.sub(r"[^a-z]", "", str(value).lower())


def _columns(header):
    names = [_squash(cell) for cell in header]

    def find(choices):
        return next((i for i, name in enumerate(names) if name in choices), None)

    return (
        find({"date", "transactiondate", "postingdate", "txndate", "valuedate"}),
        find({"credit", "creditamount", "deposit", "deposits", "depositamt", "cr"}),
        find({"description", "narration", "particulars", "remarks", "transactiondetails"}),
        len(header),
    )


def _date(raw):
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(str(raw).strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _stem(narration):
    first = " ".join(narration.upper().split())
    parts = [part for part in re.split(r"[/\\]", first) if part][:3]
    return "/".join(re.sub(r"[^A-Z ]", "", part).strip() for part in parts[:2] if part).strip("/")


def _rows(documents):
    for document in documents:
        mapping = None
        for table in document.tables:
            rows = table_rows(table)
            if not rows:
                continue
            columns = _columns(rows[0])
            body = rows[1:]
            if None in columns[:3]:
                if mapping is None or len(rows[0]) != mapping[3]:
                    continue
                columns, body = mapping, rows
            else:
                mapping = columns
            date, credit, description, width = columns
            for row in body:
                if len(row) != width:
                    continue
                narration = " ".join(str(row[description] or "").split())
                try:
                    value = amount(str(row[credit]))
                except ValueError:
                    continue
                yield document, table["page"], row, _date(row[date]), narration, value


def recurring_credits(documents):
    streams = defaultdict(list)
    for document, page, row, day, narration, value in _rows(documents):
        stem = _stem(narration)
        if day and value > 0 and stem:
            streams[stem].append((document, page, row, day, narration, value))
    chosen = {
        stem: items
        for stem, items in streams.items()
        if len({(i[3], i[4], i[5]) for i in items}) >= MINIMUM_CREDITS
        and INCOME_NARRATION.search(stem)
        and not re.search(r"loan|disbursement|advance|refund|reversal", stem, re.I)
    }
    if not chosen:
        return [], []
    stem = max(chosen, key=lambda key: len(chosen[key]))
    credits, sources, seen = [], [], set()
    for document, page, row, day, narration, value in (i for i in chosen[stem]):
        key = (day, narration, value)
        if key in seen:
            continue
        seen.add(key)
        credits.append({"date": day, "amount": value, "narration": narration, "employer": stem})
        sources.append(
            {
                "document": document.filename,
                "page": page,
                "raw_value": " | ".join(str(cell) for cell in row),
                "parser_version": "bank-recurring-1.0",
            }
        )
    return credits, sources
