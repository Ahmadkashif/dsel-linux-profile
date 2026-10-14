"""Thermal state per cell, from the file alone (added 2026-10-14, S16(a) on Linux)."""

from __future__ import annotations

from pathlib import Path

from dsel.live.sampler.host import find_package_sensor, read_sample
from dsel.live.schema import HostRecord, LatencyWindowRecord, PhaseRecord
from dsel.phenomena.thermal import drift, per_cell


def _host(t: int, temp: int | None, freq: int) -> HostRecord:
    return HostRecord(t_ms=t, w="host-0", seq=t, package_temp_mc=temp, cpu_freq_khz={"4": freq})


def test_each_cell_gets_only_the_host_samples_inside_its_measure_window() -> None:
    records: list[object] = [
        PhaseRecord(t_ms=1000, w="d", seq=0, cell="a", phase="measure", event="begin"),
        PhaseRecord(t_ms=3000, w="d", seq=1, cell="a", phase="measure", event="end"),
        PhaseRecord(t_ms=5000, w="d", seq=2, cell="b", phase="measure", event="begin"),
        PhaseRecord(t_ms=7000, w="d", seq=3, cell="b", phase="measure", event="end"),
        _host(500, 90_000, 800_000),  # before any cell: ignored
        _host(2000, 50_000, 4_700_000),
        _host(2500, 52_000, 4_700_000),
        _host(6000, 60_000, 4_000_000),
    ]
    cells = per_cell(records)
    assert [c.cell for c in cells] == ["a", "b"]
    assert cells[0].samples == 2 and cells[0].package_temp_c_median == 51.0
    assert cells[1].package_temp_c_max == 60.0 and cells[1].freq_mhz_median == 4000.0
    assert drift(cells) == 9.0


def test_latency_windows_bound_a_cell_without_phase_records() -> None:
    records: list[object] = [
        LatencyWindowRecord(t_ms=t, w="d", seq=t, cell="c", window_ms=1000, op="r",
                            count=1, rate_per_s=1.0)
        for t in (1000, 2000)
    ] + [_host(1500, 40_000, 1)]  # fmt: skip
    assert per_cell(records)[0].samples == 1


def test_no_temperature_reads_as_none_not_zero() -> None:
    cells = per_cell(
        [
            PhaseRecord(t_ms=0, w="d", seq=0, cell="a", phase="measure", event="begin"),
            PhaseRecord(t_ms=9, w="d", seq=1, cell="a", phase="measure", event="end"),
            _host(5, None, 1000),
        ]
    )
    assert cells[0].package_temp_c_median is None
    assert drift(cells) is None


def test_sampler_reads_a_sys_tree_and_writes_nothing(tmp_path: Path) -> None:
    thermal = tmp_path / "thermal"
    (thermal / "thermal_zone0").mkdir(parents=True)
    (thermal / "thermal_zone0" / "type").write_text("acpitz\n")
    (thermal / "thermal_zone0" / "temp").write_text("27800\n")
    (thermal / "thermal_zone2").mkdir()
    (thermal / "thermal_zone2" / "type").write_text("x86_pkg_temp\n")
    (thermal / "thermal_zone2" / "temp").write_text("45000\n")
    cpu = tmp_path / "cpu" / "cpu4" / "cpufreq"
    cpu.mkdir(parents=True)
    (cpu / "scaling_cur_freq").write_text("4700000\n")
    (cpu / "scaling_governor").write_text("powersave\n")
    before = sorted((p, p.read_bytes()) for p in tmp_path.rglob("*") if p.is_file())

    sensor = find_package_sensor(thermal)
    assert sensor == thermal / "thermal_zone2" / "temp"
    record = read_sample([4, 5], sensor, cpu_root=tmp_path / "cpu")
    assert record.package_temp_mc == 45000
    assert record.cpu_freq_khz == {"4": 4700000}
    assert record.governor == "powersave"
    after = sorted((p, p.read_bytes()) for p in tmp_path.rglob("*") if p.is_file())
    assert before == after
