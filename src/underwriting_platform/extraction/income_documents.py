"""Salary slips and ITR computation pages read from reconstructed rows."""

import re

MONTHS = {
    name: index
    for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
SLIP_MARKER = re.compile(r"payslip|salaryslip|paystatement|salarystatement|payrollslip|payadvice")
MONTH_PATTERN = re.compile(
    r"(?:monthof|monthended|payperiod|salaryslip|payslip|forthemonth)[:\-]*"
    r"(jan[a-z]*|feb[a-z]*|mar[a-z]*|apr[a-z]*|may|jun[a-z]*|jul[a-z]*|aug[a-z]*|sep[a-z]*|"
    r"oct[a-z]*|nov[a-z]*|dec[a-z]*)[-./,]*(20\d{2})"
)
GROSS_LABEL = re.compile(
    r"gross\s*earnings|gross\s*pay\b|total\s*earnings|gross\s*salary", re.I
)
LEADING_NUMBERS = re.compile(r"[^0-9]{0,8}((?:\d[\d,]*(?:\.\d+)?[\s|]*)+)")
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def squash(text):
    return re.sub(r"\s+", "", text.casefold())


def _amount(raw):
    return int(round(float(raw.replace(",", ""))))


def slip_page(text):
    flat = squash(text)
    if not SLIP_MARKER.search(flat):
        return None
    month = MONTH_PATTERN.search(flat)
    if not month:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        label = GROSS_LABEL.search(line)
        if not label:
            continue
        found = LEADING_NUMBERS.match(line[label.end() :])
        if not found and index + 1 < len(lines):
            found = LEADING_NUMBERS.match(lines[index + 1])
        if not found:
            continue
        numbers = NUMBER.findall(found[1])
        gross = _amount(numbers[-1])
        if 1000 <= gross <= 10_000_000:
            return {
                "month_year": f"{month[2]}-{MONTHS[month[1][:3]]:02d}",
                "gross_salary": gross,
            }, f"{label[0]} {numbers[-1]}"
    return None


YEAR_PAIR = re.compile(r"\b(20\d{2})\s*-\s*(20\d{2}|\d{2})\b")
INDIAN_AMOUNT = r"(-?\d[\d,]*)"


def _row_amount(lines, label):
    pattern = re.compile(label + r"[^0-9\n-]{0,12}" + INDIAN_AMOUNT, re.I)
    for line in lines:
        match = pattern.search(line)
        if match:
            return int(match[1].replace(",", "")), match[0]
    return None, None


def computation_page(text):
    flat = squash(text)
    if "computationoftotalincome" not in flat or "totalincome" not in flat:
        return None
    header = text.casefold().split("computation")[0]
    years = [int(a) for a, _ in YEAR_PAIR.findall(header)]
    if not years:
        return None
    start = max(years)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    business, business_raw = _row_amount(lines, r"business\s*or\s*profession")
    salary, salary_raw = _row_amount(lines, r"income\s*from\s*salar(?:y|ies)")
    rounded, rounded_raw = _row_amount(lines, r"total\s*income\s*rounded\s*off\s*u\s*/?\s*s\s*288\s*a")
    gross, gross_raw = _row_amount(lines, r"gross\s*total\s*income")
    total, total_raw = _row_amount(
        lines, r"(?<!gross)(?<!gross )(?<!computation of )(?<!computationof)total\s*income(?!\s*rounded)"
    )
    chosen = rounded if rounded is not None else total if total is not None else gross
    if chosen is None:
        return None
    business_head = max(business or 0, 0)
    salary_head = max(salary or 0, 0)
    earned = business_head + salary_head
    row = {
        "assessment_year": f"{start}-{start + 1}",
        "gross_total_income": gross if gross is not None else chosen,
        "salary_head": salary_head,
        "business_head": business_head,
        "other_head": max((gross if gross is not None else chosen) - earned, 0),
        "earned_total": earned or chosen,
        "total_income": chosen,
        "name_on_document": None,
    }
    raw = "; ".join(part for part in (business_raw, salary_raw, gross_raw, rounded_raw or total_raw) if part)
    return row, raw


BUSINESS_DESCRIPTION = re.compile(r"\b(\d{5})\s*-\s*([A-Za-z][^\n]{6,140})")


def business_description(text):
    match = BUSINESS_DESCRIPTION.search(text)
    if not match:
        return None
    description = " ".join(match[2].split())
    description = re.split(r"\s+sale\s*of|\s+n\.e\.c", description, maxsplit=1, flags=re.I)[0]
    return description if len(description.split()) >= 2 else None
