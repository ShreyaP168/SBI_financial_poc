"""Correlation context shared by use cases and transport adapters."""

from contextvars import ContextVar

request_id: ContextVar[str] = ContextVar("request_id", default="")
