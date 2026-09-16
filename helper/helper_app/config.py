"""Helper configuration (environment driven, ``HELPER_`` prefix)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HELPER_", env_file=".env", extra="ignore")

    # HTTP
    listen_host: str = "0.0.0.0"
    listen_port: int = 8443
    tls_cert_file: Optional[str] = None
    tls_key_file: Optional[str] = None
    ui_dir: str = str(PACKAGE_DIR / "ui")  # static web UI shipped with the package

    # Web UI sessions (users log in with vCenter credentials)
    session_cookie_name: str = "vcoci_session"
    session_ttl_s: int = 8 * 3600  # idle timeout
    cookie_secure: bool = True  # set false only for plain-HTTP development

    # Optional defaults for the login page: the vCenter/ESXi server and the TLS-verification checkbox are
    # entered there per login (the Resource Manager stack does not set these); the values chosen at login
    # apply to the SOAP API and the NFC disk download of that session.
    vcenter_host: str = ""
    vcenter_port: int = 443
    vcenter_verify_ssl: bool = False

    # NFC export
    nfc_host_override: Optional[str] = None  # host substituted for '*' in lease URLs; default: session's vCenter
    nfc_chunk_bytes: int = 1024 * 1024
    nfc_pipeline_depth: int = 8  # chunks buffered between download and decode/write when a job opts in
    lease_progress_interval_s: int = 60
    lease_ready_timeout_s: int = 300
    disk_retry_attempts: int = 3
    # Powered-on source VMs are shut down right before the export: guest OS shutdown through VMware Tools,
    # hard power-off when Tools is not running or the guest has not stopped after this many seconds
    guest_shutdown_timeout_s: int = 300

    # Logging (both adjustable from the Setup page; changes persist in runtime_settings_path)
    log_level: str = "INFO"
    oci_log_requests: bool = False  # log every OCI SDK request/response (bodies included) at DEBUG
    runtime_settings_path: str = "/var/lib/vc-oci-helper/runtime-settings.json"

    # OCI authentication: instance principals on the helper VM, config file for development
    oci_auth: Literal["instance_principal", "config_file", "mock"] = "instance_principal"
    oci_config_file: str = "~/.oci/config"
    oci_profile: str = "DEFAULT"

    # Identity of the helper itself; auto-discovered from the instance metadata service when empty
    instance_id: str = ""
    compartment_id: str = ""
    availability_domain: str = ""
    region: str = ""
    tenancy_id: str = ""

    # Seed image handling
    seed_bucket: str = "vc-oci-seed-images"
    seed_compartment_id: str = ""  # defaults to the helper compartment
    seed_disk_size_gb: int = 1

    # Instance defaults
    default_shape: str = "VM.Standard.E5.Flex"
    max_memory_gb_per_ocpu: int = 64
    min_volume_gb: int = 50
    device_prefix: str = "/dev/oracleoci/oraclevd"

    # Job execution
    db_path: str = "/var/lib/vc-oci-helper/jobs.sqlite3"
    max_concurrent_jobs: int = 2
    launch_timeout_s: int = 1800
    volume_timeout_s: int = 900
    image_import_timeout_s: int = 3600
    skip_zero_grains: bool = True

    # Remote (VNC) console of a migrated instance: OCI instance console connection + SSH tunnel bridged into
    # the browser.  The console connection is deleted after this long without a viewer.
    console_idle_timeout_s: int = 600
    console_connect_timeout_s: int = 120  # CreateInstanceConsoleConnection -> ACTIVE, and the SSH hops

    # Self-update from the git checkout the helper was installed from (source install on the helper VM)
    update_source_dir: str = "/opt/vc-oci/src"
    update_venv_dir: str = "/opt/vc-oci/venv"
    update_service: str = "vc-oci-helper"
    update_log_path: str = "/var/lib/vc-oci-helper/update.log"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
