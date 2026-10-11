"""Lease renewal prevents long-running extraction jobs being claimed twice."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Protocol

from underwriting_platform.application.context import request_id
from underwriting_platform.application.errors import (
    ApplicationError,
    DependencyUnavailable,
    LeaseLost,
)
from underwriting_platform.application.job_port import JobRepository
from underwriting_platform.application.workflow import Workflow
from underwriting_platform.contracts.v1 import JobStatus

logger = logging.getLogger(__name__)


class WorkerSettings(Protocol):
    lease_seconds: int
    max_attempts: int
    poll_seconds: float


class Worker:
    def __init__(self, repository: JobRepository, workflow: Workflow, settings: WorkerSettings):
        self.repository = repository
        self.workflow = workflow
        self.settings = settings
        self.owner = str(uuid.uuid4())

    async def _heartbeat(self, job_id: str) -> None:
        while True:
            await asyncio.sleep(self.settings.lease_seconds / 3)
            try:
                await asyncio.to_thread(
                    self.repository.renew, job_id, self.owner, self.settings.lease_seconds
                )
            except LeaseLost:
                raise
            except Exception:
                # Stop lease-owned publication; retry/reclaim resumes persisted stages.
                raise DependencyUnavailable() from None

    async def run_once(self) -> bool:
        job = await asyncio.to_thread(
            self.repository.claim,
            self.owner,
            self.settings.lease_seconds,
            self.settings.max_attempts,
        )
        if job is None:
            return False
        correlation = request_id.set(job.job_id)
        logger.info(
            "Job claimed",
            extra={
                "request_id": job.job_id,
                "queue_age_seconds": round(time.time() - job.created_at, 3),
                "attempt": job.attempts,
            },
        )
        operation = asyncio.create_task(self.workflow.execute(job, self.owner))
        heartbeat = asyncio.create_task(self._heartbeat(job.job_id))
        try:
            done, _ = await asyncio.wait(
                {operation, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in done:
                await heartbeat
            await operation
        except LeaseLost:
            logger.warning("Job lease lost", extra={"job_id": job.job_id})
        except Exception as exc:
            code = exc.code if isinstance(exc, ApplicationError) else "PROCESSING_FAILED"
            retry = (
                isinstance(exc, ApplicationError)
                and exc.retryable
                and job.attempts < self.settings.max_attempts
            )
            logger.error("Workflow stage failed", extra={"job_id": job.job_id, "error_code": code})
            try:
                if not retry and getattr(exc, "partial_result", None) is not None:
                    await asyncio.to_thread(
                        self.repository.finish,
                        job.job_id,
                        self.owner,
                        JobStatus.INCOMPLETE,
                        exc.partial_result,
                    )
                else:
                    await asyncio.to_thread(
                        self.repository.fail, job.job_id, self.owner, code, retry
                    )
            except Exception:
                # A database outage may prevent fail(). Leave the lease to expire;
                # claim() recovers it with the bounded attempt budget.
                logger.warning(
                    "Failure persistence unavailable",
                    extra={"job_id": job.job_id, "error_code": "JOB_STATE_UNAVAILABLE"},
                )
        finally:
            operation.cancel()
            heartbeat.cancel()
            await asyncio.gather(operation, heartbeat, return_exceptions=True)
            request_id.reset(correlation)
        return True

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                worked = await self.run_once()
                if worked:
                    continue
            except Exception:
                logger.error(
                    "Worker dependency unavailable", extra={"error_code": "JOB_STATE_UNAVAILABLE"}
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.poll_seconds)
            except TimeoutError:
                pass
