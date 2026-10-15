"""Local dated salary/ITR-1 parsers; complex or ambiguous financials stay unresolved."""

import re
from decimal import Decimal

from .documents import itr_computation


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


def _signed(raw):
    value = int(raw.replace("-", "").replace(",", ""))
    return -value if raw.startswith("-") else value


def _head(flat, name):
    match = re.search(
        r"income chargeable under the head\s*[\"“”']?\s*" + name + r"[\"“”']?\s*(-?[\d,]+)",
        flat,
        re.I,
    )
    return (_signed(match[1]), match[0]) if match else (0, None)


def assessee_year(pages):
    flat = " ".join(" ".join(text.split()) for _, text in pages)
    year = re.search(r"Assessment Year\s+(\d{4}-\d{4})", flat)
    if not year or "Computation of Total Income" not in flat:
        return None
    total = re.search(r"Gross Total Income\s+(-?\d[\d,]{3,})", flat)
    business = re.search(r"Income from Business or Profession[^\d-]*(-?\d[\d,]*)", flat)
    salary = re.search(r"Income from Salar(?:y|ies)[^\d-]*(-?\d[\d,]*)", flat)
    if not total or not (business or salary):
        return None
    gross = _signed(total[1])
    business_head = max(_signed(business[1]), 0) if business else 0
    salary_head = max(_signed(salary[1]), 0) if salary else 0
    earned = business_head + salary_head
    row = {
        "assessment_year": year[1],
        "gross_total_income": gross,
        "salary_head": salary_head,
        "business_head": business_head,
        "other_head": max(gross - earned, 0),
        "earned_total": earned,
        "name_on_document": itr_computation(flat).get("applicant.applicant_name"),
    }
    raw = "; ".join(m[0] for m in (salary, business, total) if m)
    return row, raw, pages[0][0]


def itr_form_year(pages):
    flat = " ".join(" ".join(text.split()) for _, text in pages)
    if not re.search(r"PART A\s+GENERAL INFORMATION", flat):
        return None
    year = re.search(r"\b(20\d{2}-\d{2})\b", flat)
    business = re.search(r"Income from Business\s*(?:&|and)\s*Profession\s*B1\s*(-?\d[\d,]*)", flat)
    salary = re.search(r"Salaries['‘’]?\s*\(iii - iv\)\s*B2\s*(-?\d[\d,]*)", flat)
    if not year or not (business or salary):
        return None
    business_head = max(_signed(business[1]), 0) if business else 0
    salary_head = max(_signed(salary[1]), 0) if salary else 0
    total = re.search(r"\bB5\s*B5\s*(-?\d[\d,]*)", flat)
    others = [
        re.search(pattern, flat)
        for pattern in (
            r"\bB3\s+(-?\d[\d,]*)\s+B4",
            r"Income from Other Sources\s*B4\s*(-?\d[\d,]*)",
        )
    ]
    gross = (
        _signed(total[1])
        if total
        else business_head + salary_head + sum(max(_signed(m[1]), 0) for m in others if m)
    )
    earned = business_head + salary_head
    row = {
        "assessment_year": year[1],
        "gross_total_income": gross,
        "salary_head": salary_head,
        "business_head": business_head,
        "other_head": max(gross - earned, 0),
        "earned_total": earned,
        "name_on_document": None,
    }
    raw = "; ".join(m[0] for m in (business, salary, total) if m)
    return row, raw, pages[0][0]


def computation_year(pages):
    flat = " ".join(" ".join(text.split()) for _, text in pages)
    if re.search(r"PART A\s+GENERAL INFORMATION", flat):
        return itr_form_year(pages)
    if "Name of Assessee" in flat:
        return assessee_year(pages)
    year = re.search(r"A\.\s?Y\.\s*(\d{4}-\d{2,4})", flat)
    if not year or not re.search(r"previous year", flat, re.I) or "statement of income" not in flat.lower():
        return None
    total = re.search(r"Gross Total Income\s+(-?\d[\d,]{3,})", flat, re.I) or re.search(
        r"Total Income\s+(-?\d[\d,]{3,})", flat, re.I
    )
    if not total:
        return None
    salary, salary_raw = _head(flat, r"Salar(?:y|ies)")
    business, business_raw = _head(flat, r"Business\s*(?:and|or|&)\s*Profession")
    gross = _signed(total[1])
    earned = max(salary, 0) + max(business, 0)
    row = {
        "assessment_year": year[1],
        "gross_total_income": gross,
        "salary_head": max(salary, 0),
        "business_head": max(business, 0),
        "other_head": max(gross - earned, 0),
        "earned_total": earned,
        "name_on_document": itr_computation(flat).get("applicant.applicant_name"),
    }
    raw = "; ".join(part for part in (salary_raw, business_raw, total[0]) if part)
    return row, raw, pages[0][0]


def computation_years(pages):
    segments = []
    for number, text in pages:
        if re.search(
            r"^\s*(?:A\.\s?Y\.\s*\d{4}-\d{2,4}|Name of Assessee\b)", text, re.M
        ) or re.search(r"PART A\s+GENERAL INFORMATION", text):
            segments.append([])
        if segments:
            segments[-1].append((number, text))
    return [found for found in map(computation_year, segments) if found]


def financial_facts(documents, page_reader):
    slips, years, citations, findings = {}, {}, {}, []
    for document in documents:
        for row, raw, page in computation_years(list(page_reader(document))):
            record = {
                "document": document.filename,
                "page": page,
                "raw_value": raw,
                "parser_version": "financial-fields-1.0",
            }
            if row["assessment_year"] in years and years[row["assessment_year"]][0] != row:
                findings.append(
                    {
                        "field": "income.itr_years",
                        "code": "CONFLICTING_ASSESSMENT_YEAR",
                        "retryable": False,
                    }
                )
            else:
                years.setdefault(row["assessment_year"], (row, record))
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
        if any(row[0].get("business_head") for row in years.values()):
            income["is_salaried"] = False
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
