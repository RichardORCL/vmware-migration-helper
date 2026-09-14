"""End-to-end: web API + migration runner against fake OCI, fake vCenter and fake NFC export."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from helper_app.config import Settings
from helper_app.disk.vmdk_stream import encode_raw_bytes
from helper_app.jobs.store import JobStore, utcnow
from helper_app.main import create_app
from helper_app.models import Job, JobPhase
from helper_app.updater import Updater
from helper_app.vsphere.inventory import vm_spec_from_vm

from .fake_oci import FakeOci
from .fake_vsphere import FakeExport, FakeVCenterConnector, make_vm
from .test_vmdk_stream import make_raw

MIB = 1024**2
AD = "Uocm:EU-FRANKFURT-1-AD-1"
USER = {"username": "admin@vsphere.local", "password": "secret"}


def wait_phase(client, job_id, *phases, timeout=30):
    deadline = time.time() + timeout
    job = None
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job.get("phase") in phases:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job did not reach {phases}: {job}")


def wait_until(pred, timeout=10, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def target(**kw):
    base = {"compartment_id": "ocid1.compartment.oc1..migr", "availability_domain": AD,
            "subnet_id": "ocid1.subnet.oc1..1"}
    base.update(kw)
    return base


class Env:
    def __init__(self, tmp_path, fail_once=frozenset({1}), block_event=None, store=None):
        self.settings = Settings(device_prefix=str(tmp_path / "dev" / "oraclevd"), db_path=str(tmp_path / "jobs.db"),
                                 seed_bucket="vc-oci-seed", launch_timeout_s=5, volume_timeout_s=5,
                                 image_import_timeout_s=5, cookie_secure=False,
                                 vcenter_host="vc.test", disk_retry_attempts=3, max_concurrent_jobs=2,
                                 update_source_dir=str(tmp_path / "src"), update_venv_dir=str(tmp_path / "venv"),
                                 update_log_path=str(tmp_path / "update.log"),
                                 runtime_settings_path=str(tmp_path / "runtime-settings.json"))
        self.commands: list[list[str]] = []  # commands the updater would run
        self.command_results: dict[tuple[str, ...], tuple[int, str]] = {}
        self.http_calls: list[str] = []
        self.remote_head = ("bbbb2222" * 5, "2026-09-14T20:00:00Z", "Add VCN selection")
        self.nfc_hosts: list[str] = []
        self.fake = FakeOci(self.settings.device_prefix)
        sizes = {0: 2 * MIB, 1: MIB}
        self.raws = {i: make_raw(s, seed=10 + i) for i, s in sizes.items()}
        payloads = {i: encode_raw_bytes(r) for i, r in self.raws.items()}
        self.vms = {
            "vm-101": make_vm(moid="vm-101", disks=((sizes[0], "pvscsi"), (sizes[1], "pvscsi"))),
            "vm-202": make_vm(moid="vm-202", name="win-01", guest_id="windows2019srvNext_64Guest",
                              guest_full_name="Microsoft Windows Server 2022 (64-bit)", firmware="bios",
                              disks=((sizes[0], "lsilogic"),), nics=("e1000",), folder="DC1/Windows"),
            "vm-on": make_vm(moid="vm-on", name="running", power_state="poweredOn"),
            "vm-tpl": make_vm(moid="vm-tpl", name="golden", template=True),
        }
        self.vcenter = FakeVCenterConnector(self.vms)
        self.store = store or JobStore(self.settings.db_path)
        FakeExport.instances.clear()
        self.updater = Updater(self.settings, runner=self._run_command, http_get=self._http_get)

        def export_factory(vm, nfc_host):
            self.nfc_hosts.append(nfc_host)
            return FakeExport(vm, payloads, fail_once=set(fail_once), block_event=block_event)

        self.app = create_app(
            settings=self.settings, clients=self.fake.clients(), store=self.store, vcenter=self.vcenter,
            export_factory=export_factory, updater=self.updater, command_runner=self._run_command,
            scan_devices=self.fake.scan_devices,
        )

    # -- fake git / systemd for the updater
    LOCAL_HEAD = "aaaa1111" * 5

    def install_from_source(self):
        (Path(self.settings.update_source_dir) / ".git").mkdir(parents=True)

    def _run_command(self, args, timeout):
        self.commands.append(list(args))
        for prefix, result in self.command_results.items():  # test overrides, keyed by command prefix
            if tuple(args[: len(prefix)]) == prefix:
                return result
        if args[0] == "git":
            sub = args[3]
            if sub == "symbolic-ref":
                return 0, "main\n"
            if sub == "remote":
                return 0, "https://github.com/acme/vCenter-OCI.git\n"
            if sub == "log":
                return 0, f"{self.LOCAL_HEAD}\x002026-09-13T10:00:00+02:00\x00Fix seed compartment\n"
            if sub == "ls-remote":
                return 0, f"{self.remote_head[0]}\trefs/heads/main\n"
            return 0, ""
        if args[0] == "systemctl":  # is-active vc-oci-helper-update
            return 3, "inactive\n"
        if args[0] == "systemd-run":
            return 0, ""
        return 127, f"{args[0]}: not found"

    def _http_get(self, url, headers):
        self.http_calls.append(url)
        sha, date, subject = self.remote_head
        return SimpleNamespace(status_code=200, json=lambda: {"sha": sha, "commit": {"committer": {"date": date},
                                                                                      "message": subject + "\n\nbody"}})


@pytest.fixture
def fast_retries():
    import helper_app.jobs.runner as runner_mod

    orig_sleep = runner_mod.time.sleep
    runner_mod.time.sleep = lambda s: orig_sleep(min(s, 0.05))
    try:
        yield
    finally:
        runner_mod.time.sleep = orig_sleep


@pytest.fixture
def env(tmp_path, fast_retries):
    e = Env(tmp_path)
    with TestClient(e.app) as client:
        e.client = client
        yield e


def login(client, **overrides):
    r = client.post("/api/auth/login", json={**USER, **overrides})
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- auth
def test_login_required_and_public_endpoints(env):
    c = env.client
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/health").json()["vcenter_host"] == "vc.test"
    assert c.get("/api/auth/config").json() == {"vcenter_host": "vc.test", "vcenter_port": 443}
    assert c.get("/ui/").status_code == 200
    assert c.get("/", follow_redirects=False).status_code == 307
    for path in ("/api/vms", "/api/jobs", "/api/oci/options", "/api/auth/me"):
        assert c.get(path).status_code == 401, path

    assert c.post("/api/auth/login", json={**USER, "password": "wrong"}).status_code == 401
    me = login(c)
    assert me["username"] == USER["username"] and me["vcenter_host"] == "vc.test"
    assert c.get("/api/auth/me").json()["username"] == USER["username"]

    assert c.post("/api/auth/logout").status_code == 204
    assert c.get("/api/auth/me").status_code == 401
    assert env.vcenter.sessions[0].closed  # no job pinned the session, so vCenter was disconnected


def test_login_to_another_vcenter(env):
    c = env.client
    me = login(c, vcenter_host="vc-dr.example.com:8443")
    assert (me["vcenter_host"], me["vcenter_port"]) == ("vc-dr.example.com", 8443)
    assert c.get("/api/auth/me").json()["vcenter_host"] == "vc-dr.example.com"
    # the NFC download of a migration goes to that vCenter, not to the configured default
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()})
    assert r.status_code == 202, r.text
    assert wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")["phase"] == "COMPLETED"
    assert env.nfc_hosts == ["vc-dr.example.com"]

    me = login(c, vcenter_host="https://10.1.2.3/")
    assert (me["vcenter_host"], me["vcenter_port"]) == ("10.1.2.3", 443)
    assert login(c, vcenter_host="")["vcenter_host"] == "vc.test"  # empty -> configured default
    r = c.post("/api/auth/login", json={**USER, "vcenter_host": "bad host;rm"})
    assert r.status_code == 502 and "invalid vCenter server" in r.text


def test_parse_vcenter_address():
    from helper_app.vsphere.session import VCenterError, parse_vcenter_address

    assert parse_vcenter_address("", "vc.test", 443) == ("vc.test", 443)
    assert parse_vcenter_address(" vc1.lab ", "vc.test", 443) == ("vc1.lab", 443)
    assert parse_vcenter_address("vc1.lab:8443", "vc.test", 443) == ("vc1.lab", 8443)
    assert parse_vcenter_address("https://vc1.lab:9443/ui", "vc.test", 443) == ("vc1.lab", 9443)
    assert parse_vcenter_address("[fd00::1]:444", "vc.test", 443) == ("[fd00::1]", 444)
    for bad in ("vc1.lab:abc", "vc1.lab:0", "a b", "-x", ""):
        with pytest.raises(VCenterError):
            parse_vcenter_address(bad, "" if bad == "" else "vc.test", 443)


# --------------------------------------------------------------------------- setup / logging
def test_logging_settings_apply_and_persist(tmp_path, fast_retries):
    import http.client
    import json
    import logging

    root = logging.getLogger()
    saved_level, saved_debug = root.level, http.client.HTTPConnection.debuglevel
    oci_client_logger = logging.getLogger("oci.base_client.12345")  # what the SDK creates per client
    oci_client_logger.disabled = True
    try:
        env = Env(tmp_path)
        with TestClient(env.app) as c:
            assert c.get("/api/setup/logging").status_code == 401
            login(c)
            lg = c.get("/api/setup/logging").json()
            assert lg["log_level"] == "INFO" and lg["oci_log_requests"] is False and lg["persisted"] is False
            assert lg["levels"] == ["DEBUG", "INFO", "WARNING", "ERROR"]

            assert c.put("/api/setup/logging", json={"log_level": "TRACE", "oci_log_requests": False}).status_code == 422
            r = c.put("/api/setup/logging", json={"log_level": "DEBUG", "oci_log_requests": True})
            assert r.status_code == 200, r.text
            assert r.json()["persisted"] is True and r.json()["warning"] == ""
            assert root.level == logging.DEBUG
            assert http.client.HTTPConnection.debuglevel == 1
            assert oci_client_logger.disabled is False and oci_client_logger.level == logging.DEBUG
            assert json.loads(Path(env.settings.runtime_settings_path).read_text()) == {"log_level": "DEBUG", "oci_log_requests": True}

            r = c.put("/api/setup/logging", json={"log_level": "WARNING", "oci_log_requests": False})
            assert r.json()["log_level"] == "WARNING" and root.level == logging.WARNING
            assert http.client.HTTPConnection.debuglevel == 0 and oci_client_logger.disabled is True

        # the persisted choice overrides the environment on the next start
        env2 = Env(tmp_path)
        assert env2.settings.log_level == "INFO"
        with TestClient(env2.app) as c:
            login(c)
            lg = c.get("/api/setup/logging").json()
            assert lg["log_level"] == "WARNING" and lg["persisted"] is True
            assert root.level == logging.WARNING
    finally:
        root.setLevel(saved_level)
        http.client.HTTPConnection.debuglevel = saved_debug
        logging.getLogger("oci").setLevel(logging.NOTSET)


# --------------------------------------------------------------------------- setup / self-update
def test_setup_info_and_software_status_without_source_install(env):
    c = env.client
    assert c.get("/api/setup/info").status_code == 401
    login(c)
    info = c.get("/api/setup/info").json()
    assert info["default_vcenter"] == "vc.test:443" and info["region"] == "eu-frankfurt-1" and info["sessions"] == 1
    sw = c.get("/api/setup/software").json()
    assert sw["install_method"] == "none" and sw["can_update"] is False and "not installed from source" in sw["reason"]
    assert c.post("/api/setup/software/update", json={}).status_code == 409
    assert not any(cmd[0] == "systemd-run" for cmd in env.commands)


def test_software_update_from_github(env):
    c = env.client
    env.install_from_source()
    login(c)
    sw = c.get("/api/setup/software").json()
    assert sw["install_method"] == "source" and sw["branch"] == "main"
    assert sw["repo_url"] == "https://github.com/acme/vCenter-OCI"
    assert sw["commit"] == env.LOCAL_HEAD and sw["latest_commit"] == env.remote_head[0]
    assert sw["latest_subject"] == "Add VCN selection" and sw["update_available"] is True and sw["can_update"] is True
    assert env.http_calls == ["https://api.github.com/repos/acme/vCenter-OCI/commits/main"]

    # GitHub unreachable -> git ls-remote fallback (no subject/date but still a comparison)
    env._http_get = lambda url, headers: (_ for _ in ()).throw(ConnectionError("offline"))
    env.updater._http_get = env._http_get
    sw = c.get("/api/setup/software").json()
    assert sw["latest_commit"] == env.remote_head[0] and sw["update_available"] is True and sw["latest_subject"] == ""

    # up to date when the remote head equals the local one
    env.remote_head = (env.LOCAL_HEAD, "", "")
    assert c.get("/api/setup/software").json()["update_available"] is False

    r = c.post("/api/setup/software/update", json={})
    assert r.status_code == 202, r.text
    run = [cmd for cmd in env.commands if cmd[0] == "systemd-run"]
    assert len(run) == 1 and "vc-oci-helper-update" in run[0]
    script = run[0][-1]
    src = env.settings.update_source_dir
    assert f"git -C {src} fetch" in script.replace("'", "") and "reset --hard origin/main" in script
    assert f"{env.settings.update_venv_dir}/bin/pip install".replace("'", "") in script.replace("'", "")
    assert "systemctl restart vc-oci-helper" in script and "UPDATE FAILED" in script
    # steps are chained so a failure aborts the rest
    assert script.count(" &&\n") >= 5

    # while the transient unit is active, the status says so and a second update is refused
    env.command_results[("systemctl", "is-active")] = (0, "activating\n")
    sw = c.get("/api/setup/software?check=false").json()
    assert sw["update_running"] is True and sw["can_update"] is False
    assert c.post("/api/setup/software/update", json={}).status_code == 409
    del env.command_results[("systemctl", "is-active")]
    # systemd refuses a duplicate unit
    env.command_results[("systemd-run",)] = (1, "Failed to start transient service unit: Unit vc-oci-helper-update.service already exists.")
    r = c.post("/api/setup/software/update", json={})
    assert r.status_code == 409 and "already running" in r.text


def test_software_update_refused_while_migrating(tmp_path, fast_retries):
    gate = threading.Event()
    env = Env(tmp_path, fail_once=frozenset(), block_event=gate)
    env.install_from_source()
    with TestClient(env.app) as c:
        login(c)
        r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()})
        assert r.status_code == 202
        job_id = r.json()["id"]
        wait_until(lambda: c.get(f"/api/jobs/{job_id}").json()["disks"][0]["status"] == "COPYING", what="copying")
        sw = c.get("/api/setup/software?check=false").json()
        assert sw["active_jobs"] == 1 and sw["can_update"] is False and "migration(s) running" in sw["reason"]
        assert c.post("/api/setup/software/update", json={}).status_code == 409
        assert not any(cmd[0] == "systemd-run" for cmd in env.commands)
        # ... unless forced
        assert c.post("/api/setup/software/update", json={"force": True}).status_code == 202
        assert any(cmd[0] == "systemd-run" for cmd in env.commands)
        gate.set()
        wait_phase(c, job_id, "COMPLETED", "FAILED")


# --------------------------------------------------------------------------- inventory
def test_vm_list_and_inspect(env):
    c = env.client
    login(c)
    vms = c.get("/api/vms").json()
    assert {v["moid"] for v in vms} == {"vm-101", "vm-on", "vm-202"}  # templates hidden
    web = next(v for v in vms if v["moid"] == "vm-101")
    assert web["folder"] == "DC1/Prod" and web["num_disks"] == 2 and web["power_state"] == "poweredOff"
    assert web["disk_capacity_bytes"] == 3 * MIB
    # cached per session, refreshed on demand
    c.get("/api/vms")
    assert env.vcenter.list_calls == 1
    c.get("/api/vms?refresh=true")
    assert env.vcenter.list_calls == 2

    r = c.get("/api/vms/vm-101")
    assert r.status_code == 200
    body = r.json()
    assert body["can_export"] is True and body["vm"]["name"] == "web-01"
    assert c.get("/api/vms/vm-on").json()["can_export"] is False
    assert c.get("/api/vms/vm-nope").status_code == 404

    r = c.get("/api/oci/options")
    assert r.status_code == 200 and r.json()["helper_availability_domain"] == AD
    opts = r.json()
    assert [v["name"] for v in opts["vcns"]] == ["vcn-dmz", "vcn-main"]
    assert opts["vcns"][1]["cidr_blocks"] == ["10.0.0.0/16"]
    # subnet -> VCN association, including a VCN that lives in another compartment
    by_subnet = {s["name"]: s for s in opts["subnets"]}
    assert by_subnet["private"]["vcn_id"] == "ocid1.vcn.oc1..1" and by_subnet["private"]["vcn_name"] == "vcn-main"
    assert by_subnet["app"]["vcn_name"] == "vcn-shared"


# --------------------------------------------------------------------------- migration
def test_full_migration_with_retry(env):
    c = env.client
    login(c)
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()})
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    assert r.json()["created_by"] == USER["username"]
    # a second job for the same VM is refused while the first is active
    assert c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()}).status_code == 409

    job = wait_phase(c, job_id, "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    assert [d["status"] for d in job["disks"]] == ["COPIED", "COPIED"]
    assert job["disks"][0]["attempts"] == 1
    assert job["disks"][1]["attempts"] == 2  # simulated NFC failure then success
    assert job["launch_options"]["firmware"] == "UEFI_64"
    assert all(d["device"] is None for d in job["disks"])  # detached from the helper again

    export = FakeExport.instances[-1]
    assert export.completed and not export.aborted

    # transfer statistics: per disk (percent of the stream, size known from the lease) and for the job
    # (bytes actually pulled including the retried half of disk 1, duration, average bandwidth)
    payload_sizes = [len(p) for p in export.payloads.values()]
    assert [d["stream_bytes"] for d in job["disks"]] == payload_sizes
    assert all(d["percent"] == 100 and d["throughput_bps"] == 0 for d in job["disks"])
    tr = job["transfer"]
    assert tr["started_at"] and tr["finished_at"] and tr["percent"] == 100 and tr["throughput_bps"] == 0
    assert sum(payload_sizes) < tr["bytes_received"] <= sum(payload_sizes) + payload_sizes[1]
    assert tr["bytes_written"] == sum(d["bytes_written"] for d in job["disks"])
    summary = job["summary"]
    assert job["finished_at"] and summary["duration_s"] >= summary["transfer_duration_s"] >= 0
    assert summary["bytes_received"] == tr["bytes_received"]
    assert summary["average_bps"] is None or summary["average_bps"] > 0

    fake = env.fake
    inst = fake.compute.instances[job["instance_id"]]
    assert inst.lifecycle_state == "RUNNING"
    target_atts = [a for a in fake.compute.vol_attachments.values()
                   if a.instance_id == job["instance_id"] and a.lifecycle_state == "ATTACHED"]
    assert len(target_atts) == 1
    # the helper wrote the raw disk content onto its "devices" (files under tmp): the boot volume shows up as
    # a plain /dev/sdX (no device path allowed), the data volume at its consistent path
    helper_atts = [a for a in fake.compute.vol_attachments.values() if a.instance_id == fake.identity.instance_id]
    used_devices = sorted(a.device or a.fake_disk for a in helper_atts)
    assert any(Path(p).name.startswith("sd") for p in used_devices)
    assert any("oraclevdb" in p for p in used_devices)
    contents = {open(p, "rb").read() for p in used_devices}
    assert env.raws[0] in contents and env.raws[1] in contents

    # diagnostics bundle: job record + relevant journal lines (job id, warnings/errors with tracebacks)
    env.command_results[("journalctl",)] = (0, "\n".join([
        f"2026-09-14T19:00:00+0000 helper vc-oci-helper[100]: 2026-09-14 19:00:00 INFO helper_app.jobs.runner: job {job_id} [PROVISIONING] Creating seed image",
        "2026-09-14T19:00:01+0000 helper vc-oci-helper[100]: 2026-09-14 19:00:01 INFO helper_app.sessions: session created for bob",
        "2026-09-14T19:00:02+0000 helper vc-oci-helper[100]: 2026-09-14 19:00:02 ERROR helper_app.jobs.runner: job other failed at step seed_image",
        "2026-09-14T19:00:02+0000 helper vc-oci-helper[100]: Traceback (most recent call last):",
        '2026-09-14T19:00:02+0000 helper vc-oci-helper[100]:   File "runner.py", line 1, in _run',
        "2026-09-14T19:00:02+0000 helper vc-oci-helper[100]: oci.exceptions.ServiceError: {'status': 400}",
        "2026-09-14T19:00:03+0000 helper vc-oci-helper[100]: 2026-09-14 19:00:03 INFO helper_app.api: GET /api/jobs",
    ]))
    r = c.get(f"/api/jobs/{job_id}/diagnostics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    text = r.text
    assert f"job {job_id}: phase=COMPLETED" in text and "helper: version" in text
    assert '"phase": "COMPLETED"' in text  # JSON record
    assert "disk 1" in text and "attempts=2" in text
    assert "Creating seed image" in text and "Traceback" in text and "ServiceError" in text
    assert "session created for bob" not in text and "GET /api/jobs" not in text
    journal_cmd = next(cmd for cmd in env.commands if cmd[0] == "journalctl")
    assert journal_cmd[1:3] == ["-u", "vc-oci-helper"] and "--since" in journal_cmd
    assert c.get("/api/jobs/nope/diagnostics").status_code == 404

    jobs = c.get("/api/jobs", params={"vm_moid": "vm-101"}).json()
    assert jobs[0]["id"] == job_id
    assert c.get("/api/jobs").json()[0]["id"] == job_id
    assert c.post(f"/api/jobs/{job_id}/cancel").status_code == 409  # already completed
    # the vCenter session is released but stays open for the still logged-in user
    assert not env.vcenter.sessions[0].closed


def test_windows_requires_license_and_license_update(env):
    c = env.client
    login(c)
    r = c.post("/api/jobs", json={"vm_moid": "vm-202", "target": target()})
    assert r.status_code == 400 and "license" in r.text
    r = c.post("/api/jobs", json={"vm_moid": "vm-202",
                                  "target": target(windows_license_type="BRING_YOUR_OWN_LICENSE",
                                                   start_after_migration=False)})
    assert r.status_code == 202, r.text
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    lo = job["launch_options"]
    assert (lo["firmware"], lo["boot_volume_type"], lo["network_type"]) == ("BIOS", "SCSI", "E1000")
    ld = [d for d in env.fake.compute.launch_details if d.display_name == "win-01"][0]
    assert ld.licensing_configs[0].license_type == "BRING_YOUR_OWN_LICENSE"
    r = c.post(f"/api/jobs/{job['id']}/licensing", json={"license_type": "OCI_PROVIDED"})
    assert r.status_code == 200, r.text
    assert r.json()["licensing_configs"][0]["license_type"] == "OCI_PROVIDED"
    assert c.get(f"/api/jobs/{job['id']}").json()["target"]["windows_license_type"] == "OCI_PROVIDED"
    assert env.fake.compute.instances[job["instance_id"]].lifecycle_state == "STOPPED"


def test_direct_esxi_download_option(env):
    c = env.client
    login(c)
    # the inspection tells the UI which host the VM lives on
    assert c.get("/api/vms/vm-101").json()["vm"]["host_name"] == "esxi-01.test"

    # default: proxied by the vCenter of the session
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()})
    assert r.status_code == 202, r.text
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED" and job["nfc_host"] == "vc.test"
    assert env.nfc_hosts == ["vc.test"]

    # per-job option: the ESXi host the VM is registered on, even when a deployment-wide override is set
    env.settings.nfc_host_override = "nfc-override.test"
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target(nfc_direct_to_esxi=True)})
    assert r.status_code == 202, r.text
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    assert job["nfc_host"] == "esxi-01.test" and job["target"]["nfc_direct_to_esxi"] is True
    assert env.nfc_hosts == ["vc.test", "esxi-01.test"]
    text = c.get(f"/api/jobs/{job['id']}/diagnostics").text
    assert "nfc download: host=esxi-01.test direct_to_esxi=True" in text

    # without the option the override applies as before
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()})
    assert wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")["nfc_host"] == "nfc-override.test"
    env.settings.nfc_host_override = None

    # a VM vCenter does not place on a host cannot be downloaded directly: the job fails cleanly
    env.vms["vm-nohost"] = make_vm(moid="vm-nohost", name="orphan", host=None,
                                   disks=((2 * MIB, "pvscsi"), (MIB, "pvscsi")))
    r = c.post("/api/jobs", json={"vm_moid": "vm-nohost", "target": target(nfc_direct_to_esxi=True)})
    assert r.status_code == 202, r.text
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "FAILED" and "no host" in job["error"], job
    assert env.nfc_hosts == ["vc.test", "esxi-01.test", "nfc-override.test"]  # no lease was opened


def test_volume_performance_option(env):
    c = env.client
    login(c)
    # only the Balanced / Higher / Ultra High tiers are offered
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target(volume_vpus_per_gb=15)})
    assert r.status_code == 422, r.text

    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target(volume_vpus_per_gb=20)})
    assert r.status_code == 202, r.text
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED" and job["target"]["volume_vpus_per_gb"] == 20
    assert env.fake.compute.launch_details[-1].source_details.boot_volume_vpus_per_gb == 20
    assert env.fake.blockstorage.volumes[job["disks"][1]["volume_id"]].vpus_per_gb == 20


def test_create_job_validation(env):
    c = env.client
    login(c)
    r = c.post("/api/jobs", json={"vm_moid": "vm-on", "target": target()})
    assert r.status_code == 400 and "powered off" in r.text
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target(availability_domain="Uocm:EU-FRANKFURT-1-AD-2")})
    assert r.status_code == 400 and "availability domain" in r.text
    assert c.post("/api/jobs", json={"vm_moid": "vm-nope", "target": target()}).status_code == 404
    assert c.get("/api/jobs/nope").status_code == 404
    # license change needs a Windows job with an instance
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()})
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert c.post(f"/api/jobs/{job['id']}/licensing", json={"license_type": "OCI_PROVIDED"}).status_code == 400


def test_cancel_during_copy_cleans_up(tmp_path, fast_retries):
    gate = threading.Event()
    env = Env(tmp_path, fail_once=frozenset(), block_event=gate)
    with TestClient(env.app) as c:
        login(c)
        r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()})
        job_id = r.json()["id"]
        # wait until the copy of disk 0 is in progress (blocked on the gate)
        wait_until(lambda: c.get(f"/api/jobs/{job_id}").json()["disks"][0]["status"] == "COPYING", what="copying")
        assert c.post(f"/api/jobs/{job_id}/cancel").status_code == 202
        gate.set()
        job = wait_phase(c, job_id, "CANCELLED", "COMPLETED", "FAILED")
        assert job["phase"] == "CANCELLED", job
        assert job["instance_id"] in env.fake.compute.terminated
        assert set(env.fake.blockstorage.deleted) == {d["volume_id"] for d in job["disks"]}
        assert FakeExport.instances[-1].aborted
        # helper attachments were detached during cleanup
        helper_atts = [a for a in env.fake.compute.vol_attachments.values()
                       if a.instance_id == env.fake.identity.instance_id]
        assert all(a.lifecycle_state == "DETACHED" for a in helper_atts)


def test_logout_keeps_vcenter_session_alive_for_running_job(tmp_path, fast_retries):
    gate = threading.Event()
    env = Env(tmp_path, fail_once=frozenset(), block_event=gate)
    with TestClient(env.app) as c:
        login(c)
        r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()})
        job_id = r.json()["id"]
        wait_until(lambda: c.get(f"/api/jobs/{job_id}").json()["disks"][0]["status"] == "COPYING", what="copying")
        vc_session = env.vcenter.sessions[0]
        assert c.post("/api/auth/logout").status_code == 204
        assert c.get("/api/jobs").status_code == 401
        assert not vc_session.closed  # pinned by the running job

        gate.set()
        login(c)  # new UI session to observe the job
        job = wait_phase(c, job_id, "COMPLETED", "FAILED")
        assert job["phase"] == "COMPLETED", job
        wait_until(lambda: vc_session.closed, what="vCenter session closed after the job released it")


def test_interrupted_jobs_fail_on_restart_and_can_be_cleaned_up(tmp_path, fast_retries):
    store = JobStore(str(tmp_path / "jobs.db"))
    now = utcnow()
    spec = vm_spec_from_vm(make_vm(moid="vm-101"))
    stale = Job(id="stale1", phase=JobPhase.EXPORTING, vm=spec,
                target=target(), created_at=now, updated_at=now)
    store.put(stale)
    env = Env(tmp_path, store=store)
    with TestClient(env.app) as c:
        login(c)
        job = c.get("/api/jobs/stale1").json()
        assert job["phase"] == "FAILED" and "restarted" in job["error"]
        assert c.post("/api/jobs/stale1/cancel").status_code == 202
        job = wait_phase(c, "stale1", "CANCELLED")
        assert "nothing to clean up" in job["message"]
        assert c.post("/api/jobs/stale1/cancel").status_code == 409


def test_seed_image_cleanup_endpoint(env):
    c = env.client
    login(c)
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()})
    wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    r = c.delete("/api/seed-images")
    assert r.status_code == 200 and len(r.json()["deleted"]) == 1
