"""Pydantic models: source VM description, OCI target, job state and API payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, computed_field


# --------------------------------------------------------------------------- #
# Source VM description (collected from vCenter)
# --------------------------------------------------------------------------- #
class Firmware(str, Enum):
    BIOS = "bios"
    EFI = "efi"


class DiskSpec(BaseModel):
    """One virtual disk of the source VM, in boot order (index 0 = boot disk)."""

    index: int
    label: str
    device_key: int
    capacity_bytes: int
    controller_type: str = Field(
        description="pvscsi | lsilogic | lsilogicsas | buslogic | ide | sata | nvme | unknown"
    )
    controller_class: str = Field(default="", description="vim class name, e.g. ParaVirtualSCSIController")
    controller_bus: int = 0
    unit_number: int = 0
    thin_provisioned: bool = False
    backing_file: str = ""

    @property
    def nfc_key_hint(self) -> str:
        """Suffix of HttpNfcLease.DeviceUrl.key for this disk, e.g. 'VirtualLsiLogicController0:0'."""
        return f"{self.controller_class}{self.controller_bus}:{self.unit_number}"

    @property
    def capacity_gb_rounded(self) -> int:
        gib = 1024**3
        return max(1, -(-self.capacity_bytes // gib))


class NicSpec(BaseModel):
    label: str
    adapter_type: str = Field(description="vmxnet3 | vmxnet2 | e1000 | e1000e | pcnet32 | unknown")
    mac_address: str = ""
    network: str = ""


class VmSpec(BaseModel):
    moid: str
    name: str
    instance_uuid: str = ""
    num_cpu: int
    memory_mb: int
    guest_id: str
    guest_full_name: str = ""
    firmware: Firmware = Firmware.BIOS
    secure_boot: bool = False
    power_state: str = "poweredOff"
    has_snapshots: bool = False
    host_name: str = Field(default="", description="ESXi host the VM is registered on (vm.runtime.host.name)")
    disks: list[DiskSpec]
    nics: list[NicSpec] = Field(default_factory=list)

    @property
    def is_windows(self) -> bool:
        return "windows" in self.guest_id.lower() or "windows" in self.guest_full_name.lower()


class VmSummary(BaseModel):
    """One row of the VM list in the web UI."""

    moid: str
    name: str
    folder: str = ""
    power_state: str = "poweredOff"
    guest_full_name: str = ""
    guest_id: str = ""
    num_cpu: int = 0
    memory_mb: int = 0
    num_disks: int = 0
    disk_capacity_bytes: int = 0
    is_template: bool = False


class GuestOsMapping(BaseModel):
    """How the guest OS will be recorded on the OCI image, and whether the user has to pick the release."""

    operating_system: str
    operating_system_version: str
    version_detected: bool = Field(description="False when vSphere does not report the release and the "
                                               "version is a default that the user should confirm or change")
    version_choices: list[str] = Field(default_factory=list, description="Releases OCI knows for this OS")


class VmInspection(BaseModel):
    vm: VmSpec
    can_export: bool
    problems: list[str]
    warnings: list[str]
    os: Optional[GuestOsMapping] = None


# --------------------------------------------------------------------------- #
# OCI target description (chosen by the user in the web UI)
# --------------------------------------------------------------------------- #
class WindowsLicenseType(str, Enum):
    OCI_PROVIDED = "OCI_PROVIDED"
    BRING_YOUR_OWN_LICENSE = "BRING_YOUR_OWN_LICENSE"


class BootVolumeType(str, Enum):
    ISCSI = "ISCSI"
    SCSI = "SCSI"
    IDE = "IDE"
    VFIO = "VFIO"
    PARAVIRTUALIZED = "PARAVIRTUALIZED"


class NetworkType(str, Enum):
    E1000 = "E1000"
    VFIO = "VFIO"
    PARAVIRTUALIZED = "PARAVIRTUALIZED"


class OciTarget(BaseModel):
    compartment_id: str
    availability_domain: str
    subnet_id: str
    display_name: Optional[str] = None
    shape: Optional[str] = Field(default=None, description="Flex shape name; helper default when omitted")
    ocpus: Optional[float] = Field(
        default=None, gt=0, le=512,
        description="OCPU override; derived from the source vCPUs (2 vCPU = 1 OCPU) when omitted",
    )
    memory_gb: Optional[float] = Field(
        default=None, gt=0, le=4096, description="Memory override in GB; derived from the source RAM when omitted"
    )
    assign_public_ip: bool = False
    start_after_migration: bool = True
    operating_system_version: Optional[str] = Field(
        default=None, max_length=64,
        description="Release recorded on the OCI image (e.g. Ubuntu '24.04'); required when vSphere does not "
                    "report it (VmInspection.os.version_detected is false), otherwise overrides the detected one",
    )
    windows_license_type: Optional[WindowsLicenseType] = None
    compatibility_mode: bool = Field(
        default=False, description="Force IDE boot volume + E1000 NIC for guests without virtio drivers"
    )
    boot_volume_type_override: Optional[BootVolumeType] = None
    network_type_override: Optional[NetworkType] = None
    nfc_direct_to_esxi: bool = Field(
        default=False,
        description="Download the disks from the ESXi host the VM is registered on instead of through the "
                    "vCenter proxy (same effect as HELPER_NFC_HOST_OVERRIDE, resolved per job)",
    )
    pipelined_decode: bool = Field(
        default=False,
        description="Decode and write the VMDK stream on a separate thread (bounded queue of "
                    "HELPER_NFC_PIPELINE_DEPTH chunks) so the download is not stalled by inflate/pwrite",
    )
    volume_vpus_per_gb: Literal[10, 20, 30] = Field(
        default=10,
        description="Volume performance units per GB for the boot and block volumes created for the VM: "
                    "10 = Balanced, 20 = Higher Performance, 30 = Ultra High Performance",
    )


class LaunchOptionsSpec(BaseModel):
    """Resolved OCI LaunchOptions for the target instance."""

    firmware: str = Field(description="BIOS | UEFI_64")
    boot_volume_type: BootVolumeType = BootVolumeType.PARAVIRTUALIZED
    network_type: NetworkType = NetworkType.PARAVIRTUALIZED
    remote_data_volume_type: str = "PARAVIRTUALIZED"
    # informational: comes from the seed image's capability schema (Linux true / Windows false); OCI does not
    # accept it in LaunchOptions ("Overriding ConsistentVolumeNamingEnabled ... is not supported")
    is_consistent_volume_naming_enabled: bool = True
    secure_boot: bool = False  # source had UEFI Secure Boot -> launch as a shielded instance with Secure Boot


# --------------------------------------------------------------------------- #
# Job state
# --------------------------------------------------------------------------- #
class JobPhase(str, Enum):
    QUEUED = "QUEUED"
    PROVISIONING = "PROVISIONING"
    EXPORTING = "EXPORTING"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in (JobPhase.COMPLETED, JobPhase.FAILED, JobPhase.CANCELLED)


class DiskStatus(str, Enum):
    PENDING = "PENDING"
    ATTACHED = "ATTACHED"
    COPYING = "COPYING"
    COPIED = "COPIED"
    FAILED = "FAILED"


class DiskState(BaseModel):
    index: int
    label: str = ""
    capacity_bytes: int
    volume_id: Optional[str] = None
    is_boot: bool = False
    size_gb: int = 0
    helper_attachment_id: Optional[str] = None
    device: Optional[str] = None
    target_attachment_id: Optional[str] = None
    status: DiskStatus = DiskStatus.PENDING
    attempts: int = 0
    bytes_received: int = 0
    bytes_written: int = 0
    grains_written: int = 0
    stream_bytes: Optional[int] = None  # size of the exported VMDK stream when the NFC lease reports it
    percent: int = 0  # progress of this disk's stream (exact with stream_bytes, else bounded by capacity)
    throughput_bps: float = 0.0  # received bytes/s over the last minute while copying
    error: Optional[str] = None


class TransferStats(BaseModel):
    """Export-phase figures: what vCenter's *Export OVF template* task shows, plus the totals for the summary."""

    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    bytes_received: int = 0  # every byte pulled from vCenter, retried attempts included
    bytes_written: int = 0  # non-zero grain bytes written onto the OCI volumes
    percent: int = 0  # the percentage reported to the NFC lease (= the vCenter task progress)
    throughput_bps: float = 0.0  # over the last minute while exporting; 0 when idle

    @property
    def duration_s(self) -> Optional[float]:
        if self.started_at is None:
            return None
        end = self.finished_at or datetime.now(timezone.utc)
        return max(0.0, (end - self.started_at).total_seconds())

    @property
    def average_bps(self) -> Optional[float]:
        d = self.duration_s
        return self.bytes_received / d if d else None


