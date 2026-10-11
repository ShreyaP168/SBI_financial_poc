"""Complete declared extraction dependencies, including optional joined evidence."""

import hashlib
import json
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

EXTERNAL_SOURCES = (
    "POLICY_DATA_XLSX",
    "SBI_IIB_DOCX",
    "SBI_SCORES_XLSX",
    "SBI_SCORES_XLSX_TABULAR",
    "SBI_IIB_XLSX",
    "SBI_BUREAU_DATA_JSON",
    "IIB_DATA_JSON",
    "ALPHA_POLICY_JSON",
)
PROVIDER_SETTINGS = (
    "LLM_BACKEND",
    "ANTHROPIC_MODEL",
    "AZURE_CLAUDE_DEPLOYMENT",
    "AZURE_CLAUDE_ENDPOINT",
    "AZURE_API_VERSION",
    "AZURE_CLAUDE_HAIKU_DEPLOYMENT",
    "GEMINI_MEDICAL_MODEL",
    "UW_DUAL_VENDOR_MEDICAL",
    "UW_EXTRACTION_MODE",
    "GEMINI_VITAL_REOCR_MODEL",
    "GEMINI_FAMILY_CHECKBOX_MODEL",
    "GEMINI_PHOTO_MATCH_MODEL",
    "VITAL_REOCR_MODEL",
    "MAX_CONCURRENT_LLM",
    "MAX_CONCURRENT_GEMINI",
    "LLM_MAX_RETRIES",
)


def evidence_paths(config_root: Path) -> dict[str, Path]:
    paths = {key: Path(os.environ[key]).resolve() for key in EXTERNAL_SOURCES if os.getenv(key)}

    def visit(value, pointer):
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, pointer + "/" + key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, pointer + "/" + str(index))
        elif isinstance(value, str) and Path(value).suffix.lower() in {
            ".pdf",
            ".docx",
            ".xlsx",
            ".json",
            ".png",
            ".jpg",
        }:
            paths[pointer] = Path(value).resolve()

    for source in sorted(config_root.rglob("*.json")):
        visit(
            json.loads(source.read_text(encoding="utf8")),
            "config:" + str(source.relative_to(config_root)),
        )
    return paths


def cache_revision(
    input_digest: str, config_root: Path, *, package_root: Path | None = None
) -> str:
    revision = hashlib.sha256(("extraction-2:" + input_digest).encode())
    from underwriting_platform.configuration import local_ocr

    revision.update(
        str(
            (
                local_ocr.provider(),
                local_ocr.get("OCR_PSM"),
                local_ocr.get("OCR_LANGUAGE"),
                local_ocr.get("OCR_TIMEOUT_SECONDS"),
                os.getenv("TESSDATA_PREFIX"),
            )
        ).encode()
    )
    for asset in local_ocr.assets():
        revision.update(str(asset).encode())
        revision.update(asset.read_bytes())
    for name in ("rapidocr-onnxruntime", "onnxruntime", "numpy", "opencv-python"):
        try:
            revision.update(f"{name}:{version(name)}".encode())
        except PackageNotFoundError:
            revision.update(f"{name}:not-installed".encode())
    package = package_root or Path(__file__).resolve().parents[1]
    # Include implementation as well as prompts: parser changes invalidate old joins.
    roots = [
        config_root,
        *(package / name for name in ("agents", "orchestrator", "llm", "configuration", "ocr")),
    ]
    for root in roots:
        for source in sorted(root.rglob("*")):
            if source.is_file() and source.suffix in {".json", ".txt", ".py"}:
                if source.name.startswith("brand_profile") or source.name in {
                    "settings.py",
                    "secrets.py",
                }:
                    continue
                revision.update(str(source.relative_to(root)).encode())
                revision.update(source.read_bytes())
    readers = (
        "reference_cache",
        "policy_data",
        "scores_xlsx",
        "sbi_iib_xlsx",
        "sbi_bureau",
        "iib",
        "alpha_policy",
        "mer_template_processor",
        "handwritten_reocr",
        "family_checkbox",
        "medical_dual_vendor",
        "identity_verification",
        "photo_match",
        "geocoding",
        "geo_utils",
    )
    for name in readers:
        source = package / "engine" / f"{name}.py"
        if source.is_file():
            revision.update(name.encode())
            revision.update(source.read_bytes())
    source = package / "domain/policy.py"
    if source.is_file():
        revision.update(source.read_bytes())
    for key in EXTERNAL_SOURCES:
        value = os.getenv(key)
        revision.update(key.encode())
        revision.update((value or "UNCONFIGURED").encode())
        if value:
            path = Path(value).resolve()
            revision.update(str(path).encode())
            revision.update(path.read_bytes() if path.is_file() else b"MISSING")
    for key, path in sorted(evidence_paths(config_root).items()):
        revision.update(key.encode())
        revision.update(str(path).encode())
        revision.update(path.read_bytes() if path.is_file() else b"MISSING")
    values = {key: os.getenv(key, "") for key in PROVIDER_SETTINGS}
    for key in ("GEMINI_API_KEY", "GOOGLE_MAPS_API_KEY"):
        values[key] = bool(os.getenv(key))
    revision.update(json.dumps(values, sort_keys=True).encode())
    return revision.hexdigest()
