"""Host profiles and read-only detection (added 2026-10-14).

Every rule is checked against captured text rather than the machine running the
tests: the reference host's `lscpu -e` is below verbatim, and the other
topologies are the ones a profile written for it must not silently mis-map.
"""

from __future__ import annotations

import pytest

from dsel.compose import budget as budget_module
from dsel.hostprofile import (
    GIB,
    LINUX_SHARED,
    LOCAL,
    HostProfile,
    ProfileError,
    cpuset_spec,
    linux_shared_profile,
    load_linux_file,
    local_profile,
)
from dsel.hostprofile.detect import (
    HostFacts,
    choose,
    docker_major,
    parse_bracketed,
    parse_lscpu_e,
    parse_meminfo_kib,
)

# `lscpu -e` on the reference host, 2026-10-14 (8 cores, SMT siblings at N+8).
REFERENCE_LSCPU = """\
CPU NODE SOCKET CORE L1d:L1i:L2:L3 ONLINE    MAXMHZ   MINMHZ      MHZ
  0    0      0    0 0:0:0:0          yes 5100.0000 800.0000 800.1350
  1    0      0    1 1:1:1:0          yes 5100.0000 800.0000 800.0060
  2    0      0    2 2:2:2:0          yes 5100.0000 800.0000 800.3750
  3    0      0    3 3:3:3:0          yes 5100.0000 800.0000 800.0000
  4    0      0    4 4:4:4:0          yes 5100.0000 800.0000 800.0330
  5    0      0    5 5:5:5:0          yes 5100.0000 800.0000 800.0000
  6    0      0    6 6:6:6:0          yes 5100.0000 800.0000 800.0120
  7    0      0    7 7:7:7:0          yes 5100.0000 800.0000 800.0000
  8    0      0    0 0:0:0:0          yes 5100.0000 800.0000 800.0000
  9    0      0    1 1:1:1:0          yes 5100.0000 800.0000 800.0000
 10    0      0    2 2:2:2:0          yes 5100.0000 800.0000 800.0000
 11    0      0    3 3:3:3:0          yes 5100.0000 800.0000 800.0000
 12    0      0    4 4:4:4:0          yes 5100.0000 800.0000 800.0240
 13    0      0    5 5:5:5:0          yes 5100.0000 800.0000 800.0420
 14    0      0    6 6:6:6:0          yes 5100.0000 800.0000 800.3130
 15    0      0    7 7:7:7:0          yes 5100.0000 800.0000 800.1000
"""

# The same CPU with siblings numbered adjacently, as some firmware does.
ADJACENT_LSCPU = "CPU CORE SOCKET ONLINE\n" + "".join(
    f"{cpu} {cpu // 2} 0 yes\n" for cpu in range(16)
)

REFERENCE = parse_lscpu_e(REFERENCE_LSCPU)


def facts(**overrides: object) -> HostFacts:
    base: dict[str, object] = {
        "os_name": "Linux",
        "kernel": "7.0.0-34-generic",
        "topology": REFERENCE,
        "nproc": 16,
        "memtotal_kib": 32567244,
        "docker_version": "29.8.1",
        "cgroup_version": "2",
        "cgroup_fs": "cgroup2fs",
    }
    base.update(overrides)
    return HostFacts(**base)  # type: ignore[arg-type]


# --- parsing ---------------------------------------------------------------------


def test_lscpu_groups_smt_siblings_into_physical_cores() -> None:
    assert len(REFERENCE) == 8
    assert REFERENCE[0] == (0, 8)
    assert REFERENCE[4] == (4, 12)
    assert REFERENCE[7] == (7, 15)


def test_lscpu_with_adjacent_siblings() -> None:
    adjacent = parse_lscpu_e(ADJACENT_LSCPU)
    assert adjacent[4] == (8, 9)


def test_offline_cpus_are_left_out() -> None:
    text = "CPU CORE SOCKET ONLINE\n0 0 0 yes\n1 1 0 no\n2 0 0 yes\n"
    assert parse_lscpu_e(text) == {0: (0, 2)}


def test_two_sockets_counting_cores_from_zero_are_not_merged() -> None:
    text = "CPU CORE SOCKET ONLINE\n0 0 0 yes\n1 0 1 yes\n"
    assert parse_lscpu_e(text) == {0: (0,), 1: (1,)}


