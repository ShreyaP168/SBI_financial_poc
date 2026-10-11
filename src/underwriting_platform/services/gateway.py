"""Public workflow API only enqueues work and reports durable job state."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, Header, HTTPException
from fastapi.responses import Response

from underwriting_platform.application.errors import ArtifactBlocked, DependencyUnavailable
from underwriting_platform.application.job_port import JobRepository
from underwriting_platform.configuration.settings import Settings
from underwriting_platform.contracts.v1 import CaseRequest, JobResponse
from underwriting_platform.infrastructure.jobs import RelationalJobRepository
from underwriting_platform.services.common import authentication, create_service


def build_app(
    settings: Settings | None = None,
    repository: JobRepository | None = None,
    download_client: httpx.AsyncClient | None = None,
):
    settings = settings or Settings.from_env()
    repository = repository or RelationalJobRepository(
        settings.database_url, settings.queue_capacity
    )

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(repository.initialize)
        async with httpx.AsyncClient(timeout=30) as client:
            app.state.download_client = download_client or client
            yield

    def ready() -> bool:
        return bool(settings.gateway_token) and repository.ready()

    app = create_service("gateway", lifespan=lifespan, ready=ready)
    protected = [Depends(authentication(settings.gateway_token))]

    @app.get("/v1/operations/queue", dependencies=protected)
    async def queue_metrics():
        return await asyncio.to_thread(repository.queue_metrics)

    @app.post("/v1/runs", status_code=202, response_model=JobResponse, dependencies=protected)
    async def submit(
        request: CaseRequest, idempotency_key: str = Header(min_length=1, max_length=128)
    ):
        if not idempotency_key.strip():
            raise HTTPException(422, "Idempotency-Key must not be blank")
        job = await asyncio.to_thread(repository.create, request, idempotency_key)
        # Idempotent resubmissions can return an already completed job; apply the
        # same public masking policy as status polling.
        return await status(uuid.UUID(job.job_id))

    @app.get("/v1/runs/{job_id}", response_model=JobResponse, dependencies=protected)
    async def status(job_id: uuid.UUID):
        job = await asyncio.to_thread(repository.get, str(job_id))
        if job is None:
            raise HTTPException(404, "Job not found")
        response = job.response()
        if response.result is not None:
            response.result.artifact.diagnostic_verdict = None
            response.result.artifact.diagnostic_download_path = None
            from underwriting_platform.application.completion import POLICY_VERSION

            checkpoint = job.checkpoints.get("decision") or {}
            completion = (
                checkpoint.get("completion")
                or (checkpoint.get("decision") or {}).get("_completion")
                or {}
            )
            if (
                not completion.get("decision_complete")
                or completion.get("policy_version") != POLICY_VERSION
            ):
                from underwriting_platform.contracts.v1 import JobStatus

                if response.status in {JobStatus.SUCCEEDED, JobStatus.INCOMPLETE}:
                    response.status = JobStatus.INCOMPLETE
                response.result.verdict = "INCOMPLETE"
        return response

    @app.get("/v1/runs/{job_id}/case-sheet", dependencies=protected)
    async def download(job_id: uuid.UUID):
        job = await asyncio.to_thread(repository.get, str(job_id))
        if job is None:
            raise HTTPException(404, "Job not found")
        if job.result is None:
            raise HTTPException(409, "Artifact is not yet available")
        from underwriting_platform.application.completion import POLICY_VERSION
        from underwriting_platform.contracts.v1 import JobStatus

        summary = job.checkpoints.get("decision") or {}
        completion = (
            summary.get("completion") or (summary.get("decision") or {}).get("_completion") or {}
        )
        if (
            job.status != JobStatus.SUCCEEDED
            or job.result.verdict in {"INCOMPLETE", "ERROR"}
            or job.result.artifact.delivery_allowed is False
            or not completion.get("decision_complete")
            or completion.get("policy_version") != POLICY_VERSION
            or job.result.artifact.qc_status == "BLOCK"
        ):
            raise ArtifactBlocked()
        path = f"/v1/artifacts/{job.result.artifact.artifact_id}/case-sheet"
        try:
            response = await app.state.download_client.get(
                settings.artifacts_url + path,
                headers={"Authorization": f"Bearer {settings.service_token}"},
            )
            if response.status_code == 409:
                raise ArtifactBlocked()
            if response.status_code != 200:
                raise DependencyUnavailable()
            return Response(
                content=response.content,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f'attachment; filename="{job.request.case_id}_case_sheet.pdf"'
                },
            )
        except httpx.HTTPError as exc:
            raise DependencyUnavailable() from exc

    @app.get(
        "/v1/runs/{job_id}/review-case-sheet",
        dependencies=[Depends(authentication(settings.reviewer_token))],
    )
    async def review(job_id: uuid.UUID):
        job = await asyncio.to_thread(repository.get, str(job_id))
        if job is None:
            raise HTTPException(404, "Job not found")
        if job.result is None:
            raise HTTPException(409, "Review artifact is not yet available")
        path = f"/v1/artifacts/{job.result.artifact.artifact_id}/review-case-sheet"
        try:
            response = await app.state.download_client.get(
                settings.artifacts_url + path,
                headers={"Authorization": f"Bearer {settings.reviewer_token}"},
            )
        except httpx.HTTPError:
            raise DependencyUnavailable() from None
        if response.status_code == 404:
            raise HTTPException(404, "Review artifact not found")
        if response.status_code != 200:
            raise DependencyUnavailable()
        return Response(
            content=response.content,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{job.request.case_id}_review_only.pdf"',
                "Cache-Control": "no-store",
                "X-UW-Review-Only": "true",
            },
        )

    return app
