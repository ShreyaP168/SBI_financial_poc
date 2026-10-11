"""Uvicorn factory selects a single bounded service per process."""

from underwriting_platform.configuration.settings import Settings
from underwriting_platform.services.common import configure_logging


def create_app():
    from underwriting_platform.configuration.secrets import load_secret_environment

    load_secret_environment()
    from underwriting_platform.services import artifacts, extraction, gateway, underwriting

    configure_logging()
    settings = Settings.from_env()
    factories = {
        "gateway": gateway.build_app,
        "extraction": extraction.build_app,
        "underwriting": underwriting.build_app,
        "artifacts": artifacts.build_app,
    }
    if settings.role not in factories:
        raise ValueError("Unknown service role")
    return factories[settings.role](settings)


def main() -> None:
    import os

    import uvicorn

    uvicorn.run(
        "underwriting_platform.services.main:create_app",
        factory=True,
        host=os.getenv("UW_BIND_HOST", "0.0.0.0"),
        port=int(os.getenv("UW_BIND_PORT", "8000")),
        access_log=False,
    )
