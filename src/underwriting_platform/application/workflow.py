"""Resume a workflow at its last committed stage checkpoint."""

from __future__ import annotations

import asyncio

from underwriting_platform.application.errors import ExtractionFailed, InvalidRequest
from underwriting_platform.application.job_port import JobRepository
from underwriting_platform.application.ports import ArtifactPort, ExtractionPort, UnderwritingPort
from underwriting_platform.contracts.v1 import (
    ArtifactResponse,
    DecisionRequest,
    DecisionResponse,
    ExtractionRequest,
    ExtractionResponse,
    JobStatus,
    WorkflowResult,
)
from underwriting_platform.domain.jobs import Job


class Workflow:
    def __init__(
        self,
        repository: JobRepository,
        extraction: ExtractionPort,
        underwriting: UnderwritingPort,
        artifacts: ArtifactPort,
    ):
        self.repository = repository
        self.extraction = extraction
        self.underwriting = underwriting
        self.artifacts = artifacts

    @staticmethod
    def _same_case(expected: str, actual: str) -> None:
        if expected != actual:
            raise InvalidRequest()

    async def execute(self, job: Job, owner: str) -> WorkflowResult:
        case_id = job.request.case_id
        checkpoints = job.checkpoints
        if "extraction" in checkpoints:
            extraction = ExtractionResponse.model_validate(checkpoints["extraction"])
        else:
            extraction = await self.extraction.extract(ExtractionRequest(case_id=case_id))
            self._same_case(case_id, extraction.case_id)
            await asyncio.to_thread(
                self.repository.checkpoint,
                job.job_id,
                owner,
                "extraction_partial"
                if extraction.case_json.get("processing_errors")
                else "extraction",
                {**extraction.model_dump(mode="json"), "extraction_report": {}},
            )
        self._same_case(case_id, extraction.case_id)

        partial = bool(extraction.case_json.get("processing_errors"))
        if "decision" in checkpoints and not partial:
            decision = DecisionResponse.model_validate(checkpoints["decision"])
        else:
            decision = await self.underwriting.decide(
                DecisionRequest(
                    case_id=case_id,
                    proposed_sa=job.request.proposed_sa,
                    case_json=extraction.case_json,
                )
            )
            self._same_case(case_id, decision.case_id)
            from underwriting_platform.application.completion import assess_decision

            decision.decision["_completion"] = assess_decision(
                extraction.case_json, decision.decision
            )
            await asyncio.to_thread(
                self.repository.checkpoint,
                job.job_id,
                owner,
                "decision_partial" if partial else "decision",
                decision.model_dump(mode="json"),
            )
        self._same_case(case_id, decision.case_id)
        from underwriting_platform.application.completion import assess_decision

        decision.decision["_completion"] = assess_decision(extraction.case_json, decision.decision)

        if "artifact" in checkpoints and not partial:
            artifact = ArtifactResponse.model_validate(checkpoints["artifact"])
        else:
            artifact = await self.artifacts.render(decision)
            self._same_case(case_id, artifact.case_id)
            await asyncio.to_thread(
                self.repository.checkpoint,
                job.job_id,
                owner,
                "artifact_partial" if partial else "artifact",
                artifact.model_dump(mode="json"),
            )
        self._same_case(case_id, artifact.case_id)
        verdict = decision.decision["verdict"]["label"]
        complete = (decision.decision.get("_completion") or {}).get("decision_complete") is True
        if not complete or artifact.qc_status == "BLOCK" or artifact.delivery_allowed is False:
            verdict = "INCOMPLETE"
        from underwriting_platform.application.completion import POLICY_VERSION

        result = WorkflowResult(
            case_id=case_id, verdict=verdict, artifact=artifact, completion_policy=POLICY_VERSION
        )
        status = (
            JobStatus.INCOMPLETE
            if verdict in {"INCOMPLETE", "ERROR"} or artifact.qc_status == "BLOCK"
            else JobStatus.SUCCEEDED
        )
        if any(
            error.get("retryable") is True
            for error in extraction.case_json.get("processing_errors") or []
        ):
            await asyncio.to_thread(self.repository.record_partial, job.job_id, owner, result)
            pending = ExtractionFailed()
            pending.partial_result = result
            raise pending
        await asyncio.to_thread(self.repository.finish, job.job_id, owner, status, result)
        return result
