"""Preserve local table cells; accept salary credits only from explicit credit columns."""

import re
from datetime import datetime
from html.parser import HTMLParser

from .financial import amount


class TableHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], [], None
        self.merged = False

    def handle_starttag(self, tag, attrs):
        if tag in {"td", "th"}:
            self.cell = ""
            self.merged |= any(
                k in {"rowspan", "colspan"} and v not in {None, "1"} for k, v in attrs
            )

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append(self.cell.strip())
            self.cell = None
        elif tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = []


def table_rows(table):
    if table.get("rows"):
        return table["rows"]
    parser = TableHTML()
    parser.feed(table.get("html", ""))
    return [] if parser.merged else parser.rows


def bank_salary_credits(documents):
    credits, sources, seen = [], [], set()
    for document in documents:
        for table in document.tables:
            rows = table_rows(table)
            if not rows:
                continue
            headers = [re.sub(r"\s", "", str(value).lower()) for value in rows[0]]

            def column(choices, names=headers):
                return next((index for index, name in enumerate(names) if name in choices), None)

            date = column({"date", "transactiondate", "postingdate"})
            credit = column({"credit", "creditamount", "deposit", "deposits"})
            description = column({"description", "narration", "particulars"})
            if None in (date, credit, description):
                continue
            for row in rows[1:]:
                if len(row) != len(headers):
                    continue
                narration = str(row[description] or "")
                if not re.search(r"\bsalary\b", narration, re.I) or re.search(
                    r"loan|disbursement|advance", narration, re.I
                ):
                    continue
                try:
                    raw_date = str(row[date])
                    parsed = next(
                        (
                            datetime.strptime(raw_date, fmt).date().isoformat()
                            for fmt in ("%d/%m/%Y", "%Y-%m-%d")
                            if _date_valid(raw_date, fmt)
                        ),
                        None,
                    )
                    value = amount(str(row[credit]))
                except ValueError:
                    continue
                key = (parsed, narration, value)
                if not parsed or value <= 0 or key in seen:
                    continue
                seen.add(key)
                credits.append({"date": parsed, "amount": value, "narration": narration})
                sources.append(
                    {
                        "document": document.filename,
                        "page": table["page"],
                        "raw_value": " | ".join(map(str, row)),
                        "parser_version": "bank-table-1.0",
                    }
                )
    return credits, sources


def _date_valid(value, fmt):
    try:
        datetime.strptime(value, fmt)
        return True
    except ValueError:
        return False
