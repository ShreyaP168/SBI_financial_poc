"""Classify safe retryability without logging dependency error bodies."""

import httpx


def retryable_dependency(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        explicit = getattr(error, "retryable", None)
        if explicit is not None:
            return explicit is True
        status = getattr(error, "status_code", None) or getattr(error, "code", None)
        if isinstance(status, int):
            return status in {408, 409, 429} or status >= 500
        if isinstance(error, (TimeoutError, ConnectionError, httpx.TransportError)):
            return True
        error = error.__cause__ or error.__context__
    return False
