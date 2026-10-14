"""Host thermal and clock sampler (added 2026-10-14, for S16(a) on Linux).

Reads `/sys/class/thermal/*/temp` for the package sensor and
`/sys/devices/system/cpu/cpuN/cpufreq/scaling_cur_freq` for the CPUs a run
uses, on a fixed cadence, and writes one `host` record per tick. Every read is
of a file the kernel exposes for reading; nothing is written. Writes records;
derives nothing -- whether a rung ran hot is `phenomena/thermal.py`'s question.

On macOS none of these files exist and every tick records `None`s, which is the
honest answer: the Mac's thermal state is not visible from where this runs.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from pathlib import Path

from dsel.live.ndjson import ShardWriter, now_ms
from dsel.live.schema import HostRecord

DEFAULT_INTERVAL_S = 1.0
PACKAGE_SENSORS = ("x86_pkg_temp", "coretemp", "k10temp", "cpu_thermal")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def find_package_sensor(root: Path = Path("/sys/class/thermal")) -> Path | None:
    """The thermal zone whose type names the CPU package, if there is one."""
    zones = sorted(root.glob("thermal_zone*")) if root.is_dir() else []
    for wanted in PACKAGE_SENSORS:
        for zone in zones:
            if _read(zone / "type") == wanted:
                return zone / "temp"
    return None


def read_sample(
    cpus: Sequence[int],
    sensor: Path | None,
    cpu_root: Path = Path("/sys/devices/system/cpu"),
    cell: str | None = None,
) -> HostRecord:
    temp_raw = _read(sensor) if sensor is not None else None
    freqs: dict[str, int] = {}
    for cpu in cpus:
        raw = _read(cpu_root / f"cpu{cpu}" / "cpufreq" / "scaling_cur_freq")
        if raw is not None and raw.isdigit():
            freqs[str(cpu)] = int(raw)
    governor = (
        _read(cpu_root / f"cpu{cpus[0]}" / "cpufreq" / "scaling_governor") if cpus else None
    )
    return HostRecord(
        t_ms=now_ms(),
        w="",
        seq=0,
        cell=cell,
        package_temp_mc=int(temp_raw) if temp_raw and temp_raw.lstrip("-").isdigit() else None,
        cpu_freq_khz=freqs,
        governor=governor,
    )


class HostSampler:
    """Polls the host's thermal state on a fixed cadence, into its own shard."""

    def __init__(
        self,
        writer: ShardWriter,
        cpus: Sequence[int],
        interval_s: float = DEFAULT_INTERVAL_S,
        cell: str | None = None,
    ) -> None:
        self.writer = writer
        self.cpus = list(cpus)
        self.interval_s = interval_s
        self.cell = cell
        self.sensor = find_package_sensor()
        self.ticks = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sample_once(self) -> None:
        self.writer.write(read_sample(self.cpus, self.sensor, cell=self.cell))
        self.ticks += 1

    def _loop(self) -> None:
        next_at = time.monotonic()
        while not self._stop.is_set():
            self.sample_once()
            next_at += self.interval_s
            self._stop.wait(max(0.0, next_at - time.monotonic()))

    def start(self) -> HostSampler:
        self._thread = threading.Thread(target=self._loop, name="host-sampler", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
