"""Translate the vSphere VM description into OCI concepts.

* guestId / guestFullName      -> custom image operatingSystem / operatingSystemVersion
* firmware / controller / NIC  -> LaunchOptions (firmware, bootVolumeType, networkType)
* vCPU / RAM                   -> flex shape OCPUs and memory
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from helper_app.models import BootVolumeType, Firmware, LaunchOptionsSpec, NetworkType, OciTarget, VmSpec

OCI_FIRMWARE_BIOS = "BIOS"
OCI_FIRMWARE_UEFI = "UEFI_64"


@dataclass(frozen=True)
class OsMetadata:
    operating_system: str
    operating_system_version: str
    family: str  # "linux" | "windows"

    @property
    def is_windows(self) -> bool:
        return self.family == "windows"

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", f"{self.operating_system}-{self.operating_system_version}".lower()).strip("-")


# vSphere guestId (lowercase, without the trailing "guest") -> (OS, version)
_WINDOWS_VERSIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"windows2025srv"), "Server 2025 Standard"),
    (re.compile(r"windows2022srv"), "Server 2022 Standard"),
    (re.compile(r"windows2019srv"), "Server 2019 Standard"),
    (re.compile(r"windows9srv|windows9server"), "Server 2016 Standard"),
    (re.compile(r"windows8srv|windows8server"), "Server 2012 R2 Standard"),
    (re.compile(r"windows7srv|windows7server"), "Server 2008 R2 Standard"),
    (re.compile(r"windows1[12]_64|windows11|windows12"), "11 Enterprise"),
    (re.compile(r"windows9_64|windows9"), "10 Enterprise"),
]

_LINUX_RULES: list[tuple[re.Pattern[str], str, str | None]] = [
    # pattern, operating_system, fixed version (None -> derive from the numeric suffix)
    (re.compile(r"^oraclelinux(\d+)"), "Oracle Linux", None),
    (re.compile(r"^rhel(\d+)"), "Red Hat Enterprise Linux", None),
    (re.compile(r"^centos(\d+)"), "CentOS", None),
    (re.compile(r"^rockylinux"), "Rocky Linux", "9"),
    (re.compile(r"^almalinux"), "AlmaLinux", "9"),
    (re.compile(r"^ubuntu"), "Ubuntu", "22.04"),
    (re.compile(r"^debian(\d+)"), "Debian", None),
    (re.compile(r"^sles(\d+)"), "SUSE Linux Enterprise Server", None),
    (re.compile(r"^opensuse"), "openSUSE", "15"),
    (re.compile(r"^fedora"), "Fedora", "40"),
    (re.compile(r"^amazonlinux(\d+)"), "Amazon Linux", None),
    (re.compile(r"^freebsd(\d+)"), "FreeBSD", None),
]

_FULLNAME_VERSION = re.compile(r"(\d+(?:\.\d+)?)")


def map_guest_os(guest_id: str, guest_full_name: str = "") -> OsMetadata:
    gid = (guest_id or "").lower()
    gid = gid[:-5] if gid.endswith("guest") else gid
    full = (guest_full_name or "").lower()

    if gid.startswith("windows") or "windows" in full:
        for pattern, version in _WINDOWS_VERSIONS:
            if pattern.search(gid):
                return OsMetadata("Windows", version, "windows")
        m = re.search(r"server\s+(20\d\d)(\s*r2)?", full)
        if m:
            ver = f"Server {m.group(1)}{' R2' if m.group(2) else ''} Standard"
            return OsMetadata("Windows", ver, "windows")
        return OsMetadata("Windows", "Server 2019 Standard", "windows")

    for pattern, os_name, fixed in _LINUX_RULES:
        m = pattern.search(gid)
        if m:
            version = fixed
            if version is None:
                version = m.group(1)
            if os_name == "Ubuntu":
                fm = re.search(r"(\d\d\.\d\d)", full)
                if fm:
                    version = fm.group(1)
            return OsMetadata(os_name, version, "linux")

    fm = _FULLNAME_VERSION.search(full)
    return OsMetadata("Custom Linux", fm.group(1) if fm else "unknown", "linux")


def oci_firmware(firmware: Firmware) -> str:
    return OCI_FIRMWARE_UEFI if firmware == Firmware.EFI else OCI_FIRMWARE_BIOS


def _boot_volume_type_for_controller(controller_type: str) -> BootVolumeType:
    c = (controller_type or "").lower()
    if c == "ide":
        return BootVolumeType.IDE
    if c in ("lsilogic", "lsilogicsas", "buslogic"):
        return BootVolumeType.SCSI
    # pvscsi, sata (AHCI), nvme: the guest already runs modern storage stacks -> virtio works best
    return BootVolumeType.PARAVIRTUALIZED


def _network_type_for_nics(vm: VmSpec) -> NetworkType:
    types = {n.adapter_type.lower() for n in vm.nics}
    if types and types <= {"e1000", "e1000e", "pcnet32", "vlance"}:
        return NetworkType.E1000
    return NetworkType.PARAVIRTUALIZED


def map_launch_options(vm: VmSpec, target: OciTarget) -> LaunchOptionsSpec:
    boot_type = _boot_volume_type_for_controller(vm.disks[0].controller_type if vm.disks else "")
    net_type = _network_type_for_nics(vm)
    if target.compatibility_mode:
        boot_type = BootVolumeType.IDE
        net_type = NetworkType.E1000
    if target.boot_volume_type_override:
        boot_type = target.boot_volume_type_override
    if target.network_type_override:
        net_type = target.network_type_override
    return LaunchOptionsSpec(
        firmware=oci_firmware(vm.firmware),
        boot_volume_type=boot_type,
        network_type=net_type,
        remote_data_volume_type="PARAVIRTUALIZED",
        is_consistent_volume_naming_enabled=True,
    )


@dataclass(frozen=True)
class ShapeConfig:
    shape: str
    ocpus: float
    memory_gb: float


def map_shape(
    vm: VmSpec,
    target: OciTarget,
    default_shape: str,
    max_memory_gb_per_ocpu: int = 64,
    min_memory_gb_per_ocpu: int = 1,
) -> ShapeConfig:
    shape = target.shape or default_shape
    # OCI counts OCPUs (physical cores); a vCPU is one hardware thread -> 2 vCPU per OCPU.
    ocpus = max(1, math.ceil(vm.num_cpu / 2))
    memory_gb = max(1, math.ceil(vm.memory_mb / 1024))
    memory_gb = min(max(memory_gb, ocpus * min_memory_gb_per_ocpu), ocpus * max_memory_gb_per_ocpu)
    return ShapeConfig(shape=shape, ocpus=float(ocpus), memory_gb=float(memory_gb))


def volume_size_gb(capacity_bytes: int, min_volume_gb: int = 50) -> int:
    return max(min_volume_gb, math.ceil(capacity_bytes / 1024**3))


def seed_image_tags(os_meta: OsMetadata, firmware: str) -> dict[str, str]:
    return {
        "vc-oci.seed": "true",
        "vc-oci.firmware": firmware,
        "vc-oci.os": os_meta.slug,
    }
