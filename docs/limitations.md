# Limitations and troubleshooting

## Known limitations

- **Powered-off only.** The UI only offers *Migrate* for powered-off VMs; the helper re-checks the power state right before opening the NFC lease. Templates are hidden.
- **One vCenter, one availability domain.** The helper is configured for a single vCenter (`HELPER_VCENTER_HOST`). Boot volumes cannot leave their AD, so target instances are created in the helper's AD. Deploy one helper per vCenter/AD pair if needed.
- **Guest drivers.** The disk content is copied verbatim. Linux guests need virtio drivers (in-tree since kernel 2.6.25; check the initramfs includes `virtio_blk`/`virtio_scsi`/`virtio_net`). Windows guests need the OCI/virtio drivers installed before the export, or use the *Maximum compatibility* preset (IDE + E1000) and install the drivers afterwards.
- **Secure Boot** is not enabled on the target even if the source used it.
- **Snapshots** are consolidated by the export (the current disk state is copied); snapshot history is not migrated.
- **Network configuration** inside the guest is untouched; static IPs must be adjusted after the migration. Only one VNIC is created.
- **Retries restart a disk from the beginning** because NFC downloads cannot be resumed; progress of already copied disks is kept.
- **Concurrency** is bounded by the helper's 32 attachment slots (`HELPER_MAX_CONCURRENT_JOBS` defaults to 2).
- **Helper restarts abort running jobs.** The export runs on the in-memory vCenter session of the user who started it; after a service restart such jobs are marked `FAILED` and must be cleaned up and restarted.
- **Seed image import time.** The first job for a new firmware/OS combination waits for a custom image import (minutes). Later jobs reuse the image.
- **Windows licensing** is honoured because the seed image is registered as Windows; OCI applies its own eligibility rules for `OCI_PROVIDED`.
- **Session idle timeout** (`HELPER_SESSION_TTL_S`, 8 h) logs the browser out, but never interrupts a running migration.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Login fails with *cannot connect to vCenter* | From the helper: `curl -k https://<vcenter>/sdk`; routing/VPN, NSG egress, `HELPER_VCENTER_HOST`/`PORT`; `HELPER_VCENTER_VERIFY_SSL=false` for VMCA certificates. |
| Login fails with *invalid user name or password* | Use the vCenter SSO form (`user@vsphere.local` or `DOMAIN\user`); the account must be allowed to log in to vCenter. |
| VM list is empty / a VM is missing | vCenter RBAC: the account needs read access to the VM; templates are hidden. Click *Refresh* (the list is cached for 30 s per session). |
| `the availability domain must be the helper's` | Choose the helper's AD in the dialog. |
| Seed image stuck in `IMPORTING` | Object Storage policy / bucket; `HELPER_IMAGE_IMPORT_TIMEOUT_S`. |
| `cannot open /dev/oracleoci/oraclevdX` | Helper container needs `--privileged` and `/dev` mounted; instance must use consistent device naming. |
| `NFC lease did not become ready` / `GET ... returned HTTP 4xx` | The account lacks `VirtualMachine.Provisioning.ExportOVF`; or the NFC URL host is unreachable from the helper (set `HELPER_NFC_HOST_OVERRIDE` only if lease URLs point at ESXi hosts you can reach). |
| `invalid VMDK stream` / `bad magic` | Something other than a stream-optimized VMDK was returned (a login page or error body); check the lease URL host and proxies between the helper and vCenter. |
| Disk retried three times then `FAILED` | Persistent network trouble between the helper and vCenter/ESXi; check MTU on the VPN/FastConnect path. |
| Target does not boot | Firmware mismatch (compare *Launch options* in the job view with the source), missing virtio drivers (try compatibility mode), or a BIOS guest whose disk uses GPT without a protective MBR. Use the OCI console connection to inspect. |
| Job `FAILED` with resources left behind | Inspect in the OCI console (tags `vc-oci-job=<id>`), then *Clean up OCI resources* in the job view (`POST /api/jobs/{id}/cancel`). |
| Job `FAILED: helper restarted` | Expected after a service restart; clean up and start the migration again. |

## Manual end-to-end test procedure

1. Deploy the helper; confirm `/api/health`, log in to the web UI and check that the VM list matches the inventory.
2. Create a small Oracle Linux 9 test VM (UEFI, PVSCSI, vmxnet3, 20 GB thin) in vCenter, install, note its disk checksum (`sha256sum /dev/sda` from a live ISO) and power it off.
3. Click *Migrate* in the VM list, keep the defaults and start. Expected sequence in the job view: PROVISIONING (seed image on first run) -> EXPORTING with per-disk progress -> FINALIZING -> COMPLETED.
4. In OCI, confirm the instance has `firmware=UEFI_64`, paravirtualized boot/network, a 50 GB boot volume, and boots to the console login.
5. Repeat with a BIOS + LSI Logic + e1000 Windows Server VM choosing BYOL; confirm `launchOptions.firmware=BIOS`, `bootVolumeType=SCSI`, `networkType=E1000`, `licensingConfigs=[WINDOWS/BRING_YOUR_OWN_LICENSE]`, then change the license type from the job view and verify in the console.
6. Test failure paths: power the VM on before the export starts (job fails with a clear message), cancel a job mid-copy (instance and volumes removed), log out while a job is copying (job completes; log in again to see it).
