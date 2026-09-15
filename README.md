# VMware to OCI Compute migration helper

Move **powered-off virtual machines from VMware vSphere (vCenter or a standalone ESXi host) to Oracle
Cloud Infrastructure compute instances** - disk for disk, straight into OCI block volumes, with no
VDDK, no OVA export and no intermediate storage.

The helper is a single VM you deploy in your OCI tenancy. It offers a web UI where you log in with
your vCenter (or ESXi) credentials, pick the VMs to move, choose where they should land in OCI and
watch the copy progress. Each migration produces a ready-to-run OCI instance with the original disks,
firmware mode (BIOS/UEFI, Secure Boot), CPU/memory sizing and Windows licensing settings.

## What you can use it for

- **Lift-and-shift of VMware VMs** to OCI compute: Linux and Windows guests, single or multi-disk,
  BIOS or UEFI, from any vCenter or ESXi host the helper can reach over your VPN/FastConnect.
- **Migrating from several sources** with one helper: the vCenter/ESXi address is entered at login.
- **Controlled cut-overs**: the source VM stays untouched (it must be powered off during the copy);
  the target is created, sized and placed (compartment, VCN/subnet, shape, OCPUs/memory) per VM.
- **Batch work**: several migrations run in parallel, further jobs queue; progress, throughput and
  a copy of the diagnostics are available per job.
- **First boot debugging**: a *Remote console* button on a completed job opens the instance's VNC console
  in the browser (OCI console connection created on the fly, tunnelled through the helper).

Not in scope: running VMs (no live/CBT sync), VMware Workstation/Fusion, Hyper-V or KVM sources, and
guest-side reconfiguration (IP addresses, drivers - see the notes on VirtIO drivers for Windows in
[docs/limitations.md](docs/limitations.md)).

## Deploy to Oracle Cloud

[![Deploy to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/RichardORCL/vmware-migration-helper/raw/main/vc-oci-helper-stack.zip)

The button opens *Create stack* in Resource Manager with the committed `vc-oci-helper-stack.zip`
preloaded. Manual deployment with Terraform and all settings are described in
[docs/install-helper.md](docs/install-helper.md).

## Quick start

1. **Deploy the helper** in OCI with the Resource Manager stack (button above, or
   `helper/deploy/terraform` locally; see [docs/install-helper.md](docs/install-helper.md)). You
   provide the vCenter host, the subnet (must route to vCenter over your VPN/FastConnect) and the
   CIDRs of the administrators' browsers.
2. **Open the web UI** at `https://<helper-ip>:8443/`, accept the self-signed certificate and log in
   with a vCenter account that can read the inventory and export the VMs
   (`VirtualMachine.Provisioning.ExportOVF` / *Allow disk access*). The vCenter server field is
   pre-filled from the stack but can be changed, so one helper can migrate from several vCenters or
   ESXi hosts.
3. **Migrate**: power off the VM in vCenter, click *Migrate* under *Source VMs*, choose the instance
   compartment, the network compartment with its VCN/subnet, an x86 flex shape (sized from the source VM
   as 2 vCPU = 1 OCPU, or set OCPUs/memory yourself) and (for Windows) the license type, and follow the
   progress in the *Jobs* view.

## Tested operating systems

Guests that have been migrated with the helper and booted in OCI. Anything with virtio drivers is
expected to work (Windows needs the Oracle VirtIO drivers installed first, or the *Maximum
compatibility* preset); the table lists what has actually been verified.

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
