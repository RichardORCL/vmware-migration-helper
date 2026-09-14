import pytest
from helper_app.models import Firmware
from helper_app.vsphere.export import DiskUrl, ExportError, match_disk_urls, rewrite_lease_url
from helper_app.vsphere.inventory import preflight, vm_spec_from_vm, warnings

from .fake_vsphere import GIB, make_vm


def test_vm_spec_from_vm_basic():
    spec = vm_spec_from_vm(make_vm())
    assert spec.moid == "vm-101" and spec.name == "web-01"
    assert spec.num_cpu == 4 and spec.memory_mb == 8192
    assert spec.firmware == Firmware.EFI
    assert spec.power_state == "poweredOff"
    assert [d.index for d in spec.disks] == [0, 1]
    assert spec.disks[0].capacity_bytes == 40 * GIB
    assert spec.disks[0].controller_type == "pvscsi"
    assert spec.disks[0].controller_class == "ParaVirtualSCSIController"
    assert spec.disks[0].nfc_key_hint == "ParaVirtualSCSIController0:0"
    assert spec.disks[1].nfc_key_hint == "ParaVirtualSCSIController0:1"
    assert spec.disks[0].thin_provisioned and spec.disks[0].backing_file.endswith("web-01.vmdk")
    assert spec.nics[0].adapter_type == "vmxnet3" and spec.nics[0].network == "VM Network"
    assert preflight(spec) == []


def test_disk_ordering_ide_before_scsi_and_controller_types():
    vm = make_vm(disks=((20 * GIB, "lsilogic"), (10 * GIB, "ide"), (30 * GIB, "sata")), nics=("e1000",),
                 firmware="bios")
    spec = vm_spec_from_vm(vm)
    assert [d.controller_type for d in spec.disks] == ["ide", "lsilogic", "sata"]
    assert spec.disks[0].capacity_bytes == 10 * GIB
    assert spec.disks[1].controller_class == "VirtualLsiLogicController"
    assert spec.firmware == Firmware.BIOS
    assert spec.nics[0].adapter_type == "e1000"


def test_preflight_and_warnings():
    spec = vm_spec_from_vm(make_vm(power_state="poweredOn", snapshot=object(), secure_boot=True, num_cpu=3,
                                   disks=((10 * GIB, "pvscsi"),)))
    problems = preflight(spec)
    assert any("powered off" in p for p in problems)
    notes = warnings(spec)
    assert any("snapshots" in n for n in notes)
    assert any("Secure Boot" in n for n in notes)
    assert any("3 vCPUs" in n for n in notes)
    assert any("50 GB" in n for n in notes)
    assert spec.is_windows is False
    assert vm_spec_from_vm(make_vm(guest_id="windows2019srv_64Guest")).is_windows


def test_rewrite_lease_url():
    assert rewrite_lease_url("https://*/ha-nfc/52a1.vmdk?x=1", "vc.example.com") == \
        "https://vc.example.com/ha-nfc/52a1.vmdk?x=1"
    assert rewrite_lease_url("https://*:443/nfc/disk-0.vmdk", "esx1") == "https://esx1:443/nfc/disk-0.vmdk"
    assert rewrite_lease_url("https://esx1/nfc/disk.vmdk", "vc") == "https://esx1/nfc/disk.vmdk"


def test_match_disk_urls_by_key():
    spec = vm_spec_from_vm(make_vm())
    urls = [
        DiskUrl(key="/vm-101/ParaVirtualSCSIController0:1", target_id="disk-1.vmdk", url="u1"),
        DiskUrl(key="/vm-101/ParaVirtualSCSIController0:0", target_id="disk-0.vmdk", url="u0"),
    ]
    m = match_disk_urls(spec.disks, urls)
    assert m[0].url == "u0" and m[1].url == "u1"


def test_match_disk_urls_fallback_target_id_and_positional():
    spec = vm_spec_from_vm(make_vm())
    urls = [DiskUrl(key="", target_id="disk-1.vmdk", url="u1"), DiskUrl(key="", target_id="disk-0.vmdk", url="u0")]
    m = match_disk_urls(spec.disks, urls)
    assert m[0].url == "u0" and m[1].url == "u1"
    urls = [DiskUrl(key="a", target_id="x.vmdk", url="p0"), DiskUrl(key="b", target_id="y.vmdk", url="p1")]
    m = match_disk_urls(spec.disks, urls)
    assert m[0].url == "p0" and m[1].url == "p1"
    with pytest.raises(ExportError):
        match_disk_urls(spec.disks, [DiskUrl(key="", target_id="", url="only-one")])
