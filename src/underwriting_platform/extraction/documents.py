"""Layout-specific field readers for documents the classifier has already identified."""

import re
from datetime import datetime

_DATE = r"\d{2}[/-](?:\d{2}|[A-Za-z]{3})[/-]\d{4}"
_GENDER = r"(?:Male|Female|Transgender|Other)"
_PAN_LABEL = re.compile(
    r"(?:^|/)\s*(?:name|father'?s\s*name|date\s*of\s*birth|gender|signature)\s*$", re.I
)


def iso_date(raw):
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError("Invalid full DOB")


_ROLE_WORDS = {
    "proprietor",
    "owner",
    "partner",
    "director",
    "self",
    "employed",
    "managing",
    "founder",
    "karta",
    "na",
    "n",
    "a",
}
_PAN_HEADER = re.compile(
    r"govt|government|india|income\s*tax|department|permanent|account|number|card|signature|"
    r"father|mother|date|birth|gender|name",
    re.I,
)


def _flat(text):
    return " ".join(text.split())


def _rows(tables):
    for table in tables:
        yield [[" ".join((cell or "").split()) for cell in row] for row in table["rows"]]


def _number(raw):
    return float(raw.replace(",", "")) if re.fullmatch(r"[\d,]+(?:\.\d+)?", raw or "") else None


def _compact(cell):
    return re.sub(r"\s+", "", cell)


def _nominees(tables):
    for rows in _rows(tables):
        for start, header in enumerate(rows):
            column = next(
                (i for i, cell in enumerate(header) if cell.lower().startswith("name of nominee")),
                None,
            )
            if column is None:
                continue
            nominees, raws = [], []
            for row in rows[start + 1 :]:
                if len(row) < column + 5 or not re.fullmatch(_DATE, _compact(row[column + 1])):
                    break
                share = _number(row[column + 4])
                nominees.append(
                    {
                        "name": re.sub(r"^(?:Mr|Mrs|Ms|Miss|Shri|Smt)\.?\s+", "", row[column]),
                        "dob": iso_date(_compact(row[column + 1])),
                        "gender": _compact(row[column + 2]),
                        "relation": _compact(row[column + 3]),
                        "share_pct": int(share) if share is not None else None,
                    }
                )
                raws.append(" ".join(row[column : column + 5]))
            if nominees:
                return "; ".join(raws), nominees
    return None


def _riders(tables):
    found, raws = [], []
    for rows in _rows(tables):
        header = next(
            (c for r in rows for c in r if c.lower().startswith("rider name")), None
        )
        if header is None:
            continue
        name = re.sub(r"(?i)^rider name\s*:\s*", "", header)
        for row in rows:
            option = next((c for c in row if re.match(r"(?i)option\s+[a-z]\s*:", c)), None)
            cells = row[row.index(option) + 1 :] if option else []
            numbers = [0.0 if c.upper() in {"NA", "N/A"} else _number(c) for c in cells]
            numbers = [n for n in numbers if n is not None]
            if option and len(numbers) >= 4:
                code = re.search(r"\(([A-Z]+)\)\s*$", option)
                found.append(
                    {
                        "rider": name,
                        "option": code[1] if code else option,
                        "policy_term": int(numbers[0]),
                        "premium_payment_term": int(numbers[1]),
                        "sum_assured": int(numbers[2]),
                        "instalment_premium": numbers[3],
                    }
                )
                raws.append(" ".join(row))
    return ("; ".join(raws), found) if found else None


def _cover_details(tables):
    for rows in _rows(tables):
        for row in rows:
            if row[0].lower().startswith("better half benefit") and len(row) > 3:
                amount = _number(row[3])
                if amount:
                    return " ".join(row), int(amount)
    return None


_POLICY_ROW = re.compile(
    r"^(?P<insurer>[A-Za-z][A-Za-z .&]+?)\s+(?P<number>[A-Z0-9]{8,})\s+\d+\s+(?P<sa>\d{5,})\s+"
    r"(?:Self|Spouse|Parent)\s+(?P<status>[A-Za-z][A-Za-z -]*)$"
)


