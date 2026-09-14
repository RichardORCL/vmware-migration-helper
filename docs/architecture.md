# Architecture

## Single component

The whole tool is one service, the **helper**, running on a compute instance in OCI:

- a web UI (`/ui`) and REST API (`/api`) served by FastAPI/uvicorn over TLS on port 8443;
- a pyVmomi client that logs in to vCenter with the *user's* credentials;
- the migration engine that provisions the OCI target, pulls the NFC export from vCenter and
  writes the disks onto OCI volumes attached to the helper itself.

There is no component inside vCenter and no shared secret: vCenter RBAC decides who may export
which VM, OCI IAM (instance principal + dynamic group policy) decides what the helper may create.

## Authentication and sessions

- `POST /api/auth/login` calls `SmartConnect` against the vCenter given on the login page
  (`host[:port]`; default `HELPER_VCENTER_HOST`) with the submitted user name/password. On success
  the helper stores the pyVmomi `ServiceInstance` together with that host in a `UserSession` and
  sets an opaque, HttpOnly, SameSite=strict cookie (`vcoci_session`). One helper can therefore
  serve several vCenters; each session (and the NFC download of the jobs it starts) is bound to the
  vCenter it logged in to.
- Every `/api/vms/*`, `/api/jobs/*`, `/api/oci/*` and `/api/setup/*` request requires that cookie;
  `/api/health` and `/api/auth/config` are public.
- Sessions expire after `HELPER_SESSION_TTL_S` (default 8 h) of inactivity or on logout.
- A session that started a migration is **pinned** by the job: logout/expiry make the cookie
  unusable immediately, but the vCenter connection is only closed after the job has finished, so
  the NFC lease is never orphaned. A new login (same or different user) can watch the job.

## Job lifecycle

`Job.phase`: `QUEUED -> PROVISIONING -> EXPORTING -> FINALIZING -> COMPLETED | FAILED | CANCELLED`.
`Job.step`/`Job.message` carry the fine-grained progress, `Job.disks[]` the per-disk state
(`PENDING -> ATTACHED -> COPYING -> COPIED | FAILED`, with `bytes_received`, `bytes_written`,
`grains_written`, `attempts`).

`MigrationRunner` runs each job in its own thread (pool size `HELPER_MAX_CONCURRENT_JOBS`):

1. **PROVISIONING** (`Provisioner.prepare`)
   - map guest OS -> seed image metadata, firmware -> `BIOS`/`UEFI_64`, controller/NIC -> launch
     options, vCPU/RAM -> flex shape ([os-mapping.md](os-mapping.md));
   - `SeedImageService.get_or_create`: import a 1 GB placeholder stream-optimized VMDK as a custom
     image (`launchMode` PARAVIRTUALIZED, or EMULATED for IDE/E1000), pin its capability schema
     (firmware fixed; all boot volume and NIC types allowed), reuse by freeform tags on later jobs;
   - `LaunchInstance` from the seed image with explicit `launchOptions`, `shapeConfig`, optional
     `licensingConfigs` (Windows) and a boot volume sized for disk 0; stop it; detach its boot
     volume;
   - create one block volume per additional disk; attach boot + data volumes to the helper
     (paravirtualized, consistent device names `/dev/oracleoci/oraclevd*`).
   - A pending cancellation is honoured between provisioning steps.
2. **EXPORTING**
   - re-check the VM is powered off, `vm.ExportVm()` on the user's vCenter session, wait for the
     lease to be `ready`, keep it alive with `HttpNfcLeaseProgress` every
     `HELPER_LEASE_PROGRESS_INTERVAL_S`;
   - match lease `deviceUrl`s to the VM disks (controller/bus/unit key, then `disk-N.vmdk` target
     id, then order); rewrite the `*` host placeholder to `HELPER_NFC_HOST_OVERRIDE` or the vCenter
     host;
   - per disk: HTTPS `GET` the stream-optimized VMDK in `HELPER_NFC_CHUNK_BYTES` chunks and feed it
     to `StreamOptimizedDecoder`, which inflates each grain and `pwrite()`s it at
     `lba * 512` on the attached volume (`BlockDeviceWriter`). All-zero grains are skipped
     (`HELPER_SKIP_ZERO_GRAINS`, fresh volumes read as zero). A failure restarts the disk from the
     beginning, up to `HELPER_DISK_RETRY_ATTEMPTS` times; the lease is completed or aborted on exit.
3. **FINALIZING** (`Provisioner.finalize`)
   - detach all volumes from the helper, attach the boot volume and the data volumes (in order)
     to the target instance, start it unless *start after migration* is off.

Cancellation sets a flag checked between chunks and steps; the runner then runs
`Provisioner.cleanup` (terminate the instance, delete volumes, best effort) and the job ends
`CANCELLED`. A `FAILED` job keeps its OCI resources for inspection; *cancel* on a failed job runs
the same cleanup.

### Restart behaviour

Jobs are persisted in SQLite (`HELPER_DB_PATH`). Because a running export depends on the user's
in-memory vCenter session and its lease, jobs still in `PROVISIONING`/`EXPORTING`/`FINALIZING`
when the helper starts are marked `FAILED` ("helper restarted..."); cancel them from the UI to
clean up and start again.

## Why no temporary storage

ESXi always serves NFC exports as *stream-optimized* VMDKs: a sequence of (LBA, deflate-compressed
64 KB grain) records with the grain directory at the end. Each record is self-describing, so the
decoder writes every grain to its final position as soon as it arrives and never needs the whole
file. Memory use is a few MB per running disk; the OCI volumes are the only storage involved.

## Firmware and seed images

OCI takes an instance's firmware and device model from its image. Platform images do not expose
those knobs, so the helper imports a placeholder VMDK as a custom image per
(firmware, OS) combination (imported as PARAVIRTUALIZED or EMULATED; `CUSTOM` cannot be requested
through the import API), applies a `ComputeImageCapabilitySchema` that fixes `Compute.Firmware` and
allows every `Storage.BootVolumeType` / `Network.AttachmentType`, and launches from it with the
job's explicit `launchOptions`. The seed's boot volume is replaced by the copied disk before the instance
ever boots. Seed images are tagged `vc-oci-seed=true` and can be removed with
`DELETE /api/seed-images`.

## Windows licensing

A Windows guest is registered as `operatingSystem=Windows` on the seed image and launched with
`licensingConfigs=[{type: WINDOWS, licenseType: BRING_YOUR_OWN_LICENSE | OCI_PROVIDED}]`; the
UI requires a choice before starting and lets you change it afterwards
(`POST /api/jobs/{id}/licensing` -> `UpdateInstance`).

## Security

- Credentials are never stored; the helper holds a pyVmomi session cookie per logged-in user in
  memory only.
- The API is protected by the session cookie (HttpOnly, SameSite=strict, `Secure` unless
  `HELPER_COOKIE_SECURE=false` for local development).
- OCI access uses the helper's instance principal; the Terraform stack scopes the policy to a
  compartment (`policy_scope_compartment_ocid`).
- vCenter TLS verification is off by default (`HELPER_VCENTER_VERIFY_SSL`, `HELPER_NFC_VERIFY_SSL`)
  because most vCenters use the VMCA certificate; enable it when your vCenter has a trusted
  certificate.
- Required vCenter privileges for the login account: read-only on the inventory plus
  `VirtualMachine.Provisioning.ExportOVF` (*Allow disk access*) on the VMs to migrate.
