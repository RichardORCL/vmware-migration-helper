"""Provisioning state machine for one migration job.

prepare():   seed image -> launch target -> stop -> detach boot volume -> create data volumes
             -> attach everything to the helper (paravirtualized); the disks are then ATTACHED
finalize():  detach from helper -> attach boot + data volumes to target -> (start) -> COMPLETED
cleanup():   best-effort teardown after a failure or cancellation
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from helper_app.config import Settings
from helper_app.models import DiskState, DiskStatus, Job, JobPhase, WindowsLicenseType
from helper_app.oci.clients import OciClients, OciError
from helper_app.oci.mapping import (
    map_guest_os,
    map_launch_options,
    map_shape,
    oci_firmware,
    volume_size_gb,
)
from helper_app.oci.seed_image import SeedImageService

log = logging.getLogger(__name__)

_HOSTNAME_RE = re.compile(r"[^a-z0-9-]")


class Provisioner:
    def __init__(
        self,
        clients: OciClients,
        settings: Settings,
        save: Callable[[Job], Job],
        seed_service: SeedImageService | None = None,
    ):
        self.c = clients
        self.s = settings
        self.save = save
        self.seeds = seed_service or SeedImageService(clients, settings)

    # ------------------------------------------------------------------ helpers
    def _step(self, job: Job, step: str, message: str = "", check: Callable[[], None] | None = None) -> None:
        if check is not None:
            check()  # give a pending cancellation a chance before the next long OCI operation
        job.step = step
        job.message = message or step
        log.info("job %s: %s %s", job.id, step, message)
        self.save(job)

    @property
    def helper_id(self) -> str:
        return self.c.identity_info.instance_id

    # ------------------------------------------------------------------ prepare
    def prepare(self, job: Job, check_cancel: Callable[[], None] | None = None) -> Job:
        """``check_cancel`` is called before every step and may raise to abort provisioning."""
        import oci.core.models as M

        def step(name: str, message: str = "") -> None:
            self._step(job, name, message, check_cancel)

        vm, target = job.vm, job.target
        if not vm.disks:
            raise OciError("source VM has no virtual disks")
        if target.availability_domain != self.c.identity_info.availability_domain:
            raise OciError(
                f"target availability domain {target.availability_domain} differs from the helper's "
                f"{self.c.identity_info.availability_domain}; boot volumes can only be attached within one AD"
            )

        job.phase = JobPhase.PROVISIONING
        os_meta = map_guest_os(vm.guest_id, vm.guest_full_name)
        firmware = oci_firmware(vm.firmware)
        launch_options = map_launch_options(vm, target)
        shape = map_shape(vm, target, self.s.default_shape, self.s.max_memory_gb_per_ocpu)
        job.launch_options = launch_options
        if not job.disks:
            job.disks = [
                DiskState(index=d.index, label=d.label, capacity_bytes=d.capacity_bytes, is_boot=(d.index == 0),
                          size_gb=volume_size_gb(d.capacity_bytes, self.s.min_volume_gb))
                for d in sorted(vm.disks, key=lambda d: d.index)
            ]

        # 1. seed image
        if not job.seed_image_id:
            step("seed_image", f"Resolving seed image for {firmware} / {os_meta.operating_system} "
                                           f"{os_meta.operating_system_version}")
            job.seed_image_id = self.seeds.get_or_create(os_meta, firmware, launch_options)
            self.save(job)

        # 2. launch the target instance
        if not job.instance_id:
            display = target.display_name or vm.name
            step("launch_instance", f"Launching {display} ({shape.shape}, {shape.ocpus:g} OCPU, "
                                               f"{shape.memory_gb:g} GB, firmware {firmware})")
            details = M.LaunchInstanceDetails(
                availability_domain=target.availability_domain,
                compartment_id=target.compartment_id,
                display_name=display,
                shape=shape.shape,
                shape_config=M.LaunchInstanceShapeConfigDetails(ocpus=shape.ocpus, memory_in_gbs=shape.memory_gb),
                create_vnic_details=M.CreateVnicDetails(
                    subnet_id=target.subnet_id,
                    assign_public_ip=target.assign_public_ip,
                    display_name=display,
                    hostname_label=_hostname_label(display),
                ),
                source_details=M.InstanceSourceViaImageDetails(
                    source_type="image",
                    image_id=job.seed_image_id,
                    boot_volume_size_in_gbs=job.disks[0].size_gb,
                ),
                launch_options=M.LaunchOptions(
                    firmware=launch_options.firmware,
                    boot_volume_type=launch_options.boot_volume_type.value,
                    network_type=launch_options.network_type.value,
                    remote_data_volume_type=launch_options.remote_data_volume_type,
                    is_consistent_volume_naming_enabled=launch_options.is_consistent_volume_naming_enabled,
                ),
                freeform_tags={
                    "vc-oci-job": job.id,
                    "vc-oci-source-vm": vm.name[:100],
                    "vc-oci-source-moid": vm.moid,
                },
                metadata={},
            )
            if vm.is_windows or os_meta.is_windows:
                lic = target.windows_license_type or WindowsLicenseType.BRING_YOUR_OWN_LICENSE
                details.licensing_configs = [
                    M.LaunchInstanceWindowsLicensingConfig(type="WINDOWS", license_type=lic.value)
                ]
            instance = self.c.compute.launch_instance(details).data
            job.instance_id = instance.id
            job.instance_display_name = instance.display_name
            self.save(job)
            self.c.wait_for(lambda: self.c.compute.get_instance(job.instance_id), "lifecycle_state",
                            ["RUNNING"], self.s.launch_timeout_s, what="target instance")

        # 3. stop it (hard stop: the placeholder image has no OS to react to ACPI)
        inst = self.c.compute.get_instance(job.instance_id).data
        if inst.lifecycle_state != "STOPPED":
            step("stop_instance", "Stopping target instance")
            if inst.lifecycle_state in ("RUNNING", "STARTING", "PROVISIONING"):
                self.c.compute.instance_action(job.instance_id, "STOP")
            self.c.wait_for(lambda: self.c.compute.get_instance(job.instance_id), "lifecycle_state",
                            ["STOPPED"], self.s.launch_timeout_s, what="target instance")

        # 4. detach boot volume from the target
        if not job.boot_volume_id:
            step("detach_boot_volume", "Detaching boot volume from target")
            atts = self.c.compute.list_boot_volume_attachments(
                target.availability_domain, target.compartment_id, instance_id=job.instance_id
            ).data
            atts = [a for a in atts if a.lifecycle_state in ("ATTACHED", "ATTACHING")]
            if not atts:
                raise OciError("target instance has no attached boot volume")
            att = atts[0]
            job.boot_volume_id = att.boot_volume_id
            job.disks[0].volume_id = att.boot_volume_id
            self.save(job)
            self.c.compute.detach_boot_volume(att.id)
            self.c.wait_for(lambda: self.c.compute.get_boot_volume_attachment(att.id), "lifecycle_state",
                            ["DETACHED"], self.s.volume_timeout_s, what="boot volume attachment")

        # 5. create data volumes
        for disk in job.disks[1:]:
            if disk.volume_id:
                continue
            step("create_volume", f"Creating {disk.size_gb} GB block volume for disk {disk.index}")
            vol = self.c.blockstorage.create_volume(
                M.CreateVolumeDetails(
                    availability_domain=target.availability_domain,
                    compartment_id=target.compartment_id,
                    display_name=f"{job.instance_display_name or vm.name}-disk{disk.index}",
                    size_in_gbs=disk.size_gb,
                    freeform_tags={"vc-oci-job": job.id, "vc-oci-disk-index": str(disk.index)},
                )
            ).data
            disk.volume_id = vol.id
            self.save(job)
            self.c.wait_for(lambda vid=vol.id: self.c.blockstorage.get_volume(vid), "lifecycle_state",
                            ["AVAILABLE"], self.s.volume_timeout_s, what=f"volume for disk {disk.index}")

        # 6. attach everything to the helper
        for disk in job.disks:
            if disk.helper_attachment_id and disk.device:
                continue
            device = self._pick_free_device(job)
            step("attach_to_helper", f"Attaching disk {disk.index} volume to helper as {device}")
            att = self.c.compute.attach_volume(
                M.AttachParavirtualizedVolumeDetails(
                    type="paravirtualized",
                    instance_id=self.helper_id,
                    volume_id=disk.volume_id,
                    device=device,
                    display_name=f"vc-oci-{job.id[:8]}-disk{disk.index}",
                )
            ).data
            disk.helper_attachment_id = att.id
            self.save(job)
            att = self.c.wait_for(lambda aid=att.id: self.c.compute.get_volume_attachment(aid), "lifecycle_state",
                                  ["ATTACHED"], self.s.volume_timeout_s, what=f"helper attachment disk {disk.index}")
            disk.device = att.device or device
            disk.status = DiskStatus.ATTACHED
            self.save(job)

        step("ready", "Volumes attached to helper; ready to receive disk streams")
        return job

    def _pick_free_device(self, job: Job) -> str:
        used = {d.device for d in job.disks if d.device}
        devices = self.c.compute.list_instance_devices(self.helper_id, is_available=True).data
        # oraclevdb..oraclevdz come before oraclevdaa..; sort by length first, then name
        names = sorted((d.name for d in devices if d.is_available and d.name.startswith(self.s.device_prefix)),
                       key=lambda n: (len(n), n))
        for name in names:
            if name not in used:
                return name
        raise OciError("no free consistent device path on the helper (max 32 attachments)")

    # ----------------------------------------------------------------- finalize
    def finalize(self, job: Job) -> Job:
        import oci.core.models as M

        not_copied = [d.index for d in job.disks if d.status != DiskStatus.COPIED]
        if not_copied:
            raise OciError(f"disks {not_copied} have not been copied")
        job.phase = JobPhase.FINALIZING
        self._detach_all_from_helper(job)

        target = job.target
        boot = job.disks[0]
        if not boot.target_attachment_id:
            self._step(job, "attach_boot_volume", "Attaching boot volume to target")
            att = self.c.compute.attach_boot_volume(
                M.AttachBootVolumeDetails(boot_volume_id=boot.volume_id, instance_id=job.instance_id)
            ).data
            boot.target_attachment_id = att.id
            self.save(job)
            self.c.wait_for(lambda: self.c.compute.get_boot_volume_attachment(att.id), "lifecycle_state",
                            ["ATTACHED"], self.s.volume_timeout_s, what="target boot volume attachment")

        use_iscsi = (job.launch_options and job.launch_options.remote_data_volume_type == "ISCSI")
        for n, disk in enumerate(job.disks[1:], start=1):
            if disk.target_attachment_id:
                continue
            self._step(job, "attach_data_volume", f"Attaching disk {disk.index} to target")
            device = f"{self.s.device_prefix}{chr(ord('a') + n)}"
            if use_iscsi:
                details = M.AttachIScsiVolumeDetails(type="iscsi", instance_id=job.instance_id,
                                                     volume_id=disk.volume_id, device=device)
            else:
                details = M.AttachParavirtualizedVolumeDetails(type="paravirtualized", instance_id=job.instance_id,
                                                               volume_id=disk.volume_id, device=device)
            att = self.c.compute.attach_volume(details).data
            disk.target_attachment_id = att.id
            self.save(job)
            self.c.wait_for(lambda aid=att.id: self.c.compute.get_volume_attachment(aid), "lifecycle_state",
                            ["ATTACHED"], self.s.volume_timeout_s, what=f"target attachment disk {disk.index}")

        if target.start_after_migration:
            self._step(job, "start_instance", "Starting target instance")
            self.c.compute.instance_action(job.instance_id, "START")
            self.c.wait_for(lambda: self.c.compute.get_instance(job.instance_id), "lifecycle_state",
                            ["RUNNING"], self.s.launch_timeout_s, what="target instance")

        job.phase = JobPhase.COMPLETED
        self._step(job, "completed", "Migration finished")
        return job

    def _detach_all_from_helper(self, job: Job) -> None:
        for disk in job.disks:
            if not disk.helper_attachment_id:
                continue
            self._step(job, "detach_from_helper", f"Detaching disk {disk.index} from helper")
            att = self.c.compute.get_volume_attachment(disk.helper_attachment_id).data
            if att.lifecycle_state not in ("DETACHED", "DETACHING"):
                self.c.compute.detach_volume(disk.helper_attachment_id)
            self.c.wait_for(lambda aid=disk.helper_attachment_id: self.c.compute.get_volume_attachment(aid),
                            "lifecycle_state", ["DETACHED"], self.s.volume_timeout_s,
                            what=f"helper detach disk {disk.index}")
            disk.helper_attachment_id = None
            disk.device = None
            self.save(job)

    # ------------------------------------------------------------------ cleanup
    def cleanup(self, job: Job) -> list[str]:
        """Best-effort teardown of everything this job created.  Returns a list of actions."""
        actions: list[str] = []

        def attempt(desc: str, fn: Callable[[], Any]) -> None:
            try:
                fn()
                actions.append(f"ok: {desc}")
            except Exception as exc:  # noqa: BLE001
                actions.append(f"failed: {desc}: {exc}")

        try:
            self._detach_all_from_helper(job)
        except Exception as exc:  # noqa: BLE001
            actions.append(f"failed: detach from helper: {exc}")
        if job.instance_id:
            attempt(f"terminate instance {job.instance_id}",
                    lambda: self.c.compute.terminate_instance(job.instance_id, preserve_boot_volume=False))
        for disk in job.disks:
            if disk.volume_id and disk.is_boot:
                attempt(f"delete boot volume {disk.volume_id}",
                        lambda vid=disk.volume_id: self.c.blockstorage.delete_boot_volume(vid))
            elif disk.volume_id:
                attempt(f"delete volume {disk.volume_id}",
                        lambda vid=disk.volume_id: self.c.blockstorage.delete_volume(vid))
        job.phase = JobPhase.CANCELLED
        job.step = "cancelled"
        job.message = "; ".join(actions) or "nothing to clean up"
        self.save(job)
        return actions

    # ---------------------------------------------------------------- licensing
    def update_windows_license(self, instance_id: str, license_type: WindowsLicenseType) -> Any:
        import oci.core.models as M

        details = M.UpdateInstanceDetails(
            licensing_configs=[M.UpdateInstanceWindowsLicensingConfig(type="WINDOWS", license_type=license_type.value)]
        )
        return self.c.compute.update_instance(instance_id, details).data


def _hostname_label(name: str) -> str | None:
    label = re.sub(r"-+", "-", _HOSTNAME_RE.sub("-", name.lower())).strip("-")[:63].strip("-")
    return label or None
