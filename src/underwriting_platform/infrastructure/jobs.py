"""SQLite for local development; PostgreSQL with SKIP LOCKED for deployment."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from underwriting_platform.application.errors import (
    IdempotencyConflict,
    LeaseLost,
    QueueFull,
)
from underwriting_platform.contracts.v1 import CaseRequest, JobStatus, WorkflowResult
from underwriting_platform.domain.jobs import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_jobs (
    job_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','incomplete','failed')),
    stage TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    lease_owner TEXT,
    lease_expires_at DOUBLE PRECISION,
    checkpoints TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    error_code TEXT
)
"""


class RelationalJobRepository:
    def __init__(self, database_url: str, max_pending: int = 100):
        self.database_url = database_url
        self.max_pending = max_pending
        self.sqlite = database_url.startswith("sqlite:///")
        if not self.sqlite and not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("Expected a SQLite or PostgreSQL database URL")
        self.path = Path(database_url.removeprefix("sqlite:///")) if self.sqlite else None

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[Any]:
        if self.sqlite:
            assert self.path is not None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA foreign_keys = ON")
            if write:
                connection.execute("BEGIN IMMEDIATE")
        else:
            import psycopg
            from psycopg.rows import dict_row

            connection = psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5)
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _sql(self, query: str) -> str:
        return query if self.sqlite else query.replace("?", "%s")

    def initialize(self) -> None:
        with self._connection(write=True) as connection:
            if not self.sqlite:
                # IF NOT EXISTS alone does not serialize PostgreSQL catalog writes.
                # Gateway and worker can start simultaneously on an empty database.
                connection.execute("SELECT pg_advisory_xact_lock(1415000123)")
            connection.execute(SCHEMA)
            connection.execute("""CREATE TABLE IF NOT EXISTS workflow_payloads (
                job_id TEXT NOT NULL, stage TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY(job_id, stage),
                FOREIGN KEY(job_id) REFERENCES workflow_jobs(job_id) ON DELETE CASCADE
            )""")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS workflow_jobs_claim ON workflow_jobs(status, lease_expires_at, created_at)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS workflow_migrations (name TEXT PRIMARY KEY)"
            )
            migrated = connection.execute(
                self._sql("SELECT name FROM workflow_migrations WHERE name=?"),
                ("checkpoint-payloads-v1",),
            ).fetchone()
            if migrated is None:
                rows = connection.execute("SELECT job_id,checkpoints FROM workflow_jobs").fetchall()
                for row in rows:
                    checkpoints = json.loads(row["checkpoints"])
                    changed = False
                    for stage, payload in list(checkpoints.items()):
                        if isinstance(payload, dict) and not payload.get("payload_ref"):
                            connection.execute(
                                self._sql("""INSERT INTO workflow_payloads(job_id,stage,payload_json)
                                VALUES (?,?,?) ON CONFLICT(job_id,stage) DO NOTHING"""),
                                (row["job_id"], stage, json.dumps(payload, allow_nan=False)),
                            )
                            completion = (payload.get("decision") or {}).get("_completion") or {}
                            checkpoints[stage] = {
                                "payload_ref": True,
                                "completion": {
                                    "decision_complete": completion.get("decision_complete", False),
                                    "policy_version": completion.get("policy_version"),
                                },
                            }
                            changed = True
                    if changed:
                        connection.execute(
                            self._sql("UPDATE workflow_jobs SET checkpoints=? WHERE job_id=?"),
                            (json.dumps(checkpoints), row["job_id"]),
                        )
                connection.execute(
                    self._sql("INSERT INTO workflow_migrations(name) VALUES (?)"),
                    ("checkpoint-payloads-v1",),
                )

    def ready(self) -> bool:
        with self._connection() as connection:
            return (
                connection.execute("SELECT 1 FROM workflow_jobs LIMIT 1").fetchone() is not None
                or connection.execute("SELECT 1").fetchone() is not None
            )

    def _job(self, row: Any, *, connection=None, hydrate: bool = False) -> Job:
        checkpoints = json.loads(row["checkpoints"])
        if hydrate:
            for stage, entry in list(checkpoints.items()):
                if isinstance(entry, dict) and entry.get("payload_ref"):
                    payload = connection.execute(
                        self._sql(
                            "SELECT payload_json FROM workflow_payloads WHERE job_id=? AND stage=?"
                        ),
                        (row["job_id"], stage),
                    ).fetchone()
                    if payload is None:
                        raise ValueError("Checkpoint payload unavailable")
                    checkpoints[stage] = json.loads(payload["payload_json"])
        return Job(
            job_id=row["job_id"],
            request=CaseRequest.model_validate_json(row["request_json"]),
            status=JobStatus(row["status"]),
            stage=row["stage"],
            attempts=row["attempts"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            checkpoints=checkpoints,
            result=WorkflowResult.model_validate_json(row["result_json"])
            if row["result_json"]
            else None,
            error_code=row["error_code"],
        )

    def create(self, request: CaseRequest, idempotency_key: str) -> Job:
        serialized = request.model_dump_json()
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        now = time.time()
        with self._connection(write=True) as connection:
            if not self.sqlite:
                connection.execute("SELECT pg_advisory_xact_lock(1415000124)")
            existing = connection.execute(
                self._sql("SELECT * FROM workflow_jobs WHERE idempotency_key=?"), (idempotency_key,)
            ).fetchone()
            if existing:
                if existing["request_hash"] != digest:
                    raise IdempotencyConflict()
                return self._job(existing)
            pending = connection.execute(
                "SELECT COUNT(*) AS pending FROM workflow_jobs WHERE status IN ('queued','running')"
            ).fetchone()["pending"]
            if pending >= self.max_pending:
                raise QueueFull()
            connection.execute(
                self._sql("""INSERT INTO workflow_jobs
                (job_id,idempotency_key,request_hash,request_json,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(idempotency_key) DO NOTHING"""),
                (str(uuid.uuid4()), idempotency_key, digest, serialized, "queued", now, now),
            )
            row = connection.execute(
                self._sql("SELECT * FROM workflow_jobs WHERE idempotency_key=?"), (idempotency_key,)
            ).fetchone()
            if row["request_hash"] != digest:
                raise IdempotencyConflict()
            return self._job(row)

    def queue_metrics(self) -> dict:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS pending, MIN(created_at) AS oldest FROM workflow_jobs WHERE status IN ('queued','running')"
            ).fetchone()
            return {
                "pending": row["pending"],
                "oldest_pending_seconds": max(0, time.time() - row["oldest"])
                if row["oldest"]
                else 0,
                "capacity": self.max_pending,
            }

    def get(self, job_id: str) -> Job | None:
        with self._connection() as connection:
            row = connection.execute(
                self._sql("SELECT * FROM workflow_jobs WHERE job_id=?"), (job_id,)
            ).fetchone()
            return self._job(row) if row else None

    def claim(self, owner: str, lease_seconds: int, max_attempts: int) -> Job | None:
        now = time.time()
        with self._connection(write=True) as connection:
            connection.execute(
                self._sql("""UPDATE workflow_jobs SET status='failed',error_code='RETRY_EXHAUSTED',updated_at=?
                WHERE status='running' AND lease_expires_at<? AND attempts>=?"""),
                (now, now, max_attempts),
            )
            query = """SELECT * FROM workflow_jobs WHERE
                (status='queued' OR (status='running' AND lease_expires_at<?)) AND attempts<?
                ORDER BY created_at LIMIT 1"""
            if not self.sqlite:
                query += " FOR UPDATE SKIP LOCKED"
            row = connection.execute(self._sql(query), (now, max_attempts)).fetchone()
            if not row:
                return None
            connection.execute(
                self._sql("""UPDATE workflow_jobs SET status='running',lease_owner=?,
                lease_expires_at=?,attempts=attempts+1,updated_at=?,error_code=NULL WHERE job_id=?"""),
                (owner, now + lease_seconds, now, row["job_id"]),
            )
            row = connection.execute(
                self._sql("SELECT * FROM workflow_jobs WHERE job_id=?"), (row["job_id"],)
            ).fetchone()
            return self._job(row, connection=connection, hydrate=True)

    def _owned(self, connection: Any, job_id: str, owner: str) -> Any:
        query = "SELECT * FROM workflow_jobs WHERE job_id=? AND lease_owner=? AND status='running' AND lease_expires_at>?"
        if not self.sqlite:
            query += " FOR UPDATE"
        row = connection.execute(self._sql(query), (job_id, owner, time.time())).fetchone()
        if not row:
            raise LeaseLost()
        return row

    def renew(self, job_id: str, owner: str, lease_seconds: int) -> None:
        with self._connection(write=True) as connection:
            self._owned(connection, job_id, owner)
            connection.execute(
                self._sql(
                    "UPDATE workflow_jobs SET lease_expires_at=?,updated_at=? WHERE job_id=?"
                ),
                (time.time() + lease_seconds, time.time(), job_id),
            )

    def checkpoint(self, job_id: str, owner: str, stage: str, data: dict[str, Any]) -> None:
        with self._connection(write=True) as connection:
            row = self._owned(connection, job_id, owner)
            checkpoints = json.loads(row["checkpoints"])
            connection.execute(
                self._sql("""INSERT INTO workflow_payloads(job_id,stage,payload_json)
                VALUES (?,?,?) ON CONFLICT(job_id,stage) DO UPDATE SET payload_json=excluded.payload_json"""),
                (job_id, stage, json.dumps(data, allow_nan=False)),
            )
            completion = (data.get("decision") or {}).get("_completion") or {}
            checkpoints[stage] = {
                "payload_ref": True,
                "completion": {
                    "decision_complete": completion.get("decision_complete", False),
                    "policy_version": completion.get("policy_version"),
                },
            }
            connection.execute(
                self._sql(
                    "UPDATE workflow_jobs SET checkpoints=?,stage=?,updated_at=? WHERE job_id=?"
                ),
                (json.dumps(checkpoints, allow_nan=False), stage, time.time(), job_id),
            )

    def finish(self, job_id: str, owner: str, status: JobStatus, result: WorkflowResult) -> None:
        if status not in {JobStatus.SUCCEEDED, JobStatus.INCOMPLETE}:
            raise ValueError("Invalid completion state")
        with self._connection(write=True) as connection:
            self._owned(connection, job_id, owner)
            connection.execute(
                self._sql("""UPDATE workflow_jobs SET status=?,stage='complete',result_json=?,
                updated_at=?,lease_owner=NULL,lease_expires_at=NULL WHERE job_id=?"""),
                (status.value, result.model_dump_json(), time.time(), job_id),
            )

    def record_partial(self, job_id: str, owner: str, result: WorkflowResult) -> None:
        with self._connection(write=True) as connection:
            self._owned(connection, job_id, owner)
            connection.execute(
                self._sql("UPDATE workflow_jobs SET result_json=?,updated_at=? WHERE job_id=?"),
                (result.model_dump_json(), time.time(), job_id),
            )

    def fail(self, job_id: str, owner: str, code: str, retry: bool) -> None:
        with self._connection(write=True) as connection:
            self._owned(connection, job_id, owner)
            connection.execute(
                self._sql("""UPDATE workflow_jobs SET status=?,error_code=?,updated_at=?,
                lease_owner=NULL,lease_expires_at=NULL WHERE job_id=?"""),
                ("queued" if retry else "failed", code, time.time(), job_id),
            )
