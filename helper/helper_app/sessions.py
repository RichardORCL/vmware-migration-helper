"""Web UI sessions: an opaque cookie token mapped to a logged-in vCenter session.

A session that started a migration is *pinned* by that job: logging out or idling past the
TTL marks the session dead, but the underlying vCenter connection is only closed once the last
pinned job has finished, so a running export never loses its lease.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from helper_app.models import SessionInfo
from helper_app.vsphere.session import VCenterSession

log = logging.getLogger(__name__)


class UserSession:
    def __init__(self, token: str, vc: VCenterSession, ttl_s: float):
        self.token = token
        self.vc = vc
        self.username = vc.username
        self.created_at = datetime.now(timezone.utc)
        self.ttl_s = ttl_s
        self._last_used = time.monotonic()
        self._pins: set[str] = set()
        self._dead = False
        self._lock = threading.Lock()
        self.cache: dict[str, object] = {}  # per-session scratch space (e.g. the VM list)

    # ------------------------------------------------------------- lifetime
    def touch(self) -> None:
        with self._lock:
            self._last_used = time.monotonic()

    @property
    def idle_s(self) -> float:
        return time.monotonic() - self._last_used

    @property
    def expired(self) -> bool:
        return self.idle_s > self.ttl_s

    @property
    def dead(self) -> bool:
        return self._dead

    @property
    def expires_at(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=max(0.0, self.ttl_s - self.idle_s))

    def info(self) -> SessionInfo:
        return SessionInfo(username=self.username, vcenter_host=self.vc.host,
                           vcenter_port=getattr(self.vc, "port", 443), vcenter_version=self.vc.version,
                           verify_ssl=bool(getattr(self.vc, "verify_ssl", False)),
                           created_at=self.created_at, expires_at=self.expires_at)

    # ------------------------------------------------------------- pinning
    def pin(self, job_id: str) -> None:
        with self._lock:
            self._pins.add(job_id)

    def unpin(self, job_id: str) -> None:
        with self._lock:
            self._pins.discard(job_id)
            close = self._dead and not self._pins
        if close:
            self.vc.close()

    @property
    def pinned_jobs(self) -> set[str]:
        with self._lock:
            return set(self._pins)

    def kill(self) -> None:
        """Mark the session unusable for the UI; disconnect from vCenter unless a job still needs it."""
        with self._lock:
            self._dead = True
            close = not self._pins
        if close:
            self.vc.close()


class SessionStore:
    def __init__(self, ttl_s: float):
        self.ttl_s = ttl_s
        self._sessions: dict[str, UserSession] = {}
        self._lock = threading.Lock()

    def create(self, vc: VCenterSession) -> UserSession:
        token = secrets.token_urlsafe(32)
        session = UserSession(token, vc, self.ttl_s)
        with self._lock:
            self._sessions[token] = session
        log.info("session created for %s", vc.username)
        return session

    def set_ttl(self, ttl_s: float) -> None:
        """Change the idle timeout for new *and* existing sessions (Setup page)."""
        with self._lock:
            self.ttl_s = ttl_s
            for s in self._sessions.values():
                s.ttl_s = ttl_s

    def get(self, token: Optional[str]) -> Optional[UserSession]:
        if not token:
            return None
        self.sweep()
        with self._lock:
            session = self._sessions.get(token)
        if session is None or session.dead:
            return None
        session.touch()
        return session

    def logout(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._lock:
            session = self._sessions.pop(token, None)
        if session is not None:
            log.info("session of %s logged out (pinned jobs: %d)", session.username, len(session.pinned_jobs))
            session.kill()

    def sweep(self) -> None:
        with self._lock:
            expired = [t for t, s in self._sessions.items() if s.expired]
            sessions = [self._sessions.pop(t) for t in expired]
        for s in sessions:
            log.info("session of %s expired", s.username)
            s.kill()

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            s.vc.close()

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
