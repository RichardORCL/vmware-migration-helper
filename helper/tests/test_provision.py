import pytest
from helper_app.config import Settings
from helper_app.jobs.store import JobStore, utcnow
from helper_app.models import (
    DiskSpec,
    DiskStatus,
    Firmware,
    Job,
    JobPhase,
    NicSpec,
    OciTarget,
    VmSpec,
    WindowsLicenseType,
)
from helper_app.oci.clients import OciError
from helper_app.oci.provision import Provisioner
from helper_app.oci.seed_image import SeedImageService

from .fake_oci import FakeOci

GIB = 1024**3


def make_vm(windows=False, firmware=Firmware.EFI, disks=2) -> VmSpec:
    return VmSpec(
        moid="vm-42",
        name="app-server-01",
        num_cpu=4,
        memory_mb=16384,
        guest_id="windows2022srvNext_64Guest" if windows else "oracleLinux8_64Guest",
        guest_full_name="Microsoft Windows Server 2022 (64-bit)" if windows else "Oracle Linux 8 (64-bit)",
        firmware=firmware,
        disks=[
            DiskSpec(index=i, label=f"Hard disk {i + 1}", device_key=2000 + i, capacity_bytes=(40 + 60 * i) * GIB,
                     controller_type="pvscsi")
            for i in range(disks)
        ],
        nics=[NicSpec(label="Network adapter 1", adapter_type="vmxnet3")],
    )


def make_target(**kw) -> OciTarget:
    base = dict(compartment_id="ocid1.compartment.oc1..migr", availability_domain="Uocm:EU-FRANKFURT-1-AD-1",
                subnet_id="ocid1.subnet.oc1..1")
    base.update(kw)
    return OciTarget(**base)


def make_job(vm: VmSpec, target: OciTarget) -> Job:
    now = utcnow()
    return Job(id="job0001", vm=vm, target=target, created_at=now, updated_at=now)


@pytest.fixture
def env(tmp_path):
    settings = Settings(device_prefix=str(tmp_path / "dev" / "oraclevd"), db_path=str(tmp_path / "jobs.db"),
                        seed_bucket="vc-oci-seed", launch_timeout_s=5, volume_timeout_s=5,
                        image_import_timeout_s=5)
    fake = FakeOci(settings.device_prefix)
    clients = fake.clients()
    store = JobStore(settings.db_path)
    prov = Provisioner(clients, settings, store.put, SeedImageService(clients, settings))
    return settings, fake, store, prov


def test_prepare_linux_two_disks(env):
    settings, fake, store, prov = env
    job = make_job(make_vm(), make_target())
    store.put(job)
    prov.prepare(job)

    assert job.phase == JobPhase.PROVISIONING and job.step == "ready"
    assert job.launch_options.firmware == "UEFI_64"
    assert len(job.disks) == 2 and job.disks[0].is_boot
    assert job.disks[0].size_gb == 50 and job.disks[1].size_gb == 100

    # seed image imported as PARAVIRTUALIZED (CUSTOM is not importable), VMDK placeholder, OS metadata, schema
    img = fake.compute.images[job.seed_image_id]
    assert img.compartment_id == fake.identity.compartment_id  # helper compartment from discovered identity
    assert all("." not in k and " " not in k for k in img.freeform_tags), "OCI rejects freeform tag keys with periods"
    assert img.launch_mode == "PARAVIRTUALIZED"
    assert img.source_image_type == "VMDK"
    assert (img.operating_system, img.operating_system_version) == ("Oracle Linux", "8")
    assert img.freeform_tags["vc-oci-firmware"] == "UEFI_64"
    assert fake.object_storage.deleted == [img.object_name]  # placeholder object removed
    assert "vc-oci-seed" in fake.object_storage.buckets
    schema = fake.compute.capability_schemas[0].schema_data
    assert schema["Compute.Firmware"].values == ["UEFI_64"]
    assert schema["Compute.LaunchMode"].default_value == "PARAVIRTUALIZED"
    # every device model stays selectable so per-job LaunchOptions are accepted at launch
    assert set(schema["Storage.BootVolumeType"].values) >= {"PARAVIRTUALIZED", "IDE", "SCSI", "ISCSI"}
    assert set(schema["Network.AttachmentType"].values) >= {"PARAVIRTUALIZED", "E1000", "VFIO"}

    # instance launched from the seed with explicit launch options and matching shape
    ld = fake.compute.launch_details[0]
    assert ld.source_details.image_id == job.seed_image_id
    assert ld.source_details.boot_volume_size_in_gbs == 50
    assert ld.launch_options.firmware == "UEFI_64"
    assert ld.launch_options.boot_volume_type == "PARAVIRTUALIZED"
    assert ld.launch_options.network_type == "PARAVIRTUALIZED"
    assert ld.shape_config.ocpus == 2 and ld.shape_config.memory_in_gbs == 16
    assert ld.licensing_configs is None
    assert ld.create_vnic_details.hostname_label == "app-server-01"

    # stopped, boot volume detached from the target and attached to the helper
    assert (job.instance_id, "STOP") in fake.compute.actions
    assert fake.compute.instances[job.instance_id].lifecycle_state == "STOPPED"
    boot_att = [a for a in fake.compute.boot_attachments.values() if a.instance_id == job.instance_id]
    assert boot_att[0].lifecycle_state == "DETACHED"
    assert job.boot_volume_id == boot_att[0].boot_volume_id
    helper_atts = [a for a in fake.compute.vol_attachments.values() if a.instance_id == fake.identity.instance_id]
    assert {a.volume_id for a in helper_atts} == {job.disks[0].volume_id, job.disks[1].volume_id}
    assert all(a.attachment_type == "paravirtualized" for a in helper_atts)
    assert job.disks[0].device.endswith("oraclevdb") and job.disks[1].device.endswith("oraclevdc")
    assert all(d.status == DiskStatus.ATTACHED for d in job.disks)
    assert [d.label for d in job.disks] == ["Hard disk 1", "Hard disk 2"]
    # store mirrors the in-memory job
    assert store.get(job.id).step == "ready"


