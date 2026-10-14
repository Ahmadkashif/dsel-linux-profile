"""Was a rung run hot? Thermal state per cell, from `metrics.ndjson` alone.

Added 2026-10-14 for S16(a) on Linux. The Mac's apparent connection cliff was
the laptop warming up -- rung order was a proxy for elapsed time, and elapsed
time for temperature. Rung order is randomised now; this measures the confound
instead of assuming it away. For each cell it takes the `host` records whose
timestamps fall inside that cell's measure window and reports the median
package temperature and the median clock of the CPUs sampled.

Reads records only; never touches Docker, the engine or `/sys`.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import dataclass

from dsel.live.schema import HostRecord, LatencyWindowRecord, PhaseRecord


@dataclass(frozen=True, slots=True)
class CellThermal:
    cell: str
    samples: int
    package_temp_c_median: float | None
    package_temp_c_max: float | None
    freq_mhz_median: float | None


def _windows(records: list[object]) -> dict[str, tuple[int, int]]:
    """Each cell's measure window: phase begin/end if present, else its latency windows."""
    begins: dict[str, int] = {}
    ends: dict[str, int] = {}
    spans: dict[str, list[int]] = {}
    for r in records:
        if isinstance(r, PhaseRecord) and r.cell and r.phase == "measure":
            if r.event == "begin":
                begins[r.cell] = min(begins.get(r.cell, r.t_ms), r.t_ms)
            else:
                ends[r.cell] = max(ends.get(r.cell, r.t_ms), r.t_ms)
        elif isinstance(r, LatencyWindowRecord) and r.cell:
            spans.setdefault(r.cell, []).append(r.t_ms)
    out: dict[str, tuple[int, int]] = {}
    for cell in set(begins) | set(spans):
        lo = begins.get(cell, min(spans.get(cell, [0])))
        hi = ends.get(cell, max(spans.get(cell, [lo])))
        out[cell] = (lo, hi)
    return out


def per_cell(records: Iterable[object]) -> list[CellThermal]:
    """Thermal state inside each cell's measure window, ordered by window start."""
    items = list(records)
    hosts = [r for r in items if isinstance(r, HostRecord)]
    result: list[CellThermal] = []
    for cell, (lo, hi) in sorted(_windows(items).items(), key=lambda kv: kv[1][0]):
        inside = [h for h in hosts if lo <= h.t_ms <= hi]
        temps = [h.package_temp_mc / 1000.0 for h in inside if h.package_temp_mc is not None]
        freqs = [f / 1000.0 for h in inside for f in h.cpu_freq_khz.values()]
        result.append(
            CellThermal(
                cell=cell,
                samples=len(inside),
                package_temp_c_median=statistics.median(temps) if temps else None,
                package_temp_c_max=max(temps) if temps else None,
                freq_mhz_median=statistics.median(freqs) if freqs else None,
            )
        )
    return result


def drift(cells: list[CellThermal]) -> float | None:
    """Spread of median package temperature across cells, in degrees C."""
    temps = [c.package_temp_c_median for c in cells if c.package_temp_c_median is not None]
    return max(temps) - min(temps) if len(temps) >= 2 else None
