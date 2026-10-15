"""Conservative labeled-field parsers. Missing/conflicting facts are never guessed."""

import re

from .documents import document_fields, iso_date, names_compatible
from .kyc import kyc_evidence

PATTERNS = {
    "applicant.applicant_name": r"(?:Name of (?:Life Assured|Applicant)|Applicant Name)\s*[:\-]\s*([^\n]+)",
    "applicant.dob": r"(?:Date of Birth|DOB)\s*[:\-]\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})",
    "applicant.age": r"(?:Age Last Birthday|Applicant Age|Age at Entry)\s*[:\-]\s*(\d{1,2})\b",
    "applicant.gender": r"(?:Gender|Sex)\s*[:\-]\s*(Male|Female|Other)\b",
    "applicant.occupation": r"(?:Occupation|Nature of Occupation)\s*[:\-]\s*([^\n]+)",
    "applicant.education_level": r"(?:Education|Educational Qualification)\s*[:\-]\s*([^\n]+)",
    "applicant.residential_status": r"(?:Residential Status|Residence Status)\s*[:\-]\s*([^\n]+)",
    "applicant.sum_assured": r"(?:Proposed Sum Assured|Sum Assured)\s*(?:\(Rs\.?\)\s*)?[:\-]?\s*(?:INR|Rs\.?|₹)?\s*([\d,]+(?:\.\d+)?(?:\s+(?:lakh|lakhs|crore|crores))?)",
    "applicant.employer_name": r"(?:Employer Name|Name of Business Organi[sz]ation(?:\s+run by you)?)\s*[:\-]?\s*(.+?)(?:\s+Your Role\b|\n|$)",
    "applicant.business_nature": r"(?:Nature of Business|Business Nature)\s*[:\-]\s*([^\n]+)",
    "applicant.pan": r"(?:PAN(?: Number| No\.?)?|Permanent Account Number(?: Card)?)\s*[:\-]?\s*([A-Z]{5}\s*\d{4}\s*[A-Z])\b",
    "compliance.kyc_status": r"KYC Status\s*[:\-]\s*(Verified|Partial|Pending)\b",
}


def normalize(path, raw):
    raw = raw.strip()
    if path in {"applicant.age", "applicant.sum_assured", "applicant.declared_annual_income"}:
        from .financial import amount

        number = amount(raw)
        if number <= 0:
            raise ValueError("Invalid positive integer")
        return int(number)
    if path == "applicant.dob":
        return iso_date(raw)
    if path == "applicant.pan":
        return re.sub(r"\s", "", raw).upper()
    if path == "applicant.gender":
        return raw.title()
    if path == "applicant.applicant_name":
        name = " ".join(raw.replace(".", " ").split())
        return name.title() if name.isupper() or name.islower() else name
    return raw


def pages(document):
    parts = re.split(r"--- page (\d+) ---\n", document.text)
    if len(parts) == 1 and document.text:
        yield 1, document.text
    for index in range(1, len(parts), 2):
        page = int(parts[index])
        ocr = next((row for row in document.ocr_pages if row.get("page") == page), None)
        text = parts[index + 1]
        if ocr is not None:
            text = "\n".join(
                row["text"] for row in ocr.get("lines", []) if row.get("confidence", 0) >= 0.9
            )
        for table in document.tables:
            if table.get("page") == page:
                from .tables import table_rows

                for row in table_rows(table):
                    if len(row) == 2 and all(isinstance(cell, str) for cell in row):
                        text += "\n" + row[0] + ": " + row[1]
        yield page, text


