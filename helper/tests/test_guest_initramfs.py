"""Guest initramfs fix-up against a scripted shell: no block devices, LVM or chroot involved."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from helper_app.guest.initramfs import (
    DRACUT_CONF_NAME,
    VIRTIO_DRIVERS,
    CmdResult,
    InitramfsFixer,
    _Session,
)

RHEL_KERNEL = "3.10.0-1160.el7.x86_64"
OLD_KERNEL = "3.10.0-1127.el7.x86_64"


class FakeShell:
    """Answers the commands the fixer runs.  ``mount`` materialises the guest tree under the mount point
    so the fixer's file checks work on a real (temporary) directory."""

    def __init__(self, layout: str = "lvm", boot_fstab="UUID=boot-uuid", virtio_in=(), tools_running=True,
                 lvm_foreign_vgs=("ocivolume",), dracut=True, dracut_rc=0, fail_mount=(), old_dracut=False,
                 udev_stale=False, lsblk_hides_lvs=False):
        self.layout = layout
        self.udev_stale = udev_stale  # lsblk shows the LVs but without FSTYPE (udev has not probed them)
        self.lsblk_hides_lvs = lsblk_hides_lvs  # lsblk does not list the LVs at all
        self.boot_fstab = boot_fstab
        self.virtio_in = set(virtio_in)  # kernels whose initramfs already has virtio
        self.foreign = lvm_foreign_vgs
        self.dracut = dracut
        self.dracut_rc = dracut_rc
        self.fail_mount = set(fail_mount)
        self.old_dracut = old_dracut
        self.calls: list[list[str]] = []
        self.active_vgs: list[str] = []
        self.mounted: dict[str, str] = {}  # mountpoint -> device
        self.rebuilt: list[str] = []

    # -- the disk as lsblk would show it
    def nodes(self):
        disk = {"name": "/dev/sdb", "type": "disk", "fstype": None, "uuid": None, "label": None, "children": []}
        if self.layout == "lvm":
            disk["children"] = [
                {"name": "/dev/sdb1", "type": "part", "fstype": "xfs", "uuid": "boot-uuid", "label": None},
                {"name": "/dev/sdb2", "type": "part", "fstype": "LVM2_member", "uuid": "pv-uuid", "label": None,
                 "children": [
                     {"name": "/dev/mapper/rhel-root", "type": "lvm", "fstype": None if self.udev_stale else "xfs",
                      "uuid": None if self.udev_stale else "root-uuid"},
                     {"name": "/dev/mapper/rhel-swap", "type": "lvm", "fstype": None if self.udev_stale else "swap",
                      "uuid": None if self.udev_stale else "swap-uuid"},
                 ] if "rhel" in self.active_vgs and not self.lsblk_hides_lvs else []},
            ]
        elif self.layout == "plain":
            disk["children"] = [
                {"name": "/dev/sdb1", "type": "part", "fstype": "ext4", "uuid": "boot-uuid", "label": "boot"},
                {"name": "/dev/sdb2", "type": "part", "fstype": "ext4", "uuid": "root-uuid", "label": None},
            ]
        elif self.layout == "luks":
            disk["children"] = [
                {"name": "/dev/sdb1", "type": "part", "fstype": "xfs", "uuid": "boot-uuid", "label": None},
                {"name": "/dev/sdb2", "type": "part", "fstype": "crypto_LUKS", "uuid": "luks-uuid", "label": None},
            ]
        elif self.layout == "windows":
            disk["children"] = [
                {"name": "/dev/sdb1", "type": "part", "fstype": "ntfs", "uuid": "0000-1111", "label": "System"},
            ]
        return {"blockdevices": [disk]}

    # -- guest file trees materialised on mount
    def fill_root(self, mnt: Path):
        (mnt / "etc").mkdir(parents=True, exist_ok=True)
        (mnt / "etc" / "fstab").write_text(
            "# guest fstab\n"
            f"{'/dev/mapper/rhel-root' if self.layout == 'lvm' else 'UUID=root-uuid'} / xfs defaults 0 0\n"
            + (f"{self.boot_fstab} /boot xfs defaults 0 0\n" if self.boot_fstab else "")
            + ("/dev/mapper/rhel-swap swap swap defaults 0 0\n" if self.layout == "lvm" else ""))
        for ver in (RHEL_KERNEL, OLD_KERNEL):
            (mnt / "lib" / "modules" / ver).mkdir(parents=True, exist_ok=True)
            (mnt / "lib" / "modules" / ver / "modules.dep").write_text("")
        (mnt / "lib" / "modules" / "3.10.0-957.el7.x86_64").mkdir()  # removed kernel: no modules.dep
        if self.dracut:
            (mnt / "usr" / "bin").mkdir(parents=True, exist_ok=True)
            (mnt / "usr" / "bin" / "dracut").write_text("#!/bin/bash\n")
        if not self.boot_fstab:  # /boot on the root fs
            self.fill_boot(mnt / "boot")
        # a RHEL 7 style network stack: NetworkManager enabled, profile bound to ens192
        (mnt / "etc" / "os-release").write_text('PRETTY_NAME="Red Hat Enterprise Linux Server 7.9 (Maipo)"\n')
        (mnt / "usr" / "lib" / "systemd" / "system").mkdir(parents=True, exist_ok=True)
        (mnt / "usr" / "lib" / "systemd" / "system" / "NetworkManager.service").write_text("[Unit]")
        (mnt / "etc" / "systemd" / "system" / "multi-user.target.wants").mkdir(parents=True, exist_ok=True)
        (mnt / "etc" / "systemd" / "system" / "multi-user.target.wants" / "NetworkManager.service").write_text("")
        (mnt / "etc" / "NetworkManager" / "system-connections").mkdir(parents=True, exist_ok=True)
        (mnt / "etc" / "NetworkManager" / "system-connections" / "ens192.nmconnection").write_text(
            "[connection]\ntype=ethernet\ninterface-name=ens192\n[ipv4]\nmethod=manual\n")

    def fill_boot(self, boot: Path):
        boot.mkdir(parents=True, exist_ok=True)
        for ver in (RHEL_KERNEL, OLD_KERNEL):
            (boot / f"vmlinuz-{ver}").write_text("kernel")
            (boot / f"initramfs-{ver}.img").write_text(
                "vmw_pvscsi.ko " + ("virtio_blk.ko" if ver in self.virtio_in else ""))
        (boot / "initramfs-0-rescue-abc.img").write_text("rescue")

    # -- command dispatcher
    def __call__(self, argv: list[str], timeout_s: int) -> CmdResult:
        self.calls.append(argv)
        cmd = argv[0]
        if cmd in ("partx", "udevadm", "sync"):
            return CmdResult(0, "", "")
        if cmd == "lsblk":
            return CmdResult(0, json.dumps(self.nodes()), "")
        if cmd == "pvs":
            if "--config" in argv:  # our disk only
                if self.layout != "lvm":
                    return CmdResult(0, "", "")
                return CmdResult(0, "  /dev/sdb2 rhel\n", "")
            return CmdResult(0, "".join(f"  /dev/sda3 {vg}\n" for vg in self.foreign), "")
        if cmd == "vgchange":
            vg = argv[-1]
            if "-ay" in argv:
                self.active_vgs.append(vg)
            else:
                self.active_vgs.remove(vg)
            return CmdResult(0, "", "")
        if cmd == "lvs":
            assert "--config" in argv and argv[-1] in self.active_vgs
            return CmdResult(0, "  /dev/mapper/rhel-root\n  /dev/mapper/rhel-swap\n", "")
        if cmd == "blkid":
            assert "-p" in argv, "direct probe expected (udev cache is what failed us)"
            probes = {"/dev/mapper/rhel-root": "TYPE=xfs\nUUID=root-uuid\n",
                      "/dev/mapper/rhel-swap": "TYPE=swap\nUUID=swap-uuid\n"}
            self.probed = getattr(self, "probed", []) + [argv[-1]]
            return CmdResult(0, probes[argv[-1]], "") if argv[-1] in probes else CmdResult(2, "", "")
        if cmd == "mount":
            if "--bind" in argv:
                self.mounted[argv[-1]] = argv[-2]
                return CmdResult(0, "", "")
            if "remount,rw" in argv[2:3]:
                return CmdResult(0, "", "")
            dev, where = argv[-2], Path(argv[-1])
            if dev in self.fail_mount:
                return CmdResult(32, "", f"mount: {dev}: wrong fs type, bad option, bad superblock")
            self.mounted[str(where)] = dev
            if dev in ("/dev/mapper/rhel-root", "/dev/sdb2") and self.layout in ("lvm", "plain"):
                self.fill_root(where)
            elif dev == "/dev/sdb1" and where.name == "boot":
                self.fill_boot(where)
            else:
                where.mkdir(parents=True, exist_ok=True)  # /boot partition probed as root: no fstab there
                (where / "vmlinuz-x").write_text("")
            return CmdResult(0, "", "")
        if cmd == "umount":
            where = argv[-1]
            self.mounted.pop(where, None)
            p = Path(where)
            if p.exists() and "--bind" not in argv:
                for child in p.iterdir():
                    if str(child) not in self.mounted:  # keep still-mounted sub mounts (never the case here)
                        shutil.rmtree(child) if child.is_dir() else child.unlink()
            return CmdResult(0, "", "")
        if cmd == "setfattr":
            return CmdResult(0, "", "")
        if cmd == "chroot":
            root, prog = Path(argv[1]), argv[2]
            if prog.endswith("setfiles"):
                return CmdResult(0, "", "")
            if prog == "lsinitrd":
                img = root / argv[3].lstrip("/")
                return CmdResult(0, img.read_text(), "") if img.exists() else CmdResult(1, "", "no such file")
            if prog == "dracut":
                if argv[3] == "--help":
                    return CmdResult(0, "" if self.old_dracut else "  -N, --no-hostonly  Host-Only mode off", "")
                if self.dracut_rc:
                    return CmdResult(self.dracut_rc, "", "dracut: Failed to install module kernel-modules")
                img, ver = root / argv[-2].lstrip("/"), argv[-1]
                assert "--add-drivers" in argv and VIRTIO_DRIVERS in argv
                assert ("--no-hostonly" in argv) is (not self.old_dracut)
                img.write_text(f"generic image {ver} virtio_blk.ko virtio_scsi.ko")
                self.rebuilt.append(ver)
                return CmdResult(0, "", "")
        raise AssertionError(f"unexpected command {argv}")


