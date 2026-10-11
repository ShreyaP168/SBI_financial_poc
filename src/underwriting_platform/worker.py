"""Dedicated worker entry point; API processes never run long workflows inline."""

from __future__ import annotations

import asyncio
import signal

import httpx

from underwriting_platform.application.worker import Worker
from underwriting_platform.application.workflow import Workflow
from underwriting_platform.configuration.settings import Settings
from underwriting_platform.infrastructure.http import HttpStages
from underwriting_platform.infrastructure.jobs import RelationalJobRepository
from underwriting_platform.services.common import configure_logging


async def run() -> None:
    from underwriting_platform.configuration.secrets import load_secret_environment

    load_secret_environment()
    settings = Settings.from_env()
    if not settings.service_token:
        raise ValueError("UW_SERVICE_TOKEN is required")
    repository = RelationalJobRepository(settings.database_url, settings.queue_capacity)
    await asyncio.to_thread(repository.initialize)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass  # Windows console cancellation is handled by asyncio.run.
    async with httpx.AsyncClient() as client:
        stages = HttpStages(settings, client)
        workflow = Workflow(repository, stages, stages, stages)
        await Worker(repository, workflow, settings).run_forever(stop)


def main() -> None:
    configure_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
