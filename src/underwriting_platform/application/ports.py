"""Small interfaces enable in-process tests and independently deployed adapters."""

from __future__ import annotations

from typing import Protocol

from underwriting_platform.contracts.v1 import (
    ArtifactResponse,
    DecisionRequest,
    DecisionResponse,
    ExtractionRequest,
    ExtractionResponse,
)


class ExtractionPort(Protocol):
    async def extract(self, request: ExtractionRequest) -> ExtractionResponse: ...


class UnderwritingPort(Protocol):
    async def decide(self, request: DecisionRequest) -> DecisionResponse: ...


class ArtifactPort(Protocol):
    async def render(self, request: DecisionResponse) -> ArtifactResponse: ...