@pytest.fixture
def base(tmp_path):
    return tmp_path / "mnt"


def run(shell: FakeShell, base: Path, device="/dev/sdb"):
    fixer = InitramfsFixer(run=shell, mount_base=base)
    msgs = []
    result = fixer.rebuild(device, notify=msgs.append)
    # everything is undone whatever happened
    if isinstance(shell, FakeShell):
        assert shell.mounted == {}, shell.mounted
        assert shell.active_vgs == []
    assert not base.exists() or not any(base.iterdir())
    return result, msgs


def test_rhel_lvm_root_rebuilt(base):
    shell = FakeShell(layout="lvm", virtio_in={OLD_KERNEL})
    result, msgs = run(shell, base)
    assert result.status == "done", result
    assert result.kernels == [RHEL_KERNEL]  # the other one already had virtio
    assert shell.rebuilt == [RHEL_KERNEL]
    assert any("activated guest volume group(s) rhel" in m for m in msgs)
    assert any("/boot on /dev/sdb1" in m for m in msgs)
    assert any("already has virtio" in m and OLD_KERNEL in m for m in msgs)
    # the dracut config snippet went into the guest before dracut ran
    conf_writes = [c for c in shell.calls if c[0] == "chroot" and c[2] == "dracut" and c[3] == "--force"]
    assert len(conf_writes) == 1
    # order: LVM activated with a filter on our disk only, deactivated at the end
    vgchange = [c for c in shell.calls if c[0] == "vgchange"]
    assert [c[-2] for c in vgchange] == ["-ay", "-an"] and all("/dev/sdb" in c[2] for c in vgchange)
    assert all("use_devicesfile=0" in c[2] for c in vgchange)
    # bind mounts for the chroot were made and removed
    binds = [c[-1] for c in shell.calls if c[0] == "mount" and "--bind" in c]
    assert [Path(b).name for b in binds] == ["dev", "proc", "sys"]
    # xfs mounted with nouuid (the same image may be attached twice)
    root_mount = next(c for c in shell.calls if c[0] == "mount" and c[-2] == "/dev/mapper/rhel-root")
    assert "nouuid" in root_mount[2]


