"""Local content classification; unclassified content is routed to extraction, never dropped."""

import json
import re
from collections import Counter

from underwriting_platform.configuration.paths import CONFIG_ROOT

UNCLASSIFIED = "other"
PHOTOGRAPH = "photograph"


def classify_text(text, *, rules=None):
    rules = rules or json.loads(
        (CONFIG_ROOT / "document_classification.json").read_text(encoding="utf8")
    )
    normalized = " ".join(text.split())
    hits = [
        rule
        for rule in rules["rules"]
        if len(normalized) <= rule.get("max_chars", len(normalized))
        and all(re.search(pattern, normalized, re.I) for pattern in rule["all"])
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
            "kind": UNCLASSIFIED,
            "label": "OTHER",
            "relevance": [],
            "candidates": sorted({rule["kind"] for rule in hits}),
            "requires_review": True,
            "extraction_required": True,
            "reason": "ambiguous_content" if hits else "insufficient_content",
            "version": rules["version"],
        }
    rule = hits[0]
    return {
        "kind": rule["kind"],
        "label": rule.get("label", "FORM_OR_TEXT"),
        "relevance": list(dict.fromkeys(agent for hit in hits for agent in hit["agents"])),
        "requires_review": False,
        "extraction_required": False,
        "reason": "content_rule_match",
        "version": rules["version"],
    }


def _photograph(document, number, text, version):
    from .photo import MAX_TEXT_CHARS, contains_face, page_image

    image = page_image(document, number)
    if len(text.strip()) > MAX_TEXT_CHARS or image is None or not contains_face(image):
        return None
    return {
        "kind": PHOTOGRAPH,
        "label": "PHOTOGRAPH",
        "relevance": ["identity"],
        "requires_review": False,
        "extraction_required": False,
        "reason": "face_detected",
        "version": version,
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
            result = classify_text(text, rules=rules)
            if result["kind"] == UNCLASSIFIED:
                result = _photograph(document, number, text, rules["version"]) or result
            pages.append(
                {"page": number, "basis": "local_ocr" if ocr else "native_text", **result}
            )
        if not pages:
            result = classify_text(document.text, rules=rules)
            if result["kind"] == UNCLASSIFIED:
                result = _photograph(document, 1, document.text, rules["version"]) or result
            pages = [
                {
                    "page": 1,
                    "basis": "local_ocr" if document.ocr_pages else "native_text",
                    **result,
                }
            ]
        counts = Counter(
            page["kind"] for page in pages if page["kind"] not in {UNCLASSIFIED, PHOTOGRAPH}
        )
        if counts:
            kind = counts.most_common(1)[0][0]
            anchor = next(page for page in pages if page["kind"] == kind)
            for page in pages:
                if page["kind"] == UNCLASSIFIED and (
                    page["reason"] == "insufficient_content"
                    or kind in page.get("candidates", [])
                ):
                    page.update(
                        kind=kind,
                        label="CONTINUATION",
                        relevance=anchor["relevance"],
                        requires_review=False,
                        extraction_required=False,
                        reason="document_continuation",
                    )
        elif all(page["kind"] == PHOTOGRAPH for page in pages):
            kind = PHOTOGRAPH
        else:
            kind = UNCLASSIFIED
        subdocs = sorted(
            {page["kind"] for page in pages if page["kind"] not in {kind, UNCLASSIFIED, PHOTOGRAPH}}
        )
        review = any(page["requires_review"] for page in pages)
        relevance = (
            list(
                dict.fromkeys(
                    ["identity", "income", "compliance", "medical"]
                    + [
                        agent
                        for page in pages
                        if page["kind"] != UNCLASSIFIED
                        for agent in page["relevance"]
                    ]
                )
            )
            if review
            else list(dict.fromkeys(agent for page in pages for agent in page["relevance"]))
        )
        rows.append(
            {
                "filename": document.filename,
                "kind": kind,
                "subdocs": subdocs,
                "relevance": relevance,
                "requires_review": review,
                "extraction_required": any(page["extraction_required"] for page in pages),
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
        "notes": "Unclassified documents are labeled other and routed to extraction.",
    }
