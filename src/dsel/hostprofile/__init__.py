"""Host profiles: where each tier runs and how much it may take.

Until 2026-10-14 the Mac's slice table was written into the code as constants
in about thirty places (`compose/budget.py`, `app/stack.py`,
`driver/calibrate.py`, `audit/interference.py`, `audit/hlog_check.py`,
`observability/compose.yaml`, the soak script and the integration tests). PLAN.md
had said the envelope "stays a spec value rather than a constant, so larger
hardware would be a YAML change"; in practice it was a refactor, and this module
is that refactor.

A `HostProfile` names every number a tier needs: its cpuset, its CPU quota, its
memory, its pids limit, where its ports are published, and the allowance the
whole harness must fit inside. Two profiles exist:

* `local` -- the Apple M5 laptop under Docker Desktop. Its values are PLAN.md's
  locked slice table, unchanged; every `profile=local` unit test passes
  untouched against it.
* `linux-shared-8c16t` -- a bare-metal Linux host that dsel *shares* with other
  live work. Its tiers are whole physical cores read from `lscpu -e`, never
  assumed, and the harness is budgeted against its own allowance rather than
  the host's total. The file it is built from is `linux-shared-8c16t.toml`
  beside this module, and carries the minimum-spec table.

Choosing between them is `dsel.hostprofile.detect`, which only reads.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dsel.runtime.envelope import ResourceEnvelope

GIB = 1024**3

PROFILE_DIR = Path(__file__).resolve().parent
LINUX_SHARED = "linux-shared-8c16t"
LOCAL = "local"

# Every published port is bound here. Docker programs its own NAT rules ahead
# of the host firewall, so `--publish 5432:5432` on a LAN or tailnet machine is
# reachable from the LAN regardless of ufw. Loopback is the only safe default.
LOOPBACK = "127.0.0.1"


class ProfileError(RuntimeError):
    """A profile could not be built, or the host does not meet its minimum spec."""


def cpuset_spec(cpus: tuple[int, ...]) -> str:
    """Render a cpuset as Docker writes it back: "2-5", "4-5,12-13"."""
    runs: list[tuple[int, int]] = []
    for cpu in sorted(cpus):
        if runs and cpu == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], cpu)
        else:
            runs.append((cpu, cpu))
    return ",".join(f"{lo}-{hi}" if lo != hi else str(lo) for lo, hi in runs)


@dataclass(frozen=True, slots=True)
class Tier:
    """One tier's claim: cpuset, quota, memory, pids."""

    name: str
    cpuset: tuple[int, ...]
    cpus: float
    memory_bytes: int
    pids_limit: int

    @property
    def cpuset_spec(self) -> str:
        return cpuset_spec(self.cpuset)

    @property
    def memory_gib(self) -> float:
        return self.memory_bytes / GIB

    def docker_flags(
        self, *, cpus: float | None = None, memory_bytes: int | None = None
    ) -> list[str]:
        """Every knob, always set: cpuset *and* quota, memory with swap equal to it,
        and a pids limit. `cpus`/`memory_bytes` narrow the tier for one container;
        they never widen it."""
        quota = self.cpus if cpus is None else min(cpus, self.cpus)
        memory = (
            self.memory_bytes if memory_bytes is None else min(memory_bytes, self.memory_bytes)
        )
        return [
            "--cpuset-cpus",
            self.cpuset_spec,
            "--cpus",
            f"{quota:g}",
            "--memory",
            str(memory),
            "--memory-swap",
            str(memory),
            "--pids-limit",
            str(self.pids_limit),
        ]


@dataclass(frozen=True, slots=True)
class Requirements:
    """A profile's minimum spec. Checked by `detect`, never by assumption."""

    os: str
    min_physical_cores: int = 0
    min_memtotal_gib: float = 0.0
    cgroup_version: str | None = None
    min_docker_major: int = 0
    smt_required: bool = False
    notes: tuple[str, ...] = ()

    def table(self) -> list[tuple[str, str]]:
        rows = [("os", self.os)]
        if self.min_physical_cores:
            rows.append(("physical cores", f">= {self.min_physical_cores}"))
        if self.min_memtotal_gib:
            rows.append(("MemTotal", f">= {self.min_memtotal_gib:g} GiB"))
        if self.cgroup_version:
            rows.append(("cgroup", f"v{self.cgroup_version}"))
        if self.min_docker_major:
            rows.append(("Docker Engine", f">= {self.min_docker_major}"))
        rows.append(("SMT", "required" if self.smt_required else "optional"))
        return rows


@dataclass(frozen=True, slots=True)
class Ports:
    """Where the observability stack and the exporter answer, and on what address."""

    bind: str = LOOPBACK
    prometheus: int = 9090
    grafana: int = 3000
    exporter: int = 9464
    # Where `dsel watch --prometheus-port` listens. Prometheus scrapes it from
    # inside a container via `host.docker.internal:host-gateway`, so loopback
    # does not work on Linux; the docker bridge gateway address does, and is
    # not reachable from the LAN. `None` keeps prometheus_client's default.
    exporter_bind: str | None = None