def test_lvs_found_even_when_udev_has_not_probed_them(base):
    """What happened on the first real RHEL 7 run: lsblk listed the freshly activated LVs without a file
    system type, so the root LV was not a candidate.  blkid -p fills the gap."""
    shell = FakeShell(layout="lvm", udev_stale=True)
    result, msgs = run(shell, base)
    assert result.status == "done" and result.kernels == [OLD_KERNEL, RHEL_KERNEL], result
    assert "/dev/mapper/rhel-root" in shell.probed
    assert any("block devices:" in m and "/dev/mapper/rhel-root (lvm, xfs)" in m for m in msgs)
    # lsblk does not even list the LVs: lvs supplies them
    shell = FakeShell(layout="lvm", lsblk_hides_lvs=True)
    result, msgs = run(shell, base)
    assert result.status == "done", result
    assert any(c[0] == "lvs" for c in shell.calls)
    assert any("guest root file system on /dev/mapper/rhel-root" in m for m in msgs)


def test_rejected_candidates_are_explained(base):
    shell = FakeShell(layout="plain", boot_fstab="")
    _, msgs = run(shell, base)
    # /dev/sdb1 (the boot partition) was probed first and rejected with a reason
    assert any("/dev/sdb1 is not the root fs (fstab=no" in m for m in msgs)


