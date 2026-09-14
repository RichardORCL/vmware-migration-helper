# Mapping tables

Implemented in `helper/helper_app/oci/mapping.py`.

## Guest OS -> seed image metadata

| vSphere guestId (prefix) | OCI `operatingSystem` | `operatingSystemVersion` |
| --- | --- | --- |
| `oracleLinuxN_64Guest` | Oracle Linux | N |
| `rhelN_64Guest` | Red Hat Enterprise Linux | N |
| `centosN_64Guest` | CentOS | N |
| `rockylinux*` / `almalinux*` | Rocky Linux / AlmaLinux | 9 |
| `ubuntu64Guest` | Ubuntu | from `guestFullName` (e.g. 24.04), else 22.04 |
| `debianN_64Guest` | Debian | N |
| `slesN_64Guest` | SUSE Linux Enterprise Server | N |
| any Windows guest whose `guestFullName` says `Server 20xx [R2]` | Windows | Server 20xx [R2] Standard (the year vCenter shows for the source VM) |
| `windows2022srvNext_64Guest` | Windows | Server 2025 Standard |
| `windows2025srv*` / `2022srv*` / `2019srv*` | Windows | Server 2025/2022/2019 Standard |
| `windows2019srvNext_64Guest` | Windows | Server 2022 Standard |
| `windows9Server64Guest` | Windows | Server 2016 Standard |
| `windows8Server64Guest` | Windows | Server 2012 R2 Standard |
| `windows9_64Guest` / `windows11_64Guest` | Windows | 10 Enterprise / 11 Enterprise |
| other `windows*` | Windows | Server 2019 Standard |
| anything else | Custom Linux | first number in `guestFullName` |

vSphere identifies a new Windows Server release as `<previous>srvNext` until the next major vSphere
release: `windows2019srvNext_64Guest` is Windows Server 2022 (vSphere 7.0 U2+) and
`windows2022srvNext_64Guest` is Windows Server 2025 (vSphere 8.0 U2+). Because that encoding is easy to
misread, the release year in `guestFullName` takes precedence over the `guestId` table.

Windows detection also looks at `guestFullName`, so `otherGuest` VMs running Windows are still
registered as Windows (and require a license type).

## Launch options

| Source | OCI `LaunchOptions` |
| --- | --- |
| `config.firmware = bios` | `firmware = BIOS` |
| `config.firmware = efi` | `firmware = UEFI_64` |
| boot disk on IDE controller | `bootVolumeType = IDE` |
| boot disk on LSI Logic / LSI SAS / BusLogic | `bootVolumeType = SCSI` |
| boot disk on PVSCSI / SATA / NVMe | `bootVolumeType = PARAVIRTUALIZED` |
| all NICs e1000 / e1000e / pcnet32 | `networkType = E1000` |
| any vmxnet NIC (or no NIC) | `networkType = PARAVIRTUALIZED` |
| *Maximum compatibility* checkbox | `IDE` + `E1000` |
| explicit overrides | win over everything |
| always | `remoteDataVolumeType = PARAVIRTUALIZED`, `isConsistentVolumeNamingEnabled = true` |

## Shape

`ocpus = max(1, ceil(vCPU / 2))`, `memory_gb = clamp(ceil(RAM_MB / 1024), 1 x ocpus, 64 x ocpus)` on the
selected flex shape (`HELPER_DEFAULT_SHAPE`, default `VM.Standard.E5.Flex`).

## Volumes

Boot volume size = disk 0 capacity rounded up to GB, minimum 50 GB. Each additional disk becomes a
block volume with the same rule. Extra space stays unpartitioned inside the guest.

All volumes of a migration are created with the performance tier chosen under *Advanced: disk
transfer* (`volume_vpus_per_gb`): 10 VPU/GB (Balanced, default), 20 (Higher Performance) or 30
(Ultra High Performance). The tier can be changed in OCI after the migration.
