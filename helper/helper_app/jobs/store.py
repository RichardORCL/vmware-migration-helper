"""SQLite backed job store (thread safe, JSON documents)."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from helper_app.models import Job


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                   id TEXT PRIMARY KEY,
                   vm_moid TEXT NOT NULL,
                   phase TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   data TEXT NOT NULL
               )"""
        )

    def put(self, job: Job) -> Job:
        job.updated_at = utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO jobs(id, vm_moid, phase, created_at, updated_at, data) VALUES (?,?,?,?,?,?)",
                (job.id, job.vm.moid, job.phase.value, job.created_at.isoformat(), job.updated_at.isoformat(),
                 job.model_dump_json()),
            )
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            row = self._conn.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def list(self, vm_moid: Optional[str] = None, limit: int = 200) -> list[Job]:
        with self._lock:
            if vm_moid:
                rows = self._conn.execute(
                    "SELECT data FROM jobs WHERE vm_moid = ? ORDER BY created_at DESC LIMIT ?", (vm_moid, limit)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT data FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [Job.model_validate_json(r[0]) for r in rows]

    def active(self) -> list[Job]:
        return [job for job in self.list(limit=10000) if not job.phase.terminal]

    def active_for_vm(self, vm_moid: str) -> Optional[Job]:
        for job in self.list(vm_moid=vm_moid):
            if not job.phase.terminal:
                return job
        return None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
