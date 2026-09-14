"""Pydantic models: source VM description, OCI target, job state and API payloads."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


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


class VmInspection(BaseModel):
    vm: VmSpec
    can_export: bool
    problems: list[str]
    warnings: list[str]


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
    assign_public_ip: bool = False
    start_after_migration: bool = True
    windows_license_type: Optional[WindowsLicenseType] = None
    compatibility_mode: bool = Field(
        default=False, description="Force IDE boot volume + E1000 NIC for guests without virtio drivers"
    )
    boot_volume_type_override: Optional[BootVolumeType] = None
    network_type_override: Optional[NetworkType] = None


class LaunchOptionsSpec(BaseModel):
    """Resolved OCI LaunchOptions for the target instance."""

    firmware: str = Field(description="BIOS | UEFI_64")
    boot_volume_type: BootVolumeType = BootVolumeType.PARAVIRTUALIZED
    network_type: NetworkType = NetworkType.PARAVIRTUALIZED
    remote_data_volume_type: str = "PARAVIRTUALIZED"
    is_consistent_volume_naming_enabled: bool = True


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
    error: Optional[str] = None


class Job(BaseModel):
    id: str
    phase: JobPhase = JobPhase.QUEUED
    step: str = ""
    message: str = ""
    error: Optional[str] = None
    vm: VmSpec
    target: OciTarget
    launch_options: Optional[LaunchOptionsSpec] = None
    seed_image_id: Optional[str] = None
    instance_id: Optional[str] = None
    instance_display_name: Optional[str] = None
    boot_volume_id: Optional[str] = None
    disks: list[DiskState] = Field(default_factory=list)
    created_by: str = ""
    created_at: datetime
    updated_at: datetime

    @property
    def total_bytes(self) -> int:
        return sum(d.capacity_bytes for d in self.disks)


class CreateJobRequest(BaseModel):
    vm_moid: str
    target: OciTarget


class LicenseUpdateRequest(BaseModel):
    license_type: WindowsLicenseType


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
