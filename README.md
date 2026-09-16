# OCI Ultimate Migration Tool

The **OCI Ultimate Migration Tool** moves **virtual machines from VMware vSphere (vCenter or a
standalone ESXi host) to Oracle Cloud Infrastructure compute instances** - disk for disk, straight into
OCI block volumes, with no VDDK, no OVA export and no intermediate storage. The copy is taken from a
powered-off VM: either you power it off beforehand, or the migration tool shuts it down for you right
before the disk export (after confirmation).

The tool runs on a single VM you deploy in your OCI tenancy, the **OCI Migration Tool VM**. It offers a
web UI where you log in with your vCenter (or ESXi) credentials, pick the VMs to move, choose where they
should land in OCI and watch the copy progress. Each migration produces a ready-to-run OCI instance with the original disks,
firmware mode (BIOS/UEFI, Secure Boot), CPU/memory sizing and Windows licensing settings.

## What you can use it for

- **Lift-and-shift of VMware VMs** to OCI compute: Linux and Windows guests, single or multi-disk,
  BIOS or UEFI, from any vCenter or ESXi host the migration tool VM can reach over your VPN/FastConnect.
- **Migrating from several sources** with one migration tool VM: the vCenter/ESXi address is entered at login.
- **Controlled cut-overs**: the source VM's disks stay untouched; a running VM is shut down by the
  migration tool only once the OCI side is prepared, right before the copy (guest shutdown through VMware
  Tools, hard power-off as fallback), which keeps the downtime short. The target is created, sized
  and placed (compartment, VCN/subnet, shape, OCPUs/memory) per VM.
- **Batch work**: several migrations run in parallel, further jobs queue; progress, throughput and
  a copy of the diagnostics are available per job.
- **First boot debugging**: a *Remote console* button on a completed job opens the instance's VNC console
  in the browser (OCI console connection created on the fly, tunnelled through the migration tool VM).

Not in scope: live migration of running VMs (no CBT/delta sync: the VM is off during the copy), VMware Workstation/Fusion, Hyper-V or KVM sources, and
guest-side reconfiguration (IP addresses, drivers - see the notes on VirtIO drivers for Windows in
[docs/limitations.md](docs/limitations.md)).

## Deploy to Oracle Cloud

