"""Errors safe to transport without leaking provider credentials or case data."""


class ApplicationError(Exception):
    code = "APPLICATION_ERROR"
    status_code = 500
    public_message = "The operation could not be completed."
    retryable = False


class MissingCase(ApplicationError):
    code = "CASE_NOT_FOUND"
    status_code = 404
    public_message = "The requested case has no available source documents."


class InvalidRequest(ApplicationError):
    code = "INVALID_REQUEST"
    status_code = 422
    public_message = "The request does not meet the service contract."


class DependencyUnavailable(ApplicationError):
    code = "DEPENDENCY_UNAVAILABLE"
    status_code = 503
    public_message = "A required processing dependency is unavailable."
    retryable = True


class IdempotencyConflict(ApplicationError):
    code = "IDEMPOTENCY_CONFLICT"
    status_code = 409
    public_message = "The idempotency key has already been used for a different request."


class ArtifactBlocked(ApplicationError):
    code = "ARTIFACT_BLOCKED"
    status_code = 409
    public_message = "This artifact did not pass the delivery gate."


class LeaseLost(ApplicationError):
    code = "LEASE_LOST"
    status_code = 409
    public_message = "The worker no longer owns this job."


class ExtractionFailed(DependencyUnavailable):
    code = "EXTRACTION_FAILED"


class ProcessingRejected(ApplicationError):
    code = "PROCESSING_REJECTED"
    status_code = 422
    public_message = "Required evidence needs correction or manual review."


class QueueFull(ApplicationError):
    code = "QUEUE_FULL"
    status_code = 429
    public_message = "The processing queue is full; retry later with the same idempotency key."
    retryable = True
