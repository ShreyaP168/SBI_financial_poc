"""PDF/QC service owns artifact storage and its delivery gate."""

from fastapi import Depends
from fastapi.responses import FileResponse, Response

from underwriting_platform.application.errors import ArtifactBlocked, MissingCase
from underwriting_platform.application.ports import ArtifactPort
from underwriting_platform.configuration.settings import Settings
from underwriting_platform.contracts.v1 import ArtifactResponse, DecisionResponse
from underwriting_platform.infrastructure.files import scoped_path
from underwriting_platform.infrastructure.kernel import KernelArtifacts
from underwriting_platform.services.common import authentication, create_service


def build_app(settings: Settings | None = None, adapter: ArtifactPort | None = None):
    settings = settings or Settings.from_env()
    adapter = adapter or KernelArtifacts(settings)
    app = create_service("artifacts", ready=lambda: bool(settings.service_token))
    protected = [Depends(authentication(settings.service_token))]

    @app.post("/v1/case-sheets", response_model=ArtifactResponse, dependencies=protected)
    async def render(request: DecisionResponse):
        return await adapter.render(request)

    @app.get("/v1/artifacts/{artifact_id}/case-sheet", dependencies=protected)
    async def download(artifact_id: str):
        folder = scoped_path(settings.artifact_root, artifact_id, artifact=True)
        manifest = folder / "manifest.json"
        if not manifest.is_file():
            raise MissingCase()
        metadata = ArtifactResponse.model_validate_json(manifest.read_text(encoding="utf8"))
        if metadata.qc_status == "BLOCK":
            raise ArtifactBlocked()
        import json

        from underwriting_platform.application.completion import assess_decision

        decision = json.loads((folder / "decision.json").read_text(encoding="utf8"))
        quality = json.loads((folder / "qc.json").read_text(encoding="utf8"))
        if (
            not assess_decision(decision.get("_normalized_input") or {}, decision)[
                "decision_complete"
            ]
            or not quality.get("decision_complete")
            or any(
                f.get("code") == "QC_INFRASTRUCTURE_FAILURE" for f in quality.get("findings") or []
            )
        ):
            raise ArtifactBlocked()
        return FileResponse(
            folder / "case-sheet.pdf",
            media_type="application/pdf",
            filename=f"{metadata.case_id}_case_sheet.pdf",
        )

    @app.get(
        "/v1/artifacts/{artifact_id}/review-case-sheet",
        dependencies=[Depends(authentication(settings.reviewer_token))],
    )
    async def review(artifact_id: str):
        from underwriting_platform.infrastructure.review_artifact import review_pdf

        folder = scoped_path(settings.artifact_root, artifact_id, artifact=True)
        if not (folder / "manifest.json").is_file() or not (folder / "case-sheet.pdf").is_file():
            raise MissingCase()
        return Response(
            content=review_pdf(folder / "case-sheet.pdf"),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{artifact_id}_review_only.pdf"',
                "Cache-Control": "no-store",
                "X-UW-Review-Only": "true",
            },
        )

    return app