def _declared_policies(text):
    policies, raws = [], []
    for line in text.splitlines():
        match = _POLICY_ROW.match(" ".join(line.split()))
        if match:
            policies.append(
                {
                    "insurer": match["insurer"].strip(),
                    "policy_no": match["number"],
                    "sum_assured": int(match["sa"]),
                    "status": match["status"].strip(),
                }
            )
            raws.append(" ".join(line.split()))
    if not policies:
        return None
    in_force = sum(
        p["sum_assured"] for p in policies if re.match(r"(?i)in\s*-?force", p["status"])
    )
    return "; ".join(raws), {
        "answer": "Yes",
        "total_life_cover_other": in_force,
        "policies_in_other_companies": policies,
        "pending_applications_other": [],
    }


def _existing_cover(tables):
    for rows in _rows(tables):
        for row in rows:
            if "existing life insurance polic" in row[0].lower():
                answer = next((c for c in reversed(row[1:]) if c), "")
                if answer.lower() == "no":
                    return row[0][-80:] + " " + answer, {
                        "answer": "No",
                        "total_life_cover_other": 0,
                        "policies_in_other_companies": [],
                        "pending_applications_other": [],
                    }
    return None


def _first(pattern, text, flags=re.I):
    match = re.search(pattern, text, flags)
    return match[1].strip() if match else None


_HEALTH_TOPIC = re.compile(
    r"diabet|blood pressure|heart|lung|respiratory|cancer|kidney|liver|digestive|neurolog|"
    r"brain|nervous|thyroid|hiv|aids|hospital|treated|surgery|operated|weight|smok|tobacco|"
    r"alcohol|narcotic|hazardous|disorder|disease|disabilit|pregnan|medical",
    re.I,
)
_QUESTION_START = re.compile(r"^(?:\d{1,2}|[a-z])\s*\.\s*\S|^(?:Have|Do|Has|Are|Is|Did)\s", re.I)
_SUB_ITEM = re.compile(r"^(Smoking|Tobacco Chewing|Alcohol|Narcotic)\s+(Yes|No)$", re.I)
_SECTION_END = re.compile(
    r"^(?:SECTION\s*[‘'’\"]?[D-Z]\b|Ayushman|PREMIUM PAYMENT|BANK ACCOUNT)", re.I
)
_TRAILING_ANSWER = re.compile(r"(?:^|\s)(Yes|No|NA|N/A)$", re.I)


def medical_declarations(text):
    answers, raws, open_question, unanswered = [], [], None, []

    def close(answer):
        nonlocal open_question
        answers.append(
            {"question": " ".join(open_question.split())[:140], "answer": answer.upper() if answer.lower() in {"na", "n/a"} else answer.title()}
        )
        raws.append(f"{open_question[:60]} {answer}")
        open_question = None

    for line in (line.strip() for line in text.splitlines()):
        if not line:
            continue
        sub = _SUB_ITEM.match(line)
        if sub:
            answers.append({"question": sub[1].title(), "answer": sub[2].title()})
            raws.append(line)
            continue
        if _QUESTION_START.match(line) and _HEALTH_TOPIC.search(line):
            if open_question is not None:
                unanswered.append(open_question[:70])
            open_question = line
        elif open_question is not None and re.fullmatch(r"(?i)yes|no|na|n/a", line):
            close(line)
            continue
        elif open_question is not None:
            open_question += " " + line
        else:
            continue
        trailing = _TRAILING_ANSWER.search(open_question)
        if trailing and len(open_question) > 25:
            close(trailing[1])
    if open_question is not None:
        unanswered.append(open_question[:70])
    if len(answers) >= 3:
        lines = []
        for line in (line.strip() for line in text.splitlines()):
            if _SECTION_END.match(line):
                break
            lines.append(line)
        stray = sum(bool(re.search(r"(?:^|\s)Yes$", line, re.I)) for line in lines) - sum(
            a["answer"] == "Yes" for a in answers
        )
        for _ in range(max(stray, 0)):
            answers.append({"question": "Unparsed line ending in Yes", "answer": "Yes"})
            raws.append("Yes")
    return answers, raws, unanswered


