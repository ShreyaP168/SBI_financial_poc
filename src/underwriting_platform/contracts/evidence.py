"""Evidence requirements for reusable extracted applicant facts."""

FIELDS = (
    "applicant_name",
    "age",
    "occupation",
    "education_level",
    "residential_status",
    "sum_assured",
    "gender",
    "employer_name",
    "business_nature",
)


def uncited_applicant_fields(case, sources=None):
    app = case.get("applicant") or {}
    sources = sources if sources is not None else case.get("field_sources") or {}
    issues = []
    for name in FIELDS:
        if app.get(name) in (None, "", 0):
            continue
        path = "applicant." + name
        records = sources.get(path) or (sources.get("applicant.dob") if name == "age" else [])
        if not isinstance(records, list) or not any(
            isinstance(record, dict)
            and record.get("document")
            and (record.get("page") or record.get("field_name") or record.get("json_pointer"))
            and record.get("raw_value") not in (None, "")
            for record in records
        ):
            issues.append(path)
    return issues
