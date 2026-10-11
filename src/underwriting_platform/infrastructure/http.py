"""HTTP adapters implement ports; failures never masquerade as successful stages."""

from __future__ import annotations

import httpx
from pydantic import ValidationError

from underwriting_platform.application.errors import (
    DependencyUnavailable,
    InvalidRequest,
    MissingCase,
    ProcessingRejected,
    QueueFull,
)
from underwriting_platform.configuration.settings import Settings
from underwriting_platform.contracts.v1 import (
    ArtifactResponse,
    DecisionRequest,
    DecisionResponse,
    ExtractionRequest,
    ExtractionResponse,
)


class HttpStages:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    def headers(self) -> dict[str, str]:
        from underwriting_platform.application.context import request_id

        return {
            "Authorization": f"Bearer {self.settings.service_token}",
            "X-Request-ID": request_id.get(),
        }

    async def _post(self, url: str, payload: dict, model: type):
        try:
            response = await self.client.post(
                url,
                json=payload,
                headers=self.headers(),
                timeout=self.settings.stage_timeout_seconds,
            )
            if response.status_code == 404:
                raise MissingCase()
            if response.status_code == 422:
                try:
                    code = response.json().get("code")
                except ValueError:
                    code = None
                if code == "PROCESSING_REJECTED":
                    raise ProcessingRejected()
                raise InvalidRequest()
            if response.status_code == 429:
                raise QueueFull()
            if response.status_code != 200:
                raise DependencyUnavailable()
            return model.model_validate(response.json())
        except (httpx.HTTPError, ValidationError, ValueError) as exc:
            raise DependencyUnavailable() from exc

    async def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        return await self._post(
            self.settings.extraction_url + "/v1/extractions",
            request.model_dump(mode="json"),
            ExtractionResponse,
        )

    async def decide(self, request: DecisionRequest) -> DecisionResponse:
        return await self._post(
            self.settings.underwriting_url + "/v1/decisions",
            request.model_dump(mode="json"),
            DecisionResponse,
        )

    async def render(self, request: DecisionResponse) -> ArtifactResponse:
        return await self._post(
            self.settings.artifacts_url + "/v1/case-sheets",
            request.model_dump(mode="json"),
            ArtifactResponse,
        )
