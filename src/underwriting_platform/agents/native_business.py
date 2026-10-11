"""Preserve a labeled native business declaration rather than a model expansion."""

import re


def reconcile_native_business(case, documents, kinds):
    app = case.get("applicant") or {}
    sources = case.setdefault("field_sources", {})
    employer = app.get("employer_name")
    normalize = lambda value: " ".join(str(value).split()).casefold()
    if not isinstance(employer, str) or not employer.strip():
        return []
    matches = []
    for document in documents:
        if kinds.get(document.filename) not in {"proposal_form", "caf"}:
            continue
        sections = re.split(r"--- page (\d+) ---\n", document.text)
        for index in range(1, len(sections), 2):
            page, text = int(sections[index]), sections[index + 1]
            if any(record.get("page") == page for record in document.ocr_pages):
                # OCR candidates are not exact native-text evidence repairs.
                continue
            for line in text.splitlines():
                match = re.search(
                    r"Name of Business Organization\s+(.+?)\s+Your Role\b", line, re.I
                )
                if match and normalize(match[1]) == normalize(employer):
                    matches.append(
                        {
                            "document": document.filename,
                            "page": page,
                            "raw_value": match[1].strip(),
                            "field_name": "Name of Business Organization",
                        }
                    )
    if not matches:
        return []
    changes = []
    if not sources.get("applicant.employer_name"):
        sources["applicant.employer_name"] = matches
        changes.append(
            {"field": "applicant.employer_name", "action": "Attach exact native declaration"}
        )
    nature = app.get("business_nature")
    # A prose expansion of a business name is not a separate declaration.
    # Retain the candidate for review; the engine sees the evidenced name.
    if (
        isinstance(nature, str)
        and not sources.get("applicant.business_nature")
        and (
            normalize(nature) == normalize(employer)
            or normalize(nature).startswith(normalize(employer) + " /")
        )
    ):
        case.setdefault("_model_inferences", {})["business_nature"] = nature
        app["business_nature"] = None
        app["declared_business_nature"] = None
        changes.append(
            {
                "field": "applicant.business_nature",
                "action": "Retain unsupported name expansion as model inference, not declaration",
            }
        )
    return changes
