"""Retain immutable inputs and the exact executable policy assets per run."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from underwriting_platform import __version__
from underwriting_platform.configuration.paths import CONFIG_ROOT
from underwriting_platform.infrastructure.files import atomic_json, native_path

PARSER_VERSION = "canonical-extraction-1.1"


def retain_blob(data: bytes, target: Path, store: Path) -> str:
    """Content-addressed objects with hardlinked bundle views on local storage."""
    if os.name == "nt":

        def extended(path):
            value = str(path.absolute())
            if value.startswith("\\\\?\\"):
                return path
            return Path(
                "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
            )

        target, store = extended(target), extended(store)
    digest = hashlib.sha256(data).hexdigest()
    store.mkdir(parents=True, exist_ok=True)
    blob = store / digest
    if not blob.is_file():
        descriptor, temporary = tempfile.mkstemp(dir=store, prefix=".pending-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, blob)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if hashlib.sha256(blob.read_bytes()).hexdigest() != digest:
        raise ValueError("Retained object integrity failure")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(blob, target)
    except OSError:
        target.write_bytes(data)
    return digest


def retain_inputs(
    folder: Path,
    case: dict,
    *,
    raw_input=None,
    extraction_report=None,
    rules: dict | None = None,
    provider: dict | None = None,
    config_root: Path = CONFIG_ROOT,
    source_directory: Path | None = None,
    external_directory: Path | None = None,
) -> dict:
    run_id = uuid.uuid4().hex
    logical_bundle = folder / "reproduction" / run_id
    bundle = logical_bundle
    if os.name == "nt":
        name = str(logical_bundle.absolute())
        bundle = Path("\\\\?\\UNC\\" + name[2:] if name.startswith("\\\\") else "\\\\?\\" + name)
    bundle.mkdir(parents=True, exist_ok=True)
    store = folder.parent / ".retained-objects"
    if os.name == "nt":
        store = Path("\\\\?\\" + str(store.absolute()))
    normalized = json.loads(json.dumps(case, allow_nan=False, default=str))
    atomic_json(bundle / "normalized_input.json", normalized)
    atomic_json(
        bundle / "raw_input.json",
        raw_input if isinstance(raw_input, dict) else {"input": raw_input},
    )
    atomic_json(bundle / "extraction_report.json", extraction_report or {})
    if rules is not None:
        atomic_json(bundle / "effective_uw_rules.json", rules)
    document_hashes = {}
    reference_hashes = {}
    if source_directory is not None:
        source_directory = native_path(Path(source_directory))
    if external_directory is not None:
        external_directory = native_path(Path(external_directory))
    if external_directory is not None and Path(external_directory).is_dir():
        root = Path(external_directory).resolve()
        for source in root.rglob("*"):
            if source.is_file():
                if not source.resolve().is_relative_to(root):
                    raise ValueError("External evidence escaped snapshot directory")
                relative = source.relative_to(root)
                reference_hashes[str(relative)] = retain_blob(
                    source.read_bytes(), bundle / "references" / relative, store
                )
    elif external_directory is None:
        from underwriting_platform.configuration.extraction import evidence_paths

        for key, source in evidence_paths(config_root).items():
            if source.is_file():
                safe_key = hashlib.sha256(key.encode()).hexdigest()
                reference_hashes[key] = retain_blob(
                    source.read_bytes(), bundle / "references" / safe_key / source.name, store
                )
    if source_directory is not None and Path(source_directory).is_dir():
        root = Path(source_directory).resolve()
        supported = {".pdf", ".zip", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
        for source in sorted(root.iterdir()):
            if not source.is_file() or source.suffix.lower() not in supported:
                continue
            if not source.resolve().is_relative_to(root):
                raise ValueError("Source document escaped case directory")
            target = bundle / "documents" / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            data = source.read_bytes()
            document_hashes[source.name] = retain_blob(data, target, store)
    from underwriting_platform import agents, engine, qc, renderer

    files = {}
    sources = list(config_root.glob("*.json"))
    sources += list((Path(agents.__path__[0]) / "prompts").glob("*.txt"))
    sources += [
        p
        for module in (engine, qc, renderer, agents)
        for p in Path(module.__path__[0]).rglob("*.py")
    ]
    sources += list(Path(__file__).resolve().parents[1].rglob("*.py"))
    sources = sorted(set(sources))
    for source in sources:
        kind = (
            "config"
            if source.parent == config_root
            else "prompts"
            if source.suffix == ".txt"
            else source.parent.name
        )
        relative = f"assets/{kind}/{source.name}"
        target = bundle / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        data = source.read_bytes()
        files[relative] = retain_blob(data, target, store)
    dependencies = {}
    for name in ("anthropic", "google-genai", "pymupdf", "reportlab", "openpyxl", "pydantic"):
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = "not_installed"
    manifest = {
        "run_id": run_id,
        "bundle_path": str(logical_bundle.relative_to(folder)),
        "platform_version": __version__,
        "parser_version": PARSER_VERSION,
        "python_version": sys.version,
        "dependencies": dependencies,
        "source_document_versions": document_hashes,
        "external_evidence_versions": reference_hashes,
        "created_at": datetime.now(UTC).isoformat(),
        "assets_sha256": files,
        "normalized_input_sha256": hashlib.sha256(
            (bundle / "normalized_input.json").read_bytes()
        ).hexdigest(),
        "input_fingerprint": case.get("_input_fingerprint"),
        "asset_storage": "content_addressed_hardlinks_with_copy_fallback",
        "provider": provider
        or {
            "backend": os.getenv("LLM_BACKEND", "not_used"),
            "model_or_deployment": os.getenv("AZURE_CLAUDE_DEPLOYMENT")
            or os.getenv("ANTHROPIC_MODEL"),
            "api_version": os.getenv("AZURE_API_VERSION"),
            "gemini_model": os.getenv("GEMINI_MEDICAL_MODEL"),
        },
    }
    manifest["prompt_versions"] = {
        name: digest for name, digest in files.items() if name.startswith("assets/prompts/")
    }
    manifest["rule_set_versions"] = {
        name: digest for name, digest in files.items() if name.startswith("assets/config/")
    }
    manifest["qc_profile_version"] = files.get("assets/config/brand_profile_sbi.json")
    manifest["parser_code_version"] = files.get("assets/agents/base.py")
    atomic_json(bundle / "manifest.json", manifest)
    atomic_json(folder / "reproduction" / "latest.json", manifest)
    return manifest