@dataclass(frozen=True, slots=True)
class HostProfile:
    """Everything that used to be a constant."""

    name: str
    description: str
    reportable: bool
    engine: Tier
    driver: Tier
    app: Tier
    observability: Tier
    # Cores the harness never gets on a shared host; on `local`, the VM's host
    # slice (which the app tier and observability share by design).
    reserved_cpuset: tuple[int, ...]
    # vCPUs on which oversubscription is expected and recorded, not refused.
    shared_cpuset: tuple[int, ...]
    ports: Ports
    requirements: Requirements
    # `None` means "budget against what `docker info` reports" (the VM on a
    # Mac). A number means the harness's own allowance on a shared host, which
    # is deliberately far less than the host total.
    allowance_memory_bytes: int | None = None
    allowance_logical_cpus: int | None = None
    allowance_disk_bytes: int | None = None
    # Stamped on every manifest made under this profile, before anything is
    # measured. Measured reasons are appended to these.
    deviation_reasons: tuple[str, ...] = ()
    # The two disjoint whole-core sets the interference sweep measures against
    # each other, as cpuset specs.
    interference_pair: tuple[str, str] = ("2-5", "6-9")
    # Logical CPUs the vCPU speed probe may run on. `None` = all the daemon has.
    probe_cpuset: tuple[int, ...] | None = None
    # Physical core -> its logical CPUs, as read from `lscpu -e`. Empty on local.
    topology: dict[int, tuple[int, ...]] = field(default_factory=dict)

    @property
    def dsel_cpuset(self) -> tuple[int, ...]:
        """Every logical CPU any dsel container may be pinned to."""
        cpus: set[int] = set()
        for tier in self.tiers:
            cpus.update(tier.cpuset)
        return tuple(sorted(cpus))

    @property
    def tiers(self) -> tuple[Tier, ...]:
        return (self.engine, self.driver, self.app, self.observability)

    def tier(self, name: str) -> Tier:
        for tier in self.tiers:
            if tier.name == name:
                return tier
        raise ProfileError(f"profile {self.name} has no tier {name!r}")

    def envelope(
        self,
        tier: str = "engine",
        *,
        cpus: float | None = None,
        memory_bytes: int | None = None,
        pids_limit: int | None = None,
    ) -> ResourceEnvelope:
        """A `ResourceEnvelope` on one tier's cores, narrowed but never widened.

        The integration tests and scripts used to write `cpuset=(2, 3, 4, 5)`
        by hand; they ask the profile now, so the same test pins to 2-5 on the
        Mac and to 4-5,12-13 on the shared Linux host.
        """
        t = self.tier(tier)
        quota = t.cpus if cpus is None else min(cpus, t.cpus)
        memory = t.memory_bytes if memory_bytes is None else min(memory_bytes, t.memory_bytes)
        pids = t.pids_limit if pids_limit is None else min(pids_limit, t.pids_limit)
        return ResourceEnvelope(
            cpuset=t.cpuset, cpus=quota, memory_bytes=memory, pids_limit=pids
        )

    def publish(self, host_port: int, container_port: int) -> str:
        """A `--publish` value bound to the profile's address, never 0.0.0.0."""
        return f"{self.ports.bind}:{host_port}:{container_port}"

    def sanity_check(self) -> None:
        """Refuse a profile that contradicts itself before it reaches Docker."""
        problems: list[str] = []
        reserved = set(self.reserved_cpuset)
        measurement = (self.engine, self.driver)
        for tier in self.tiers:
            if not tier.cpuset:
                problems.append(f"{tier.name}: empty cpuset")
            if tier.cpus > len(tier.cpuset):
                problems.append(
                    f"{tier.name}: --cpus {tier.cpus:g} exceeds its cpuset width "
                    f"{len(tier.cpuset)}"
                )
            if self.allowance_memory_bytes is not None and reserved & set(tier.cpuset):
                problems.append(
                    f"{tier.name}: cpuset {tier.cpuset_spec} touches reserved cores "
                    f"{cpuset_spec(tuple(sorted(reserved & set(tier.cpuset))))}"
                )
        for i, a in enumerate(measurement):
            for b in self.tiers[i + 1 :]:
                if set(a.cpuset) & set(b.cpuset):
                    problems.append(f"{a.name} and {b.name} share logical CPUs")
        if self.topology:
            # Whole physical cores per tier: both SMT siblings of a core go to
            # the same tier, and no core is split between tiers.
            owner: dict[int, str] = {}
            for tier in self.tiers:
                for core, logical in self.topology.items():
                    hit = set(logical) & set(tier.cpuset)
                    if hit and hit != set(logical):
                        problems.append(
                            f"{tier.name}: has {sorted(hit)} of physical core {core} but "
                            f"not its siblings {sorted(set(logical) - hit)}"
                        )
                    if hit and tier.name not in ("app", "observability"):
                        if core in owner:
                            problems.append(
                                f"physical core {core} split: {owner[core]}, {tier.name}"
                            )
                        owner[core] = tier.name
        if problems:
            raise ProfileError(
                f"profile {self.name} is inconsistent:\n  - " + "\n  - ".join(problems)
            )


