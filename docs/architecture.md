# Architecture

## Single component

The whole tool is one service, the **OCI Ultimate Migration Tool**, running on a compute instance in OCI
(the *OCI Migration Tool VM*):

- a web UI (`/ui`) and REST API (`/api`) served by FastAPI/uvicorn over TLS on port 8443;
- a pyVmomi client that logs in to vCenter with the *user's* credentials, or a small `httpx` REST client
  that logs in to Azure with the *user's* service principal (`azure/client.py`: OAuth2 client credentials
  against Entra ID, Azure Resource Manager calls, long-running-operation polling on the
  `Azure-AsyncOperation` / `Location` headers with `Retry-After`; no `azure-*` SDK dependencies);
- the migration engine that provisions the OCI target, pulls the disks from the source (NFC export from
  vCenter, or page-range download of the exported managed disks from Azure) and writes them onto OCI
  volumes attached to the migration tool itself.

There is no component inside vCenter or Azure and no shared secret: vCenter RBAC / Azure RBAC decide who
may export which VM, OCI IAM (instance principal + dynamic group policy) decides what the migration tool
may create.

## Authentication and sessions

- `POST /api/auth/login` calls `SmartConnect` against the vCenter given on the login page
  (`host[:port]`; optional default `HELPER_VCENTER_HOST`) with the submitted user name/password and
  the *Verify the server certificate* choice (unticked: an SSL context without verification; ticked:
  pyVmomi's default, the system CA store). On success the migration tool stores the pyVmomi
  `ServiceInstance` together with that host and the TLS choice in a `UserSession` and sets an opaque,
  HttpOnly, SameSite=strict cookie (`vcoci_session`). One migration tool can therefore serve several
  vCenters; each session (and the NFC download of the jobs it starts, which reuses the TLS choice) is
  bound to the vCenter it logged in to.
- `POST /api/auth/azure/login` takes a tenant ID, an application (client) ID and a client secret,
  obtains a token for `https://management.azure.com/.default` with the OAuth2 client-credentials grant
  (`AADSTS*` errors are mapped to readable 401s: wrong secret, unknown tenant/application, missing
  consent) and lists the subscriptions the principal can read (403 when there are none). The
  `AzureSession` (credentials, cached token with automatic refresh, subscription list) lives in the same
  `UserSession` slot family as the vCenter session: a session is either anonymous, a vCenter login or an
  Azure login, and `GET /api/auth/me` reports which (`azure_tenant_id`, `azure_client_id`,
  `azure_subscriptions`). Logging in to the other platform replaces the session.
- Every `/api/vms/*`, `/api/azure/*`, `/api/jobs/*`, `/api/oci/*` and `/api/setup/*` request requires that
  cookie; `/api/health` and `/api/auth/config` are public. `/api/vms/*` and `POST /api/jobs` need a vCenter
  login, `/api/azure/*` and `POST /api/jobs/azure` an Azure login (`require_vcenter_session` /
  `require_azure_session`).
- Sessions expire after `HELPER_SESSION_TTL_S` (default 8 h) of inactivity or on logout.
- A session that started a migration is **pinned** by the job: logout/expiry make the cookie
  unusable immediately, but the vCenter / Azure connection is only closed after the job has finished, so
  the NFC lease is never orphaned and the export SAS / snapshots can always be revoked. A new login (same
  or different user) can watch the job.

## Job lifecycle

`Job.kind` is `vmware` (VM from vSphere), `azure` (VM from Azure, with `Job.azure` holding the
subscription, resource group, capture mode, the disk IDs, the snapshots created and the SAS state) or
`iso`.
`Job.phase`: `QUEUED -> PROVISIONING -> EXPORTING -> FINALIZING -> COMPLETED | FAILED | CANCELLED`.
`Job.step`/`Job.message` carry the fine-grained progress; `Job.step_percent` is set while a step is backed
by an OCI work request with a `percentComplete` (the seed image import polls its `CreateImage` work
request between image state checks; reading it is best effort and needs `read work-requests`).
`Job.disks[]` holds the per-disk state
(`PENDING -> ATTACHED -> COPYING -> COPIED | FAILED`, with `bytes_received`, `bytes_written`,
`grains_written`, `attempts`, `stream_bytes` (size of the exported stream when the lease reports it),
`percent` and `throughput_bps` (received bytes/s over the last minute)).
`Job.transfer` aggregates the export phase: `percent` is the same value the runner reports to the NFC
lease, i.e. what vCenter shows on its *Export OVF template* task; `bytes_received` counts every byte
pulled from vCenter including retried attempts; `started_at`/`finished_at` bracket the export.
`Job.summary` (derived on read, not stored) gives `duration_s`, `transfer_duration_s`,
`bytes_received`, `bytes_written` and `average_bps` for finished jobs; `Job.finished_at` is set when a
job reaches a terminal phase. Progress is persisted every 128 MiB or 2 seconds, whichever comes first.

`MigrationRunner` runs each job in its own thread; at most `HELPER_MAX_CONCURRENT_JOBS` of them hold a
migration slot at a time (adjustable on the *Setup* page at runtime - raising it starts queued jobs,
lowering it never interrupts a running one), the rest stay **QUEUED** ("Waiting for a free migration slot"):

1. **PROVISIONING** (`Provisioner.prepare`)
   - map guest OS -> seed image metadata, firmware -> `BIOS`/`UEFI_64`, device model -> launch
     options (paravirtualized unless *Maximum compatibility* / overrides), vCPU/RAM -> flex shape
     ([os-mapping.md](os-mapping.md));
   - `SeedImageService.get_or_create`: import a 1 GB placeholder stream-optimized VMDK as a custom
     image (`launchMode` PARAVIRTUALIZED, or EMULATED for IDE/E1000), pin its capability schema
     (firmware fixed; all boot volume and NIC types allowed), reuse by freeform tags on later jobs;
   - `LaunchInstance` from the seed image with explicit `launchOptions`, `shapeConfig`, optional
     `licensingConfigs` (Windows) and a boot volume sized for disk 0. The instance carries
     provenance freeform tags: `vc-oci-job`, `vc-oci-source-vcenter` (the vCenter the job was
     started against, `host[:port]`), `vc-oci-source-esxi-host`, `vc-oci-source-vm`,
     `vc-oci-source-moid` and `vc-oci-source-vm-details` (sizing: vCPU, RAM, disk count and
     capacities, NICs, guest OS, firmware/Secure Boot); Azure jobs carry
     `vc-oci-source-azure = <subscription>/<resource group>` instead of the vCenter tags and the Azure
     resource ID as `vc-oci-source-moid`;
   - create one block volume per additional disk and attach them to the target **while it is
     still running** from the seed image, as read/write *shareable* attachments (OCI only attaches
     data volumes to a `RUNNING` instance, and the target has to be stopped for the boot volume
     swap below). These attachments are kept, so the guest finds all its disks on its first boot.
     Emulated attachments (*Maximum compatibility*, `SCSI`/`IDE`) cannot be shareable and are
     hot-plugged in the finalize step instead;
   - stop the target (hard stop; the placeholder has no OS); detach its boot volume;
   - attach boot + data volumes to the migration tool (paravirtualized; the data volumes as the second
     shareable attachment). Data volumes get consistent device names (`/dev/oracleoci/oraclevd*`);
     OCI does not allow a device path for a boot volume attached as a data volume, so the migration tool
     snapshots `/sys/block`, attaches, and takes the one new disk of the expected size (serialised
     across jobs).
   - A pending cancellation is honoured between provisioning steps.
2. **EXPORTING**
   - re-check the power state; a VM that is still powered on (and whose job carries the operator's
     `power_off_source` confirmation) is shut down now (`vsphere/power.py`: `ShutdownGuest` when
     Tools runs, waiting `HELPER_GUEST_SHUTDOWN_TIMEOUT_S`, else/then `PowerOffVM_Task`); the
     outcome is stored as `job.power_off_result`;
   - `vm.ExportVm()` on the user's vCenter session, wait for the
     lease to be `ready`, keep it alive with `HttpNfcLeaseProgress` every
     `HELPER_LEASE_PROGRESS_INTERVAL_S`;
   - match lease `deviceUrl`s to the VM disks (controller/bus/unit key, then `disk-N.vmdk` target
     id, then order); rewrite the `*` host placeholder to the ESXi host the VM is registered on
     (`vm.runtime.host.name`, when the job was started with *Download the disks directly from the
     ESXi host*), else `HELPER_NFC_HOST_OVERRIDE`, else the vCenter host; the chosen host is
     recorded as `Job.nfc_host`;
   - per disk: HTTPS `GET` the stream-optimized VMDK in `HELPER_NFC_CHUNK_BYTES` chunks and feed it
     to `StreamOptimizedDecoder`, which inflates each grain and `pwrite()`s it at
     `lba * 512` on the attached volume (`BlockDeviceWriter`). All-zero grains are skipped
     (`HELPER_SKIP_ZERO_GRAINS`, fresh volumes read as zero). With *Decode and write on a separate
     thread* (`OciTarget.pipelined_decode`) the decoder runs behind a bounded queue of
     `HELPER_NFC_PIPELINE_DEPTH` chunks (`PipelinedDecoder`), so the socket keeps being read while
     grains are inflated and written; decoder/writer errors are re-raised on the download thread
     with their original type. A failure restarts the disk from the beginning, up to
     `HELPER_DISK_RETRY_ATTEMPTS` times; the lease is completed or aborted on exit.
   - **Azure jobs** (`MigrationRunner._run_azure`) replace the NFC part of this phase:
     - *deallocate* mode: a VM that is still running (or stopped but allocated) and whose job carries the
       operator's `power_off_source` confirmation is deallocated now (`POST .../deallocate`, polled up to
       `HELPER_AZURE_DEALLOCATE_TIMEOUT_S`; `power_off_result = deallocated`, or `already_off`); without
       the confirmation a running VM fails the job before any export. *snapshot* mode never touches the
       VM: `AzureDiskExport` creates one snapshot per disk (`PUT .../snapshots/<name>`, polled up to
       `HELPER_AZURE_SNAPSHOT_TIMEOUT_S`, `power_off_result = snapshotted`) and records their IDs in
       `Job.azure.snapshot_ids` as soon as each exists, so cleanup can find them;
     - `beginGetAccess` (read SAS, `HELPER_AZURE_SAS_DURATION_S`) on every disk or snapshot; the granted
       resource IDs and the expiry are stored in `Job.azure.sas_granted` / `sas_expires_at`;
     - per disk (`_copy_azure_disk`): `disk/vhd_range_copy.py` reads the blob length (minus the 512-byte
       VHD footer), lists the allocated page ranges (`GET ?comp=pagelist`, paginated with `marker`) and
       downloads them in `HELPER_AZURE_RANGE_CHUNK_BYTES` pieces with `HELPER_AZURE_RANGE_WORKERS` threads,
       each `pwrite()`-ing at its offset on the attached volume (`BlockDeviceWriter`, positional, so no
       ordering is needed). A single range is retried with backoff (3 tries) before the disk attempt
       fails; a 403 from the blob refreshes the SAS (`beginGetAccess` again) and continues; a disk attempt
       failure restarts the disk like the NFC path, up to `HELPER_DISK_RETRY_ATTEMPTS`. `disk.stream_bytes`
       is the allocated size, so `percent` is exact and `bytes_received == bytes_written`;
     - on exit (success, failure or cancel) `AzureDiskExport.close()` calls `endGetAccess` on every granted
       resource and deletes the snapshots it created, clearing the corresponding lists on the job.
   - guest fix-up (`guest/fixup.py`, Linux only, while the boot volume is still attached to the
     migration tool): the guest root is located and mounted once (`partx`, LVM activation with a filter on
     that disk, `find_root`, `/boot` from the guest's fstab) and the opt-in steps run on it -
     `initramfs.py` (chroot dracut with virtio drivers for kernels lacking them,
     `OciTarget.rebuild_initramfs`) and `network.py` (NetworkManager wildcard DHCP keyfile /
     first-boot unit for legacy network-scripts / netplan / networkd drop-in, MAC-pinned udev rules
     disabled, SELinux labels via the guest's `setfiles`, `OciTarget.fix_network`). Each step ends in
     its own `GuestFixup` (`Job.guest_fixup`, `Job.network_fixup`: done / not_needed / skipped /
     failed with a log) and never fails the migration.
3. **FINALIZING** (`Provisioner.finalize`)
   - detach all volumes from the migration tool (the data volumes stay attached to the target), attach
     the boot volume to the target instance, start it unless *start after migration* is off.
   - Data volumes that could not be pre-attached (emulated attachments) need a running instance:
     the target is started first and they are hot-plugged (with consistent device paths for Linux
     guests, without for Windows); with *start after migration* off it is then soft-stopped again.

Cancellation sets a flag checked between chunks and steps; the runner then runs
`Provisioner.cleanup` (terminate the instance, delete volumes, best effort) and the job ends
`CANCELLED`. A `FAILED` job keeps its OCI resources for inspection; *cancel* on a failed job runs
the same cleanup. For Azure jobs the cleanup additionally revokes any export SAS and deletes any
snapshots still recorded on the job (`_release_azure`), using the Azure session of the user who cancels
(the job's own session when it is still pinned, else a later Azure login); when no Azure session is
available the job message names the resources left behind so they can be released by hand
(`az disk revoke-access`, `az snapshot delete`). If it failed with every disk `COPIED` and an instance launched (i.e. inside
`finalize`), `POST /api/jobs/{id}/finalize` (*Retry finalize* in the UI) re-runs
`Provisioner.finalize` alone: it is idempotent (skips attachments that already exist) and needs no
vCenter session, so the copied data is not exported again.

### Restart behaviour

Jobs are persisted in SQLite (`HELPER_DB_PATH`). Because a running export depends on the user's
in-memory vCenter or Azure session (and its lease / export SAS), jobs still in
`PROVISIONING`/`EXPORTING`/`FINALIZING` when the migration tool starts are marked `FAILED` ("migration tool
restarted..."); cancel them from the UI to clean up and start again. An Azure job's SAS grants and
snapshots survive the restart in the job record, so cancelling it after a fresh Azure login releases them.

## Why no temporary storage

ESXi always serves NFC exports as *stream-optimized* VMDKs: a sequence of (LBA, deflate-compressed
64 KB grain) records with the grain directory at the end. Each record is self-describing, so the
decoder writes every grain to its final position as soon as it arrives and never needs the whole
file. Memory use is a few MB per running disk; the OCI volumes are the only storage involved.

Azure exports a managed disk as a fixed VHD in a page blob, whose *allocated* page ranges can be listed
and fetched by offset. The blob's data section is byte-for-byte the disk image (the VHD footer is the last
512 bytes), so every range is written at the same offset on the OCI volume as it arrives; unallocated
ranges are never transferred. Memory use is bounded by workers x chunk size per running disk.

## Firmware and seed images

OCI takes an instance's firmware and device model from its image. Platform images do not expose
those knobs, so the migration tool imports a placeholder VMDK as a custom image per
(firmware, OS, Secure Boot, launch mode) combination (imported as PARAVIRTUALIZED, or EMULATED for the
IDE/E1000 compatibility preset; `CUSTOM` cannot be requested through the import API, and OCI rejects a
paravirtualized launch from an EMULATED image as "mixing paravirtualized and emulated volumes", so reuse
also matches on the image's `launchMode`), applies a `ComputeImageCapabilitySchema` that fixes
`Compute.Firmware`, sets `Compute.SecureBoot` to whether the source used Secure Boot, allows every
`Storage.BootVolumeType` / `Network.AttachmentType`, and defaults `Storage.RemoteDataVolumeType` /
`Storage.LocalDataVolumeType` to the boot volume's device class (OCI resolves the data volume model from
the schema, not from the launch request: an IDE boot with a schema still defaulting the data volumes to
PARAVIRTUALIZED is refused as "mixing paravirtualized and emulated volumes"; a reused image with such a
stale schema is repaired before the launch), and launches from it with the job's explicit
`launchOptions`. A source with `efiSecureBootEnabled` is launched with a `platformConfig`
(`AMD_VM`, `INTEL_VM` or `GENERIC_BM` depending on the shape family) that has `isSecureBootEnabled`,
i.e. as a shielded instance. On VM shapes (and for Windows on bare metal) `isMeasuredBootEnabled` and
`isTrustedPlatformModuleEnabled` are set as well, because OCI rejects Secure Boot on its own there
("... Secure Boot, Measured Boot, and the Trusted Platform Module must be enabled"). Shapes without such
a platform config (Ampere) are refused before any resource is created. The seed's boot volume is replaced by the copied disk before the instance ever
boots. Seed images are tagged `vc-oci-seed=true` (plus `vc-oci-firmware`, `vc-oci-os`,
`vc-oci-secure-boot`; seeds from before the Secure Boot tag count as `false`) and can be removed with
`DELETE /api/seed-images`.

## Windows licensing

A Windows guest is registered as `operatingSystem=Windows` on the seed image and launched with
`licensingConfigs=[{type: WINDOWS, licenseType: BRING_YOUR_OWN_LICENSE | OCI_PROVIDED}]`; the
UI requires a choice before starting and lets you change it afterwards
(`POST /api/jobs/{id}/licensing` -> `UpdateInstance`).

## Security

- Credentials are never stored; the migration tool holds a pyVmomi session cookie, or the Azure service
  principal secret and its bearer token, per logged-in user in memory only. The browser remembers the
  Azure tenant and client ID in `localStorage`, never the secret.
- The API is protected by the session cookie (HttpOnly, SameSite=strict, `Secure` unless
  `HELPER_COOKIE_SECURE=false` for local development).
- OCI access uses the migration tool's instance principal; the Terraform stack scopes the policy to a
  compartment (`policy_scope_compartment_ocid`).
- vCenter TLS verification is chosen per login (*Verify the server certificate*, off unless
  `HELPER_VCENTER_VERIFY_SSL=true`) because most vCenters use the VMCA certificate; tick it when your
  vCenter has a certificate from a CA the VM trusts. The same choice governs the NFC disk download.
- Required vCenter privileges for the login account: read-only on the inventory plus
  `VirtualMachine.Provisioning.ExportOVF` (*Allow disk access*) on the VMs to migrate.
- Required Azure RBAC for the service principal: *Reader* on the subscriptions plus
  `Microsoft.Compute/virtualMachines/deallocate/action`, `Microsoft.Compute/disks/beginGetAccess/action`,
  `Microsoft.Compute/disks/endGetAccess/action`, `Microsoft.Compute/snapshots/write`,
  `Microsoft.Compute/snapshots/delete`, `Microsoft.Compute/snapshots/beginGetAccess/action` and
  `Microsoft.Compute/snapshots/endGetAccess/action` on the resource groups holding the VMs. Azure TLS is
  always verified (public CA certificates). The export SAS is read-only, time-limited and revoked by the
  job.