[![Deploy to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/RichardORCL/vmware-migration-helper/raw/main/vc-oci-helper-stack.zip)

The button opens *Create stack* in Resource Manager with the committed `vc-oci-helper-stack.zip`
preloaded. Manual deployment with Terraform and all settings are described in
[docs/install-helper.md](docs/install-helper.md).

## Quick start

1. **Deploy the OCI Migration Tool VM** in OCI with the Resource Manager stack (button above, or
   `helper/deploy/terraform` locally; see [docs/install-helper.md](docs/install-helper.md)). You
   provide the subnet (must route to vCenter/ESXi over your VPN/FastConnect) and the CIDRs of the
   administrators' browsers; nothing about vCenter is configured in the stack.
2. **Open the web UI** at `https://<migration-tool-vm-ip>:8443/`, accept the self-signed certificate,
   enter the vCenter Server or ESXi host (with *Verify the server certificate* ticked only for a
   CA-signed certificate) and log in with an account that can read the inventory and export the VMs
   (`VirtualMachine.Provisioning.ExportOVF` / *Allow disk access*). The server is chosen per login, so
   one migration tool VM can migrate from several vCenters or ESXi hosts; the browser remembers the
   servers used last.
3. **Migrate**: click *Migrate* under *Source VMs*, choose the instance compartment, the network
   compartment with its VCN/subnet, an x86 flex shape (sized from the source VM as 2 vCPU = 1 OCPU, or
   set OCPUs/memory yourself) and (for Windows) the license type, and follow the progress in the *Jobs*
   view. A VM that is still powered on is shut down by the migration tool right before the disk export; you are
   asked to confirm this (by VM name) when you start the migration.

## Networking requirements

All flows are TCP and are initiated by the browser or by the migration tool VM; nothing has to reach into
your on-premises network from OCI, and the target instances need no inbound ports. The migration tool VM
is meant to run in a **private subnet without a public IP**: everything it needs in OCI is reachable through a Service
Gateway (*All <region> Services in Oracle Services Network*) and/or a NAT gateway.

| From | To | Port | Purpose |
| --- | --- | --- | --- |
| **Migration Tool VM** | **vCenter Server** (or a standalone **ESXi** host given at login) | 443 | vSphere SOAP API and the NFC disk download (vCenter proxies the ESXi hosts by default). Over your VPN / FastConnect; the migration tool VM's subnet must route to it. |
| **Migration Tool VM** | **ESXi hosts** | 443 | Only with *Download the disks directly from the ESXi host* (per migration): the migration tool VM must resolve and reach the host the VM runs on. Several times faster than the vCenter proxy. |
| **User's web browser** | **Migration Tool VM** | 8443 | Web UI and API over HTTPS (self-signed certificate by default); the *Remote console* runs over the same port as a WebSocket. Restricted by the stack to `allowed_source_cidrs`. |
| **Administrator** | **Migration Tool VM** | 22 | Optional SSH administration, same source CIDRs. |
| **Migration Tool VM** | **OCI APIs** (`iaas`, `objectstorage`, `identity` in the region) | 443 | Compute, Block Storage, Object Storage; Service Gateway or NAT gateway. |
| **Migration Tool VM** | **OCI console connection service** `instance-console.<region>.oci.oraclecloud.com` | 443 | *Remote console* of a migrated instance: SSH tunnel to the instance's VNC console. Service Gateway (*All Services in Oracle Services Network*) or NAT gateway. |
| **Migration Tool VM** | Oracle Linux yum repositories, GitHub, PyPI | 443 | Installation and *Setup -> Update now* (`dnf`, `git`, `pip`). The Oracle yum servers are in the Oracle Services Network (Service Gateway); GitHub and PyPI need a NAT gateway. |

The Resource Manager stack creates a network security group with the 8443/22 ingress rules for the
administrators' CIDRs and unrestricted egress; the migration tool VM's subnet route table must provide the paths
above (VPN/FastConnect to vSphere, Service Gateway and NAT gateway to OCI and the internet). Details and
troubleshooting in [docs/how-it-works.md](docs/how-it-works.md#networking).

## Tested operating systems

Guests that have been migrated with the OCI Ultimate Migration Tool and booted in OCI. Anything with virtio drivers is
expected to work (Windows needs the Oracle VirtIO drivers installed first, or the *Maximum
compatibility* preset); the table lists what has actually been verified. Encrypted VMs - including
Windows 11 VMs with a Virtual TPM, which vSphere only allows on encrypted VMs - cannot be exported by
vSphere; remove the vTPM and decrypt the VM in vCenter first (see
[docs/limitations.md](docs/limitations.md)).

| Operating system | Version(s) | Firmware | Result / notes |
| --- | --- | --- | --- |
| Oracle Linux | | | |
| Red Hat Enterprise Linux | | | |
| Ubuntu | | | |
| SUSE Linux Enterprise Server | | | |
| Windows Server | | | |
| Windows 10 / 11 | | | |

## Documentation

- [docs/how-it-works.md](docs/how-it-works.md) - migration mechanism, supported source environments,
  network flows, repository layout, development setup
- [docs/install-helper.md](docs/install-helper.md) - deployment, IAM, configuration reference
- [docs/architecture.md](docs/architecture.md) - internals of the migration pipeline
- [docs/os-mapping.md](docs/os-mapping.md) - guest OS, launch option and shape mapping tables
- [docs/limitations.md](docs/limitations.md) - known limitations and troubleshooting

## License

[UPL 1.0](LICENSE). The browser-side VNC client is [noVNC](https://github.com/novnc/noVNC) (MPL-2.0) with
pako (MIT), redistributed unmodified under `helper/helper_app/ui/vendor/novnc`; see
[THIRD_PARTY_LICENSES.txt](THIRD_PARTY_LICENSES.txt).
