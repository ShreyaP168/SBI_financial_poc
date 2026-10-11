"""Request labels contain domain names, never applicant or response content."""

from contextvars import ContextVar

request_stage = ContextVar("llm_request_stage", default="other")
