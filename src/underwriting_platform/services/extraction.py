"""Document extraction service; only this deployment receives provider keys."""

from fastapi import Depends

from underwriting_platform.application.ports import ExtractionPort
from underwriting_platform.configuration.settings import Settings
from underwriting_platform.contracts.v1 import ExtractionRequest, ExtractionResponse
from underwriting_platform.infrastructure.kernel import KernelExtractor
from underwriting_platform.services.common import authentication, create_service


def build_app(settings: Settings | None = None, adapter: ExtractionPort | None = None):
    settings = settings or Settings.from_env()
    adapter = adapter or KernelExtractor(settings)
    app = create_service(
        "extraction", ready=lambda: bool(settings.service_token) and settings.case_root.is_dir()
    )

    @app.post(
        "/v1/extractions",
        response_model=ExtractionResponse,
        dependencies=[Depends(authentication(settings.service_token))],
    )
    async def extract(request: ExtractionRequest):
        return await adapter.extract(request)

    @app.get(
        "/v1/extractions/{case_id}/{revision}/evidence",
        dependencies=[Depends(authentication(settings.service_token))],
    )
    async def evidence(case_id: str, revision: str):
        import asyncio

        from underwriting_platform.infrastructure.evidence import export_evidence

        return await asyncio.to_thread(
            export_evidence, settings.extraction_cache, case_id, revision
        )

    return app