# --- local: PLAN.md's locked slice table, verbatim -----------------------------
#
#   cpu 0-1   host + docker daemon + app tier + observability stack
#   cpu 2-5   ENGINE under test              mem 3 GiB
#   cpu 6-9   load driver                    mem 1 GiB
#             app tier                       mem 1 GiB
#             prometheus/grafana/exporters   mem 1.5 GiB

LOCAL_HOST_CPUSET = (0, 1)
LOCAL_ENGINE_CPUSET = (2, 3, 4, 5)
LOCAL_DRIVER_CPUSET = (6, 7, 8, 9)


def local_profile() -> HostProfile:
    """The Apple M5 / Docker Desktop profile. Values unchanged since 2026-09-02."""
    return HostProfile(
        name=LOCAL,
        description="Apple M5 laptop, Docker Desktop VM with 10 vCPU / 7.75 GiB",
        reportable=False,
        engine=Tier("engine", LOCAL_ENGINE_CPUSET, 4.0, 3 * GIB, 4096),
        driver=Tier("driver", LOCAL_DRIVER_CPUSET, 4.0, 1 * GIB, 4096),
        app=Tier("app", LOCAL_HOST_CPUSET, 2.0, 1 * GIB, 1024),
        observability=Tier("observability", LOCAL_HOST_CPUSET, 1.0, 1536 * 1024**2, 512),
        reserved_cpuset=LOCAL_HOST_CPUSET,
        shared_cpuset=LOCAL_HOST_CPUSET,
        # The exporter keeps prometheus_client's default bind on local: Docker
        # Desktop reaches it through host.docker.internal, and tightening it to
        # loopback is UNVERIFIED on the Mac (backlog).
        ports=Ports(bind=LOOPBACK, prometheus=9090, grafana=3000, exporter=9464),
        requirements=Requirements(os="Darwin", notes=("Docker Desktop VM",)),
        deviation_reasons=(),
        interference_pair=("2-5", "6-9"),
        probe_cpuset=None,
    )


# --- linux-shared: built from a TOML file and the host's real topology ---------


def _as_int_list(value: object, key: str) -> list[int]:
    if not isinstance(value, list) or not all(isinstance(v, int) for v in value):
        raise ProfileError(f"{key}: expected a list of integers, got {value!r}")
    return [int(v) for v in value]


def _table(data: dict[str, object], key: str) -> dict[str, object]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ProfileError(f"profile file: [{key}] missing or not a table")
    return value


def _number(data: dict[str, object], key: str) -> float:
    value = data.get(key)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ProfileError(f"profile file: {key} must be a number, got {value!r}")
    return float(value)


def load_linux_file(path: Path | None = None) -> dict[str, object]:
    """Read the profile file as data. Pure apart from the read."""
    path = path or PROFILE_DIR / f"{LINUX_SHARED}.toml"
    with path.open("rb") as handle:
        return tomllib.load(handle)