def extract_local(documents, page_kinds=None):
    candidates, windows, findings = {}, [], []
    page_kinds = page_kinds or {}
    for document in documents:
        for page, text in pages(document):
            kind, label = page_kinds.get((document.filename, page), (None, None))
            page_tables = [t for t in document.tables if t.get("page") == page]
            for path, found in document_fields(kind, label, text, page_tables).items():
                raw, value = found if isinstance(found, tuple) else (found, None)
                try:
                    value = normalize(path, raw) if value is None else value
                except (ValueError, ArithmeticError):
                    continue
                source = {
                    "document": document.filename,
                    "page": page,
                    "raw_value": raw,
                    "parser_version": "document-fields-1.0",
                }
                candidates.setdefault(path, []).append((value, source))
            for path, pattern in PATTERNS.items():
                if kind == "mhr" and path == "applicant.occupation":
                    continue
                for match in re.finditer(r"^\s*" + pattern, text, re.I | re.M):
                    context = text[max(0, match.start() - 180) : match.start()]
                    if path.startswith("applicant.") and re.search(
                        r"nominee details|family member|spouse details", context, re.I
                    ):
                        continue
                    raw = match[1].strip()
                    try:
                        value = normalize(path, raw)
                    except ValueError:
                        continue
                    source = {
                        "document": document.filename,
                        "page": page,
                        "raw_value": raw,
                        "parser_version": "labeled-fields-1.0",
                    }
                    candidates.setdefault(path, []).append((value, source))
            # Windows are bounded per labeled section, never a whole-folder prompt.
            lines = text.splitlines()
            for index, line in enumerate(lines):
                if re.search(
                    r"applicant|life assured|dob|occupation|income|salary|assessment year|kyc|business|sum assured",
                    line,
                    re.I,
                ):
                    windows.append(
                        {
                            "document": document.filename,
                            "page": page,
                            "text": "\n".join(lines[max(0, index - 1) : index + 4])[:1000],
                        }
                    )
    block = {
        "applicant": {},
        "income": {},
        "compliance": {},
        "medical": {},
        "assets_liabilities": {},
    }
    sources = {}
    for path, values in candidates.items():
        if path == "medical.declaration_answers":
            merged = [item for value, _ in values for item in value]
            block["medical"]["declaration_answers"] = merged
            sources[path] = [source for _, source in values]
            if not any(item["answer"] == "Yes" for item in merged):
                block["medical"]["declaration_reviewed"] = True
                sources["medical.declaration_reviewed"] = sources[path]
            continue
        unique = {str(value) for value, _ in values}
        chosen = values[0][0]
        if path == "applicant.applicant_name" and len(unique) > 1:
            if all(names_compatible(a, b) for a in unique for b in unique):
                declared = [
                    value
                    for value, source in values
                    if page_kinds.get((source["document"], source["page"]), ("",))[0]
                    == "proposal_form"
                ]
                chosen = (declared or [max(unique, key=len)])[0]
                unique = {str(chosen)}
        if len(unique) != 1:
            findings.append(
                {"field": path, "code": "CONFLICTING_LOCAL_EVIDENCE", "retryable": False}
            )
            continue
        owner, name = path.split(".", 1)
        block[owner][name] = chosen
        sources[path] = [source for _, source in values]
    from .financial import financial_facts

    income, income_sources, income_findings = financial_facts(documents, pages)
    block["income"] = income
    if not income:
        from underwriting_platform.engine.calculator import _bank_credits_modal_annual

        from .tables import bank_salary_credits

        credits, credit_sources = bank_salary_credits(documents)
        if len({row["date"][:7] for row in credits}) >= 3:
            computed = _bank_credits_modal_annual(credits)
            if computed:
                block["income"] = {
                    "primary_source": "bank_credits",
                    "annual_income": computed[0],
                    "bank_salary_credits": credits,
                }
                income_sources["income.bank_salary_credits"] = credit_sources
                income_sources["income.annual_income"] = credit_sources
    sources.update(income_sources)
    findings.extend(income_findings)
    evidence = kyc_evidence(page_kinds, sources)
    if evidence:
        block["compliance"]["kyc_evidence"], sources["compliance.kyc_evidence"] = evidence
    return block, sources, findings, windows
