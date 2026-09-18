"""Custom image import plumbing shared by the seed images and the ISO images.

Waiting for a CreateImage import (with the work request's ``percentComplete`` as progress), explaining a
failed import from its work request, and pinning an image's capability schema so that explicit
``LaunchOptions`` are accepted at launch.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from helper_app.config import Settings
from helper_app.models import LaunchOptionsSpec
from helper_app.oci.clients import OciClients, OciError, describe_error
from helper_app.oci.mapping import is_emulated

log = logging.getLogger(__name__)

# called while an import runs: (percent complete 0-100, human readable status)
ProgressCallback = Callable[[int, str], None]


def import_launch_mode(lo: LaunchOptionsSpec) -> str:
    """Launch mode for the image import.  ``CUSTOM`` cannot be requested through the public API (it is
    what OCI reports after launch options were edited), so pick the closest supported mode; the
    capability schema and the per-job ``LaunchOptions`` take care of the details."""
    if is_emulated(lo.boot_volume_type, lo.network_type):
        return "EMULATED"
    return "PARAVIRTUALIZED"


def wait_import(c: OciClients, settings: Settings, image_id: str, work_request_id: str, display: str,
                on_progress: Optional[ProgressCallback], what: str = "image") -> None:
    """Poll the image until AVAILABLE; in between, read ``percentComplete`` of the CreateImage work request
    so the job can show how far the import is.  The image state stays authoritative: OCI deletes an image
    whose import failed, and the work request may be unreadable (no ``read work-requests``)."""
    deadline = time.monotonic() + settings.image_import_timeout_s
    last_percent = -1
    wr_readable = bool(work_request_id) and c.work_requests is not None
    while True:
        state = c.compute.get_image(image_id).data.lifecycle_state
        if state == "AVAILABLE":
            return
        if state in ("DELETED", "DISABLED"):
            raise OciError(f"{what} {display} entered state {state} while waiting for ['AVAILABLE']")
        if wr_readable and on_progress:
            try:
                wr = c.work_requests.get_work_request(work_request_id).data
            except Exception as exc:  # noqa: BLE001 - progress is best effort
                log.info("cannot read work request %s for import progress: %s", work_request_id, describe_error(exc))
                wr_readable = False
            else:
                percent = int(getattr(wr, "percent_complete", None) or 0)
                status = str(getattr(wr, "status", "") or "IN_PROGRESS")
                if percent != last_percent:
                    last_percent = percent
                    on_progress(max(0, min(99, percent)),
                                f"Importing {what} {display}: {percent}% ({status.lower().replace('_', ' ')})")
        if time.monotonic() >= deadline:
            raise OciError(f"timed out after {settings.image_import_timeout_s:.0f}s waiting for {what} "
                           f"{display} to reach ['AVAILABLE'] (last={state})")
        time.sleep(c.poll_interval_s)


def import_failure_detail(c: OciClients, work_request_id: str, bucket: str, object_name: str = "") -> str:
    """Errors and log of the CreateImage work request, or the usual cause when OCI recorded nothing."""
    if not work_request_id:
        return "OCI returned no work request id for the import"
    parts = [f"import work request {work_request_id}"]
    if c.work_requests is None:
        parts.append("(work request client not configured)")
        return "; ".join(parts)
    try:
        errors = list(c.work_requests.list_work_request_errors(work_request_id).data or [])
        logs = list(c.work_requests.list_work_request_logs(work_request_id).data or [])
    except Exception as exc:  # noqa: BLE001 - diagnostics must not mask the real failure
        parts.append(f"(could not read it: {describe_error(exc)}; check it in the OCI console)")
        return "; ".join(parts)
    parts += [f"OCI error {e.code}: {e.message}" for e in errors]
    if logs:
        parts.append("import log: " + " / ".join(entry.message for entry in logs))
    if not errors and not logs:
        obj = f" ({object_name})" if object_name else ""
        parts.append(
            "OCI recorded no import log or error, which usually means the image import service could not "
            f"read the source object{obj} from bucket '{bucket}': it fetches the object through a "
            "pre-authenticated request created as the migration tool VM, so its policy needs "
            f"\"manage buckets ... where all {{target.bucket.name = '{bucket}', "
            "request.permission = 'PAR_MANAGE'}\" (see docs/limitations.md)"
        )
    return "; ".join(parts)


def global_schema_version_name(c: OciClients) -> str:
    schemas = c.compute.list_compute_global_image_capability_schemas().data
    if not schemas:
        raise OciError("no global image capability schema available in this region")
    schema = schemas[0]
    name = getattr(schema, "current_version_name", None)
    if name:
        return name
    versions = c.compute.list_compute_global_image_capability_schema_versions(schema.id).data
    if not versions:
        raise OciError("global image capability schema has no versions")
    return versions[0].name


def apply_capability_schema(
    c: OciClients, compartment_id: str, image_id: str, firmware: str, lo: LaunchOptionsSpec, display: str,
    tags: dict[str, str], launch_mode: str = "PARAVIRTUALIZED", consistent_naming: bool = True,
) -> None:
    """Pin the firmware and allow every device model, so that the explicit ``LaunchOptions`` of each job
    (which may differ from the import defaults) are accepted at launch.  ``consistent_naming`` (Linux-only
    /dev/oracleoci paths) cannot be overridden at launch, so it is decided here per image."""
    import oci.core.models as M

    def enum(values: list[str], default: str):
        return M.EnumStringImageCapabilitySchemaDescriptor(source="IMAGE", values=values, default_value=default)

    def boolean(default: bool):
        return M.BooleanImageCapabilitySchemaDescriptor(source="IMAGE", default_value=default)

    schema_data = {
        "Compute.Firmware": enum([firmware], firmware),
        "Compute.LaunchMode": enum(["PARAVIRTUALIZED", "EMULATED", "NATIVE", "CUSTOM"], launch_mode),
        "Storage.BootVolumeType": enum(["PARAVIRTUALIZED", "ISCSI", "SCSI", "IDE"], lo.boot_volume_type.value),
        "Storage.RemoteDataVolumeType": enum(["PARAVIRTUALIZED", "ISCSI"], "PARAVIRTUALIZED"),
        "Network.AttachmentType": enum(["PARAVIRTUALIZED", "E1000", "VFIO"], lo.network_type.value),
        "Storage.ConsistentVolumeNaming": boolean(consistent_naming),
        # a shielded (Secure Boot) launch is only accepted from an image whose schema declares support
        "Compute.SecureBoot": boolean(lo.secure_boot),
    }
    details = M.CreateComputeImageCapabilitySchemaDetails(
        compartment_id=compartment_id,
        compute_global_image_capability_schema_version_name=global_schema_version_name(c),
        image_id=image_id,
        display_name=f"{display}-capabilities",
        freeform_tags=tags,
        schema_data=schema_data,
    )
    c.compute.create_compute_image_capability_schema(details)
    log.info("capability schema applied to %s: firmware=%s secure_boot=%s", image_id, firmware, lo.secure_boot)
