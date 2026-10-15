import pytest

from underwriting_platform.application.completion import identity_blocked

REVIEW = {"stage": "medical", "type": "LOCAL_EVIDENCE_REVIEW_REQUIRED", "retryable": False}


@pytest.mark.parametrize(
    "case,blocked",
    [
        ({"_extraction_mode": "local_first", "processing_errors": [REVIEW]}, False),
        ({"_extraction_mode": "local_first", "processing_errors": []}, False),
        (
            {
                "_extraction_mode": "local_first",
                "processing_errors": [REVIEW, {"stage": "identity", "type": "TOKEN_BUDGET_EXCEEDED"}],
            },
            True,
        ),
        ({"_extraction_mode": "local_first", "processing_errors": [{"stage": "extraction"}]}, True),
        ({"_extraction_mode": "source_once", "processing_errors": [{"stage": "x", "type": "E"}]}, False),
    ],
)
def test_identity_waits_for_real_extraction_failures_but_not_for_review_flags(case, blocked):
    assert identity_blocked(case) is blocked