def test_prepare_windows_bios_licensing_and_seed_reuse(env):
    settings, fake, store, prov = env
    target = make_target(windows_license_type=WindowsLicenseType.OCI_PROVIDED, compatibility_mode=True,
                         display_name="Win Box #1")
    job = make_job(make_vm(windows=True, firmware=Firmware.BIOS, disks=1), target)
    store.put(job)
    prov.prepare(job)

    ld = fake.compute.launch_details[0]
    assert ld.launch_options.firmware == "BIOS"
    assert ld.launch_options.boot_volume_type == "IDE" and ld.launch_options.network_type == "E1000"
    assert ld.licensing_configs[0].type == "WINDOWS"
    assert ld.licensing_configs[0].license_type == "OCI_PROVIDED"
    assert ld.create_vnic_details.hostname_label == "win-box-1"
    img = fake.compute.images[job.seed_image_id]
    assert img.operating_system == "Windows" and img.operating_system_version == "Server 2022 Standard"
    assert img.launch_mode == "EMULATED"  # IDE + E1000 requested

    # a second Windows/BIOS job reuses the seed image
    job2 = make_job(make_vm(windows=True, firmware=Firmware.BIOS, disks=1), target)
    job2.id = "job0002"
    store.put(job2)
    prov.prepare(job2)
    assert job2.seed_image_id == job.seed_image_id
    assert len(fake.compute.images) == 1


def test_prepare_rejects_other_ad(env):
    settings, fake, store, prov = env
    job = make_job(make_vm(), make_target(availability_domain="Uocm:EU-FRANKFURT-1-AD-2"))
    store.put(job)
    with pytest.raises(OciError, match="availability domain"):
        prov.prepare(job)


def test_prepare_cancel_hook_aborts_between_steps(env):
    settings, fake, store, prov = env
    job = make_job(make_vm(), make_target())
    store.put(job)
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        prov.prepare(job, check_cancel=check)
    assert job.instance_id is not None  # launched before the third step
    assert all(d.device is None for d in job.disks)  # never got to the attachments


def test_finalize_requires_copied_then_reattaches_and_starts(env):
    settings, fake, store, prov = env
    job = make_job(make_vm(), make_target())
    store.put(job)
    prov.prepare(job)

    with pytest.raises(OciError, match="not been copied"):
        prov.finalize(job)

    for d in job.disks:
        d.status = DiskStatus.COPIED
    prov.finalize(job)

    assert job.phase == JobPhase.COMPLETED
    helper_atts = [a for a in fake.compute.vol_attachments.values() if a.instance_id == fake.identity.instance_id]
    assert all(a.lifecycle_state == "DETACHED" for a in helper_atts)
    boot_atts = [a for a in fake.compute.boot_attachments.values()
                 if a.instance_id == job.instance_id and a.lifecycle_state == "ATTACHED"]
    assert len(boot_atts) == 1 and boot_atts[0].boot_volume_id == job.boot_volume_id
    target_atts = [a for a in fake.compute.vol_attachments.values()
                   if a.instance_id == job.instance_id and a.lifecycle_state == "ATTACHED"]
    assert [a.volume_id for a in target_atts] == [job.disks[1].volume_id]
    assert target_atts[0].device.endswith("oraclevdb")
    assert (job.instance_id, "START") in fake.compute.actions
    assert fake.compute.instances[job.instance_id].lifecycle_state == "RUNNING"


def test_finalize_without_start(env):
    settings, fake, store, prov = env
    job = make_job(make_vm(disks=1), make_target(start_after_migration=False))
    store.put(job)
    prov.prepare(job)
    job.disks[0].status = DiskStatus.COPIED
    prov.finalize(job)
    assert (job.instance_id, "START") not in fake.compute.actions
    assert job.phase == JobPhase.COMPLETED


def test_cleanup_tears_down(env):
    settings, fake, store, prov = env
    job = make_job(make_vm(), make_target())
    store.put(job)
    prov.prepare(job)
    actions = prov.cleanup(job)
    assert job.phase == JobPhase.CANCELLED
    assert job.instance_id in fake.compute.terminated
    assert set(fake.blockstorage.deleted) == {job.disks[0].volume_id, job.disks[1].volume_id}
    assert all(a.startswith("ok:") for a in actions), actions


def test_update_windows_license(env):
    settings, fake, store, prov = env
    job = make_job(make_vm(windows=True, disks=1), make_target(windows_license_type=WindowsLicenseType.BRING_YOUR_OWN_LICENSE))
    store.put(job)
    prov.prepare(job)
    inst = prov.update_windows_license(job.instance_id, WindowsLicenseType.OCI_PROVIDED)
    assert inst.licensing_configs[0].license_type == "OCI_PROVIDED"
    assert fake.compute.updates[0][0] == job.instance_id


def test_wait_for_timeout_and_failure_state(env):
    settings, fake, store, prov = env
    clients = fake.clients()
    from types import SimpleNamespace

    class R:
        def __init__(self, state):
            self.data = SimpleNamespace(lifecycle_state=state)

    with pytest.raises(OciError, match="timed out"):
        clients.wait_for(lambda: R("PROVISIONING"), "lifecycle_state", ["RUNNING"], timeout_s=0.0, what="x")
    with pytest.raises(OciError, match="entered state TERMINATED"):
        clients.wait_for(lambda: R("TERMINATED"), "lifecycle_state", ["RUNNING"], timeout_s=1, what="x")