class Job(BaseModel):
    id: str
    phase: JobPhase = JobPhase.QUEUED
    step: str = ""
    step_percent: Optional[int] = None  # progress of the current step when OCI reports one (work requests)
    message: str = ""
    error: Optional[str] = None
    vm: VmSpec
    vcenter_host: str = ""  # vCenter the VM was inspected on ("host" or "host:port"); tagged onto the instance
    target: OciTarget
    launch_options: Optional[LaunchOptionsSpec] = None
    seed_image_id: Optional[str] = None
    instance_id: Optional[str] = None
    instance_display_name: Optional[str] = None
    boot_volume_id: Optional[str] = None
    nfc_host: Optional[str] = None  # host the disk streams were downloaded from (vCenter or ESXi)
    disks: list[DiskState] = Field(default_factory=list)
    transfer: TransferStats = Field(default_factory=TransferStats)
    created_by: str = ""
    created_at: datetime
    updated_at: datetime
    finished_at: Optional[datetime] = None  # set when the job reaches a terminal phase

    @property
    def total_bytes(self) -> int:
        return sum(d.capacity_bytes for d in self.disks)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summary(self) -> "JobSummary":
        """Duration / volume / bandwidth figures for the job view (derived, not stored)."""
        end = self.finished_at or (datetime.now(timezone.utc) if not self.phase.terminal else self.updated_at)
        return JobSummary(
            duration_s=max(0.0, (end - self.created_at).total_seconds()),
            transfer_duration_s=self.transfer.duration_s,
            bytes_received=self.transfer.bytes_received,
            bytes_written=self.transfer.bytes_written,
            average_bps=self.transfer.average_bps,
        )


