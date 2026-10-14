#!/usr/bin/env python3
"""Re-derive a load curve's landmarks from `metrics.ndjson` alone (PLAN.md S15).

    python scripts/rederive_landmarks.py runs/<run-id>/metrics.ndjson

Prints the curve and its knee, collapse and max sustainable rate as JSON.

This is the script S15's acceptance criterion asks for. It takes a file and
nothing else: no run directory, no Docker daemon, no engine, no state left over
from the run that produced it. If it cannot recover the same landmarks the run
reported, then those landmarks were not in the evidence -- they were in the
process's memory, and nobody outside that process could ever check them.

It deliberately imports the *rules* rather than restating them. A second copy
of "what a knee is" would drift from the first, and agreement between two
drifting definitions proves nothing. What is independent here is the data path,
which is the thing that has to be.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from dsel.live.merge import find_shards, merge_records, read_merged  # noqa: E402
from dsel.phenomena.conn_cliff import (  # noqa: E402
    connection_curve_from_records,
    curve_from_records,
)
from dsel.phenomena.thermal import drift, per_cell  # noqa: E402


def load(path: Path) -> list:
    """Records from a merged metrics file, or from a run's shard directory."""
    if path.is_dir():
        shards = find_shards(path / "shards" if (path / "shards").is_dir() else path)
        if not shards:
            raise SystemExit(f"no shards under {path}")
        return list(merge_records(shards))
    return list(read_merged(path))


def _fmt(value: float | None, digits: int) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("metrics", type=Path, help="metrics.ndjson, or a run directory")
    parser.add_argument("--table", action="store_true", help="also print the curve")
    parser.add_argument(
        "--axis",
        choices=("rate", "connections"),
        default="rate",
        help="rate ramp (S11/S15) or connection ramp (S16a), repeats pooled by median",
    )
    parser.add_argument(
        "--thermal",
        action="store_true",
        help="also print package temperature and clock per cell (from `host` records)",
    )
    args = parser.parse_args(argv)

    records = load(args.metrics)
    curve = (
        connection_curve_from_records(records)
        if args.axis == "connections"
        else curve_from_records(records)
    )
    if not curve.points:
        raise SystemExit(f"{args.metrics} holds no latency_window records with a cell id")

    if args.table:
        x = "conns" if args.axis == "connections" else "offered"
        print(f"{x:>9} {'achieved':>9} {'p99 us':>10} {'errors':>8}  verdict")
        for point in curve.points:
            print(
                f"{point.x:>9.0f} {point.achieved_rate_per_s:>9.0f} "
                f"{point.p99_us:>10.0f} {point.errors:>8}  {point.verdict}"
            )
    payload: dict[str, object] = {
        "source": str(args.metrics),
        "steps": len(curve.points),
        **curve.landmarks(),
    }
    if args.thermal:
        cells = per_cell(records)
        print(f"{'cell':<44} {'samples':>7} {'pkg C med':>9} {'pkg C max':>9} {'MHz med':>8}")
        for c in cells:
            print(
                f"{c.cell:<44} {c.samples:>7} {_fmt(c.package_temp_c_median, 1):>9} "
                f"{_fmt(c.package_temp_c_max, 1):>9} {_fmt(c.freq_mhz_median, 0):>8}"
            )
        payload["thermal_drift_c"] = drift(cells)
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
