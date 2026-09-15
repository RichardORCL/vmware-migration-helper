"""Translate the vSphere VM description into OCI concepts.

* guestId / guestFullName      -> custom image operatingSystem / operatingSystemVersion
* firmware / controller / NIC  -> LaunchOptions (firmware, bootVolumeType, networkType)
* vCPU / RAM                   -> flex shape OCPUs and memory
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

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
# NB: VMware names a new server release "<previous>srvNext" until the next major vSphere release, so
#     windows2019srvNext = Windows Server 2022 (vSphere 7.0 U2+) and windows2022srvNext = Server 2025 (8.0 U2+).
_WINDOWS_VERSIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"windows2025srv"), "Server 2025 Standard"),
    (re.compile(r"windows2022srvnext"), "Server 2025 Standard"),
    (re.compile(r"windows2022srv"), "Server 2022 Standard"),
    (re.compile(r"windows2019srvnext"), "Server 2022 Standard"),
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
        # The release year vCenter displays (guestFullName) is unambiguous; the guestId encoding is not
        # (e.g. windows2019srvNext is Server 2022), so the full name wins when it names a server release.
        m = re.search(r"server\s+(20\d\d)(\s*r2)?", full)
        if m:
            ver = f"Server {m.group(1)}{' R2' if m.group(2) else ''} Standard"
            return OsMetadata("Windows", ver, "windows")
        for pattern, version in _WINDOWS_VERSIONS:
            if pattern.search(gid):
                return OsMetadata("Windows", version, "windows")
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


def remote_data_volume_type_for(boot_type: BootVolumeType) -> str:
    """OCI refuses to mix paravirtualized and emulated volumes in one instance, so the data volumes follow
    the boot volume's device class: virtio with virtio, emulated SCSI with IDE/SCSI, iSCSI with iSCSI."""
    if boot_type == BootVolumeType.PARAVIRTUALIZED:
        return "PARAVIRTUALIZED"
    if boot_type == BootVolumeType.ISCSI:
        return "ISCSI"
    return "SCSI"


def is_emulated(boot_type: BootVolumeType, net_type: NetworkType) -> bool:
    return boot_type != BootVolumeType.PARAVIRTUALIZED or net_type == NetworkType.E1000


def map_launch_options(vm: VmSpec, target: OciTarget) -> LaunchOptionsSpec:
    """Device model for the target.  Always paravirtualized (virtio) - the vSphere controller / NIC model says
    nothing about what the guest can drive in OCI, and virtio is what OCI images run on.  Only the
    *Maximum compatibility* preset (IDE + E1000, for guests without virtio drivers) or an explicit override
    picks emulated devices."""
    boot_type = BootVolumeType.PARAVIRTUALIZED
    net_type = NetworkType.PARAVIRTUALIZED
    if target.compatibility_mode:
        boot_type = BootVolumeType.IDE
        net_type = NetworkType.E1000
    if target.boot_volume_type_override:
        boot_type = target.boot_volume_type_override
    if target.network_type_override:
        net_type = target.network_type_override
    firmware = oci_firmware(vm.firmware)
    return LaunchOptionsSpec(
        firmware=firmware,
        boot_volume_type=boot_type,
        network_type=net_type,
        remote_data_volume_type=remote_data_volume_type_for(boot_type),
        is_consistent_volume_naming_enabled=True,
        # Secure Boot only exists with UEFI; vSphere reports the flag on EFI VMs only, but stay defensive
        secure_boot=bool(vm.secure_boot) and firmware == OCI_FIRMWARE_UEFI,
    )


# OCI "platform config" type per shape family; Secure Boot (shielded instances) is switched on there.
PLATFORM_AMD_VM = "AMD_VM"
PLATFORM_INTEL_VM = "INTEL_VM"
PLATFORM_GENERIC_BM = "GENERIC_BM"


def platform_config_type(shape: str) -> Optional[str]:
    """Which ``LaunchInstancePlatformConfig`` subtype a shape takes, or None when the shape family has no
    platform config with Secure Boot (Ampere A1/A2 and other ARM shapes)."""
    s = (shape or "").upper()
    if s.startswith("BM."):
        return PLATFORM_GENERIC_BM
    if not s.startswith("VM."):
        return None
    family = s[3:]  # e.g. STANDARD.E5.FLEX, STANDARD3.FLEX, STANDARD.A1.FLEX, DENSEIO2.8
    if re.search(r"\.A\d", family):
        return None  # Ampere (ARM)
    if re.search(r"\.E\d", family):
        return PLATFORM_AMD_VM  # E2..E6 AMD EPYC
    return PLATFORM_INTEL_VM  # Standard2/3, Optimized3, DenseIO2, GPU shapes: Intel


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


SEED_SECURE_BOOT_TAG = "vc-oci-secure-boot"
# tags older seed images may lack, with the value they implicitly had (so they keep being reused)
SEED_TAG_DEFAULTS = {SEED_SECURE_BOOT_TAG: "false"}


def seed_image_tags(os_meta: OsMetadata, firmware: str, secure_boot: bool = False) -> dict[str, str]:
    """Identity of a seed image.  Secure Boot is part of it: the image's capability schema must declare
    ``Compute.SecureBoot`` for OCI to accept a shielded launch from it."""
    return {
        "vc-oci-seed": "true",
        "vc-oci-firmware": firmware,
        "vc-oci-os": os_meta.slug,
        SEED_SECURE_BOOT_TAG: "true" if secure_boot else "false",
    }