def linux_shared_profile(
    topology: dict[int, tuple[int, ...]],
    *,
    exporter_bind: str | None = None,
    data: dict[str, object] | None = None,
) -> HostProfile:
    """Map the profile file's *physical-core* tiers onto this host's logical CPUs.

    `topology` is physical core index -> its logical CPUs, ordered as `lscpu -e`
    numbers them. The file says "engine gets physical cores 4 and 5"; which
    logical CPUs those are is read from the host, never assumed -- on the
    reference host core 4 is CPUs 4 and 12, but a BIOS or kernel that numbers
    siblings adjacently (4 and 5) would give a different cpuset for the same
    profile.
    """
    data = data if data is not None else load_linux_file()
    tiers_raw = _table(data, "tiers")
    req_raw = _table(data, "requirements")
    allowance = _table(data, "allowance")
    ports_raw = _table(data, "ports")

    def logical(cores: list[int], tier: str) -> tuple[int, ...]:
        missing = [c for c in cores if c not in topology]
        if missing:
            raise ProfileError(
                f"{tier}: physical cores {missing} do not exist on this host "
                f"(it has {sorted(topology)})"
            )
        return tuple(sorted(cpu for c in cores for cpu in topology[c]))

    def tier(name: str, *, quota_share: float = 1.0) -> Tier:
        raw = _table(tiers_raw, name)
        cpus = logical(_as_int_list(raw.get("cores"), f"tiers.{name}.cores"), name)
        share = _number(raw, "cpus_share") if "cpus_share" in raw else quota_share
        return Tier(
            name=name,
            cpuset=cpus,
            cpus=round(len(cpus) * share, 3),
            memory_bytes=int(_number(raw, "memory_gib") * GIB),
            pids_limit=int(_number(raw, "pids_limit")),
        )

    reserved = logical(_as_int_list(tiers_raw.get("reserved"), "tiers.reserved"), "reserved")
    engine, driver = tier("engine"), tier("driver")
    app, observability = tier("app"), tier("observability")
    pair = data.get("interference_pair")
    if not isinstance(pair, dict):
        raise ProfileError("profile file: [interference_pair] missing")
    measured = logical(_as_int_list(pair.get("measured"), "interference_pair.measured"), "pair")
    contended = logical(
        _as_int_list(pair.get("contended"), "interference_pair.contended"), "pair"
    )
    reasons = data.get("deviation_reasons", [])
    notes = req_raw.get("notes", [])
    profile = HostProfile(
        name=str(data.get("name", LINUX_SHARED)),
        description=str(data.get("description", "")),
        reportable=bool(data.get("reportable", False)),
        engine=engine,
        driver=driver,
        app=app,
        observability=observability,
        reserved_cpuset=reserved,
        shared_cpuset=tuple(sorted(set(app.cpuset) | set(observability.cpuset))),
        ports=Ports(
            bind=str(ports_raw.get("bind", LOOPBACK)),
            prometheus=int(_number(ports_raw, "prometheus")),
            grafana=int(_number(ports_raw, "grafana")),
            exporter=int(_number(ports_raw, "exporter")),
            exporter_bind=exporter_bind,
        ),
        requirements=Requirements(
            os=str(req_raw.get("os", "Linux")),
            min_physical_cores=int(_number(req_raw, "min_physical_cores")),
            min_memtotal_gib=_number(req_raw, "min_memtotal_gib"),
            cgroup_version=str(req_raw.get("cgroup_version", "2")),
            min_docker_major=int(_number(req_raw, "min_docker_major")),
            smt_required=bool(req_raw.get("smt_required", False)),
            notes=tuple(str(n) for n in notes) if isinstance(notes, list) else (),
        ),
        allowance_memory_bytes=int(_number(allowance, "memory_gib") * GIB),
        allowance_logical_cpus=len(engine.cpuset + driver.cpuset)
        + len(set(app.cpuset) | set(observability.cpuset)),
        allowance_disk_bytes=int(_number(allowance, "disk_gb") * 1000**3),
        deviation_reasons=tuple(str(r) for r in reasons) if isinstance(reasons, list) else (),
        interference_pair=(cpuset_spec(measured), cpuset_spec(contended)),
        probe_cpuset=tuple(
            sorted(set(engine.cpuset + driver.cpuset + app.cpuset + observability.cpuset))
        ),
        topology=dict(topology),
    )
    expected_cores = _number(allowance, "physical_cores")
    used = {c for c, cpus in topology.items() if set(cpus) & set(profile.dsel_cpuset)}
    if len(used) > expected_cores:
        raise ProfileError(
            f"tiers use {len(used)} physical cores, allowance is {expected_cores:g}"
        )
    profile.sanity_check()
    return profile


# --- choosing one ---------------------------------------------------------------

_ACTIVE: list[HostProfile] = []


def active() -> HostProfile:
    """The profile this process runs under.

    `DSEL_PROFILE=local` or `DSEL_PROFILE=linux-shared-8c16t` forces one;
    otherwise `detect` chooses, reading the host and never changing it. Cached:
    the topology does not change under a running process, and detection shells
    out to `lscpu` and `docker info`.
    """
    if _ACTIVE:
        return _ACTIVE[0]
    from dsel.hostprofile.detect import choose

    forced = os.environ.get("DSEL_PROFILE") or None
    profile = choose(forced=forced).profile
    _ACTIVE.append(profile)
    return profile


def reset_active() -> None:
    """Forget the cached profile (tests)."""
    _ACTIVE.clear()


def pin_current_process(cpuset: tuple[int, ...]) -> bool:
    """Pin this process (and children it spawns) to `cpuset`, if the OS allows.

    For the drivers that run on the host rather than in a container -- the soak,
    the connection ramp. On Linux this is `sched_setaffinity` on our own pid: it
    changes nothing about the host and is gone when the process exits. On macOS
    there is no such call, and nothing to pin to: the host is not where the VM's
    vCPUs are. Returns whether pinning happened.
    """
    setter = getattr(os, "sched_setaffinity", None)
    if setter is None:
        return False
    setter(0, set(cpuset))
    return True
