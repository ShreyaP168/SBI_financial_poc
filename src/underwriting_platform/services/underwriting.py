"""Deterministic underwriting service; no model-provider dependency at runtime."""

import json

from fastapi import Depends

from underwriting_platform.application.ports import UnderwritingPort
from underwriting_platform.configuration.settings import Settings
from underwriting_platform.contracts.v1 import DecisionRequest, DecisionResponse
from underwriting_platform.infrastructure.kernel import KernelUnderwriter
from underwriting_platform.services.common import authentication, create_service


def build_app(settings: Settings | None = None, adapter: UnderwritingPort | None = None):
    settings = settings or Settings.from_env()
    adapter = adapter or KernelUnderwriter(settings)

    def ready() -> bool:
        from underwriting_platform.engine.rule_catalog import validate_sbi_rules

        if not settings.service_token:
            return False
        validate_sbi_rules(
            json.loads(
                (settings.config_root / "sbi_smart_shield_rules.json").read_text(encoding="utf8")
            )
        )
        json.loads((settings.config_root / "uw_rules.json").read_text(encoding="utf8"))
        return True

    app = create_service("underwriting", ready=ready)

    @app.post(
        "/v1/decisions",
        response_model=DecisionResponse,
        dependencies=[Depends(authentication(settings.service_token))],
    )
    async def decide(request: DecisionRequest):
        return await adapter.decide(request)

    return app