def test_small_parsers() -> None:
    assert parse_meminfo_kib("MemTotal:       32567244 kB\nMemFree: 1 kB\n") == 32567244
    assert parse_meminfo_kib("nothing") is None
    assert docker_major("29.8.1") == 29
    assert docker_major("") is None
    assert parse_bracketed("always [madvise] never") == "madvise"


def test_cpuset_spec_renders_like_docker() -> None:
    assert cpuset_spec((4, 5, 12, 13)) == "4-5,12-13"
    assert cpuset_spec((6, 14)) == "6,14"
    assert cpuset_spec((2, 3, 4, 5)) == "2-5"


# --- local is unchanged --------------------------------------------------------------


def test_local_profile_is_plan_md_slice_table() -> None:
    """The constants that used to live in five modules, now in one place."""
    p = local_profile()
    assert p.name == LOCAL and p.reportable is False
    assert p.engine.cpuset == (2, 3, 4, 5) and p.engine.cpus == 4.0
    assert p.engine.memory_bytes == 3 * GIB
    assert p.driver.cpuset == (6, 7, 8, 9) and p.driver.memory_bytes == 1 * GIB
    assert p.app.cpuset == p.observability.cpuset == (0, 1)
    assert p.interference_pair == ("2-5", "6-9")
    assert (p.ports.prometheus, p.ports.grafana, p.ports.exporter) == (9090, 3000, 9464)
    # And the historical names still mean what they meant.
    assert p.engine.cpuset == budget_module.ENGINE_CPUSET
    assert p.driver.cpuset == budget_module.DRIVER_CPUSET
    assert p.reserved_cpuset == budget_module.HOST_CPUSET


def test_local_budget_plan_is_identical_through_the_profile_path() -> None:
    vm = budget_module.Budget(total_vcpus=10, total_memory_bytes=8319504384)
    for app_tier in (True, False):
        for obs in budget_module.Observability:
            old = budget_module.plan_local(vm, with_app_tier=app_tier, observability=obs)
            new = budget_module.plan_for(
                local_profile(),
                budget_module.Budget.for_profile(
                    local_profile(), ncpu=10, mem_total_bytes=8319504384
                ),
                with_app_tier=app_tier,
                observability=obs,
            )
            assert old.components == new.components
            assert old.budget == new.budget
            assert old.arithmetic() == new.arithmetic()


# --- linux-shared-8c16t ----------------------------------------------------------------


@pytest.fixture
def linux() -> HostProfile:
    return linux_shared_profile(REFERENCE, exporter_bind="172.17.0.1")


def test_linux_tiers_are_whole_physical_cores_from_the_topology(linux: HostProfile) -> None:
    assert linux.engine.cpuset == (4, 5, 12, 13)
    assert linux.driver.cpuset == (6, 14)
    assert linux.app.cpuset == linux.observability.cpuset == (7, 15)
    assert linux.reserved_cpuset == (0, 1, 2, 3, 8, 9, 10, 11)
    assert linux.dsel_cpuset == (4, 5, 6, 7, 12, 13, 14, 15)


def test_linux_tiers_follow_a_different_numbering_rather_than_assuming_one() -> None:
    p = linux_shared_profile(parse_lscpu_e(ADJACENT_LSCPU))
    assert p.engine.cpuset == (8, 9, 10, 11)
    assert p.driver.cpuset == (12, 13)
    assert p.reserved_cpuset == tuple(range(8))


def test_no_tier_touches_the_reserved_cores(linux: HostProfile) -> None:
    for tier in linux.tiers:
        assert not set(tier.cpuset) & set(linux.reserved_cpuset), tier.name


def test_the_allowance_is_four_cores_and_twelve_gib(linux: HostProfile) -> None:
    assert linux.allowance_logical_cpus == 8
    assert linux.allowance_memory_bytes == 12 * GIB
    claimed = sum(t.memory_bytes for t in linux.tiers)
    assert claimed == int(10.5 * GIB) <= linux.allowance_memory_bytes
    assert linux.engine.memory_bytes == 6 * GIB
    assert linux.driver.memory_bytes == 2 * GIB


def test_quota_matches_cpuset_width(linux: HostProfile) -> None:
    assert linux.engine.cpus == 4.0
    assert linux.driver.cpus == 2.0
    assert linux.app.cpus + linux.observability.cpus == 2.0


