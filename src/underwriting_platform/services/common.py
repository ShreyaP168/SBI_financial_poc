"""Authentication, correlation, health and redacted error responses."""

from __future__ import annotations

import json
import logging
import secrets
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from underwriting_platform.application.context import request_id
from underwriting_platform.application.errors import ApplicationError
from underwriting_platform.contracts.v1 import Problem


def authentication(expected: str) -> Callable:
    async def verify(request: Request) -> None:
        if not expected:
            raise HTTPException(status_code=503, detail="Authentication is not configured")
        actual = request.headers.get("Authorization", "")
        if not secrets.compare_digest(actual.encode(), f"Bearer {expected}".encode()):
            raise HTTPException(
                status_code=401,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return verify


def create_service(name: str, *, lifespan=None, ready: Callable | None = None) -> FastAPI:
    app = FastAPI(title=f"Underwriting Platform — {name}", version="1.0.0", lifespan=lifespan)
    logger = logging.getLogger(f"underwriting_platform.{name}")

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        provided = request.headers.get("X-Request-ID", "")
        try:
            identifier = str(uuid.UUID(provided))
        except ValueError:
            identifier = str(uuid.uuid4())
        token = request_id.set(identifier)
        start = time.monotonic()
        try:
            try:
                response = await call_next(request)
            except Exception:
                logger.error(
                    "Service processing failed",
                    extra={"request_id": identifier, "error_code": "PROCESSING_FAILED"},
                )
                response = problem(
                    "PROCESSING_FAILED",
                    "Processing could not be completed; no issuance decision was made.",
                    500,
                )
            response.headers["X-Request-ID"] = identifier
            logger.info(
                "HTTP request",
                extra={
                    "request_id": identifier,
                    "status_code": response.status_code,
                    "duration_ms": round((time.monotonic() - start) * 1000, 1),
                },
            )
            return response
        finally:
            request_id.reset(token)

    def problem(code: str, message: str, status: int) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content=Problem(code=code, message=message, request_id=request_id.get()).model_dump(),
        )

    @app.exception_handler(ApplicationError)
    async def application_error(request: Request, error: ApplicationError):
        return problem(error.code, error.public_message, error.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return problem("INVALID_REQUEST", "The request does not meet the API contract.", 422)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        response = problem("HTTP_ERROR", str(error.detail), error.status_code)
        response.headers.update(error.headers or {})
        return response

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception):
        logger.error("Service processing failed", extra={"error_code": "PROCESSING_FAILED"})
        return problem(
            "PROCESSING_FAILED",
            "Processing could not be completed; no issuance decision was made.",
            500,
        )

    @app.get("/health/live")
    async def live():
        return {"service": name, "status": "alive"}

    @app.get("/health/ready")
    async def readiness():
        if ready is not None:
            import asyncio

            try:
                if not await asyncio.to_thread(ready):
                    raise HTTPException(503, "Dependency unavailable")
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(503, "Dependency unavailable") from exc
        return {"service": name, "status": "ready"}

    return app


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "request_id",
            "job_id",
            "error_code",
            "status_code",
            "duration_ms",
            "stage",
            "wait_seconds",
            "processing_seconds",
            "queue_age_seconds",
            "attempt",
        ):
            if hasattr(record, key):
                fields[key] = getattr(record, key)
        return json.dumps(fields)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.WARNING)
    logging.getLogger("underwriting_platform").setLevel(logging.INFO)