def proposal_form(text, tables=()):
    flat = _flat(text)
    found = {}
    found["applicant.declared_annual_income"] = _first(
        r"Annual\s+Total\s+Income\s*:?\s*(?:Rs\.?|INR|₹)?\s*([\d,]+)", flat
    )
    found["applicant.nominees"] = _nominees(tables)
    found["applicant.riders"] = _riders(tables)
    found["compliance.existing_cover_declared"] = _existing_cover(tables) or _declared_policies(text)
    riders = found["applicant.riders"]
    for option, path in (("ADB", "applicant.adb_sum_assured"), ("APPD", "applicant.appd_sum_assured")):
        row = next((r for r in (riders[1] if riders else []) if r["option"] == option), None)
        if row and row["sum_assured"] > 0:
            found[path] = (riders[0], row["sum_assured"])
    cover = _cover_details(tables)
    if cover:
        found["applicant.bhb_sum_assured"] = cover
    answers, raws, _ = medical_declarations(text)
    if answers:
        found["medical.declaration_answers"] = ("; ".join(raws)[:300], answers)
    name = re.search(
        r"First\s+Name\s+(.+?)\s+Middle\s+Name\s*(.*?)\s*Last\s+Name\s+(.+?)"
        r"(?=\s+(?:Gender|Date of Birth)\b)",
        flat,
        re.I,
    )
    if name:
        found["applicant.applicant_name"] = " ".join(part for part in name.groups() if part)
        found["applicant.dob"] = _first(rf"Date of Birth\s+({_DATE})", flat)
        found["applicant.gender"] = _first(rf"\bGender\s+({_GENDER})\b", flat)
    found["applicant.occupation"] = _first(
        r"(?<!\w)Occupation\s+(?!Details\b)([A-Za-z][A-Za-z /-]{2,30}?)"
        r"(?=\s+(?:Force Name|If Business|Name of Business|Employer|Designation|Annual|Are you)\b)",
        flat,
    )
    role = _first(r"Your\s+role\s+(.+?)(?=\s+Duration\b)", flat)
    if role and not set(re.findall(r"[a-z]+", role.casefold())) <= _ROLE_WORDS:
        found["applicant.business_nature"] = role
    found["applicant.age"] = _first(r"\bAge\s+(\d{1,3})\s+Years", flat)
    found["applicant.residential_status"] = _first(
        r"Resident\s+Status\s+((?:Non[- ])?Resident(?: Indian)?|NRI|PIO|OCI|Foreign National)"
        r"(?=\s+Nationality\b)",
        flat,
    )
    found["applicant.education_level"] = _first(
        r"\bQualification\s+(.{1,60}?)(?=\s+CONTACT DETAILS\b)", flat
    )
    return {path: raw for path, raw in found.items() if raw}


def _value_before_label(lines):
    found = {}
    for index, line in enumerate(lines[1:], start=1):
        previous = lines[index - 1]
        label = re.sub(r"[^a-z]", "", line.casefold())
        if label.endswith("name") and "father" not in label and "mother" not in label:
            if re.fullmatch(r"[A-Za-z][A-Za-z .']*", previous):
                found["applicant.applicant_name"] = previous
        elif label.endswith("dateofbirth") and re.fullmatch(_DATE, previous):
            found["applicant.dob"] = previous
        elif label.endswith("gender") and re.fullmatch(_GENDER, previous, re.I):
            found["applicant.gender"] = previous
    return found


