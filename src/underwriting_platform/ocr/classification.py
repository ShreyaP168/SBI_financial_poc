"""Local content classification; unknowns stay explicit and are never dropped."""

import json
import re

from underwriting_platform.configuration.paths import CONFIG_ROOT


def classify_text(text, *, rules=None):
    rules = rules or json.loads(
        (CONFIG_ROOT / "document_classification.json").read_text(encoding="utf8")
    )
    normalized = " ".join(text.split())
    hits = [
        rule
        for rule in rules["rules"]
        if all(re.search(pattern, normalized, re.I) for pattern in rule["all"])
    ]
    # A proposal/financial document can mention ID labels without being an ID card.
    substantive = [rule for rule in hits if rule["kind"] != "id_proof"]
    hits = substantive or hits
    if hits:
        priority = max(rule.get("priority", 100) for rule in hits)
        hits = [rule for rule in hits if rule.get("priority", 100) == priority]
    distinct = {(rule["kind"], rule.get("label", "FORM_OR_TEXT")) for rule in hits}
    if len(distinct) != 1:
        return {
            "kind": "unknown",
            "label": "OTHER",
            "relevance": [],
            "requires_review": True,
            "reason": "ambiguous_content" if hits else "insufficient_content",
            "version": rules["version"],
        }
    rule = hits[0]
    return {
        "kind": rule["kind"],
        "label": rule.get("label", "FORM_OR_TEXT"),
        "relevance": list(dict.fromkeys(agent for hit in hits for agent in hit["agents"])),
        "requires_review": False,
        "reason": "content_rule_match",
        "version": rules["version"],
    }


def survey_documents(documents, agents):
    rules = json.loads((CONFIG_ROOT / "document_classification.json").read_text(encoding="utf8"))
    rows = []
    for document in documents:
        parts = re.split(r"--- page (\d+) ---\n", document.text)
        pages = []
        for index in range(1, len(parts), 2):
            number, text = int(parts[index]), parts[index + 1]
            ocr = next(
                (record for record in document.ocr_pages if record.get("page") == number), None
            )
            if ocr is not None:
                text = "\n".join(
                    line["text"]
                    for line in ocr.get("lines", [])
                    if (
                        line.get("mean_confidence")
                        if line.get("mean_confidence") is not None
                        else line.get("confidence", 0)
                    )
                    >= rules["min_ocr_line_confidence"]
                )
            pages.append(
                {
                    "page": number,
                    "basis": "local_ocr" if ocr else "native_text",
                    **classify_text(text, rules=rules),
                }
            )
        if not pages:
            pages = [
                {
                    "page": 1,
                    "basis": "local_ocr" if document.ocr_pages else "native_text",
                    **classify_text(document.text, rules=rules),
                }
            ]
        kinds = {page["kind"] for page in pages if page["kind"] != "unknown"}
        kind = next(iter(kinds)) if len(kinds) == 1 else "unknown"
        review = any(page["requires_review"] for page in pages) or len(kinds) != 1
        relevance = (
            list(agents)
            if review
            else list(dict.fromkeys(agent for page in pages for agent in page["relevance"]))
        )
        rows.append(
            {
                "filename": document.filename,
                "kind": kind,
                "relevance": relevance,
                "requires_review": review,
                "pages": pages,
            }
        )
    return {
        "files": rows,
        "agents_to_dispatch": list(
            dict.fromkeys(
                ["identity", "income", "compliance"]
                + [agent for row in rows for agent in row["relevance"]]
            )
        ),
        "skip_agents": [],
        "applicant_signal": "unknown",
        "missing_likely": [],
        "classifier_version": rules["version"],
        "provider": "local_ocr_rules",
        "provider_tokens": 0,
        "notes": "Unknown or mixed documents retained for review and conservative extraction routing.",
    }
