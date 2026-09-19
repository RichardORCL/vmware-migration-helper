"""Thin REST client for Google Compute Engine and Cloud Storage (service account JWT auth).

Built on ``httpx`` like the Azure client: token exchange, compute operations, LRO polling, and
authenticated GCS object reads with HTTP Range.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Callable, Iterator, Optional
from urllib.parse import quote

import httpx
import jwt

log = logging.getLogger(__name__)

COMPUTE_BASE = "https://compute.googleapis.com/compute/v1"
STORAGE_BASE = "https://storage.googleapis.com/storage/v1"
RM_BASE = "https://cloudresourcemanager.googleapis.com/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = (
    "https://www.googleapis.com/auth/compute",
    "https://www.googleapis.com/auth/devstorage.read_write",
    "https://www.googleapis.com/auth/cloud-platform",
)

TOKEN_REFRESH_MARGIN_S = 300
DEFAULT_POLL_S = 2.0
MAX_POLL_S = 30.0

_INSTANCE = re.compile(
    r"^projects/(?P<project>[^/]+)/zones/(?P<zone>[^/]+)/instances/(?P<name>[^/]+)$",
    re.IGNORECASE,
)
_DISK = re.compile(
    r"^projects/(?P<project>[^/]+)/zones/(?P<zone>[^/]+)/disks/(?P<name>[^/]+)$",
    re.IGNORECASE,
)


class GcpError(RuntimeError):
    def __init__(self, message: str, code: str = "", status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.status = status


class GcpAuthError(GcpError):
    pass


def _api_error(resp: httpx.Response, what: str) -> GcpError:
    message, code = "", ""
    try:
        body = resp.json()
        err = body.get("error") or body
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("errors", [{}])[0].get("reason") or "")
            message = str(err.get("message") or "")
    except Exception:  # noqa: BLE001
        message = resp.text.strip()[:500]
    text = f"{what}: HTTP {resp.status_code}" + (f" ({code})" if code else "") + (f": {message}" if message else "")
    if resp.status_code in (401, 403):
        return GcpAuthError(text, code=code, status=resp.status_code)
    return GcpError(text, code=code, status=resp.status_code)


def parse_service_account_json(raw: str) -> dict[str, str]:
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GcpAuthError(f"service account JSON is not valid JSON: {exc}") from exc
    for key in ("type", "project_id", "private_key", "client_email"):
        if not doc.get(key):
            raise GcpAuthError(f"service account JSON is missing {key!r}")
    if doc.get("type") != "service_account":
        raise GcpAuthError('service account JSON must have "type": "service_account"')
    return {
        "project_id": str(doc["project_id"]),
        "client_email": str(doc["client_email"]),
        "private_key": str(doc["private_key"]),
    }


def normalize_instance_id(resource: str) -> str:
    """Canonical id: ``projects/.../zones/.../instances/...`` (no URL prefix)."""
    r = (resource or "").strip()
    if r.startswith("https://"):
        r = r.split("/compute/v1/", 1)[-1] if "/compute/v1/" in r else r.rsplit("/", 3)[-1]
    if r.startswith("compute/v1/"):
        r = r[len("compute/v1/"):]
    return r


def parse_instance_id(resource: str) -> dict[str, str]:
    m = _INSTANCE.match(normalize_instance_id(resource))
    if not m:
        raise GcpError(f"not a Compute Engine instance id: {resource!r}")
    return m.groupdict()


class GcpClient:
    def __init__(
        self,
        project_id: str,
        client_email: str,
        private_key: str,
        *,
        http: Optional[httpx.Client] = None,
        compute_base: str = COMPUTE_BASE,
        storage_base: str = STORAGE_BASE,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.project_id = project_id.strip()
        self.client_email = client_email.strip()
        self._private_key = private_key
        self.compute_base = compute_base.rstrip("/")
        self.storage_base = storage_base.rstrip("/")
        self._own_http = http is None
        self.http = http or httpx.Client(timeout=httpx.Timeout(300.0, connect=30.0), follow_redirects=False)
        self._sleep = sleep
        self._clock = clock
        self._token: Optional[str] = None
        self._token_expires = 0.0
        self._lock = threading.Lock()
        self._closed = False

    @classmethod
    def from_service_account_json(cls, raw: str, **kw: Any) -> "GcpClient":
        sa = parse_service_account_json(raw)
        return cls(sa["project_id"], sa["client_email"], sa["private_key"], **kw)

    def token(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._token_expires - TOKEN_REFRESH_MARGIN_S:
                return self._token
            now = int(time.time())
            assertion = jwt.encode(
                {
                    "iss": self.client_email,
                    "sub": self.client_email,
                    "aud": TOKEN_URL,
                    "iat": now,
                    "exp": now + 3600,
                    "scope": " ".join(SCOPES),
                },
                self._private_key,
                algorithm="RS256",
            )
            try:
                resp = self.http.post(
                    TOKEN_URL,
                    data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
                )
            except httpx.HTTPError as exc:
                raise GcpError(f"cannot reach {TOKEN_URL}: {exc}") from exc
            if resp.status_code != 200:
                try:
                    body = resp.json()
                    desc = body.get("error_description") or body.get("error") or resp.text[:300]
                except Exception:  # noqa: BLE001
                    desc = resp.text[:300]
                raise GcpAuthError(f"GCP login failed: {desc}", status=resp.status_code)
            body = resp.json()
            self._token = str(body["access_token"])
            self._token_expires = self._clock() + float(body.get("expires_in") or 3600)
            return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}", "Accept": "application/json"}

    def _url(self, base: str, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return base + (path if path.startswith("/") else "/" + path)

    def _request(
        self,
        method: str,
        base: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Any = None,
        what: str = "",
        auth: bool = True,
    ) -> httpx.Response:
        url = self._url(base, path)
        what = what or f"{method} {path.split('?', 1)[0]}"
        headers = self._headers() if auth else {}
        try:
            resp = self.http.request(method, url, params=params or None, json=json_body, headers=headers)
        except httpx.HTTPError as exc:
            raise GcpError(f"{what}: {exc}") from exc
        if resp.status_code >= 400:
            raise _api_error(resp, what)
        return resp

    def get_json(self, base: str, path: str, *, params: Optional[dict] = None, what: str = "") -> dict:
        resp = self._request("GET", base, path, params=params, what=what)
        return resp.json() if resp.content else {}

    def paged(self, base: str, path: str, *, what: str = "", items_key: str = "items") -> Iterator[dict]:
        page_token: Optional[str] = None
        while True:
            params = {"pageToken": page_token} if page_token else None
            body = self.get_json(base, path, params=params, what=what)
            for item in body.get(items_key) or []:
                yield item
            page_token = body.get("nextPageToken")
            if not page_token:
                return

    @staticmethod
    def _retry_after(resp: httpx.Response, default: float = DEFAULT_POLL_S) -> float:
        return default

    def wait_zone_operation(self, project: str, zone: str, op_name: str, timeout_s: float,
                            what: str = "", on_wait: Optional[Callable[[], None]] = None) -> dict:
        path = f"/projects/{project}/zones/{zone}/operations/{op_name}"
        deadline = self._clock() + timeout_s
        wait = DEFAULT_POLL_S
        while True:
            if on_wait is not None:
                on_wait()
            if self._clock() > deadline:
                raise GcpError(f"{what or op_name}: operation did not finish within {int(timeout_s)} s")
            self._sleep(wait)
            op = self.get_json(self.compute_base, path, what=what or "poll zone operation")
            if op.get("status") == "DONE":
                if op.get("error"):
                    err = op["error"]
                    raise GcpError(f"{what or op_name}: {err.get('errors', err)}")
                return op
            wait = self._retry_after(httpx.Response(200), wait)

    def wait_global_operation(self, project: str, op_name: str, timeout_s: float,
                              what: str = "", on_wait: Optional[Callable[[], None]] = None) -> dict:
        path = f"/projects/{project}/global/operations/{op_name}"
        deadline = self._clock() + timeout_s
        wait = DEFAULT_POLL_S
        while True:
            if on_wait is not None:
                on_wait()
            if self._clock() > deadline:
                raise GcpError(f"{what or op_name}: operation did not finish within {int(timeout_s)} s")
            self._sleep(wait)
            op = self.get_json(self.compute_base, path, what=what or "poll global operation")
            if op.get("status") == "DONE":
                if op.get("error"):
                    err = op["error"]
                    raise GcpError(f"{what or op_name}: {err.get('errors', err)}")
                return op
            wait = DEFAULT_POLL_S

    # -------------------------------------------------------- resource manager
    def list_projects(self) -> list[dict]:
        try:
            return [{"id": p.get("projectId") or "", "name": p.get("name") or p.get("displayName") or "",
                     "number": str(p.get("projectNumber") or "")}
                    for p in self.paged(RM_BASE, "/projects", what="list projects")]
        except GcpAuthError:
            raise
        except GcpError:
            return [{"id": self.project_id, "name": self.project_id, "number": ""}]

    # ------------------------------------------------------------ compute
    def aggregated_instances(self, project: str) -> list[dict]:
        path = f"/projects/{project}/aggregated/instances"
        out: list[dict] = []
        page_token: Optional[str] = None
        while True:
            params = {"pageToken": page_token} if page_token else None
            resp = self._request("GET", self.compute_base, path, params=params,
                                 what=f"list instances in {project}")
            body = resp.json() if resp.content else {}
            for group in (body.get("items") or {}).values():
                for inst in group.get("instances") or []:
                    out.append(inst)
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return out

    def get_instance(self, instance_id: str) -> dict:
        ids = parse_instance_id(instance_id)
        path = f"/projects/{ids['project']}/zones/{ids['zone']}/instances/{ids['name']}"
        return self.get_json(self.compute_base, path, what=f"get instance {ids['name']}")

    def get_disk(self, disk_url: str) -> dict:
        """``disk_url`` is a full disk URL or ``projects/.../zones/.../disks/...``."""
        if disk_url.startswith("http"):
            disk_url = disk_url.split("/compute/v1/", 1)[-1]
        return self.get_json(self.compute_base, f"/{disk_url.lstrip('/')}", what=f"get disk {disk_url.rsplit('/', 1)[-1]}")

    def get_machine_type(self, machine_type_url: str) -> dict:
        if machine_type_url.startswith("http"):
            machine_type_url = machine_type_url.split("/compute/v1/", 1)[-1]
        return self.get_json(self.compute_base, f"/{machine_type_url.lstrip('/')}", what="get machine type")

    def stop_instance(self, instance_id: str, timeout_s: float, on_wait: Optional[Callable[[], None]] = None) -> None:
        ids = parse_instance_id(instance_id)
        path = f"/projects/{ids['project']}/zones/{ids['zone']}/instances/{ids['name']}/stop"
        resp = self._request("POST", self.compute_base, path, what=f"stop instance {ids['name']}")
        op = resp.json() if resp.content else {}
        name = op.get("name") or ""
        if op.get("status") == "DONE":
            return
        if name:
            self.wait_zone_operation(ids["project"], ids["zone"], name, timeout_s,
                                     what=f"stop {ids['name']}", on_wait=on_wait)

    def create_snapshot(
        self,
        project: str,
        zone: str,
        disk_name: str,
        snapshot_name: str,
        timeout_s: float,
        labels: Optional[dict[str, str]] = None,
        on_wait: Optional[Callable[[], None]] = None,
    ) -> dict:
        path = f"/projects/{project}/zones/{zone}/disks/{disk_name}/createSnapshot"
        body: dict[str, Any] = {"name": snapshot_name}
        if labels:
            body["labels"] = labels
        resp = self._request("POST", self.compute_base, path, json_body=body,
                             what=f"create snapshot {snapshot_name}")
        op = resp.json() if resp.content else {}
        if op.get("status") != "DONE" and op.get("name"):
            self.wait_zone_operation(project, zone, op["name"], timeout_s,
                                     what=f"snapshot {snapshot_name}", on_wait=on_wait)
        snap_path = f"/projects/{project}/global/snapshots/{snapshot_name}"
        return self.get_json(self.compute_base, snap_path, what=f"get snapshot {snapshot_name}")

    def export_snapshot(
        self,
        project: str,
        snapshot_name: str,
        bucket: str,
        object_name: str,
        timeout_s: float,
        on_wait: Optional[Callable[[], None]] = None,
    ) -> None:
        path = f"/projects/{project}/global/snapshots/{snapshot_name}/exportToCloudStorage"
        body = {"destinationBucket": bucket, "destinationPath": object_name}
        resp = self._request("POST", self.compute_base, path, json_body=body,
                             what=f"export snapshot {snapshot_name}")
        op = resp.json() if resp.content else {}
        if op.get("status") != "DONE" and op.get("name"):
            self.wait_global_operation(project, op["name"], timeout_s,
                                       what=f"export snapshot {snapshot_name}", on_wait=on_wait)

    def delete_snapshot(self, project: str, snapshot_name: str, timeout_s: float = 300.0) -> None:
        path = f"/projects/{project}/global/snapshots/{snapshot_name}"
        try:
            resp = self._request("DELETE", self.compute_base, path, what=f"delete snapshot {snapshot_name}")
        except GcpError as exc:
            if exc.status == 404:
                return
            raise
        op = resp.json() if resp.content else {}
        if op.get("name"):
            try:
                self.wait_global_operation(project, op["name"], timeout_s, what=f"delete snapshot {snapshot_name}")
            except GcpError as exc:
                if exc.status != 404:
                    raise

    # -------------------------------------------------------------- storage
    def get_bucket(self, bucket: str) -> dict:
        return self.get_json(self.storage_base, f"/b/{quote(bucket, safe='')}", what=f"get bucket {bucket}")

    def delete_object(self, bucket: str, object_name: str) -> None:
        path = f"/b/{quote(bucket, safe='')}/o/{quote(object_name, safe='')}"
        try:
            self._request("DELETE", self.storage_base, path, what=f"delete gs://{bucket}/{object_name}")
        except GcpError as exc:
            if exc.status == 404:
                return
            raise

    def object_media_url(self, bucket: str, object_name: str) -> str:
        return f"{self.storage_base}/b/{quote(bucket, safe='')}/o/{quote(object_name, safe='')}?alt=media"

    def object_head(self, bucket: str, object_name: str) -> dict:
        path = f"/b/{quote(bucket, safe='')}/o/{quote(object_name, safe='')}"
        return self.get_json(self.storage_base, path, what=f"head gs://{bucket}/{object_name}")

    def object_get_range(self, bucket: str, object_name: str, start: int, end: int) -> bytes:
        url = self.object_media_url(bucket, object_name)
        headers = {**self._headers(), "Range": f"bytes={start}-{end}"}
        try:
            resp = self.http.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise GcpError(f"read gs://{bucket}/{object_name}: {exc}") from exc
        if resp.status_code not in (200, 206):
            raise _api_error(resp, f"read gs://{bucket}/{object_name}")
        return resp.content

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self._own_http:
            self.http.close()