def test_plain_partitions_and_boot_on_root(base):
    shell = FakeShell(layout="plain", boot_fstab="")  # no separate /boot
    result, _ = run(shell, base)
    assert result.status == "done" and sorted(result.kernels) == sorted([OLD_KERNEL, RHEL_KERNEL])
    assert not any(c[0] == "vgchange" for c in shell.calls)


def test_boot_by_guest_device_name_maps_to_our_disk(base):
    # fstab says /dev/sda1: must become partition 1 of *our* disk, never the helper's /dev/sda1
    shell = FakeShell(layout="lvm", boot_fstab="/dev/sda1")
    result, msgs = run(shell, base)
    assert result.status == "done"
    assert any("/boot on /dev/sdb1" in m for m in msgs)


def test_not_needed_when_virtio_present(base):
    shell = FakeShell(layout="lvm", virtio_in={OLD_KERNEL, RHEL_KERNEL})
    result, _ = run(shell, base)
    assert result.status == "not_needed" and shell.rebuilt == []
    assert "2 kernel(s) already" in result.detail


def test_skips(base):
    # LUKS
    result, _ = run(FakeShell(layout="luks"), base)
    assert result.status == "skipped" and "LUKS" in result.detail
    # Windows / no Linux root
    result, _ = run(FakeShell(layout="windows"), base)
    assert result.status == "skipped" and "no Linux root" in result.detail
    # no dracut in the guest
    result, _ = run(FakeShell(layout="plain", boot_fstab="", dracut=False), base)
    assert result.status == "skipped" and "no dracut" in result.detail
    # volume group name clash with the helper
    shell = FakeShell(layout="lvm", lvm_foreign_vgs=("rhel",))
    result, _ = run(shell, base)
    assert result.status == "skipped" and "same name" in result.detail
    assert not any(c[0] == "vgchange" for c in shell.calls)  # never activated
    # /boot listed in fstab but not on this disk
    result, _ = run(FakeShell(layout="lvm", boot_fstab="UUID=elsewhere"), base)
    assert result.status == "skipped" and "/boot" in result.detail


def test_failures_are_reported_not_raised(base):
    shell = FakeShell(layout="lvm", dracut_rc=1)
    result, _ = run(shell, base)
    assert result.status == "failed" and "dracut failed for kernel" in result.detail
    assert "Failed to install module" in result.detail
    # the disk cannot be mounted at all
    result, _ = run(FakeShell(layout="lvm", fail_mount={"/dev/mapper/rhel-root", "/dev/sdb1"}), base)
    assert result.status == "skipped" and "no Linux root" in result.detail
    # missing tool on the helper
    def no_lsblk(argv, timeout_s):
        if argv[0] == "lsblk":
            raise FileNotFoundError("lsblk")
        return CmdResult(0, "", "")
    result, _ = run(no_lsblk, base)  # type: ignore[arg-type]
    assert result.status == "failed" and "cannot run lsblk" in result.detail


def test_old_dracut_without_no_hostonly(base):
    shell = FakeShell(layout="plain", boot_fstab="", old_dracut=True)
    result, _ = run(shell, base)
    assert result.status == "done"


