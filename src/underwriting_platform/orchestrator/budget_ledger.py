"""Case-scoped spend independent of parser/prompt/extraction revisions."""

import hashlib
import os
from pathlib import Path

from .checkpoints import StageStore


def attach_budget(budget, root: Path, case_id: str, legacy_store=None):
    if getattr(budget, "_ledger_case", None) == case_id:
        return budget._ledger
    ledger = StageStore(root / "case-budgets" / hashlib.sha256(case_id.encode()).hexdigest())
    ledger.root.mkdir(parents=True, exist_ok=True)
    handle = (ledger.root / "lease.lock").open("a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        from underwriting_platform.application.errors import ExtractionFailed

        raise ExtractionFailed() from None
    try:
        saved = ledger.read("budget")
        if saved is None:
            total, migrated = 0, []
            for candidate in root.iterdir():
                if not candidate.is_dir() or candidate.name == "case-budgets":
                    continue
                previous = StageStore(candidate)
                legacy = previous.read("budget")
                if not legacy:
                    continue
                identity = previous.read("case_identity") or {}
                survey = previous.read("survey") or {}
                belongs = (
                    identity.get("case_id") == case_id
                    or any(
                        str(row.get("filename", "")).startswith(case_id + "_")
                        for row in survey.get("files", [])
                    )
                    or (legacy_store is not None and previous.root == legacy_store.root)
                )
                if belongs:
                    total += int(legacy.get("spent", 0)) + int(legacy.get("reserved", 0))
                    migrated.append(candidate.name)
            saved = {"spent": total, "reserved": 0, "migrated_revisions": migrated}
        budget.spent = max(budget.spent, int(saved.get("spent", 0)) + int(saved.get("reserved", 0)))
        if budget.spent < 0:
            raise ValueError("Invalid case budget ledger")
        budget.on_change = lambda snapshot: ledger.write(
            "budget", {**saved, **snapshot, "case_id": case_id}
        )
        budget.on_close = handle.close
        budget._ledger_case, budget._ledger = case_id, ledger
        budget.persist()
    except Exception:
        handle.close()
        raise
    return ledger
