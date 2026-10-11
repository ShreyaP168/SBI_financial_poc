"""Public API v1. Domain payloads retain the existing case/decision schema."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

JsonObject = dict[str, Any]
CASE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
ARTIFACT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"


class CaseRequest(Contract):
    case_id: str = Field(pattern=CASE_PATTERN)
    proposed_sa: int | None = Field(default=None, gt=0, strict=True)


class ExtractionRequest(Contract):
    case_id: str = Field(pattern=CASE_PATTERN)


class ExtractionResponse(Contract):
    case_id: str = Field(pattern=CASE_PATTERN)
    input_fingerprint: str
    case_json: JsonObject
    extraction_report: JsonObject

    @model_validator(mode="after")
    def validate_identity(self) -> ExtractionResponse:
        if self.case_json.get("case_id") != self.case_id:
            raise ValueError("Case identity differs from extraction envelope")
        return self


class DecisionRequest(CaseRequest):
    case_json: JsonObject

    @model_validator(mode="after")
    def validate_identity(self) -> DecisionRequest:
        if self.case_json.get("case_id") != self.case_id:
            raise ValueError("Case identity differs from decision request")
        return self


class DecisionResponse(Contract):
    case_id: str = Field(pattern=CASE_PATTERN)
    decision: JsonObject

    @model_validator(mode="after")
    def validate_decision(self) -> DecisionResponse:
        if self.decision.get("case_id") != self.case_id:
            raise ValueError("Case identity differs from decision envelope")
        labels = {
            "STANDARD_ACCEPTANCE",
            "STANDARD_ISSUANCE",
            "FURTHER_REQUIREMENT",
            "CONDITIONAL",
            "REFER",
            "DECLINE",
            "INCOMPLETE",
            "ERROR",
        }
        if (
            not isinstance(self.decision.get("verdict"), dict)
            or self.decision["verdict"].get("label") not in labels
        ):
            raise ValueError("Unknown or missing underwriting verdict")
        return self


class ArtifactResponse(Contract):
    case_id: str = Field(pattern=CASE_PATTERN)
    artifact_id: str = Field(pattern=ARTIFACT_PATTERN)
    qc_status: Literal["PASS", "WARN", "BLOCK"]
    qc_summary: dict[str, int]
    download_path: str
    diagnostic_download_path: str | None = None
    delivery_allowed: bool | None = None
    diagnostic_verdict: str | None = None


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class WorkflowResult(Contract):
    case_id: str
    verdict: str
    artifact: ArtifactResponse
    completion_policy: str | None = None


class JobResponse(Contract):
    job_id: str
    case_id: str
    status: JobStatus
    stage: str
    attempts: int
    created_at: datetime
    updated_at: datetime
    result: WorkflowResult | None = None
    error_code: str | None = None


class Problem(Contract):
    code: str
    message: str
    request_id: str
