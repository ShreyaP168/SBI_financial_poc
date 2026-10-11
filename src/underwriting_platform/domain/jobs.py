"""Durable job state independent of any transport or database driver."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from underwriting_platform.contracts.v1 import CaseRequest, JobResponse, JobStatus, WorkflowResult


@dataclass(frozen=True)
class Job:
    job_id: str
    request: CaseRequest
    status: JobStatus
    stage: str
    attempts: int
    created_at: float
    updated_at: float
    checkpoints: dict[str, Any] = field(default_factory=dict)
    result: WorkflowResult | None = None
    error_code: str | None = None

    def response(self) -> JobResponse:
        return JobResponse(
            job_id=self.job_id,
            case_id=self.request.case_id,
            status=self.status,
            stage=self.stage,
            attempts=self.attempts,
            created_at=datetime.fromtimestamp(self.created_at, UTC),
            updated_at=datetime.fromtimestamp(self.updated_at, UTC),
            result=self.result,
            error_code=self.error_code,
        )
