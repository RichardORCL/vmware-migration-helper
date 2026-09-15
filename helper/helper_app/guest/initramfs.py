"""Rebuild a Linux guest's initramfs with virtio drivers on the copied boot volume.

Why: dracut builds *hostonly* initramfs images on RHEL/CentOS/Oracle Linux (and SUSE).  An image
built while the VM ran on VMware only contains ``vmw_pvscsi``/``mptspi``; in OCI the boot volume is a
virtio device, so the kernel never finds the disk, LVM never finds ``rhel/root`` and dracut drops to
the emergency shell ("dracut-initqueue timeout ... /dev/mapper/rhel-root does not exist").

How: the target boot volume is still attached to the helper right after the copy.  We find the guest
root file system (plain partition or LVM), mount it together with ``/boot``, and run the *guest's
own* dracut in a chroot (``dracut --force --no-hostonly --add-drivers "virtio ..."``) for every
installed kernel whose initramfs lacks virtio modules.  Only the target copy is touched; the source
VM stays as it was.  Anything unexpected (LUKS, btrfs, no dracut, volume group name clash with the
helper) ends in a *skipped*/*failed* result with the reason - never in a failed migration.

All shell interaction goes through an injectable ``Runner`` so the logic is unit-testable without
block devices.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from helper_app.models import GuestFixup

log = logging.getLogger(__name__)

VIRTIO_DRIVERS = "virtio virtio_pci virtio_ring virtio_blk virtio_scsi virtio_net"
DRACUT_CONF_NAME = "oci-virtio.conf"
ROOT_FS_TYPES = {"xfs", "ext4", "ext3", "ext2"}
DRACUT_TIMEOUT_S = 900
MIN_FREE_BOOT_BYTES = 150 * 1024 * 1024  # a non-hostonly initramfs is 50-90 MB

# one fix-up at a time: two RHEL guests both call their volume group "rhel"
_LOCK = threading.Lock()

Notify = Callable[[str], None]


class CmdResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], int], CmdResult]  # (argv, timeout_s) -> result; OSError if the binary is missing


def default_runner(argv: list[str], timeout_s: int = 120) -> CmdResult:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return CmdResult(124, out, f"timed out after {timeout_s} s")
    return CmdResult(p.returncode, p.stdout, p.stderr)


class Skip(Exception):
    """The fix-up does not apply to this guest / layout (reason is user facing)."""


class Fail(Exception):
    """The fix-up was attempted but did not succeed."""


class BlockNode(NamedTuple):
    path: str
    type: str  # disk | part | lvm | crypt ...
    fstype: str
    uuid: str
    label: str


def _tail(text: str, n: int = 6) -> str:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return " | ".join(lines[-n:])


class InitramfsFixer:
    def __init__(self, run: Runner = default_runner, mount_base: str | Path = "/run/vc-oci-helper/guest"):
        self.run = run
        self.mount_base = Path(mount_base)

    # ------------------------------------------------------------------ public
    def rebuild(self, device: str, notify: Optional[Notify] = None) -> GuestFixup:
        """Fix the initramfs images on the guest disk ``device`` (the copied boot volume)."""
        notes: list[str] = []

        def note(msg: str) -> None:
            notes.append(msg)
            log.info("guest fix-up %s: %s", device, msg)
            if notify:
                notify(msg)

        with _LOCK:
            session = _Session(self, device, note)
            try:
                kernels, rebuilt = session.execute()
                if not rebuilt:
                    return GuestFixup(status="not_needed", kernels=[], log=notes,
                                      detail=f"initramfs of {len(kernels)} kernel(s) already contains virtio drivers")
                return GuestFixup(status="done", kernels=rebuilt, log=notes,
                                  detail=f"initramfs rebuilt with virtio drivers for {', '.join(rebuilt)}")
            except Skip as exc:
                note(f"skipped: {exc}")
                return GuestFixup(status="skipped", detail=str(exc), log=notes)
            except Fail as exc:
                note(f"failed: {exc}")
                return GuestFixup(status="failed", detail=str(exc), log=notes)
            except Exception as exc:  # noqa: BLE001 - never let the fix-up take the migration down
                log.exception("guest fix-up on %s crashed", device)
                note(f"failed: {exc}")
                return GuestFixup(status="failed", detail=f"unexpected error: {exc}", log=notes)
            finally:
                session.cleanup()


class _Session:
    """One fix-up run with its mounts and activated volume groups (undone in cleanup())."""

    def __init__(self, fixer: InitramfsFixer, device: str, note: Notify):
        self.f = fixer
        self.run = fixer.run
        self.note = note
        self.device = device
        self.real = os.path.realpath(device) if os.path.exists(device) else device
        self.mnt = fixer.mount_base / uuid.uuid4().hex[:8]
        self.mounts: list[Path] = []  # mounted paths, unmounted in reverse order
        self.vgs: list[str] = []  # guest volume groups we activated
        # LVM must look at this disk only (and ignore the helper's devices file, which does not list it)
        self.lvm_config = ("devices { use_devicesfile=0 filter=[ "
                           f'"a|^{re.escape(self.real)}.*|", "r|.*|" ] }}')

    # ---------------------------------------------------------------- helpers
    def sh(self, argv: list[str], timeout_s: int = 120, ok: bool = True) -> CmdResult:
        try:
            r = self.run(argv, timeout_s)
        except OSError as exc:
            raise Fail(f"cannot run {argv[0]} on the helper: {exc}") from exc
        if ok and r.returncode != 0:
            raise Fail(f"{' '.join(argv[:3])} failed (rc {r.returncode}): {_tail(r.stderr or r.stdout)}")
        return r

    def mount(self, what: str, where: Path, opts: list[str]) -> None:
        where.mkdir(parents=True, exist_ok=True)
        argv = ["mount"] + (["-o", ",".join(opts)] if opts else []) + [what, str(where)]
        self.sh(argv)
        self.mounts.append(where)

    def bind(self, src: str, where: Path) -> None:
        where.mkdir(parents=True, exist_ok=True)
        self.sh(["mount", "--bind", src, str(where)])
        self.mounts.append(where)

    def umount(self, where: Path) -> None:
        r = self.sh(["umount", str(where)], ok=False)
        if r.returncode != 0:
            self.sh(["umount", "-l", str(where)], ok=False)
        if where in self.mounts:
            self.mounts.remove(where)

    def lsblk(self) -> list[BlockNode]:
        r = self.sh(["lsblk", "-J", "-p", "-o", "NAME,TYPE,FSTYPE,UUID,LABEL", self.real])
        try:
            data = json.loads(r.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise Fail(f"cannot parse lsblk output: {exc}") from exc
        nodes: list[BlockNode] = []

        def walk(items):
            for it in items or []:
                nodes.append(BlockNode(path=it.get("name") or "", type=it.get("type") or "",
                                       fstype=it.get("fstype") or "", uuid=it.get("uuid") or "",
                                       label=it.get("label") or ""))
                walk(it.get("children"))

        walk(data.get("blockdevices"))
        return nodes

    # ------------------------------------------------------------------ steps
    def execute(self) -> tuple[list[str], list[str]]:
        self.note(f"scanning {self.real} for the guest root file system")
        self.sh(["partx", "-u", self.real], ok=False)
        self.sh(["udevadm", "settle"], timeout_s=30, ok=False)
        nodes = self.lsblk()

        if any(n.fstype == "LVM2_member" for n in nodes):
            self.activate_lvm()
            nodes = self.lsblk()

        root_node = self.find_root(nodes)
        self.note(f"guest root file system on {root_node.path} ({root_node.fstype})")
        self.mount_boot(nodes)

        dracut = next((p for p in ("usr/bin/dracut", "usr/sbin/dracut", "sbin/dracut") if (self.mnt / p).exists()),
                      None)
        if dracut is None:
            raise Skip("guest has no dracut (Ubuntu/Debian initramfs-tools images already include virtio drivers)")

        kernels = self.installed_kernels()
        if not kernels:
            raise Skip("no installed kernels found under /lib/modules with an initramfs in /boot")

        self.bind("/dev", self.mnt / "dev")
        self.bind("/proc", self.mnt / "proc")
        self.bind("/sys", self.mnt / "sys")

        todo = [(ver, img) for ver, img in kernels if not self.has_virtio(img)]
        for ver, img in kernels:
            self.note(f"kernel {ver}: {img.name} {'lacks' if (ver, img) in todo else 'already has'} virtio drivers")
        if not todo:
            return [v for v, _ in kernels], []

        conf_dir = self.mnt / "etc" / "dracut.conf.d"
        conf_dir.mkdir(parents=True, exist_ok=True)
        (conf_dir / DRACUT_CONF_NAME).write_text(
            "# added by the VMware -> OCI migration helper: virtio drivers for OCI paravirtualized devices\n"
            f'add_drivers+=" {VIRTIO_DRIVERS} "\n')

        # --no-hostonly: the image must not be tailored to the helper's hardware/mounts either (dracut 004 on
        # RHEL 6 does not know the flag and is not hostonly anyway)
        helptext = self.sh(["chroot", str(self.mnt), "dracut", "--help"], ok=False)
        extra = ["--no-hostonly"] if "--no-hostonly" in (helptext.stdout + helptext.stderr) else []
        rebuilt = []
        for ver, img in todo:
            self.check_boot_space(img)
            self.note(f"rebuilding {img.name} for kernel {ver} (dracut, this takes a minute)")
            rel = "/" + str(img.relative_to(self.mnt)).replace(os.sep, "/")
            r = self.sh(["chroot", str(self.mnt), "dracut", "--force", *extra,
                         "--add-drivers", VIRTIO_DRIVERS, rel, ver], timeout_s=DRACUT_TIMEOUT_S, ok=False)
            if r.returncode != 0:
                raise Fail(f"dracut failed for kernel {ver} (rc {r.returncode}): {_tail(r.stderr or r.stdout)}")
            if not self.has_virtio(img):
                raise Fail(f"dracut finished for kernel {ver} but {img.name} still shows no virtio modules")
            rebuilt.append(ver)
        self.sh(["sync"], ok=False)
        return [v for v, _ in kernels], rebuilt

    def activate_lvm(self) -> None:
        r = self.sh(["pvs", "--config", self.lvm_config, "--noheadings", "-o", "pv_name,vg_name"], ok=False)
        if r.returncode != 0:
            raise Fail(f"pvs failed: {_tail(r.stderr)}")
        guest_vgs = sorted({ln.split()[1] for ln in r.stdout.splitlines() if len(ln.split()) >= 2})
        if not guest_vgs:
            return
        # volume groups of the helper itself (and of anything else attached) = VGs with a PV that is not on
        # our disk; a guest VG with the same name cannot be activated alongside it
        active = self.sh(["pvs", "--noheadings", "-o", "pv_name,vg_name"], ok=False)
        foreign = {ln.split()[1] for ln in (active.stdout if active.returncode == 0 else "").splitlines()
                   if len(ln.split()) >= 2 and not ln.split()[0].startswith(self.real)}
        clash = [vg for vg in guest_vgs if vg in foreign]
        if clash:
            raise Skip(f"guest volume group '{clash[0]}' has the same name as a volume group on the helper; "
                       "it cannot be activated here - rebuild the initramfs inside the guest instead")
        for vg in guest_vgs:
            self.sh(["vgchange", "--config", self.lvm_config, "-ay", vg], timeout_s=120)
            self.vgs.append(vg)
        self.sh(["udevadm", "settle"], timeout_s=30, ok=False)
        self.note(f"activated guest volume group(s) {', '.join(guest_vgs)}")

    def find_root(self, nodes: list[BlockNode]) -> BlockNode:
        candidates = [n for n in nodes if n.fstype in ROOT_FS_TYPES and n.type in ("part", "lvm", "disk")]
        # LVs first: when both exist, the plain partition is usually /boot
        candidates.sort(key=lambda n: 0 if n.type == "lvm" else 1)
        for n in candidates:
            opts = ["ro"] + (["nouuid"] if n.fstype == "xfs" else [])
            try:
                self.mount(n.path, self.mnt, opts)
            except Fail as exc:
                self.note(f"cannot mount {n.path}: {exc}")
                continue
            if (self.mnt / "etc" / "fstab").exists() and (
                    (self.mnt / "lib" / "modules").is_dir() or (self.mnt / "usr" / "lib" / "modules").is_dir()):
                self.sh(["mount", "-o", "remount,rw", str(self.mnt)])
                return n
            self.umount(self.mnt)
        if any(n.fstype == "crypto_LUKS" for n in nodes):
            raise Skip("the guest root file system is LUKS encrypted; rebuild the initramfs inside the guest")
        if any(n.fstype == "btrfs" for n in nodes):
            raise Skip("the guest uses btrfs, which the fix-up does not handle")
        raise Skip("no Linux root file system (with /etc/fstab and /lib/modules) found on the boot disk")

    def mount_boot(self, nodes: list[BlockNode]) -> None:
        """Mount a separate /boot from the guest's fstab; nothing to do when /boot lives on the root fs."""
        fstab = (self.mnt / "etc" / "fstab").read_text(errors="replace")
        for line in fstab.splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0].startswith("#") or parts[1] != "/boot":
                continue
            spec, fstype = parts[0], parts[2] if len(parts) > 2 else ""
            dev = self.resolve_spec(spec, nodes)
            if dev is None:
                raise Skip(f"/boot ({spec} in fstab) not found on the boot disk")
            opts = ["nouuid"] if fstype == "xfs" or any(n.path == dev and n.fstype == "xfs" for n in nodes) else []
            self.mount(dev, self.mnt / "boot", opts)
            self.note(f"/boot on {dev}")
            return

    @staticmethod
    def resolve_spec(spec: str, nodes: list[BlockNode]) -> Optional[str]:
        """Map an fstab device spec of the *guest* onto a node of *our* disk.  Only nodes from lsblk are
        returned: a guest ``/dev/sda1`` must never resolve to the helper's own /dev/sda1."""
        if spec.startswith("UUID="):
            return next((n.path for n in nodes if n.uuid == spec[5:]), None)
        if spec.startswith("LABEL="):
            return next((n.path for n in nodes if n.label == spec[6:]), None)
        m = re.match(r"^/dev/(?:mapper/([^/]+)|(?!mapper)([^/]+)/([^/]+))$", spec)
        if m:  # /dev/mapper/vg-lv or /dev/vg/lv -> the LV among the lvm nodes (dm escapes '-' as '--')
            dm = m.group(1) if m.group(1) else f"{m.group(2).replace('-', '--')}-{m.group(3).replace('-', '--')}"
            return next((n.path for n in nodes if n.type == "lvm" and Path(n.path).name == dm), None)
        m = re.match(r"^/dev/(?:sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|nvme\d+n\d+p)(\d+)$", spec)
        if m:  # /dev/sda2 in the guest -> partition 2 of our disk
            number = m.group(1)
            return next((n.path for n in nodes if n.type == "part" and re.search(r"(\d+)$", n.path)
                         and re.search(r"(\d+)$", n.path).group(1) == number), None)
        return None

    def installed_kernels(self) -> list[tuple[str, Path]]:
        mod_dir = next((self.mnt / p for p in ("lib/modules", "usr/lib/modules") if (self.mnt / p).is_dir()), None)
        result = []
        for d in sorted(mod_dir.iterdir()) if mod_dir else []:
            if not (d / "modules.dep").exists():
                continue  # leftovers of removed kernels
            ver = d.name
            for name in (f"initramfs-{ver}.img", f"initrd-{ver}", f"initrd.img-{ver}"):
                img = self.mnt / "boot" / name
                if img.exists():
                    result.append((ver, img))
                    break
        return result

    def has_virtio(self, img: Path) -> bool:
        rel = "/" + str(img.relative_to(self.mnt)).replace(os.sep, "/")
        r = self.sh(["chroot", str(self.mnt), "lsinitrd", rel], timeout_s=120, ok=False)
        if r.returncode != 0:
            return False  # no lsinitrd, or an image it cannot read: rebuild to be safe
        return "virtio" in r.stdout

    def check_boot_space(self, img: Path) -> None:
        try:
            free = shutil.disk_usage(img.parent).free
        except OSError:
            return
        if free < MIN_FREE_BOOT_BYTES:
            raise Fail(f"only {free // (1024 * 1024)} MB free in the guest's /boot; a non-hostonly initramfs "
                       "needs about 150 MB - free space in /boot inside the guest and migrate again")

    # ---------------------------------------------------------------- cleanup
    def cleanup(self) -> None:
        for where in reversed(list(self.mounts)):
            try:
                self.umount(where)
            except Fail as exc:
                log.warning("cannot unmount %s: %s", where, exc)
        for vg in reversed(self.vgs):
            try:
                self.sh(["vgchange", "--config", self.lvm_config, "-an", vg], ok=False)
            except Fail as exc:
                log.warning("cannot deactivate %s: %s", vg, exc)
        try:
            if self.mnt.exists():
                shutil.rmtree(self.mnt, ignore_errors=True)
        except OSError:
            pass
