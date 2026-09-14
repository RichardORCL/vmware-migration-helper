"""Diagnostics bundle for a job: everything needed to analyse a failure, as one text block.

Shown behind the *Copy diagnostics* button of the job view.  Contains the helper identity, the
complete job record and the relevant lines of the service journal (lines mentioning the job id,
warnings/errors and tracebacks since the job was created).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Callable

from helper_app import __version__
from helper_app.config import Settings
from helper_app.models import Job
from helper_app.oci.clients import HelperIdentity
from helper_app.updater import Runner, _default_runner

JOURNAL_MAX_LINES = 400
_LEVEL_RE = re.compile(r"\b(WARNING|ERROR|CRITICAL|Traceback)\b")


def journal_excerpt(job: Job, service: str, run: Runner = _default_runner) -> str:
    """Relevant service journal lines since one minute before the job was created."""
    since = (job.created_at - timedelta(minutes=1)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    rc, out = run(["journalctl", "-u", service, "--since", since, "--no-pager", "-o", "short-iso", "--utc"], 30.0)
    if rc != 0:
        return f"(journal not available: {out.strip() or rc})"
    keep: list[str] = []
    in_traceback = False
    for line in out.splitlines():
        # journal lines: "<timestamp> <host> <unit>[pid]: <message>"
        message = line.split("]: ", 1)[1] if "]: " in line else line
        continuation = message.startswith((" ", "\t")) or message.startswith(("File ", "Traceback"))
        if job.id in message or _LEVEL_RE.search(message):
            keep.append(line)
            in_traceback = "Traceback" in message or " ERROR " in line
        elif in_traceback and (continuation or not message.startswith("20")):
            keep.append(line)  # traceback body / exception line following an ERROR
            if not continuation and re.match(r"^\w+(\.\w+)*(Error|Exception|Cancelled)\b", message):
                in_traceback = False
        else:
            in_traceback = False
    if len(keep) > JOURNAL_MAX_LINES:
        keep = [f"... {len(keep) - JOURNAL_MAX_LINES} earlier lines omitted ..."] + keep[-JOURNAL_MAX_LINES:]
    return "\n".join(keep) or "(no matching journal lines)"


def _num(v) -> str:
    return "-" if v is None else f"{v:.0f}"


def collect(job: Job, settings: Settings, ident: HelperIdentity, commit: str,
            run: Runner = _default_runner, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> str:
    """The text block copied to the clipboard."""
    disks = "\n".join(
        f"  disk {d.index} {d.label or ''} {d.capacity_bytes} bytes boot={d.is_boot} status={d.status.value} "
        f"attempts={d.attempts} received={d.bytes_received} written={d.bytes_written} volume={d.volume_id or '-'} "
        f"device={d.device or '-'}" + (f"\n    error: {d.error}" if d.error else "")
        for d in job.disks
    )
    lo = job.launch_options
    header = [
        "=== vCenter to OCI helper - job diagnostics ===",
        f"generated: {now().isoformat(timespec='seconds')}",
        f"helper: version {__version__} commit {commit or '-'}",
        f"helper identity: instance {ident.instance_id} compartment {ident.compartment_id} "
        f"region {ident.region} AD {ident.availability_domain}",
        f"settings: vcenter_default={settings.vcenter_host or '-'} "
        f"nfc_host_override={settings.nfc_host_override or '-'} seed_bucket={settings.seed_bucket} "
        f"seed_compartment={settings.seed_compartment_id or '(helper)'} "
        f"max_concurrent_jobs={settings.max_concurrent_jobs} disk_retry_attempts={settings.disk_retry_attempts}",
        "",
        f"job {job.id}: phase={job.phase.value} step={job.step or '-'}",
        f"  created {job.created_at.isoformat(timespec='seconds')} by {job.created_by or '-'}; "
        f"updated {job.updated_at.isoformat(timespec='seconds')}",
        f"  message: {job.message or '-'}",
        f"  error: {job.error or '-'}",
        f"  source VM: {job.vm.name} ({job.vm.moid}) guest={job.vm.guest_id} firmware={job.vm.firmware} "
        f"cpu={job.vm.num_cpu} mem_mb={job.vm.memory_mb} disks={len(job.vm.disks)}",
        f"  target: compartment={job.target.compartment_id} AD={job.target.availability_domain} "
        f"subnet={job.target.subnet_id} shape={job.target.shape or '(default)'} "
        f"public_ip={job.target.assign_public_ip} license={job.target.windows_license_type}",
        f"  launch options: {lo.firmware} boot={lo.boot_volume_type.value} nic={lo.network_type.value}" if lo
        else "  launch options: -",
        f"  instance={job.instance_id or '-'} seed_image={job.seed_image_id or '-'} "
        f"boot_volume={job.boot_volume_id or '-'}",
        f"  transfer: percent={job.transfer.percent} received={job.transfer.bytes_received} "
        f"written={job.transfer.bytes_written} "
        f"duration_s={_num(job.transfer.duration_s)} average_bps={_num(job.transfer.average_bps)} "
        f"job_duration_s={_num(job.summary.duration_s)}",
        "  disks:",
        disks or "  (none)",
        "",
        "--- job record (JSON) ---",
        json.dumps(job.model_dump(mode="json"), indent=2, sort_keys=True),
        "",
        f"--- helper journal ({settings.update_service}) ---",
        journal_excerpt(job, settings.update_service, run),
    ]
    return "\n".join(header) + "\n"
