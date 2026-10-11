"""Compare independent reviewer columns without treating agreement as accuracy."""


def normalize_decision(value: str | None) -> str:
    return "_".join(str(value or "").strip().upper().split())


def compare_reviewer_decisions(
    uw: str, validation: str, actual: str, *, source_available: bool
) -> dict:
    uw, validation, actual = map(normalize_decision, (uw, validation, actual))
    if not source_available:
        return {
            "uw_match": "UNVERIFIED",
            "validation_match": "UNVERIFIED",
            "baseline_conflict": uw != validation,
            "overall": "UNVERIFIED_SOURCE_MISSING",
        }
    uw_match = actual == uw
    validation_match = actual == validation
    return {
        "uw_match": "MATCH" if uw_match else "MISMATCH",
        "validation_match": "MATCH" if validation_match else "MISMATCH",
        "baseline_conflict": uw != validation,
        "overall": "MISMATCH_VS_UW"
        if not uw_match
        else "REVIEW_BASELINE_CONFLICT"
        if uw != validation
        else "DECISION_MATCH",
    }
