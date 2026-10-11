"""Local dated salary/ITR-1 parsers; complex or ambiguous financials stay unresolved."""

import re
from decimal import Decimal


def amount(raw):
    match = re.fullmatch(r"\s*([\d,]+(?:\.\d+)?)\s*(lakh|lakhs|crore|crores)?\s*", raw, re.I)
    if not match:
        raise ValueError("Invalid amount")
    value = Decimal(match[1].replace(",", "")) * (
        100000
        if (match[2] or "").lower().startswith("lakh")
        else 10000000
        if (match[2] or "").lower().startswith("crore")
        else 1
    )
    if value != value.to_integral_value() or value < 0:
        raise ValueError("Invalid integral amount")
    return int(value)


def financial_facts(documents, page_reader):
    slips, years, citations, findings = {}, {}, {}, []
    for document in documents:
        for page, text in page_reader(document):

            def source(raw, filename=document.filename, number=page):
                return {
                    "document": filename,
                    "page": number,
                    "raw_value": raw,
                    "parser_version": "financial-fields-1.0",
                }

            if re.search(r"pay\s*slip|salary\s*slip|pay statement", text, re.I):
                month = re.search(
                    r"(?:Pay Period|Salary Month|Month Year)\s*[:\-]\s*(\d{4}-\d{2}|[A-Za-z]+\s+\d{4})",
                    text,
                    re.I,
                )
                gross = re.search(
                    r"(?:Gross Salary|Gross Earnings|Total Earnings|Gross Pay)\s*[:\-]\s*(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d+)?)\b",
                    text,
                    re.I,
                )
                if month and gross:
                    key = month[1].strip().lower()
                    value = amount(gross[1])
                    if key in slips and slips[key][0]["gross_salary"] != value:
                        findings.append(
                            {
                                "field": "income.salary_slips",
                                "code": "CONFLICTING_SALARY_PERIOD",
                                "retryable": False,
                            }
                        )
                    else:
                        slips[key] = (
                            {"month_year": month[1], "gross_salary": value},
                            source(gross[0]),
                        )
            # ITR-1 excludes business/profession; do not infer earned income from GTI.
            if re.search(r"\bITR[- ]?1\b", text, re.I) and re.search(
                r"income tax return", text, re.I
            ):
                year = re.search(r"Assessment Year\s*[:\-]\s*(\d{4}-\d{2,4})", text, re.I)
                salary = re.search(
                    r"Income from Salary\s*[:\-]\s*(?:Rs\.?|INR|₹)?\s*([\d,]+)\b", text, re.I
                )
                gti = re.search(
                    r"Gross Total Income\s*[:\-]\s*(?:Rs\.?|INR|₹)?\s*([\d,]+)\b", text, re.I
                )
                if year and salary and gti and amount(salary[1]) <= amount(gti[1]):
                    row = {
                        "assessment_year": year[1],
                        "earned_total": amount(salary[1]),
                        "gross_total_income": amount(gti[1]),
                    }
                    if year[1] in years and years[year[1]][0] != row:
                        findings.append(
                            {
                                "field": "income.itr_years",
                                "code": "CONFLICTING_ASSESSMENT_YEAR",
                                "retryable": False,
                            }
                        )
                    else:
                        years[year[1]] = (row, source(salary[0] + "; " + gti[0]))
    income = {}
    if years and not any(f["code"] == "CONFLICTING_ASSESSMENT_YEAR" for f in findings):
        income = {
            "itr_years": [years[key][0] for key in sorted(years)],
            "primary_source": "itr",
            "annual_income": years[max(years)][0]["earned_total"],
        }
        citations["income.itr_years"] = [row[1] for row in years.values()]
        citations["income.annual_income"] = [years[max(years)][1]]
    elif slips and not any(f["code"] == "CONFLICTING_SALARY_PERIOD" for f in findings):
        from underwriting_platform.engine.calculator import _salary_slip_mean_annual

        rows = [row[0] for row in slips.values()]
        computed = _salary_slip_mean_annual(rows)
        if computed:
            income = {
                "salary_slips": rows,
                "primary_source": "salary_slip",
                "annual_income": computed[0],
            }
            citations["income.salary_slips"] = [row[1] for row in slips.values()]
            citations["income.annual_income"] = citations["income.salary_slips"]
    return income, citations, findings
