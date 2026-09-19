"""Post-copy guest fix-up: everything the helper changes on the copied boot volume so the guest boots and
talks in OCI, in one mount session.

Steps (each opt-in per job, each reported on its own, none can fail the migration):

* initramfs - rebuild with virtio drivers (``helper_app.guest.initramfs``)
* network   - DHCP on the renamed network interface (``helper_app.guest.network``)
* azure_cloud - drop Azure cloud-init/waagent/fstab sr0; enable OCI serial console (``azure_cloud``)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from helper_app.guest.azure_cloud import AzureCloudFixer
from helper_app.guest.initramfs import _LOCK, Fail, Runner, Skip, _Session, default_runner, initramfs_outcome
from helper_app.guest.network import NetworkFixer
from helper_app.models import GuestFixup

log = logging.getLogger(__name__)

Notify = Callable[[str], None]


class GuestFixupResult(NamedTuple):
    initramfs: Optional[GuestFixup]  # None when the step was not requested
    network: Optional[GuestFixup]
    azure_cloud: Optional[GuestFixup]


# (boot volume device, initramfs, network, azure_cloud, progress callback) -> per-step outcomes
GuestFixerFn = Callable[[str, bool, bool, bool, Optional[Notify]], GuestFixupResult]


class GuestFixer:
    def __init__(self, run: Runner = default_runner, mount_base: str | Path = "/run/vc-oci-helper/guest"):
        self.run = run  # shell runner, used by _Session
        self.mount_base = Path(mount_base)

    def fix(self, device: str, initramfs: bool = True, network: bool = True, azure_cloud: bool = False,
            notify: Optional[Notify] = None) -> GuestFixupResult:
        """Open the guest root on ``device`` once and run the requested steps."""
        notes: list[str] = []

        def note(msg: str) -> None:
            notes.append(msg)
            log.info("guest fix-up %s: %s", device, msg)
            if notify:
                notify(msg)

        def outcome(exc: BaseException, log_from: int) -> GuestFixup:
            if isinstance(exc, Skip):
                note(f"skipped: {exc}")
                return GuestFixup(status="skipped", detail=str(exc), log=notes[log_from:])
            if isinstance(exc, Fail):
                note(f"failed: {exc}")
                return GuestFixup(status="failed", detail=str(exc), log=notes[log_from:])
            log.exception("guest fix-up on %s crashed", device)
            note(f"failed: {exc}")
            return GuestFixup(status="failed", detail=f"unexpected error: {exc}", log=notes[log_from:])

        if not initramfs and not network and not azure_cloud:
            return GuestFixupResult(None, None, None)
        res_i: Optional[GuestFixup] = None
        res_n: Optional[GuestFixup] = None
        res_a: Optional[GuestFixup] = None
        with _LOCK:
            session = _Session(self, device, note)
            try:
                try:
                    session.open_root()
                except Exception as exc:  # noqa: BLE001 - no root: every step gets the same answer
                    same = outcome(exc, 0)
                    return GuestFixupResult(same if initramfs else None, same if network else None,
                                            same if azure_cloud else None)
                if initramfs:
                    try:
                        kernels, rebuilt = session.rebuild_initramfs()
                        res_i = initramfs_outcome(kernels, rebuilt, notes[:])  # incl. the disk scan notes
                    except Exception as exc:  # noqa: BLE001
                        res_i = outcome(exc, 0)
                if network:
                    start = len(notes)
                    try:
                        status, detail = NetworkFixer(session.mnt, session.sh, note).apply()
                        res_n = GuestFixup(status=status, detail=detail, log=notes[start:])  # type: ignore[arg-type]
                    except Exception as exc:  # noqa: BLE001
                        res_n = outcome(exc, start)
                if azure_cloud:
                    start = len(notes)
                    try:
                        status, detail = AzureCloudFixer(session.mnt, note).apply()
                        res_a = GuestFixup(status=status, detail=detail, log=notes[start:])  # type: ignore[arg-type]
                    except Exception as exc:  # noqa: BLE001
                        res_a = outcome(exc, start)
                try:
                    session.sh(["sync"], ok=False)
                except Fail:
                    pass
            finally:
                session.cleanup()
        return GuestFixupResult(res_i, res_n, res_a)
