"""Discovering block devices on the helper.

Block volumes are attached with a consistent device path (``/dev/oracleoci/oraclevdX``), but OCI does
not allow a device path when a *boot* volume is attached as a data volume ("Device paths are not
available when you attach a boot volume as a data volume to a second instance").  For those the helper
takes a snapshot of the whole disks it can see, attaches, and waits for exactly one new disk of the
expected size to appear.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

SYS_BLOCK = Path("/sys/block")
_DISK_PREFIXES = ("sd", "vd", "xvd", "nvme")

DeviceScanner = Callable[[], dict[str, int]]  # device path -> size in bytes


def scan_block_devices(sys_block: Path = SYS_BLOCK) -> dict[str, int]:
    """Whole disks (no partitions, no loop/ram/dm devices) with their size in bytes."""
    devices: dict[str, int] = {}
    if not sys_block.is_dir():
        return devices
    for entry in sys_block.iterdir():
        name = entry.name
        if not name.startswith(_DISK_PREFIXES) or (entry / "partition").exists():
            continue
        try:
            sectors = int((entry / "size").read_text().strip())
        except (OSError, ValueError):
            continue
        if sectors > 0:
            devices[f"/dev/{name}"] = sectors * 512
    return devices


def wait_for_new_device(
    before: dict[str, int],
    expected_bytes: int,
    timeout_s: float,
    scan: DeviceScanner = scan_block_devices,
    poll_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Return the path of the disk that appeared since ``before`` and has ``expected_bytes`` capacity.

    Waits a little after the first sighting so a disk whose size is still being reported as 0 (udev
    settling) is not misjudged.  Raises ``RuntimeError`` when nothing suitable shows up in time.
    """
    deadline = time.monotonic() + timeout_s
    last: dict[str, int] = {}
    while True:
        now = scan()
        new = {path: size for path, size in now.items() if path not in before}
        matching = [path for path, size in new.items() if size == expected_bytes]
        if len(matching) == 1:
            return matching[0]
        if len(matching) > 1:
            raise RuntimeError(
                f"{len(matching)} new disks of {expected_bytes} bytes appeared at once ({sorted(matching)}); "
                "cannot tell which one is the attached boot volume"
            )
        last = new
        if time.monotonic() >= deadline:
            break
        sleep(poll_s)
    seen = ", ".join(f"{p} ({s} bytes)" for p, s in sorted(last.items())) or "none"
    raise RuntimeError(
        f"no new disk of {expected_bytes} bytes appeared on the helper within {timeout_s:.0f}s (new disks seen: {seen})"
    )
