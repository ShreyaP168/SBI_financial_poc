"""Authenticated internal handoff of frozen evidence between independent stores."""

import base64
import hashlib
import json
import re
from pathlib import Path

from underwriting_platform.application.errors import EvidenceTooLarge, InvalidRequest, MissingCase
from underwriting_platform.infrastructure.files import native_path, scoped_path

# File-backed POC handoffs are bounded; large folders require streaming/object storage.
MAX_EVIDENCE_BYTES = 64 * 1024 * 1024


def validate_intake_size(folder: Path, config_root: Path):
    from underwriting_platform.configuration.extraction import evidence_paths

    source_bytes = (
        sum(
            path.stat().st_size
            for path in folder.iterdir()
            if path.is_file()
            and path.suffix.lower()
            in {".pdf", ".zip", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
        )
        if folder.is_dir()
        else 0
    )
    reference_bytes = sum(
        path.stat().st_size for path in evidence_paths(config_root).values() if path.is_file()
    )
    if source_bytes + reference_bytes > MAX_EVIDENCE_BYTES:
        raise EvidenceTooLarge()


def export_evidence(cache: Path, case_id: str, revision: str) -> dict:
    if not re.fullmatch("[0-9a-f]{64}", revision):
        raise InvalidRequest()
    root = scoped_path(cache, case_id)
    record = root / f"{revision}.json"
    if not record.is_file():
        record = root / f"{revision}.partial.json"
    if not record.is_file():
        raise MissingCase()
    data = json.loads(record.read_text(encoding="utf8"))
    bundle = native_path(root / revision)
    files = {}
    total_bytes = 0
    for group in ("documents", "references"):
        folder = bundle / group
        if folder.is_dir():
            for source in folder.rglob("*"):
                if source.is_file():
                    if not source.resolve().is_relative_to(bundle.resolve()):
                        raise InvalidRequest()
                    total_bytes += source.stat().st_size
                    if total_bytes > MAX_EVIDENCE_BYTES:
                        raise EvidenceTooLarge()
                    content = source.read_bytes()
                    files[source.relative_to(bundle).as_posix()] = {
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "content": base64.b64encode(content).decode("ascii"),
                    }
    return {
        "case_id": case_id,
        "revision": revision,
        "input_fingerprint": data["input_fingerprint"],
        "extraction_report": data["extraction_report"],
        "files": files,
    }


def receive_evidence(data: dict, target: Path, case_id: str, revision: str, fingerprint: str):
    if (
        data.get("case_id") != case_id
        or data.get("revision") != revision
        or data.get("input_fingerprint") != fingerprint
    ):
        raise InvalidRequest()
    root = native_path(target.resolve())
    for name, record in data["files"].items():
        relative = Path(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.parts[0] not in {"documents", "references"}
        ):
            raise InvalidRequest()
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()):
            raise InvalidRequest()
        content = base64.b64decode(record["content"], validate=True)
        if hashlib.sha256(content).hexdigest() != record["sha256"]:
            raise InvalidRequest()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return data["extraction_report"]