def pan_card(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    before = _value_before_label(lines)
    if "applicant.applicant_name" in before:
        return before
    start = next(
        (i for i, line in enumerate(lines) if re.fullmatch(r"[A-Z]{5}\s*\d{4}\s*[A-Z]", line)),
        None,
    )
    if start is None:
        return {}
    names, found = [], {}
    for line in lines[start + 1 :]:
        if _PAN_LABEL.search(line):
            continue
        if re.fullmatch(_DATE, line):
            found.setdefault("applicant.dob", line)
        elif re.fullmatch(_GENDER, line, re.I):
            found.setdefault("applicant.gender", line)
        elif (
            len(names) < 2
            and re.fullmatch(r"[A-Za-z][A-Za-z .']*", line)
            and not _PAN_HEADER.search(line)
        ):
            names.append(line)
    if names:
        found["applicant.applicant_name"] = names[0]
    return found


def passport(text):
    flat = _flat(text)
    names = re.search(r"P<IND([A-Z<]+?)<<([A-Z<]+?)<{2,}", flat)
    zone = re.search(r"\b[A-Z0-9<]{9}\d[A-Z]{3}(\d{6})\d([MF<])", flat)
    found = {}
    if names:
        surname, given = (part.replace("<", " ").strip() for part in names.groups())
        found["applicant.applicant_name"] = f"{given} {surname}"
    if zone:
        year, month, day = (int(zone[1][i : i + 2]) for i in (0, 2, 4))
        for century in (2000, 1900):
            printed = f"{day:02d}/{month:02d}/{century + year}"
            if century + year <= datetime.now().year and printed in flat:
                found["applicant.dob"] = printed
                break
        if zone[2] in "MF":
            found["applicant.gender"] = "Male" if zone[2] == "M" else "Female"
    return found


def aadhaar_letter(text):
    flat = _flat(text)
    found = {
        "applicant.applicant_name": _first(
            r"\bTo\s+([A-Za-z][A-Za-z .]*?)\s+(?:S/O|D/O|W/O|C/O)\b", flat
        ),
        "applicant.dob": _first(rf"\bDOB\s*:\s*({_DATE})", flat),
        "applicant.gender": _first(rf"\b({_GENDER})\s*/\s*(?:MALE|FEMALE|OTHER)\b", flat),
    }
    return {path: raw for path, raw in found.items() if raw}


def assessee_computation(text):
    flat = _flat(text)
    found = {
        "applicant.applicant_name": _first(r"Name of Assessee\s+(.+?)\s+(?:Father|A\.Y\.)", flat),
        "applicant.dob": _first(rf"Date of Birth\s+({_DATE})", flat),
        "applicant.gender": _first(rf"\bGender\s+({_GENDER})\b", flat),
        "applicant.business_nature": _first(
            r"Nature of Business\s+(.+?)(?=\s+Stock Valuation Method|\s+Filing Status|$)", flat
        ),
    }
    return {path: raw for path, raw in found.items() if raw}


def itr_computation(text):
    flat = _flat(text)
    if "Name of Assessee" in flat:
        return assessee_computation(text)
    if "Previous Year" not in flat:
        return {}
    found = {
        "applicant.applicant_name": _first(
            r"(?<!Father's )(?<!Father’s )\bName\s*:\s*([A-Za-z][A-Za-z .]+?)"
            r"\s+(?:Previous Year|Father|PAN|Address|Status)\b",
            flat,
        ),
        "applicant.dob": _first(rf"Date of Birth\s*:\s*({_DATE})", flat),
    }
    return {path: raw for path, raw in found.items() if raw}


def document_fields(kind, label, text, tables=()):
    if kind == "proposal_form":
        return proposal_form(text, tables)
    if kind == "id_proof" and label == "PAN_CARD":
        return pan_card(text)
    if kind == "id_proof" and (label or "").startswith("AADHAAR"):
        return aadhaar_letter(text)
    if kind == "id_proof" and label == "PASSPORT":
        return passport(text)
    if kind == "itr":
        return itr_computation(text)
    return {}


def names_compatible(first, second):
    left, right = (re.sub(r"[^a-z ]", " ", name.casefold()).split() for name in (first, second))
    if not left or not right:
        return False
    if "".join(left) == "".join(right):
        return True
    return all(
        a == b or (len(a) == 1 or len(b) == 1) and a[0] == b[0]
        for a, b in zip(left, right, strict=False)
    )
