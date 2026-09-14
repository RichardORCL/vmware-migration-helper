"""vCenter connectivity.

Users log in to the web UI with their vCenter credentials.  ``VCenterConnector.login`` opens a
pyVmomi session for them and wraps it in a ``VCenterSession`` that is used for inventory
lookups and for the long running export.
"""

from __future__ import annotations

import logging
import ssl
import threading
from typing import Optional

from helper_app.config import Settings
from helper_app.models import VmSummary

log = logging.getLogger(__name__)


class VCenterError(RuntimeError):
    pass


class VCenterAuthError(VCenterError):
    """Wrong user name or password."""


class VCenterSession:
    """A logged-in pyVmomi ``ServiceInstance`` bound to one user."""

    def __init__(self, si, username: str, host: str):
        self._si = si
        self.username = username
        self.host = host
        self._lock = threading.RLock()
        self._closed = False

    @property
    def service_instance(self):
        return self._si

    @property
    def version(self) -> str:
        try:
            about = self._si.content.about
            return f"{about.fullName}"
        except Exception:  # noqa: BLE001
            return ""

    def vm(self, moid: str):
        """Return a vim.VirtualMachine for ``moid`` (e.g. 'vm-123'), raising if it does not exist."""
        from pyVmomi import vim, vmodl

        vm = vim.VirtualMachine(moid)
        vm._stub = self._si._stub
        try:
            _ = vm.name
        except vmodl.fault.ManagedObjectNotFound as exc:
            raise VCenterError(f"virtual machine {moid} not found") from exc
        return vm

    def list_vms(self) -> list[VmSummary]:
        from helper_app.vsphere.inventory import list_vm_summaries

        with self._lock:
            return list_vm_summaries(self._si)

    def keepalive(self) -> bool:
        """Touch the session so vCenter does not expire it; returns False when it is gone."""
        with self._lock:
            if self._closed:
                return False
            try:
                self._si.CurrentTime()
                return True
            except Exception as exc:  # noqa: BLE001
                log.info("vCenter session of %s is no longer valid: %s", self.username, exc)
                return False

    def close(self) -> None:
        from pyVim.connect import Disconnect

        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                Disconnect(self._si)
            except Exception as exc:  # noqa: BLE001
                log.debug("disconnect for %s failed: %s", self.username, exc)


class VCenterConnector:
    def __init__(self, settings: Settings):
        self.s = settings

    @property
    def host(self) -> str:
        return self.s.vcenter_host

    def _ssl_context(self) -> Optional[ssl.SSLContext]:
        if self.s.vcenter_verify_ssl:
            return None
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def login(self, username: str, password: str) -> VCenterSession:
        from pyVim.connect import SmartConnect
        from pyVmomi import vim

        if not self.s.vcenter_host:
            raise VCenterError("HELPER_VCENTER_HOST is not configured")
        if not username or not password:
            raise VCenterAuthError("user name and password are required")
        kwargs = {}
        ctx = self._ssl_context()
        if ctx is not None:
            kwargs["sslContext"] = ctx
        log.info("vCenter login %s@%s", username, self.s.vcenter_host)
        try:
            si = SmartConnect(host=self.s.vcenter_host, port=self.s.vcenter_port, user=username, pwd=password,
                              **kwargs)
        except vim.fault.InvalidLogin as exc:
            raise VCenterAuthError("invalid vCenter user name or password") from exc
        except vim.fault.NoPermission as exc:
            raise VCenterAuthError("the account is not allowed to log in to vCenter") from exc
        except Exception as exc:  # noqa: BLE001
            raise VCenterError(f"cannot connect to vCenter {self.s.vcenter_host}: {exc}") from exc
        return VCenterSession(si, username, self.s.vcenter_host)
