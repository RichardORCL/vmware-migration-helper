"""End-to-end: web API + migration runner against fake OCI, fake vCenter and fake NFC export."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from helper_app.config import Settings
from helper_app.disk.vmdk_stream import encode_raw_bytes
from helper_app.guest.fixup import GuestFixupResult
from helper_app.jobs.store import JobStore, utcnow
from helper_app.main import create_app
from helper_app.models import GuestFixup, Job, JobPhase
from helper_app.updater import Updater
from helper_app.vsphere.inventory import vm_spec_from_vm

from .fake_oci import FakeOci, service_error
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
    def __init__(self, tmp_path, fail_once=frozenset({1}), block_event=None, store=None, tunnel_factory=None):
        self.settings = Settings(device_prefix=str(tmp_path / "dev" / "oraclevd"), db_path=str(tmp_path / "jobs.db"),
                                 seed_bucket="vc-oci-seed", launch_timeout_s=5, volume_timeout_s=5,
                                 image_import_timeout_s=5, cookie_secure=False, console_connect_timeout_s=5,
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
            "vm-303": make_vm(moid="vm-303", name="desk-01", guest_id="windows9_64Guest",
                              guest_full_name="Microsoft Windows 11 (64-bit)", firmware="efi",
                              disks=((sizes[0], "lsilogicsas"),), folder="DC1/Desktops"),
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

        # post-copy guest fix-up: scripted outcome per test (device -> GuestFixup or exception)
        self.fixups: list[tuple[str, bool, bool]] = []  # (boot device, initramfs wanted, network wanted)
        self.fixup_result = GuestFixup(status="done", detail="initramfs rebuilt with virtio drivers for 3.10.0-1160",
                                       kernels=["3.10.0-1160.el7.x86_64"])
        self.network_result = GuestFixup(status="done", detail="NetworkManager DHCP profile for any Ethernet "
                                                               "interface added")

        def guest_fixer(device, initramfs, network, notify):
            self.fixups.append((device, initramfs, network))
            notify("scanning the boot disk")
            if isinstance(self.fixup_result, Exception):
                raise self.fixup_result
            return GuestFixupResult(self.fixup_result if initramfs else None, self.network_result if network else None)

        extra = {"tunnel_factory": tunnel_factory} if tunnel_factory else {}
        self.app = create_app(
            settings=self.settings, clients=self.fake.clients(), store=self.store, vcenter=self.vcenter,
            export_factory=export_factory, updater=self.updater, command_runner=self._run_command,
            scan_devices=self.fake.scan_devices, guest_fixer=guest_fixer, **extra,
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


def test_operation_settings_apply_and_persist(tmp_path, fast_retries):
    import json

    env = Env(tmp_path)
    with TestClient(env.app) as c:
        assert c.get("/api/setup/operation").status_code == 401
        login(c)
        op = c.get("/api/setup/operation").json()
        assert op["max_concurrent_jobs"] == 2 and op["session_ttl_s"] == 8 * 3600 and op["persisted"] is False
        assert op["max_concurrent_jobs_limit"] == 16

        # bounds are enforced
        assert c.put("/api/setup/operation", json={"max_concurrent_jobs": 0, "session_ttl_s": 3600}).status_code == 422
        assert c.put("/api/setup/operation", json={"max_concurrent_jobs": 17, "session_ttl_s": 3600}).status_code == 422
        assert c.put("/api/setup/operation", json={"max_concurrent_jobs": 2, "session_ttl_s": 60}).status_code == 422

        # logging settings written first must survive in the shared file
        c.put("/api/setup/logging", json={"log_level": "INFO", "oci_log_requests": False})
        r = c.put("/api/setup/operation", json={"max_concurrent_jobs": 4, "session_ttl_s": 1800})
        assert r.status_code == 200, r.text
        assert r.json()["persisted"] is True and r.json()["max_concurrent_jobs"] == 4
        st = env.app.state
        assert st.runner.max_concurrent == 4 and st.settings.max_concurrent_jobs == 4
        # existing logins get the new idle timeout too
        assert st.sessions.ttl_s == 1800 and all(s.ttl_s == 1800 for s in st.sessions._sessions.values())
        assert json.loads(Path(env.settings.runtime_settings_path).read_text()) == {
            "log_level": "INFO", "oci_log_requests": False, "max_concurrent_jobs": 4, "session_ttl_s": 1800,
        }
        info = c.get("/api/setup/info").json()
        assert info["max_concurrent_jobs"] == 4 and info["session_ttl_s"] == 1800

    # the persisted values override the environment on the next start
    env2 = Env(tmp_path)
    assert env2.settings.max_concurrent_jobs == 2
    with TestClient(env2.app) as c:
        login(c)
        op = c.get("/api/setup/operation").json()
        assert (op["max_concurrent_jobs"], op["session_ttl_s"], op["persisted"]) == (4, 1800, True)
        assert env2.app.state.runner.max_concurrent == 4 and env2.app.state.sessions.ttl_s == 1800


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
    assert {v["moid"] for v in vms} == {"vm-101", "vm-on", "vm-202", "vm-303"}  # templates hidden
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
    assert body["can_export"] is True and body["vm"]["name"] == "web-01" and body["needs_power_off"] is False
    on = c.get("/api/vms/vm-on").json()
    assert on["can_export"] is True and on["needs_power_off"] is True  # migratable, shut down by the job
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
    # Ampere (ARM) shapes are not offered: an x86 guest cannot boot on them
    assert [s["name"] for s in opts["shapes"]] == ["VM.Standard.E5.Flex"]


def test_oci_options_network_compartment(env):
    c = env.client
    login(c)
    net = env.fake.network
    net.listed_compartments.clear()
    # VCNs/subnets come from the network compartment, everything else from the instance compartment
    r = c.get("/api/oci/options", params={"compartment_id": "ocid1.compartment.oc1..inst",
                                          "network_compartment_id": "ocid1.compartment.oc1..net"})
    assert r.status_code == 200, r.text
    assert set(net.listed_compartments) == {"ocid1.compartment.oc1..net"}
    # without a network compartment the instance compartment is used for both
    net.listed_compartments.clear()
    c.get("/api/oci/options", params={"compartment_id": "ocid1.compartment.oc1..inst"})
    assert set(net.listed_compartments) == {"ocid1.compartment.oc1..inst"}


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
    # the job view asks OCI for the live state of the target instance (name + lifecycle state)
    assert job["instance_display_name"] == "web-01"
    st = c.get(f"/api/jobs/{job_id}/instance")
    assert st.status_code == 200, st.text
    assert st.json()["lifecycle_state"] == "RUNNING" and st.json()["display_name"] == "web-01"
    assert st.json()["instance_id"] == job["instance_id"] and st.json()["checked_at"]
    # ... and the addresses OCI gave the primary VNIC (DHCP here, no public IP requested)
    vnic = fake.network.vnics[fake.compute.list_vnic_attachments("c", instance_id=job["instance_id"]).data[0].vnic_id]
    assert st.json()["private_ip"] == vnic.private_ip and vnic.private_ip.startswith("10.0.1.")
    assert st.json()["public_ip"] is None
    fake.compute.instance_action(job["instance_id"], "STOP")  # someone stops it in the console
    assert c.get(f"/api/jobs/{job_id}/instance").json()["lifecycle_state"] == "STOPPED"
    fake.compute.instance_action(job["instance_id"], "START")
    assert c.get(f"/api/jobs/{job_id}/instance").json()["lifecycle_state"] == "RUNNING"
    inst_404 = service_error(404, "NotAuthorizedOrNotFound", "instance not found", "GetInstance")
    fake.compute.get_instance = lambda iid, _e=inst_404: (_ for _ in ()).throw(_e)
    assert c.get(f"/api/jobs/{job_id}/instance").json()["lifecycle_state"] == "NOT_FOUND"
    del fake.compute.get_instance
    target_atts = [a for a in fake.compute.vol_attachments.values()
                   if a.instance_id == job["instance_id"] and a.lifecycle_state == "ATTACHED"]
    assert len(target_atts) == 1
    # the instance is tagged with where it came from: the vCenter of the session, the VM and its sizing
    assert job["vcenter_host"] == "vc.test"
    tags = fake.compute.launch_details[-1].freeform_tags
    assert tags["vc-oci-source-vcenter"] == "vc.test" and tags["vc-oci-source-esxi-host"] == "esxi-01.test"
    assert tags["vc-oci-source-vm-details"].startswith("4 vCPU, 8 GB RAM, 2 disk(s)"), tags
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


def test_resume_finalize_after_attach_failure(env):
    """A job that fails while attaching to the target keeps its copied volumes; "Retry finalize" picks up
    where it stopped without exporting again (and without a vCenter session)."""
    c = env.client
    login(c)
    env.fake.compute.boot_attach_errors.append(service_error(
        409, "IncorrectState", "Boot volume is in Attaching state, when it was expected to be in Available state",
        "attach_boot_volume"))
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()})
    assert r.status_code == 202, r.text
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "FAILED" and job["step"] == "attach_boot_volume", job
    assert all(d["status"] == "COPIED" for d in job["disks"])
    # the data volume stayed attached to the target since prepare(); only the boot volume is still missing
    assert job["disks"][1]["target_attachment_id"] and not job["disks"][0]["target_attachment_id"]
    exports_before = len(FakeExport.instances)
    assert c.post("/api/jobs/does-not-exist/finalize").status_code == 404

    r = c.post(f"/api/jobs/{job['id']}/finalize")
    assert r.status_code == 202, r.text
    job = wait_phase(c, job["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    assert job["error"] is None
    assert len(FakeExport.instances) == exports_before  # nothing was exported again
    inst = env.fake.compute.instances[job["instance_id"]]
    assert inst.lifecycle_state == "RUNNING"
    assert c.post(f"/api/jobs/{job['id']}/finalize").status_code == 409  # nothing left to resume


def test_powered_on_vm_is_shut_down_before_export(env):
    """A running VM can be migrated once the user confirmed the shutdown: the helper shuts it down
    through VMware Tools right before opening the export lease (after the OCI side is prepared)."""
    c = env.client
    login(c)
    disks = ((2 * MIB, "pvscsi"), (MIB, "pvscsi"))  # sizes of the fake export payloads
    vm = env.vms["vm-run"] = make_vm(moid="vm-run", name="running-tools", power_state="poweredOn", disks=disks)
    insp = c.get("/api/vms/vm-run").json()
    assert insp["can_export"] and insp["needs_power_off"] and insp["tools_running"]
    r = c.post("/api/jobs", json={"vm_moid": "vm-run", "target": target(), "power_off_source": True})
    assert r.status_code == 202, r.text
    assert r.json()["power_off_source"] is True and "shut down" in r.json()["message"]
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    assert job["power_off_result"] == "guest_shutdown" and job["vm"]["power_state"] == "poweredOff"
    assert vm.power_ops == ["ShutdownGuest"] and str(vm.runtime.powerState) == "poweredOff"
    # the export only started once the VM was off, and it was left powered off
    assert FakeExport.instances[-1].completed
    diag = c.get(f"/api/jobs/{job['id']}/diagnostics").text
    assert "power_off_source=True power_off_result=guest_shutdown" in diag

    # without Tools the VM is powered off hard (the pop-up said so)
    env.vms["vm-on2"] = make_vm(moid="vm-on2", name="running-notools", power_state="poweredOn", tools_running=False,
                                disks=disks)
    assert c.get("/api/vms/vm-on2").json()["tools_running"] is False
    r = c.post("/api/jobs", json={"vm_moid": "vm-on2", "target": target(), "power_off_source": True})
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED" and job["power_off_result"] == "powered_off", job
    assert env.vms["vm-on2"].power_ops == ["PowerOffVM_Task"]

    # a powered-off VM confirmed "just in case" is left alone
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target(), "power_off_source": True})
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["power_off_source"] is False and job["power_off_result"] is None
    assert env.vms["vm-101"].power_ops == []


def test_windows_requires_license(env):
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
    # LSI Logic + e1000 on the source: still paravirtualized (only "Maximum compatibility" picks IDE/E1000)
    assert (lo["firmware"], lo["boot_volume_type"], lo["network_type"]) == ("BIOS", "PARAVIRTUALIZED", "PARAVIRTUALIZED")
    ld = [d for d in env.fake.compute.launch_details if d.display_name == "win-01"][0]
    assert ld.licensing_configs[0].license_type == "BRING_YOUR_OWN_LICENSE"
    assert c.get(f"/api/jobs/{job['id']}").json()["target"]["windows_license_type"] == "BRING_YOUR_OWN_LICENSE"
    assert env.fake.compute.instances[job["instance_id"]].lifecycle_state == "STOPPED"


def test_windows_client_edition_uses_catalog_version_and_byol(env):
    """OCI only knows "Windows10"/"Windows11" for client editions (CreateImage rejects "10 Enterprise"), and
    it has no licenses for them, so OCI_PROVIDED is refused up front."""
    c = env.client
    login(c)
    r = c.post("/api/jobs", json={"vm_moid": "vm-303", "target": target(windows_license_type="OCI_PROVIDED")})
    assert r.status_code == 400 and "Windows 10/11" in r.text, r.text
    r = c.post("/api/jobs", json={"vm_moid": "vm-303",
                                  "target": target(windows_license_type="BRING_YOUR_OWN_LICENSE")})
    assert r.status_code == 202, r.text
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    img = env.fake.compute.images[job["seed_image_id"]]
    # imported without OS metadata (CreateImage rejects Windows1x), then registered via UpdateImage
    assert (img.operating_system, img.operating_system_version) == ("Windows", "Windows11")
    assert job["launch_options"]["firmware"] == "UEFI_64"
    ld = [d for d in env.fake.compute.launch_details if d.display_name == "desk-01"][0]
    assert ld.licensing_configs[0].license_type == "BRING_YOUR_OWN_LICENSE"  # still a Windows image


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


def test_os_version_selection_when_vsphere_does_not_report_it(env):
    c = env.client
    login(c)
    env.vms["vm-ubu"] = make_vm(moid="vm-ubu", name="ubu-01", guest_id="ubuntu64Guest",
                                guest_full_name="Ubuntu Linux (64-bit)", disks=((2 * MIB, "pvscsi"),))
    # the inspection tells the UI to ask for the release and which ones OCI knows
    osinfo = c.get("/api/vms/vm-ubu").json()["os"]
    assert osinfo["operating_system"] == "Ubuntu" and osinfo["version_detected"] is False
    assert osinfo["version_choices"] == ["18.04", "20.04", "22.04", "24.04", "26.04"]
    # a guest whose guestId names the release needs no choice
    osinfo = c.get("/api/vms/vm-101").json()["os"]
    assert osinfo["version_detected"] is True and osinfo["operating_system_version"] == "8"

    r = c.post("/api/jobs", json={"vm_moid": "vm-ubu", "target": target()})
    assert r.status_code == 400 and "select the OS version" in r.json()["detail"], r.text

    r = c.post("/api/jobs", json={"vm_moid": "vm-ubu", "target": target(operating_system_version="24.04")})
    assert r.status_code == 202, r.text
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    img = env.fake.compute.images[job["seed_image_id"]]
    assert (img.operating_system, img.operating_system_version) == ("Ubuntu", "24.04")
    assert "os_version=24.04" in c.get(f"/api/jobs/{job['id']}/diagnostics").text


def test_arm_shape_is_refused(env):
    c = env.client
    login(c)
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target(shape="VM.Standard.A1.Flex")})
    assert r.status_code == 400 and "ARM" in r.json()["detail"], r.text
    assert not env.fake.compute.launch_details


def test_custom_sizing_overrides_source_mapping(env):
    c = env.client
    login(c)
    # sizing must be positive when given
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target(ocpus=0)})
    assert r.status_code == 422, r.text

    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target(ocpus=3, memory_gb=24)})
    assert r.status_code == 202, r.text
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED" and job["target"]["ocpus"] == 3 and job["target"]["memory_gb"] == 24
    shape_config = env.fake.compute.launch_details[-1].shape_config
    assert (shape_config.ocpus, shape_config.memory_in_gbs) == (3, 24)

    r = c.get(f"/api/jobs/{job['id']}/diagnostics")
    assert "ocpus=3.0 memory_gb=24.0" in r.text


def test_pipelined_decode_option(env):
    c = env.client
    login(c)
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target(pipelined_decode=True)})
    assert r.status_code == 202, r.text
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    assert job["target"]["pipelined_decode"] is True
    # the simulated mid-stream failure on disk 1 aborted the pipeline and the retry decoded from scratch
    assert [d["attempts"] for d in job["disks"]] == [1, 2]
    assert all(d["status"] == "COPIED" for d in job["disks"])
    fake = env.fake
    helper_atts = [a for a in fake.compute.vol_attachments.values() if a.instance_id == fake.identity.instance_id]
    contents = {open(a.device or a.fake_disk, "rb").read() for a in helper_atts}
    assert env.raws[0] in contents and env.raws[1] in contents  # bytes on the volumes are identical
    assert "pipelined_decode=True" in c.get(f"/api/jobs/{job['id']}/diagnostics").text
    # no decode worker left behind
    assert not [t for t in threading.enumerate() if t.name.startswith("vmdk-decode")]


def test_create_job_validation(env):
    c = env.client
    login(c)
    # a powered-on VM needs the explicit confirmation that it may be shut down
    r = c.post("/api/jobs", json={"vm_moid": "vm-on", "target": target()})
    assert r.status_code == 400 and "powered on" in r.text and "running" in r.text
    env.vms["vm-sus"] = make_vm(moid="vm-sus", name="sleeping", power_state="suspended")
    r = c.post("/api/jobs", json={"vm_moid": "vm-sus", "target": target(), "power_off_source": True})
    assert r.status_code == 400 and "suspended" in r.text
    # encrypted VM (Windows 11 with a vTPM): vSphere would refuse ExportVm, so refuse before creating anything
    env.vms["vm-enc"] = make_vm(moid="vm-enc", name="Win11", guest_id="windows11_64Guest", encrypted=True, vtpm=True)
    insp = c.get("/api/vms/vm-enc").json()
    assert insp["can_export"] is False and insp["vm"]["encrypted"] and insp["vm"]["has_vtpm"]
    assert any(vm["moid"] == "vm-enc" and vm["encrypted"] for vm in c.get("/api/vms").json())
    launched_before = len(env.fake.compute.instances)
    r = c.post("/api/jobs", json={"vm_moid": "vm-enc", "target": target()})
    assert r.status_code == 400 and "encrypted" in r.text and "Virtual TPM" in r.text
    assert len(env.fake.compute.instances) == launched_before
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target(availability_domain="Uocm:EU-FRANKFURT-1-AD-2")})
    assert r.status_code == 400 and "availability domain" in r.text
    assert c.post("/api/jobs", json={"vm_moid": "vm-nope", "target": target()}).status_code == 404
    assert c.get("/api/jobs/nope").status_code == 404


def test_fixed_private_ip(env):
    """A fixed private IP is validated against the subnet (syntax, CIDR, OCI reserved addresses, already
    allocated) before the job is accepted, and handed to LaunchInstance; empty means DHCP."""
    from types import SimpleNamespace as NS

    c = env.client
    login(c)
    bad = [("10.0.1", "IPv4"), ("10.0.1.300", "IPv4"), ("2001:db8::5", "IPv4"),
           ("10.0.2.5", "not inside the CIDR 10.0.1.0/24"),  # subnet ocid1.subnet.oc1..1 is 10.0.1.0/24
           ("10.0.1.0", "reserved"), ("10.0.1.1", "reserved"), ("10.0.1.255", "reserved")]
    for ip, text in bad:
        r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target(private_ip=ip)})
        assert r.status_code in (400, 422) and text in r.text, (ip, r.text)
    # the form's pre-check answers the same questions without creating anything
    check = lambda ip, subnet="ocid1.subnet.oc1..1": c.get("/api/oci/private-ip-check",  # noqa: E731
                                                              params={"subnet_id": subnet, "ip": ip})
    r = check("10.0.1.25")
    assert r.status_code == 200 and r.json()["available"] is True and "free" in r.json()["message"]
    assert check("10.0.1.1").json() == {"ip": "10.0.1.1", "subnet_id": "ocid1.subnet.oc1..1", "available": False,
                                        "message": check("10.0.1.1").json()["message"]}
    assert "reserved" in check("10.0.1.1").json()["message"]
    assert "not inside" in check("10.0.2.5").json()["message"]
    assert "not an IPv4" in check("nope").json()["message"]
    assert check("10.0.1.25", subnet="ocid1.subnet.oc1..nope").status_code == 502
    # in use by another VNIC in that subnet
    env.fake.network.private_ips.append(NS(hostname_label="db-old", subnet_id="ocid1.subnet.oc1..1", vnic_id="v9",
                                           ip_address="10.0.1.25"))
    r = check(" 10.0.1.25 ")
    assert r.json()["available"] is False and "already in use" in r.json()["message"] and r.json()["ip"] == "10.0.1.25"
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target(private_ip="10.0.1.25")})
    assert r.status_code == 400 and "already in use" in r.text and "db-old" in r.text
    # the same address in another subnet is no clash
    env.fake.network.private_ips.append(NS(hostname_label="x", subnet_id="ocid1.subnet.oc1..2", vnic_id="v8",
                                           ip_address="10.0.1.26"))
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target(private_ip=" 10.0.1.26 ")})
    assert r.status_code == 202, r.text
    assert r.json()["target"]["private_ip"] == "10.0.1.26"
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    assert env.fake.compute.launched_vnics[-1].private_ip == "10.0.1.26"
    assert "private_ip=10.0.1.26" in c.get(f"/api/jobs/{job['id']}/diagnostics").text
    # the job view shows the address OCI actually assigned to the primary VNIC (the fixed one here)
    st = c.get(f"/api/jobs/{job['id']}/instance").json()
    assert st["private_ip"] == "10.0.1.26" and st["public_ip"] is None
    # ... and is now taken
    win = dict(windows_license_type="BRING_YOUR_OWN_LICENSE")
    r = c.post("/api/jobs", json={"vm_moid": "vm-202", "target": target(private_ip="10.0.1.26", **win)})
    assert r.status_code == 400 and "already in use" in r.text
    # empty / blank -> DHCP (None); with a public IP
    r = c.post("/api/jobs", json={"vm_moid": "vm-202", "target": target(private_ip="  ", assign_public_ip=True, **win)})
    assert r.status_code == 202 and r.json()["target"]["private_ip"] is None
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert env.fake.compute.launched_vnics[-1].private_ip is None
    assert "private_ip=dhcp" in c.get(f"/api/jobs/{job['id']}/diagnostics").text
    st = c.get(f"/api/jobs/{job['id']}/instance").json()
    assert st["private_ip"].startswith("10.0.1.") and st["public_ip"].startswith("130.61.")
    # a VNIC lookup failure does not hide the lifecycle state
    env.fake.network.get_vnic = lambda vid: (_ for _ in ()).throw(service_error(500, "InternalError", "x", "GetVnic"))
    st = c.get(f"/api/jobs/{job['id']}/instance").json()
    assert st["lifecycle_state"] == "RUNNING" and st["private_ip"] is None
    del env.fake.network.get_vnic


def test_guest_fixup_runs_after_copy(env):
    """The initramfs and network fix-ups run on the boot volume once all disks are copied (one call, both
    steps); their outcomes are recorded and never fail the migration; Windows guests and opted-out steps
    are skipped."""
    c = env.client
    login(c)
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()})
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED", job
    # ran on the boot volume's disk (the device path is cleared from the record once it is detached)
    assert env.fixups == [(str(Path(env.settings.device_prefix).parent / "sdb"), True, True)]
    assert job["guest_fixup"]["status"] == "done" and job["guest_fixup"]["kernels"] == ["3.10.0-1160.el7.x86_64"]
    assert job["network_fixup"]["status"] == "done" and "NetworkManager" in job["network_fixup"]["detail"]
    assert "power_off" not in job["step"]
    diag = c.get(f"/api/jobs/{job['id']}/diagnostics").text
    assert "guest fixup=done" in diag and "network fixup=done" in diag
    assert job["message"].startswith("Migration complete")

    # a crash inside the fix-up is recorded for both steps, the migration still completes
    env.fixup_result = RuntimeError("mount: /dev/sdb2: unknown filesystem type")
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()})
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["phase"] == "COMPLETED" and job["guest_fixup"]["status"] == "failed"
    assert "unknown filesystem" in job["guest_fixup"]["detail"]
    assert job["network_fixup"]["status"] == "failed"
    env.fixup_result = GuestFixup(status="not_needed", detail="virtio present")

    # one step opted out: only the other one runs
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target(rebuild_initramfs=False)})
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["guest_fixup"] == {"status": "skipped", "detail": "disabled for this job", "kernels": [], "log": []}
    assert job["network_fixup"]["status"] == "done"
    assert env.fixups[-1][1:] == (False, True)
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target(fix_network=False)})
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["network_fixup"]["detail"] == "disabled for this job" and job["guest_fixup"]["status"] == "not_needed"
    assert env.fixups[-1][1:] == (True, False)

    # both opted out / Windows: skipped without touching the disk
    n = len(env.fixups)
    r = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target(rebuild_initramfs=False, fix_network=False)})
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["guest_fixup"]["detail"] == "disabled for this job" and job["network_fixup"]["detail"] == "disabled for this job"
    r = c.post("/api/jobs", json={"vm_moid": "vm-202", "target": target(windows_license_type="BRING_YOUR_OWN_LICENSE")})
    job = wait_phase(c, r.json()["id"], "COMPLETED", "FAILED")
    assert job["guest_fixup"]["status"] == "skipped" and "Windows" in job["guest_fixup"]["detail"]
    assert job["network_fixup"]["status"] == "skipped" and "Windows" in job["network_fixup"]["detail"]
    assert len(env.fixups) == n


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


def test_concurrency_limit_queues_jobs_and_can_be_raised_at_runtime(tmp_path, fast_retries):
    gate = threading.Event()
    env = Env(tmp_path, fail_once=frozenset(), block_event=gate)
    env.vms["vm-b"] = make_vm(moid="vm-b", name="web-02", disks=((2 * MIB, "pvscsi"),))
    with TestClient(env.app) as c:
        login(c)
        c.put("/api/setup/operation", json={"max_concurrent_jobs": 1, "session_ttl_s": 3600})
        a = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()}).json()["id"]
        wait_until(lambda: c.get(f"/api/jobs/{a}").json()["disks"][0]["status"] == "COPYING", what="copying")
        b = c.post("/api/jobs", json={"vm_moid": "vm-b", "target": target()}).json()["id"]
        wait_until(lambda: "Waiting for a free migration slot" in (c.get(f"/api/jobs/{b}").json()["message"] or ""),
                   what="second job queued")
        assert c.get(f"/api/jobs/{b}").json()["phase"] == "QUEUED"

        # raising the limit lets the queued job start without touching the running one
        c.put("/api/setup/operation", json={"max_concurrent_jobs": 2, "session_ttl_s": 3600})
        wait_until(lambda: c.get(f"/api/jobs/{b}").json()["phase"] == "EXPORTING", what="second job running")
        assert c.get(f"/api/jobs/{a}").json()["phase"] == "EXPORTING"
        gate.set()
        assert wait_phase(c, a, "COMPLETED", "FAILED")["phase"] == "COMPLETED"
        assert wait_phase(c, b, "COMPLETED", "FAILED")["phase"] == "COMPLETED"


def test_cancel_while_queued_needs_no_cleanup(tmp_path, fast_retries):
    gate = threading.Event()
    env = Env(tmp_path, fail_once=frozenset(), block_event=gate)
    env.vms["vm-b"] = make_vm(moid="vm-b", name="web-02", disks=((2 * MIB, "pvscsi"),))
    with TestClient(env.app) as c:
        login(c)
        c.put("/api/setup/operation", json={"max_concurrent_jobs": 1, "session_ttl_s": 3600})
        a = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()}).json()["id"]
        wait_until(lambda: c.get(f"/api/jobs/{a}").json()["disks"][0]["status"] == "COPYING", what="copying")
        b = c.post("/api/jobs", json={"vm_moid": "vm-b", "target": target()}).json()["id"]
        wait_until(lambda: "Waiting for a free migration slot" in (c.get(f"/api/jobs/{b}").json()["message"] or ""),
                   what="second job queued")
        launched = len(env.fake.compute.launch_details)
        assert c.post(f"/api/jobs/{b}/cancel").status_code == 202
        job = wait_phase(c, b, "CANCELLED", "COMPLETED", "FAILED")
        assert job["phase"] == "CANCELLED" and job["instance_id"] is None, job
        assert len(env.fake.compute.launch_details) == launched  # nothing was provisioned for it
        gate.set()
        assert wait_phase(c, a, "COMPLETED", "FAILED")["phase"] == "COMPLETED"


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


def test_purge_job_records(tmp_path, fast_retries):
    """Setup page: delete failed (FAILED + CANCELLED) or all finished job records; active jobs stay."""
    gate = threading.Event()
    env = Env(tmp_path, fail_once=frozenset(), block_event=gate)
    with TestClient(env.app) as c:
        assert c.delete("/api/setup/jobs").status_code == 401
        login(c)
        # one completed, one failed, one cancelled and one that is still copying (blocked on the gate)
        store = env.app.state.store
        done = c.post("/api/jobs", json={"vm_moid": "vm-101", "target": target()}).json()
        gate.set()
        wait_phase(c, done["id"], "COMPLETED")
        gate.clear()
        for phase in (JobPhase.FAILED, JobPhase.CANCELLED):
            store.put(Job.model_validate({**done, "id": uuid.uuid4().hex, "phase": phase.value}))
        running = c.post("/api/jobs", json={"vm_moid": "vm-303", "target": target(
            windows_license_type="BRING_YOUR_OWN_LICENSE")}).json()
        wait_phase(c, running["id"], "EXPORTING")
        assert len(c.get("/api/jobs").json()) == 4

        assert c.delete("/api/setup/jobs?scope=bogus").status_code == 422
        r = c.delete("/api/setup/jobs?scope=failed")
        assert r.status_code == 200 and r.json() == {"scope": "failed", "deleted": 2, "kept_active": 1}
        assert {j["phase"] for j in c.get("/api/jobs").json()} == {"COMPLETED", "EXPORTING"}
        r = c.delete("/api/setup/jobs?scope=all")
        assert r.json() == {"scope": "all", "deleted": 1, "kept_active": 1}
        left = c.get("/api/jobs").json()
        assert [j["id"] for j in left] == [running["id"]]  # the active job is never deleted
        assert c.delete("/api/setup/jobs").json()["deleted"] == 0  # default scope: failed; nothing left
        gate.set()
        wait_phase(c, running["id"], "COMPLETED", "FAILED")
