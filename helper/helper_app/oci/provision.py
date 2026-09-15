"""Provisioning state machine for one migration job.

prepare():   seed image -> launch target -> create data volumes -> attach them to the (running) target as
             read/write shareable -> stop -> detach boot volume -> attach everything to the helper
             (paravirtualized; data volumes as the second shareable attachment); the disks are then ATTACHED
finalize():  detach from helper -> attach boot volume to target -> (start) -> COMPLETED
cleanup():   best-effort teardown after a failure or cancellation

OCI only attaches data volumes to a RUNNING instance, and the target must be STOPPED while its boot volume is
swapped.  Attaching the data volumes while the target still runs from the seed image (and keeping those
attachments) means the guest sees all its disks on its very first boot instead of having them hot-plugged
afterwards.  Emulated attachments (Maximum compatibility) cannot be shareable; those are attached after the
start in finalize().
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable

from helper_app.config import Settings
from helper_app.disk.devices import DeviceScanner, scan_block_devices, wait_for_new_device
from helper_app.models import DiskState, DiskStatus, Job, JobPhase, WindowsLicenseType
from helper_app.oci.clients import OciClients, OciError
from helper_app.oci.mapping import (
    PLATFORM_AMD_VM,
    PLATFORM_GENERIC_BM,
    PLATFORM_INTEL_VM,
    map_guest_os,
    map_launch_options,
    map_shape,
    oci_firmware,
    platform_config_type,
    volume_size_gb,
    with_os_version,
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
        scan_devices: DeviceScanner = scan_block_devices,
    ):
        self.c = clients
        self.s = settings
        self.save = save
        self.seeds = seed_service or SeedImageService(clients, settings)
        self.scan_devices = scan_devices
        # boot volumes are attached without a device path and identified by "which disk appeared", so only
        # one such attachment may be in flight at a time even with concurrent jobs
        self._attach_lock = threading.Lock()

    # ------------------------------------------------------------------ helpers
    def _step(self, job: Job, step: str, message: str = "", check: Callable[[], None] | None = None) -> None:
        if check is not None:
            check()  # give a pending cancellation a chance before the next long OCI operation
        job.step = step
        job.step_percent = None
        job.message = message or step
        log.info("job %s: %s %s", job.id, step, message)
        self.save(job)

    def _step_progress(self, job: Job, percent: int, message: str,
                       check: Callable[[], None] | None = None) -> None:
        """Progress inside the current step (e.g. ``percentComplete`` of an OCI work request)."""
        if check is not None:
            check()  # a long import is a good place to notice a cancellation
        job.step_percent = max(0, min(100, int(percent)))
        job.message = message
        log.info("job %s: %s %s%% %s", job.id, job.step, job.step_percent, message)
        self.save(job)

    @property
    def helper_id(self) -> str:
        return self.c.identity_info.instance_id

    def _free_hostname_label(self, subnet_id: str, display: str) -> str | None:
        """DNS labels are unique per subnet; a clash makes the launch fail asynchronously ("Hostname ... is
        already used in subnet"), so pick ``name``, ``name-2``, ``name-3``, ... against the subnet's private IPs.
        Falls back to the plain label when the subnet cannot be listed."""
        import oci

        base = _hostname_label(display)
        if not base:
            return None
        try:
            ips = oci.pagination.list_call_get_all_results(self.c.network.list_private_ips, subnet_id=subnet_id).data
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot list private IPs of subnet %s to check the hostname label: %s", subnet_id, exc)
            return base
        taken = {ip.hostname_label.lower() for ip in ips if getattr(ip, "hostname_label", None)}
        if base not in taken:
            return base
        for n in range(2, 1000):
            suffix = f"-{n}"
            candidate = base[: 63 - len(suffix)].rstrip("-") + suffix
            if candidate not in taken:
                log.info("hostname label %s is already used in subnet %s; using %s", base, subnet_id, candidate)
                return candidate
        return None

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
        os_meta = with_os_version(map_guest_os(vm.guest_id, vm.guest_full_name), target.operating_system_version)
        firmware = oci_firmware(vm.firmware)
        launch_options = map_launch_options(vm, target)
        shape = map_shape(vm, target, self.s.default_shape, self.s.max_memory_gb_per_ocpu)
        job.launch_options = launch_options
        # Secure Boot on the source -> shielded instance; decided up front so an unsuitable shape fails
        # before any OCI resource exists
        platform_config = (_secure_boot_platform_config(shape.shape, vm.is_windows or os_meta.is_windows)
                           if launch_options.secure_boot else None)
        if not job.disks:
            job.disks = [
                DiskState(index=d.index, label=d.label, capacity_bytes=d.capacity_bytes, is_boot=(d.index == 0))
                for d in sorted(vm.disks, key=lambda d: d.index)
            ]
        # the API pre-creates the disk records without a size (so the job view can list them right away)
        for disk in job.disks:
            if disk.size_gb <= 0:
                disk.size_gb = volume_size_gb(disk.capacity_bytes, self.s.min_volume_gb)

        # 1. seed image
        if not job.seed_image_id:
            step("seed_image", f"Resolving seed image for {firmware} / {os_meta.operating_system} "
                                           f"{os_meta.operating_system_version}")
            job.seed_image_id = self.seeds.get_or_create(
                os_meta, firmware, launch_options,
                on_progress=lambda pct, text: self._step_progress(job, pct, text, check_cancel))
            job.step_percent = None
            self.save(job)

        # 2. launch the target instance
        if not job.instance_id:
            display = target.display_name or vm.name
            shielded = ""
            if platform_config is not None:
                shielded = ", shielded: Secure Boot"
                if platform_config.is_measured_boot_enabled:
                    shielded += " + Measured Boot + TPM"
            step("launch_instance", f"Launching {display} ({shape.shape}, {shape.ocpus:g} OCPU, "
                                               f"{shape.memory_gb:g} GB, firmware {firmware}{shielded})")
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
                    hostname_label=self._free_hostname_label(target.subnet_id, display),
                ),
                source_details=M.InstanceSourceViaImageDetails(
                    source_type="image",
                    image_id=job.seed_image_id,
                    boot_volume_size_in_gbs=job.disks[0].size_gb,
                    boot_volume_vpus_per_gb=target.volume_vpus_per_gb,
                ),
                # isConsistentVolumeNamingEnabled is deliberately absent: OCI rejects any value that differs
                # from the image's Storage.ConsistentVolumeNaming ("Overriding ... is not supported"), so the
                # seed image's schema carries it (true for Linux, false for Windows)
                launch_options=M.LaunchOptions(
                    firmware=launch_options.firmware,
                    boot_volume_type=launch_options.boot_volume_type.value,
                    network_type=launch_options.network_type.value,
                    remote_data_volume_type=launch_options.remote_data_volume_type,
                ),
                freeform_tags=source_tags(job),
                metadata={},
            )
            if platform_config is not None:
                details.platform_config = platform_config
            if vm.is_windows or os_meta.is_windows:
                lic = target.windows_license_type or WindowsLicenseType.BRING_YOUR_OWN_LICENSE
                details.licensing_configs = [
                    M.LaunchInstanceWindowsLicensingConfig(type="WINDOWS", license_type=lic.value)
                ]
            instance = self.c.compute.launch_instance(details).data
            job.instance_id = instance.id
            job.instance_display_name = instance.display_name
            self.save(job)
            try:
                self.c.wait_for(lambda: self.c.compute.get_instance(job.instance_id), "lifecycle_state",
                                ["RUNNING"], self.s.launch_timeout_s, what="target instance")
            except OciError as exc:
                # a launch that fails asynchronously (VNIC, capacity, ...) only explains itself on its work request
                reason = self.c.work_request_errors(target.compartment_id, job.instance_id)
                raise OciError(f"{exc}; {reason}" if reason else str(exc)) from exc

        # 3. create data volumes
        for disk in job.disks[1:]:
            if disk.volume_id:
                continue
            step("create_volume", f"Creating {disk.size_gb} GB block volume for disk {disk.index} "
                                  f"({target.volume_vpus_per_gb} VPU/GB)")
            vol = self.c.blockstorage.create_volume(
                M.CreateVolumeDetails(
                    availability_domain=target.availability_domain,
                    compartment_id=target.compartment_id,
                    display_name=f"{job.instance_display_name or vm.name}-disk{disk.index}",
                    size_in_gbs=disk.size_gb,
                    vpus_per_gb=target.volume_vpus_per_gb,
                    freeform_tags={"vc-oci-job": job.id, "vc-oci-disk-index": str(disk.index)},
                )
            ).data
            disk.volume_id = vol.id
            self.save(job)
            self.c.wait_for(lambda vid=vol.id: self.c.blockstorage.get_volume(vid), "lifecycle_state",
                            ["AVAILABLE"], self.s.volume_timeout_s, what=f"volume for disk {disk.index}")

        # 4. attach the data volumes to the target while it is still running (OCI refuses data volume
        #    attachments on a stopped instance) as read/write shareable, so the helper can take a second
        #    attachment for the copy and the guest finds every disk in place on its first boot
        inst = self.c.compute.get_instance(job.instance_id).data
        if inst.lifecycle_state == "RUNNING" and self._shareable_target_attachments(job):
            for n, disk in enumerate(job.disks[1:], start=1):
                if disk.target_attachment_id:
                    continue
                step("attach_data_volume", f"Attaching disk {disk.index} to target (shareable, before its first boot)")
                self._attach_to_target(job, disk, n, shareable=True)

        # 5. stop it (hard stop: the placeholder image has no OS to react to ACPI)
        if inst.lifecycle_state != "STOPPED":
            step("stop_instance", "Stopping target instance")
            if inst.lifecycle_state in ("RUNNING", "STARTING", "PROVISIONING"):
                self.c.compute.instance_action(job.instance_id, "STOP")
            self.c.wait_for(lambda: self.c.compute.get_instance(job.instance_id), "lifecycle_state",
                            ["STOPPED"], self.s.launch_timeout_s, what="target instance")

        # 6. detach boot volume from the target
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

        # 7. attach everything to the helper
        for disk in job.disks:
            if disk.helper_attachment_id and disk.device:
                continue
            if disk.is_boot:
                # OCI refuses a device path for a boot volume attached as a data volume
                # ("the specified device attribute ... is invalid"); find the disk by its appearance instead
                step("attach_to_helper", f"Attaching disk {disk.index} (boot volume) to helper")
                self._attach_boot_volume_to_helper(job, disk)
            else:
                device = self._pick_free_device(job)
                step("attach_to_helper", f"Attaching disk {disk.index} volume to helper as {device}")
                # a volume that already hangs off the target must be shared on every attachment
                att = self._attach_to_helper(job, disk, device, shareable=bool(disk.target_attachment_id))
                disk.device = att.device or device
            disk.status = DiskStatus.ATTACHED
            self.save(job)

        step("ready", "Volumes attached to helper; ready to receive disk streams")
        return job

    @staticmethod
    def _remote_data_volume_type(job: Job) -> str:
        """Device class announced in ``launchOptions.remoteDataVolumeType``; every data volume attachment
        must use it."""
        return job.launch_options.remote_data_volume_type if job.launch_options else "PARAVIRTUALIZED"

    def _shareable_target_attachments(self, job: Job) -> bool:
        """Multi-attach (read/write shareable) exists for paravirtualized and iSCSI attachments only, not for
        emulated (SCSI/IDE) ones."""
        return self._remote_data_volume_type(job) not in ("SCSI", "IDE")

    def _target_device(self, job: Job, n: int) -> str | None:
        """Consistent device path of the ``n``-th data disk on the target (Linux only: OCI rejects the
        attribute for Windows instances, "device attribute ... is not supported ... for Windows")."""
        return None if _is_windows_job(job) else f"{self.s.device_prefix}{chr(ord('a') + n)}"

    def _attach_to_target(self, job: Job, disk: DiskState, n: int, shareable: bool) -> Any:
        import oci.core.models as M

        remote_type = self._remote_data_volume_type(job)
        common = dict(instance_id=job.instance_id, volume_id=disk.volume_id, device=self._target_device(job, n),
                      display_name=f"{job.instance_display_name or job.vm.name}-disk{disk.index}")
        if remote_type == "ISCSI":
            details = M.AttachIScsiVolumeDetails(type="iscsi", **common)
        elif remote_type in ("SCSI", "IDE"):
            details = M.AttachEmulatedVolumeDetails(type="emulated", **common)
        else:
            details = M.AttachParavirtualizedVolumeDetails(type="paravirtualized", **common)
        if shareable:
            details.is_shareable = True
        att = self.c.compute.attach_volume(details).data
        disk.target_attachment_id = att.id
        self.save(job)
        return self.c.wait_for(lambda: self.c.compute.get_volume_attachment(att.id), "lifecycle_state",
                               ["ATTACHED"], self.s.volume_timeout_s, what=f"target attachment disk {disk.index}")

    def _attach_to_helper(self, job: Job, disk: DiskState, device: str | None, shareable: bool = False) -> Any:
        import oci.core.models as M

        details = M.AttachParavirtualizedVolumeDetails(
            type="paravirtualized",
            instance_id=self.helper_id,
            volume_id=disk.volume_id,
            device=device,
            display_name=f"vc-oci-{job.id[:8]}-disk{disk.index}",
        )
        if shareable:
            details.is_shareable = True
        att = self.c.compute.attach_volume(details).data
        disk.helper_attachment_id = att.id
        self.save(job)
        return self.c.wait_for(lambda: self.c.compute.get_volume_attachment(att.id), "lifecycle_state",
                               ["ATTACHED"], self.s.volume_timeout_s, what=f"helper attachment disk {disk.index}")

    def _attach_boot_volume_to_helper(self, job: Job, disk: DiskState) -> None:
        expected = disk.size_gb * 1024**3
        with self._attach_lock:
            before = self.scan_devices()
            self._attach_to_helper(job, disk, None)
            try:
                disk.device = wait_for_new_device(before, expected, self.s.volume_timeout_s, self.scan_devices)
            except RuntimeError as exc:
                raise OciError(f"boot volume attached to the helper but its disk was not found: {exc}") from exc
        log.info("job %s: boot volume %s appeared on the helper as %s", job.id, disk.volume_id, disk.device)

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

        # Data volumes were normally attached (shareable) in prepare() while the target still ran.  Whatever is
        # left (emulated attachments cannot be shared) can only be attached to a RUNNING instance, so the
        # guest is started first and the disks are hot-plugged.
        pending = [(n, d) for n, d in enumerate(job.disks[1:], start=1) if not d.target_attachment_id]
        started_for_attach = False
        if pending:
            self._start_target(job, "Starting target instance (OCI attaches data volumes to running instances only)")
            started_for_attach = True
            for n, disk in pending:
                self._step(job, "attach_data_volume", f"Attaching disk {disk.index} to target")
                self._attach_to_target(job, disk, n, shareable=False)

        if target.start_after_migration:
            if not started_for_attach:
                self._start_target(job, "Starting target instance")
        elif started_for_attach:
            # the user asked for a stopped result; the guest already boots, so give it an orderly shutdown
            self._step(job, "stop_instance", "Stopping target instance again (start after migration is off)")
            self.c.compute.instance_action(job.instance_id, "SOFTSTOP")
            self.c.wait_for(lambda: self.c.compute.get_instance(job.instance_id), "lifecycle_state",
                            ["STOPPED"], self.s.launch_timeout_s, what="target instance")

        job.phase = JobPhase.COMPLETED
        self._step(job, "completed", "Migration finished")
        return job

    def _start_target(self, job: Job, message: str) -> None:
        inst = self.c.compute.get_instance(job.instance_id).data
        if inst.lifecycle_state == "RUNNING":
            return
        self._step(job, "start_instance", message)
        if inst.lifecycle_state not in ("STARTING", "PROVISIONING"):
            self.c.compute.instance_action(job.instance_id, "START")
        self.c.wait_for(lambda: self.c.compute.get_instance(job.instance_id), "lifecycle_state",
                        ["RUNNING"], self.s.launch_timeout_s, what="target instance")

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

    def _detach_from_target(self, disk: DiskState) -> None:
        att = self.c.compute.get_volume_attachment(disk.target_attachment_id).data
        if att.lifecycle_state not in ("DETACHED", "DETACHING"):
            self.c.compute.detach_volume(disk.target_attachment_id)
        self.c.wait_for(lambda: self.c.compute.get_volume_attachment(disk.target_attachment_id), "lifecycle_state",
                        ["DETACHED"], self.s.volume_timeout_s, what=f"target detach disk {disk.index}")
        disk.target_attachment_id = None

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
        # data volumes attached to the target in prepare(): release them first, otherwise deleting the volume
        # races the detach that terminating the instance triggers
        for disk in job.disks[1:]:
            if disk.target_attachment_id:
                attempt(f"detach disk {disk.index} from target",
                        lambda d=disk: self._detach_from_target(d))
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


TAG_VALUE_MAX = 256  # OCI freeform tag values are limited to 256 characters (keys to 100)


def source_tags(job: Job) -> dict[str, str]:
    """Freeform tags that record where the instance came from: the job, the source vCenter, the VM and its
    sizing (vCPU, memory, disks with their capacities, guest OS, firmware)."""
    vm = job.vm
    disks = ", ".join(f"{_gb(d.capacity_bytes):g} GB" for d in sorted(vm.disks, key=lambda d: d.index))
    total = sum(d.capacity_bytes for d in vm.disks)
    firmware = "UEFI" if vm.firmware.value.lower() == "efi" else "BIOS"
    if vm.secure_boot:
        firmware += " Secure Boot"
    details = (f"{vm.num_cpu} vCPU, {vm.memory_mb / 1024:g} GB RAM, {len(vm.disks)} disk(s) {_gb(total):g} GB "
               f"[{disks}], {len(vm.nics)} NIC(s), {vm.guest_full_name or vm.guest_id}, {firmware}")
    tags = {
        "vc-oci-job": job.id,
        "vc-oci-source-vm": vm.name[:TAG_VALUE_MAX],
        "vc-oci-source-moid": vm.moid,
        "vc-oci-source-vm-details": details[:TAG_VALUE_MAX],
    }
    if job.vcenter_host:
        tags["vc-oci-source-vcenter"] = job.vcenter_host[:TAG_VALUE_MAX]
    if vm.host_name:
        tags["vc-oci-source-esxi-host"] = vm.host_name[:TAG_VALUE_MAX]
    return tags


def _gb(n: int) -> float:
    return round(n / 1024**3, 1)


def _is_windows_job(job: Job) -> bool:
    return job.vm.is_windows or map_guest_os(job.vm.guest_id, job.vm.guest_full_name).is_windows


def _secure_boot_platform_config(shape: str, windows: bool):
    """``platform_config`` that turns Secure Boot on for the given shape (OCI calls this a shielded
    instance).

    On VM shapes OCI insists that Secure Boot, Measured Boot and the (virtual) TPM are enabled together;
    sending Secure Boot alone is rejected (for Windows images with "Invalid platform configuration for
    instances secured with Credential Guard ...").  Bare metal allows the three independently, but Windows
    is held to the same all-or-nothing rule there, while Measured Boot on Linux is VM-only."""
    import oci.core.models as M

    kind = platform_config_type(shape)
    all_three = kind != PLATFORM_GENERIC_BM or windows
    flags = dict(is_secure_boot_enabled=True, is_measured_boot_enabled=all_three,
                 is_trusted_platform_module_enabled=all_three)
    if kind == PLATFORM_AMD_VM:
        return M.AmdVmLaunchInstancePlatformConfig(**flags)
    if kind == PLATFORM_INTEL_VM:
        return M.IntelVmLaunchInstancePlatformConfig(**flags)
    if kind == PLATFORM_GENERIC_BM:
        return M.GenericBmLaunchInstancePlatformConfig(**flags)
    raise OciError(
        f"the source VM boots with UEFI Secure Boot, but shape {shape} cannot launch a shielded instance; "
        "choose an x86 shape (e.g. VM.Standard.E4/E5.Flex, VM.Standard3.Flex, VM.Optimized3.Flex)"
    )


def _hostname_label(name: str) -> str | None:
    label = re.sub(r"-+", "-", _HOSTNAME_RE.sub("-", name.lower())).strip("-")[:63].strip("-")
    return label or None
