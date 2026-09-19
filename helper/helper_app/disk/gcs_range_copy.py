"""Copy a GCS object (raw disk export) onto a PositionalWriter with parallel range GETs."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

from helper_app.disk.writer import PositionalWriter
from helper_app.gcp.client import GcpClient, GcpError

log = logging.getLogger(__name__)

DEFAULT_CHUNK_BYTES = 8 * 1024 * 1024
DEFAULT_RETRIES = 3


class GcsCopyError(RuntimeError):
    pass


@dataclass
class GcsCopyStats:
    bytes_received: int = 0
    bytes_written: int = 0
    chunks_written: int = 0
    retries: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, n: int) -> None:
        with self._lock:
            self.bytes_received += n
            self.bytes_written += n
            self.chunks_written += 1


def object_size(client: GcpClient, bucket: str, object_name: str) -> int:
    meta = client.object_head(bucket, object_name)
    size = int(meta.get("size") or 0)
    if size <= 0:
        raise GcsCopyError(f"gs://{bucket}/{object_name} has no size")
    return size


def copy_sequential(
    client: GcpClient,
    bucket: str,
    object_name: str,
    writer: PositionalWriter,
    *,
    total_bytes: int,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    workers: int = 4,
    check_cancel: Optional[Callable[[], None]] = None,
    on_progress: Optional[Callable[[int], None]] = None,
) -> GcsCopyStats:
    stats = GcsCopyStats()
    ranges: list[tuple[int, int]] = []
    pos = 0
    while pos < total_bytes:
        end = min(total_bytes - 1, pos + chunk_bytes - 1)
        ranges.append((pos, end))
        pos = end + 1

    def one_range(start: int, end: int) -> None:
        if check_cancel:
            check_cancel()
        last: Optional[Exception] = None
        for attempt in range(1, DEFAULT_RETRIES + 1):
            try:
                data = client.object_get_range(bucket, object_name, start, end)
                writer.write_at(start, data)
                stats.add(len(data))
                if on_progress:
                    on_progress(len(data))
                return
            except (GcpError, OSError) as exc:
                last = exc
                with stats._lock:
                    stats.retries += 1
        raise GcsCopyError(f"range {start}-{end}: {last}") from last

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = [pool.submit(one_range, s, e) for s, e in ranges]
        for f in as_completed(futs):
            f.result()
    return stats
