"""Immutable, environment-driven configuration; secrets never appear in repr."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from underwriting_platform.configuration.paths import CASE_ROOT, CONFIG_ROOT, RUNTIME_ROOT


@dataclass(frozen=True)
class Settings:
    deployment_mode: str = "controlled_poc"
    queue_capacity: int = 100
    role: str = "gateway"
    service_token: str = field(default="", repr=False)
    gateway_token: str = field(default="", repr=False)
    database_url: str = field(default="sqlite:///" + str(RUNTIME_ROOT / "jobs.sqlite3"), repr=False)
    case_root: Path = CASE_ROOT
    artifact_root: Path = RUNTIME_ROOT / "artifacts"
    extraction_cache: Path = RUNTIME_ROOT / "extractions"
    config_root: Path = CONFIG_ROOT
    extraction_url: str = "http://127.0.0.1:8001"
    underwriting_url: str = "http://127.0.0.1:8002"
    artifacts_url: str = "http://127.0.0.1:8003"
    stage_timeout_seconds: float = 1800
    lease_seconds: int = 60
    poll_seconds: float = 1
    max_attempts: int = 3

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            deployment_mode=os.getenv("UW_DEPLOYMENT_MODE", "controlled_poc"),
            queue_capacity=int(os.getenv("UW_QUEUE_CAPACITY", "100")),
            role=os.getenv("UW_SERVICE_ROLE", "gateway"),
            service_token=os.getenv("UW_SERVICE_TOKEN", ""),
            gateway_token=os.getenv("UW_GATEWAY_TOKEN", ""),
            database_url=os.getenv(
                "UW_DATABASE_URL", "sqlite:///" + str(RUNTIME_ROOT / "jobs.sqlite3")
            ),
            case_root=Path(os.getenv("UW_CASE_ROOT", str(cls.case_root))).resolve(),
            artifact_root=Path(os.getenv("UW_ARTIFACT_ROOT", str(cls.artifact_root))).resolve(),
            extraction_cache=Path(
                os.getenv("UW_EXTRACTION_CACHE", str(cls.extraction_cache))
            ).resolve(),
            config_root=Path(os.getenv("UW_CONFIG_ROOT", str(CONFIG_ROOT))).resolve(),
            extraction_url=os.getenv("UW_EXTRACTION_URL", cls.extraction_url),
            underwriting_url=os.getenv("UW_UNDERWRITING_URL", cls.underwriting_url),
            artifacts_url=os.getenv("UW_ARTIFACTS_URL", cls.artifacts_url),
            stage_timeout_seconds=float(os.getenv("UW_STAGE_TIMEOUT_SECONDS", "1800")),
            lease_seconds=int(os.getenv("UW_LEASE_SECONDS", "60")),
            poll_seconds=float(os.getenv("UW_POLL_SECONDS", "1")),
            max_attempts=int(os.getenv("UW_MAX_ATTEMPTS", "3")),
        )

    def __post_init__(self) -> None:
        if self.deployment_mode != "controlled_poc":
            raise ValueError(
                "Production deployment is blocked until tenant authorization and data lifecycle prerequisites are implemented and verified"
            )
        if self.queue_capacity < 1:
            raise ValueError("UW_QUEUE_CAPACITY must be positive")
        if (
            self.lease_seconds < 6
            or self.max_attempts < 1
            or self.stage_timeout_seconds <= 0
            or self.poll_seconds <= 0
        ):
            raise ValueError("Invalid timeout, lease or retry configuration")
