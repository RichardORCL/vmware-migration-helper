# Installing the helper VM

## Prerequisites

- An OCI tenancy with a VCN/subnet that **routes to vCenter** (VPN or FastConnect; the helper opens
  HTTPS connections to vCenter on 443 for the API and the NFC disk download) and that the
  administrators' browsers can reach on 8443 (through the same VPN, or via a public IP on the helper).
- Permissions to create instances, block volumes, custom images, an Object Storage bucket, a dynamic group, a policy and a tag namespace (or an administrator who creates the IAM parts for you, see `create_iam`).
- The helper VM must be able to reach GitHub to clone the helper repository [RichardORCL/vmware-migration-helper](https://github.com/RichardORCL/vmware-migration-helper) (and PyPI to install its dependencies). Point `source_git_url` at your own fork if you maintain one.
- A vCenter account for each operator with read access to the inventory and the
  `VirtualMachine.Provisioning.ExportOVF` privilege (*Allow disk access*) on the VMs to migrate.

The Terraform in `helper/deploy/terraform` is a self-contained [Resource Manager](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/home.htm) stack (it ships a `schema.yaml` for the console form) and also works with a local `terraform apply`.

## 1. Option A - deploy with Resource Manager (recommended)

1. Get the stack zip. Either click the **Deploy to Oracle Cloud** button in the [vmware-migration-helper README](https://github.com/RichardORCL/vmware-migration-helper#deploy-to-oracle-cloud) (opens *Create stack* with the zip preloaded, skip step 2), download the committed `vc-oci-helper-stack.zip` from that repository, or rebuild it locally with `helper/deploy/package_stack.sh` (`package_stack.ps1` on Windows), which writes it to the repository root.
   Pass `--create <compartment-ocid>` (`-CreateInCompartment`) to create the stack straight from the OCI CLI instead of uploading it.
2. In the console: **Developer Services > Resource Manager > Stacks > Create stack > My configuration > .zip file**, upload the zip.
3. Fill in the form:
   - *Placement*: compartment, availability domain (target instances land in the same AD), VCN, subnet, whether to assign a public IP, and the CIDRs of the administrators' networks allowed to reach the web UI.
   - *vCenter*: host name/IP as reachable from the helper subnet, port, whether to verify its TLS certificate.
   - *Helper instance*: shape/OCPUs/memory and your SSH public key.
   - *Helper service*: git URL + ref to install (defaulting to the `vmware-migration-helper` GitHub repo), seed bucket, default target shape, number of parallel migrations.
   - *IAM*: keep **Create IAM resources** on unless an administrator already created the dynamic group/policy/tag namespace; optionally limit the compartment where the helper may create target instances.
4. Run **Plan**, then **Apply**. Outputs show `helper_ui_url`, the vCenter host, the availability domain and next steps.

To upgrade the helper use the **Setup** page in the web UI (see below), or on the VM run `sudo /usr/local/sbin/vc-oci-helper-install && sudo systemctl restart vc-oci-helper`. Changing the git ref in the stack alone does not upgrade an existing VM (cloud-init changes are ignored).

## 1. Option B - local Terraform

```bash
cd helper/deploy/terraform
cp terraform.tfvars.example terraform.tfvars   # edit values, incl. tenancy_ocid/region/compartment_ocid/vcenter_host
terraform init && terraform apply
terraform output helper_ui_url
```

## 2. What the stack creates

- an Oracle Linux 9 flex instance with paravirtualized storage/network and consistent device naming (`/dev/oracleoci/oraclevd*`), tagged `vc-oci.role=helper`;
- a network security group allowing TCP 8443 (web UI) and 22 (SSH) from `allowed_source_cidrs`, all egress;
- the `vc-oci-seed-images` Object Storage bucket used while importing seed images;
- (when `create_iam = true`) the `vc-oci` tag namespace, a dynamic group matching the tagged instance in the helper compartment, and a policy granting it `manage instance-family` / `manage volume-family` / `use virtual-network-family` in `policy_scope_compartment_ocid` (default: tenancy), plus `manage instance-images` / `compute-image-capability-schema` / volume attachments in its own compartment, object access to the seed bucket and `PAR_MANAGE` on that bucket (the image import service reads the placeholder through a pre-authenticated request it creates on the helper's behalf);
- cloud-init that writes `/etc/vc-oci-helper/helper.env` (`HELPER_VCENTER_*`, OCI settings), installs the helper (git clone + `pip install` into `/opt/vc-oci/venv`), generates a self-signed certificate, opens 8443 in firewalld and runs the `vc-oci-helper` systemd unit.

**Target instances can only be created in the helper's AD** because boot volumes are AD-local; deploy one stack per AD if you need more.

When `create_iam = false`, an administrator must create beforehand: tag namespace `vc-oci` with key `role`; a dynamic group with rule `ALL {instance.compartment.id = '<helper compartment>', tag.vc-oci.role.value = 'helper'}`; and the policy statements listed in `main.tf`.

## 3. Verify

```bash
curl -k https://<helper-ip>:8443/api/health          # {"status":"ok", ..., "vcenter_host": "..."}
ssh opc@<helper-ip> curl -k -o /dev/null -w '%{http_code}\n' https://<vcenter-host>/sdk   # vCenter reachable from the helper
```

Then open `https://<helper-ip>:8443/` in a browser, accept the self-signed certificate and log in
with your vCenter credentials. The login page proposes the vCenter from the stack (`HELPER_VCENTER_HOST`)
but accepts any other server (`host` or `host:port`) the helper can reach, so one helper serves
several vCenters; the browser remembers the servers used last. The VM list should show the inventory
the account is allowed to see.

To replace the self-signed certificate, put your own into `/etc/vc-oci-helper/server.crt` /
`server.key` and restart the unit.

## Configuration reference (environment, `HELPER_` prefix)

| Variable | Default | Description |
| --- | --- | --- |
| `HELPER_VCENTER_HOST` / `HELPER_VCENTER_PORT` | – / 443 | Default vCenter Server offered on the login page (users may enter another one) |
| `HELPER_VCENTER_VERIFY_SSL` | `false` | Verify the vCenter certificate |
| `HELPER_NFC_HOST_OVERRIDE` | session's vCenter | Host substituted for `*` in lease URLs, for all jobs. The per-VM *Download the disks directly from the ESXi host* option in the migration dialog resolves the VM's current host instead and takes precedence |
| `HELPER_NFC_VERIFY_SSL` | `false` | Verify TLS on the NFC download |
| `HELPER_NFC_CHUNK_BYTES` | `1048576` | Download chunk size (client-side read granularity; NFC itself has no block size) |
| `HELPER_NFC_PIPELINE_DEPTH` | `8` | Chunks buffered between the download and the decode/write thread when a job uses *Decode and write on a separate thread* (memory per disk copy: depth x chunk size) |
| `HELPER_LEASE_PROGRESS_INTERVAL_S` / `HELPER_LEASE_READY_TIMEOUT_S` | 60 / 300 | Lease keep-alive interval / time to wait for the lease |
| `HELPER_DISK_RETRY_ATTEMPTS` | `3` | Attempts per disk (each restarts from the beginning) |
| `HELPER_SESSION_TTL_S` | `28800` | Idle timeout of web sessions (5 min - 7 days); changeable on the *Setup* page |
| `HELPER_COOKIE_SECURE` | `true` | Set `false` only for plain-HTTP development |
| `HELPER_MAX_CONCURRENT_JOBS` | `2` | Migrations copying disks at the same time (1-16, further jobs queue); changeable on the *Setup* page |
| `HELPER_OCI_AUTH` | `instance_principal` | `config_file` for local development (`HELPER_OCI_CONFIG_FILE`, `HELPER_OCI_PROFILE`) |
| `HELPER_INSTANCE_ID`, `HELPER_COMPARTMENT_ID`, `HELPER_AVAILABILITY_DOMAIN`, `HELPER_REGION`, `HELPER_TENANCY_ID` | auto | Discovered from the instance metadata service when empty |
| `HELPER_SEED_BUCKET` | `vc-oci-seed-images` | Bucket for seed image imports |
| `HELPER_SEED_COMPARTMENT_ID` | helper compartment | Where seed images are kept |
| `HELPER_DEFAULT_SHAPE` | `VM.Standard.E5.Flex` | Flex shape for target instances |
| `HELPER_MIN_VOLUME_GB` | `50` | Minimum OCI volume size |
| `HELPER_DEVICE_PREFIX` | `/dev/oracleoci/oraclevd` | Consistent device path prefix |
| `HELPER_LAUNCH_TIMEOUT_S` / `HELPER_VOLUME_TIMEOUT_S` / `HELPER_IMAGE_IMPORT_TIMEOUT_S` | 1800 / 900 / 3600 | Waiter timeouts |
| `HELPER_SKIP_ZERO_GRAINS` | `true` | Do not write all-zero grains (fresh volumes read as zero) |
| `HELPER_CONSOLE_IDLE_TIMEOUT_S` / `HELPER_CONSOLE_CONNECT_TIMEOUT_S` | 600 / 120 | *Remote console*: delete the OCI console connection after this long without a viewer; timeout for creating it and for the SSH hops |
| `HELPER_DB_PATH` | `/var/lib/vc-oci-helper/jobs.sqlite3` | Job database |
| `HELPER_TLS_CERT_FILE` / `HELPER_TLS_KEY_FILE` | – | TLS material for 8443 |
| `HELPER_LOG_LEVEL` | `INFO` | Helper log level (`journalctl -u vc-oci-helper`); changeable on the *Setup* page |
| `HELPER_OCI_LOG_REQUESTS` | `false` | Dump every OCI SDK request/response including bodies (for analysing `InvalidParameter` errors); changeable on the *Setup* page |
| `HELPER_RUNTIME_SETTINGS_PATH` | `/var/lib/vc-oci-helper/runtime-settings.json` | Where *Setup* page changes (logging, concurrency, session timeout) are persisted; they override the environment on start |
| `HELPER_UPDATE_SOURCE_DIR` / `HELPER_UPDATE_VENV_DIR` | `/opt/vc-oci/src` / `/opt/vc-oci/venv` | Git checkout and virtualenv used by the self-update |
| `HELPER_UPDATE_SERVICE` / `HELPER_UPDATE_LOG_PATH` | `vc-oci-helper` / `/var/lib/vc-oci-helper/update.log` | systemd unit restarted after an update; update log shown in the UI |

## Updating the helper

The **Setup** tab compares the installed commit with the head of the branch the helper was installed
from (`source_git_ref`, queried through the GitHub API, falling back to `git ls-remote`) and offers
**Update now**. The update runs as a transient systemd unit (`vc-oci-helper-update`) on the VM:
`git fetch` + `reset --hard origin/<branch>`, `pip install` of the `helper` package into the
virtualenv, then `systemctl restart vc-oci-helper`. All web sessions end with the restart; the page
waits for the new version and returns to the login screen. The update is refused while migrations
are running (the restart would abort them) unless forced through the API
(`POST /api/setup/software/update {"force": true}`).

The same thing by hand: `sudo /usr/local/sbin/vc-oci-helper-install && sudo systemctl restart vc-oci-helper`.

## Maintenance

- Seed images accumulate one per firmware/OS combination. Delete them from the *Setup* tab, with `DELETE /api/seed-images` (logged in) or from the console (tag `vc-oci-seed=true`).
- Jobs are stored in `HELPER_DB_PATH`. A failed job leaves its OCI resources in place for inspection; *Clean up OCI resources* in the job view (`POST /api/jobs/{id}/cancel`) terminates the instance and deletes the volumes.
- After a restart of the service, jobs that were running are marked `FAILED` (their vCenter session is gone); clean them up and start again.
- The helper supports up to 32 attached volumes at once, which bounds `HELPER_MAX_CONCURRENT_JOBS`.
