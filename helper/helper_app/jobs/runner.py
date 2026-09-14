"""Migration orchestration: one thread per job runs the whole pipeline.

    provision OCI target (Provisioner.prepare) -> open NFC lease on the user's vCenter session
    -> for each disk: GET stream-optimized VMDK, decode grains onto the attached OCI volume
       (retried from the beginning on failure) -> finalize (Provisioner.finalize)
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Optional

import httpx

from helper_app.config import Settings
from helper_app.disk.vmdk_stream import DecodeStats, StreamOptimizedDecoder, VmdkFormatError
from helper_app.disk.writer import BlockDeviceWriter
from helper_app.jobs.progress import RateMeter
from helper_app.jobs.store import JobStore, utcnow
from helper_app.models import DiskState, DiskStatus, Job, JobPhase
from helper_app.oci.clients import describe_error
from helper_app.oci.provision import Provisioner
from helper_app.sessions import UserSession
from helper_app.vsphere.export import ExportError, NfcExport, match_disk_urls
from helper_app.vsphere.inventory import esxi_host_name

log = logging.getLogger(__name__)

PROGRESS_SAVE_BYTES = 128 * 1024 * 1024
PROGRESS_SAVE_SECONDS = 2.0  # also persist progress this often, so slow links still show movement


class JobCancelled(RuntimeError):
    pass


class MigrationRunner:
    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        provisioner: Provisioner,
        export_factory: Optional[Callable[[object], NfcExport]] = None,
    ):
        self.s = settings
        self.store = store
        self.prov = provisioner
        self.export_factory = export_factory or self._default_export_factory
        self.pool = ThreadPoolExecutor(max_workers=max(1, settings.max_concurrent_jobs),
                                       thread_name_prefix="migration")
        self._sessions: dict[str, UserSession] = {}
        self._running: set[str] = set()
        self._cancel_requested: set[str] = set()
        self._lock = threading.Lock()

    def _default_export_factory(self, vm, nfc_host: str) -> NfcExport:
        return NfcExport(
            vm,
            nfc_host=nfc_host,
            verify_ssl=self.s.nfc_verify_ssl,
            progress_interval_s=self.s.lease_progress_interval_s,
            ready_timeout_s=self.s.lease_ready_timeout_s,
            chunk_bytes=self.s.nfc_chunk_bytes,
        )

    # --------------------------------------------------------------- control
    def submit(self, job_id: str, session: UserSession) -> Future:
        """Start the migration ``job_id`` using ``session``'s vCenter connection."""
        session.pin(job_id)
        with self._lock:
            self._sessions[job_id] = session
            self._running.add(job_id)
        return self.pool.submit(self._run_safely, job_id)

    def cleanup(self, job_id: str) -> Future:
        """Tear down the OCI resources of a job that is not running (failed or restarted)."""
        with self._lock:
            self._running.add(job_id)
        return self.pool.submit(self._cleanup_safely, job_id)

    def request_cancel(self, job_id: str) -> None:
        with self._lock:
            self._cancel_requested.add(job_id)

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._running

    def _cancelled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancel_requested

    def fail_stale_jobs(self) -> list[str]:
        """Jobs that were in flight when the helper stopped cannot resume (their vCenter session is gone)."""
        failed = []
        for job in self.store.active():
            job.error = "helper restarted during the migration; cancel the job to clean up its OCI resources"
            job.phase = JobPhase.FAILED
            job.message = job.error
            self.store.put(job)
            failed.append(job.id)
        if failed:
            log.warning("marked %d interrupted job(s) as FAILED: %s", len(failed), failed)
        return failed

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------- execution
    def _save(self, job: Job, phase: Optional[JobPhase] = None, message: Optional[str] = None) -> None:
        if phase is not None:
            job.phase = phase
        if message is not None:
            job.message = message
            log.info("job %s [%s] %s", job.id, job.phase.value, message)
        self.store.put(job)

    def _finish(self, job_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(job_id, None)
            self._running.discard(job_id)
            self._cancel_requested.discard(job_id)
        if session is not None:
            session.unpin(job_id)

    def _run_safely(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            self._finish(job_id)
            return
        try:
            self._run(job)
        except JobCancelled:
            self._save(job, message="Cancelled; cleaning up OCI resources")
            try:
                self.prov.cleanup(job)
            except Exception as exc:  # noqa: BLE001
                log.warning("cleanup for %s failed: %s", job.id, describe_error(exc))
                job.phase = JobPhase.CANCELLED
                job.error = f"cleanup incomplete: {describe_error(exc)}"
                self.store.put(job)
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s failed at step %s", job_id, job.step)
            detail = describe_error(exc)
            job.error = f"step '{job.step}': {detail}" if job.step else detail
            self._save(job, JobPhase.FAILED, f"Failed in step {job.step or '?'}: {detail}")
        finally:
            self._finish(job_id)

    def _cleanup_safely(self, job_id: str) -> None:
        job = self.store.get(job_id)
        try:
            if job is not None:
                self.prov.cleanup(job)
        except Exception as exc:  # noqa: BLE001
            log.exception("cleanup of %s failed", job_id)
            if job is not None:
                job.phase = JobPhase.CANCELLED
                job.error = f"cleanup incomplete: {describe_error(exc)}"
                self.store.put(job)
        finally:
            self._finish(job_id)

    def _check_cancel(self, job: Job) -> None:
        if self._cancelled(job.id):
            raise JobCancelled()

    def _run(self, job: Job) -> None:
        session = self._sessions[job.id]

        # 1. provision the OCI target and attach its volumes to the helper
        self._save(job, JobPhase.PROVISIONING, "Requesting target instance and volumes in OCI")
        self.prov.prepare(job, check_cancel=lambda: self._check_cancel(job))
        self._check_cancel(job)

        # 2. export
        self._save(job, JobPhase.EXPORTING, "Checking the source VM")
        vm = session.vc.vm(job.vm.moid)
        power = str(vm.runtime.powerState)
        if power != "poweredOff":
            raise ExportError(f"VM is {power}; it must stay powered off during the export")
        nfc_host = self._resolve_nfc_host(job, vm, session)
        job.nfc_host = nfc_host
        self._save(job, message=f"Opening NFC export lease (disk download via {nfc_host})")
        with self.export_factory(vm, nfc_host) as export:
            urls = match_disk_urls(job.vm.disks, export.disk_urls())
            for disk in job.disks:
                disk.stream_bytes = urls[disk.index].file_size or None
            job.transfer.started_at = job.transfer.started_at or utcnow()
            job.transfer.percent = 0
            self.store.put(job)
            try:
                for disk in job.disks:
                    if disk.status == DiskStatus.COPIED:
                        continue
                    self._check_cancel(job)
                    try:
                        self._export_disk(job, disk, export, urls[disk.index].url)
                    except BaseException:
                        export.mark_failed(f"disk {disk.index}")
                        raise
                job.transfer.percent = 100
            finally:
                job.transfer.finished_at = utcnow()
                job.transfer.throughput_bps = 0.0

        # 3. finalize
        self._save(job, JobPhase.FINALIZING, "All disks copied; attaching volumes to the target instance")
        self.prov.finalize(job)
        self._save(job, message=f"Migration complete: instance {job.instance_id}")

    def _resolve_nfc_host(self, job: Job, vm, session: UserSession) -> str:
        """Host substituted for the ``*`` placeholder in the lease URLs.

        Per-job *download directly from ESXi* wins: the host the VM is registered on right now (it may
        have moved since the inspection; the inspected name is the fallback).  Otherwise the deployment
        wide ``HELPER_NFC_HOST_OVERRIDE`` applies, and by default the download is proxied by the vCenter
        this session is logged in to.
        """
        if job.target.nfc_direct_to_esxi:
            host = esxi_host_name(vm) or job.vm.host_name
            if not host:
                raise ExportError("direct ESXi download requested but vCenter reports no host for the VM")
            return host
        return self.s.nfc_host_override or session.vc.host.strip("[]")

    @staticmethod
    def _record_progress(job: Job, disk: DiskState, export: NfcExport, received: int, stats: DecodeStats,
                         rate_bps: float, written_before: int) -> None:
        """Copy the live counters of the disk being copied into the job record (what the UI polls)."""
        disk.bytes_received = received
        disk.bytes_written = stats.bytes_written
        disk.grains_written = stats.grains_written
        disk.throughput_bps = rate_bps
        total = disk.stream_bytes or disk.capacity_bytes
        disk.percent = max(0, min(99, int(received * 100 / total))) if total else 0
        job.transfer.bytes_written = written_before + disk.bytes_written
        job.transfer.throughput_bps = rate_bps
        job.transfer.percent = export.percent  # identical to the figure sent to the NFC lease / vCenter task

    def _export_disk(self, job: Job, disk: DiskState, export: NfcExport, url: str) -> None:
        last_error: Optional[Exception] = None
        label = disk.label or f"disk {disk.index}"
        for attempt in range(1, self.s.disk_retry_attempts + 1):
            self._check_cancel(job)
            disk.attempts = attempt
            disk.status = DiskStatus.COPYING
            disk.bytes_received = disk.bytes_written = disk.grains_written = 0
            disk.percent = 0
            disk.error = None
            job.step = "copying"
            self._save(job, message=f"Copying {label} to {disk.device} "
                                    f"(attempt {attempt}/{self.s.disk_retry_attempts})")

            try:
                writer = BlockDeviceWriter(disk.device, expected_min_size=disk.capacity_bytes)
            except (OSError, ValueError) as exc:
                raise ExportError(f"cannot open {disk.device}: {exc}") from exc
            meter = RateMeter()
            written_before = sum(d.bytes_written for d in job.disks if d is not disk)  # other disks' share
            try:
                writer.ensure_size(disk.capacity_bytes)
                decoder = StreamOptimizedDecoder(writer.write_at, expected_capacity_bytes=disk.capacity_bytes,
                                                 skip_zero_grains=self.s.skip_zero_grains)
                received = 0
                last_saved = 0
                last_saved_at = time.monotonic()
                for chunk in export.iter_disk(url):
                    self._check_cancel(job)
                    decoder.feed(chunk)
                    received += len(chunk)
                    job.transfer.bytes_received += len(chunk)
                    meter.add(len(chunk))
                    now = time.monotonic()
                    if received - last_saved >= PROGRESS_SAVE_BYTES or now - last_saved_at >= PROGRESS_SAVE_SECONDS:
                        last_saved, last_saved_at = received, now
                        self._record_progress(job, disk, export, received, decoder.stats, meter.rate(),
                                              written_before)
                        self.store.put(job)
                stats = decoder.finish()
                self._record_progress(job, disk, export, received, stats, meter.rate(), written_before)
                disk.percent = 100
                disk.throughput_bps = 0.0
                disk.status = DiskStatus.COPIED
                self._save(job, message=f"{label} copied ({received:,} bytes received, "
                                        f"{stats.grains_written:,} grains written)")
                return
            except JobCancelled:
                disk.status = DiskStatus.FAILED
                disk.error = "cancelled"
                raise
            except (ExportError, VmdkFormatError, OSError, httpx.HTTPError) as exc:
                last_error = exc
                disk.status = DiskStatus.FAILED
                disk.throughput_bps = 0.0
                job.transfer.throughput_bps = 0.0
                disk.error = str(exc)
                self._save(job, message=f"{label} attempt {attempt} failed: {exc}")
                if attempt < self.s.disk_retry_attempts:
                    time.sleep(min(30, 5 * attempt))
            finally:
                writer.close()
        raise ExportError(f"{label} failed after {disk.attempts} attempts: {last_error}")
