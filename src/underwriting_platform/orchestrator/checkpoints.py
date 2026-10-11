"""Private, integrity-checked stage checkpoints for bounded extraction retries."""

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from underwriting_platform.agents.base import AgentResult


class StageStore:
    def __init__(self, root: Path):
        from underwriting_platform.infrastructure.files import native_path

        self.root = native_path(root)

    def read(self, key: str):
        path = self.root / (hashlib.sha256(key.encode()).hexdigest() + ".json")
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf8"))
        serialized = json.dumps(data["value"], sort_keys=True, allow_nan=False)
        if hashlib.sha256(serialized.encode()).hexdigest() != data["sha256"]:
            raise ValueError("Extraction checkpoint integrity failed")
        return data["value"]

    def write(self, key: str, value: dict):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / (hashlib.sha256(key.encode()).hexdigest() + ".json")
        serialized = json.dumps(value, sort_keys=True, allow_nan=False)
        data = {"value": value, "sha256": hashlib.sha256(serialized.encode()).hexdigest()}
        descriptor, temporary = tempfile.mkstemp(dir=self.root, prefix=".pending-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf8") as stream:
                json.dump(data, stream, allow_nan=False)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def agent(self, key: str) -> AgentResult | None:
        data = self.read(key)
        if data is not None and data.get("error"):
            return None
        if data is not None:
            from underwriting_platform.contracts.evidence import uncited_applicant_fields

            if uncited_applicant_fields(data.get("block") or {}, data.get("field_sources") or {}):
                return None
        return AgentResult(**data) if data is not None else None

    def save_agent(self, key: str, result: AgentResult):
        # Provider/configuration failures can recover without source changes.
        # Retry policy decides immediate retries; durable caches retain success only.
        from underwriting_platform.contracts.evidence import uncited_applicant_fields

        if (
            not result.error
            and not result.source_findings
            and not uncited_applicant_fields(result.block, result.field_sources)
        ):
            self.write(key, asdict(result))
