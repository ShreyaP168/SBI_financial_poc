"""Scoped filesystem access and atomic persistence for service-owned artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from underwriting_platform.application.errors import InvalidRequest, MissingCase
from underwriting_platform.contracts.v1 import ARTIFACT_PATTERN, CASE_PATTERN


def native_path(path: Path) -> Path:
    """Preserve Windows long-path support for deeply retained evidence."""
    if os.name != "nt":
        return path
    value = str(path.absolute())
    if value.startswith("\\\\?\\"):
        return path
    return Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)


def scoped_path(root: Path, identifier: str, *, artifact: bool = False) -> Path:
    pattern = ARTIFACT_PATTERN if artifact else CASE_PATTERN
    if not re.fullmatch(pattern, identifier):
        raise InvalidRequest()
    root = root.resolve()
    target = (root / identifier).resolve()
    if not target.is_relative_to(root):
        raise InvalidRequest()
    return target


def case_directory(root: Path, case_id: str) -> Path:
    target = scoped_path(root, case_id)
    if not target.is_dir():
        raise MissingCase()
    return target


def fingerprint(folder: Path) -> str:
    folder = native_path(folder)
    digest = hashlib.sha256()
    supported = {".pdf", ".zip", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
    files = [
        path
        for path in sorted(folder.iterdir())
        if path.is_file() and path.suffix.lower() in supported
    ]
    if not files:
        raise MissingCase()
    for path in files:
        if not path.resolve().is_relative_to(folder.resolve()):
            raise InvalidRequest()
        digest.update(path.name.encode())
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path = native_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