def test_ports_are_loopback_and_clear_of_the_defaults(linux: HostProfile) -> None:
    assert linux.ports.bind == "127.0.0.1"
    assert (linux.ports.prometheus, linux.ports.grafana, linux.ports.exporter) == (
        19090,
        13000,
        19464,
    )
    assert linux.publish(5555, 5432) == "127.0.0.1:5555:5432"
    assert linux.ports.exporter_bind == "172.17.0.1"


def test_linux_is_not_reportable_by_default(linux: HostProfile) -> None:
    assert linux.reportable is False
    assert "shared_host_cotenants" in linux.deviation_reasons


def test_every_tier_flag_set_has_cpuset_quota_memory_swap_and_pids(linux: HostProfile) -> None:
    flags = linux.driver.docker_flags()
    for flag in ("--cpuset-cpus", "--cpus", "--memory", "--memory-swap", "--pids-limit"):
        assert flag in flags
    assert flags[flags.index("--memory") + 1] == flags[flags.index("--memory-swap") + 1]


def test_narrowing_never_widens(linux: HostProfile) -> None:
    flags = linux.driver.docker_flags(cpus=99, memory_bytes=99 * GIB)
    assert flags[flags.index("--cpus") + 1] == "2"
    assert flags[flags.index("--memory") + 1] == str(2 * GIB)
    env = linux.envelope("engine", cpus=1.0, memory_bytes=64 * GIB)
    assert env.cpuset == (4, 5, 12, 13) and env.cpus == 1.0 and env.memory_bytes == 6 * GIB


def test_a_profile_that_splits_a_core_is_refused() -> None:
    data = load_linux_file()
    tiers = dict(data["tiers"])  # type: ignore[call-overload]
    tiers["driver"] = dict(tiers["driver"]) | {"cores": [5]}
    with pytest.raises(ProfileError, match="share logical CPUs"):
        linux_shared_profile(REFERENCE, data=dict(data) | {"tiers": tiers})


def test_a_profile_on_reserved_cores_is_refused() -> None:
    data = load_linux_file()
    tiers = dict(data["tiers"])  # type: ignore[call-overload]
    tiers["engine"] = dict(tiers["engine"]) | {"cores": [3, 4]}
    with pytest.raises(ProfileError, match="reserved"):
        linux_shared_profile(REFERENCE, data=dict(data) | {"tiers": tiers})


# --- detection ------------------------------------------------------------------------


def test_detect_picks_linux_on_the_reference_host() -> None:
    detection = choose(facts())
    assert detection.profile.name == LINUX_SHARED
    assert all(c.ok for c in detection.checks)
    table = detection.table()
    assert "4-5,12-13" in table and "never used by dsel" in table


def test_detect_picks_local_on_a_mac() -> None:
    detection = choose(HostFacts(os_name="Darwin", kernel="25.0.0"))
    assert detection.profile == local_profile()


@pytest.mark.parametrize(
    ("override", "row"),
    [
        (
            {"topology": parse_lscpu_e("CPU CORE SOCKET ONLINE\n0 0 0 yes\n1 1 0 yes\n")},
            "cores",
        ),
        ({"memtotal_kib": 16 * 1024 * 1024}, "MemTotal"),
        ({"cgroup_version": "1"}, "cgroup"),
        ({"docker_version": "27.5.1"}, "Docker"),
        ({"docker_version": None}, "Docker"),
        ({"nproc": 4}, "nproc"),
    ],
)
def test_detect_refuses_a_host_below_minimum_spec_and_names_the_row(
    override: dict[str, object], row: str
) -> None:
    with pytest.raises(ProfileError, match=row):
        choose(facts(**override))


def test_a_literal_32_gib_floor_would_refuse_a_32_gb_machine() -> None:
    """Recorded deliberately: the brief proposed >= 32 GiB, and the reference
    host -- a 32 GB machine -- reports 31.06 GiB of MemTotal."""
    reference = facts()
    assert reference.memtotal_gib is not None
    assert 31.0 < reference.memtotal_gib < 32.0
    req = load_linux_file()["requirements"]
    assert isinstance(req, dict) and req["min_memtotal_gib"] <= reference.memtotal_gib


def test_forced_unknown_profile_is_refused() -> None:
    with pytest.raises(ProfileError, match="unknown profile"):
        choose(facts(), forced="cloud-xl")