class JobSummary(BaseModel):
    duration_s: float
    transfer_duration_s: Optional[float] = None
    bytes_received: int = 0
    bytes_written: int = 0
    average_bps: Optional[float] = None


class CreateJobRequest(BaseModel):
    vm_moid: str
    target: OciTarget


class LicenseUpdateRequest(BaseModel):
    license_type: WindowsLicenseType


class InstanceStatus(BaseModel):
    """Live state of the job's target instance as OCI reports it (GET /api/jobs/{id}/instance)."""

    instance_id: str
    display_name: Optional[str] = None
    lifecycle_state: str  # OCI lifecycle state, or NOT_FOUND when OCI no longer knows the OCID
    checked_at: datetime


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
class LoginRequest(BaseModel):
    username: str
    password: str
    vcenter_host: str = ""  # defaults to HELPER_VCENTER_HOST; may be "host" or "host:port"
    vcenter_port: Optional[int] = Field(default=None, ge=1, le=65535)


class SessionInfo(BaseModel):
    username: str
    vcenter_host: str
    vcenter_port: int = 443
    vcenter_version: str = ""
    created_at: datetime
    expires_at: datetime


# --------------------------------------------------------------------------- #
# OCI inventory for the UI dropdowns
# --------------------------------------------------------------------------- #
class OciCompartment(BaseModel):
    id: str
    name: str
    path: str = ""


class OciVcn(BaseModel):
    id: str
    name: str
    cidr_blocks: list[str] = Field(default_factory=list)


class OciSubnet(BaseModel):
    id: str
    name: str
    vcn_id: str
    vcn_name: str = ""
    cidr_block: str = ""
    availability_domain: Optional[str] = None
    prohibit_public_ip: bool = False


class OciShape(BaseModel):
    name: str
    is_flex: bool
    min_ocpus: Optional[float] = None
    max_ocpus: Optional[float] = None
    min_memory_gb: Optional[float] = None
    max_memory_gb: Optional[float] = None


class OciOptions(BaseModel):
    region: str
    helper_instance_id: str
    helper_compartment_id: str
    helper_availability_domain: str
    default_shape: str
    compartments: list[OciCompartment]
    availability_domains: list[str]
    vcns: list[OciVcn]
    subnets: list[OciSubnet]
    shapes: list[OciShape]