def test_dracut_conf_written_into_guest(base, monkeypatch):
    captured = {}
    shell = FakeShell(layout="plain", boot_fstab="")
    orig = shell.__call__

    def spy(argv, timeout_s):
        if argv[0] == "chroot" and argv[2] == "dracut" and argv[3] == "--force":
            conf = Path(argv[1]) / "etc" / "dracut.conf.d" / DRACUT_CONF_NAME
            captured["conf"] = conf.read_text()
        return orig(argv, timeout_s)

    result, _ = run(spy, base)  # type: ignore[arg-type]
    assert result.status == "done"
    assert f'add_drivers+=" {VIRTIO_DRIVERS} "' in captured["conf"]


def test_both_steps_share_one_mount_session(base):
    from helper_app.guest.fixup import GuestFixer
    from helper_app.guest.network import NM_KEYFILE

    shell = FakeShell(layout="lvm", virtio_in={OLD_KERNEL})
    written = {}
    orig = shell.__call__

    def spy(argv, timeout_s):
        # the network step runs while the root is still mounted; capture what it wrote before umount
        if argv[0] == "umount":
            kf = Path(argv[-1]) / "etc" / "NetworkManager" / "system-connections" / NM_KEYFILE
            if kf.exists():
                written["keyfile"] = kf.read_text()
        return orig(argv, timeout_s)

    msgs = []
    res = GuestFixer(run=spy, mount_base=base).fix("/dev/sdb", initramfs=True, network=True, notify=msgs.append)
    assert res.initramfs.status == "done" and res.initramfs.kernels == [RHEL_KERNEL]
    assert res.network.status == "done" and "NetworkManager DHCP profile" in res.network.detail
    assert "type=ethernet" in written["keyfile"]
    # one scan, one root mount, everything undone
    assert sum(1 for c in shell.calls if c[0] == "lsblk") == 2  # before and after LVM activation
    assert sum(1 for c in shell.calls if c[0] == "mount" and c[-2] == "/dev/mapper/rhel-root") == 1
    assert shell.mounted == {} and shell.active_vgs == []
    # each step has its own log; the network log does not repeat the disk scan
    assert any("guest root file system on" in ln for ln in res.initramfs.log)
    assert not any("guest root file system on" in ln for ln in res.network.log)
    assert any("network stack: NetworkManager (enabled)" in ln for ln in res.network.log)

    # one step failing does not stop the other
    shell = FakeShell(layout="lvm", dracut_rc=1)
    res = GuestFixer(run=shell, mount_base=base).fix("/dev/sdb", initramfs=True, network=True)
    assert res.initramfs.status == "failed" and res.network.status == "done"
    # a step that is not requested is not reported
    res = GuestFixer(run=FakeShell(layout="lvm"), mount_base=base).fix("/dev/sdb", initramfs=False, network=True)
    assert res.initramfs is None and res.network.status == "done"
    # no root at all: both requested steps get the same answer
    res = GuestFixer(run=FakeShell(layout="luks"), mount_base=base).fix("/dev/sdb")
    assert res.initramfs.status == "skipped" and res.network.status == "skipped" and "LUKS" in res.network.detail


def test_resolve_spec():
    from helper_app.guest.initramfs import BlockNode
    nodes = [BlockNode("/dev/sdb1", "part", "xfs", "u1", "BOOT"), BlockNode("/dev/sdb2", "part", "LVM2_member", "u2", ""),
             BlockNode("/dev/mapper/my--vg-root", "lvm", "xfs", "u3", ""), BlockNode("/dev/sdb15", "part", "vfat", "u4", "")]
    r = _Session.resolve_spec
    assert r("UUID=u1", nodes) == "/dev/sdb1"
    assert r("LABEL=BOOT", nodes) == "/dev/sdb1"
    assert r("/dev/mapper/my--vg-root", nodes) == "/dev/mapper/my--vg-root"
    assert r("/dev/my-vg/root", nodes) == "/dev/mapper/my--vg-root"
    assert r("/dev/sda1", nodes) == "/dev/sdb1"
    assert r("/dev/nvme0n1p15", nodes) == "/dev/sdb15"
    assert r("/dev/sda3", nodes) is None
    assert r("/dev/sda", nodes) is None  # whole disk: not a partition
    assert r("PARTUUID=abc", nodes) is None
