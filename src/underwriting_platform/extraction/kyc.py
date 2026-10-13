"""Which identity documents are on file; never a statement that KYC is verified."""

import re

from .documents import iso_date, names_compatible


def _names(sources):
    return {" ".join(re.sub(r"[^a-z ]", " ", s["raw_value"].casefold()).split()) for s in sources}


def _name_match(sources):
    names = _names(sources)
    if len(sources) < 2:
        return None
    if len(names) == 1:
        return "Match"
    compatible = all(names_compatible(a, b) for a in names for b in names)
    return "Minor Mismatch" if compatible else "Major Mismatch"


def _dob_match(sources):
    dates = set()
    for source in sources:
        try:
            dates.add(iso_date(source["raw_value"]))
        except ValueError:
            continue
    if not dates:
        return None
    if len(dates) > 1:
        return "Mismatch"
    return "Match" if len(sources) > 1 else "Single Source"


def kyc_evidence(page_kinds, sources):
    pages = [(key, kind, label) for key, (kind, label) in page_kinds.items()]
    if any(label == "OTHER" for _, _, label in pages):
        return None
    labels = {label for _, _, label in pages}
    identity_pages = [
        (key, label)
        for key, kind, label in pages
        if kind == "id_proof" and label not in {"CONTINUATION", "OTHER"}
    ]
    if not identity_pages:
        return None
    aadhaar = any(label.startswith("AADHAAR") for label in labels)
    passport, licence = "PASSPORT" in labels, "DRIVING_LICENSE" in labels
    evidence = {
        "pan_present": "PAN_CARD" in labels,
        "aadhaar_present": aadhaar,
        "passport_present": passport,
        "voter_id_present": False,
        "driving_licence": licence,
        "address_proof_present": aadhaar or passport or licence,
        "vpsc_present": False,
        "piv_present": any(kind == "vmer" for _, kind, _ in pages),
        "ckyc_id": None,
        "name_match_across_ids": _name_match(sources.get("applicant.applicant_name", [])),
        "dob_match_across_ids": _dob_match(sources.get("applicant.dob", [])),
    }
    citations = [
        {
            "document": document,
            "page": page,
            "raw_value": label,
            "parser_version": "kyc-evidence-1.0",
        }
        for (document, page), label in identity_pages
    ]
    return evidence, citations
