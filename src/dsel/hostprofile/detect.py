"""Choosing a host profile by reading the host, and only reading it.

Everything here is a read: `uname`, `lscpu -e`, `nproc`, `/proc/meminfo`,
`docker info`, the cgroup filesystem type, and a few `/sys` files that are
recorded but never written. Nothing sets a sysctl, a governor or a daemon
option. On a shared host the harness is a guest, and a guest that rearranged
the furniture to suit its measurement would be measuring a machine that does
not exist once it leaves.

The parsers are pure functions over text, so every rule here is unit-tested
against captured output rather than against whichever machine runs the tests.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from dsel.hostprofile import (
    GIB,
    LINUX_SHARED,
    LOCAL,
    HostProfile,
    ProfileError,
    linux_shared_profile,
    load_linux_file,
    local_profile,
)

_TIMEOUT_S = 20.0


# --- pure parsers ----------------------------------------------------------------


def parse_lscpu_e(text: str) -> dict[int, tuple[int, ...]]:
    """`lscpu -e` -> physical core index -> its online logical CPUs.

    Physical cores are keyed by `(SOCKET, CORE)` and then renumbered 0..n-1 in
    that order, so a two-socket host whose sockets both count cores from 0 does
    not have its cores merged. Offline CPUs are left out: a tier pinned to an
    offline CPU would fail at `docker run`, or worse, silently get fewer cores.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ProfileError("lscpu -e produced no output")
    header = lines[0].split()
    try:
        cpu_i, core_i = header.index("CPU"), header.index("CORE")
    except ValueError as exc:
        raise ProfileError(f"lscpu -e header has no CPU/CORE columns: {lines[0]!r}") from exc
    socket_i = header.index("SOCKET") if "SOCKET" in header else None
    online_i = header.index("ONLINE") if "ONLINE" in header else None
    grouped: dict[tuple[int, int], list[int]] = {}
    for line in lines[1:]:
        cols = line.split()
        if len(cols) <= max(cpu_i, core_i):
            continue
        if online_i is not None and len(cols) > online_i and cols[online_i] != "yes":
            continue
        if cols[core_i] in ("-", ""):
            continue
        socket = int(cols[socket_i]) if socket_i is not None and cols[socket_i] != "-" else 0
        grouped.setdefault((socket, int(cols[core_i])), []).append(int(cols[cpu_i]))
    return {
        index: tuple(sorted(cpus))
        for index, (_, cpus) in enumerate(sorted(grouped.items(), key=lambda kv: kv[0]))
    }


def parse_meminfo_kib(text: str, key: str = "MemTotal") -> int | None:
    for line in text.splitlines():
        if line.startswith(f"{key}:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return None


def docker_major(version: str) -> int | None:
    head = version.strip().split(".", 1)[0]
    return int(head) if head.isdigit() else None


def parse_bracketed(text: str) -> str | None:
    """`always [madvise] never` -> `madvise`."""
    start, end = text.find("["), text.find("]")
    if 0 <= start < end:
        return text[start + 1 : end]
    stripped = text.strip()
    return stripped or None


# --- the host, as read -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HostFacts:
    """What detection read. Every field is evidence, so all of it is kept."""

    os_name: str
    kernel: str
    cpu_model: str | None = None
    topology: dict[int, tuple[int, ...]] = field(default_factory=dict)
    nproc: int | None = None
    memtotal_kib: int | None = None
    docker_version: str | None = None
    docker_ncpu: int | None = None
    docker_mem_bytes: int | None = None
    cgroup_version: str | None = None
    cgroup_fs: str | None = None
    smt_active: bool | None = None
    governor: str | None = None
    thp: str | None = None
    bridge_gateway: str | None = None

    @property
    def physical_cores(self) -> int:
        return len(self.topology)

    @property
    def logical_cpus(self) -> int:
        return sum(len(v) for v in self.topology.values())

    @property
    def memtotal_gib(self) -> float | None:
        return self.memtotal_kib * 1024 / GIB if self.memtotal_kib is not None else None


def _run(args: list[str]) -> str | None:
    if shutil.which(args[0]) is None:
        return None
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def read_host() -> HostFacts:
    """Read the host. Never writes anything, anywhere."""
    os_name = platform.system()
    kernel = platform.release()
    docker_raw = _run(
        [
            "docker",
            "info",
            "--format",
            "{{.ServerVersion}}|{{.NCPU}}|{{.MemTotal}}|{{.CgroupVersion}}",
        ]
    )
    docker_version = docker_ncpu = docker_mem = cgroup_version = None
    if docker_raw:
        parts = docker_raw.strip().split("|")
        if len(parts) == 4:
            docker_version = parts[0] or None
            docker_ncpu = int(parts[1]) if parts[1].isdigit() else None
            docker_mem = int(parts[2]) if parts[2].isdigit() else None
            cgroup_version = parts[3] or None
    if os_name != "Linux":
        return HostFacts(
            os_name=os_name,
            kernel=kernel,
            docker_version=docker_version,
            docker_ncpu=docker_ncpu,
            docker_mem_bytes=docker_mem,
            cgroup_version=cgroup_version,
        )

    lscpu = _run(["lscpu", "-e=CPU,CORE,SOCKET,NODE,ONLINE"])
    topology = parse_lscpu_e(lscpu) if lscpu else {}
    model = None
    cpuinfo = _read("/proc/cpuinfo") or ""
    for line in cpuinfo.splitlines():
        if line.startswith("model name"):
            model = line.split(":", 1)[1].strip()
            break
    nproc_raw = _run(["nproc"])
    governor = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    thp_raw = _read("/sys/kernel/mm/transparent_hugepage/enabled")
    smt_raw = _read("/sys/devices/system/cpu/smt/active")
    fs = _run(["stat", "-f", "-c", "%T", "/sys/fs/cgroup"])
    gateway = _run(
        [
            "docker",
            "network",
            "inspect",
            "bridge",
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
        ]
    )
    return HostFacts(
        os_name=os_name,
        kernel=kernel,
        cpu_model=model,
        topology=topology,
        nproc=int(nproc_raw.strip()) if nproc_raw and nproc_raw.strip().isdigit() else None,
        memtotal_kib=parse_meminfo_kib(_read("/proc/meminfo") or ""),
        docker_version=docker_version,
        docker_ncpu=docker_ncpu,
        docker_mem_bytes=docker_mem,
        cgroup_version=cgroup_version,
        cgroup_fs=fs.strip() if fs else None,
        smt_active=(smt_raw == "1") if smt_raw is not None else None,
        governor=governor,
        thp=parse_bracketed(thp_raw) if thp_raw else None,
        bridge_gateway=(gateway.strip() or None) if gateway else None,
    )


# --- the decision ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Check:
    """One row of the minimum-spec table, against what was read."""

    requirement: str
    wanted: str
    observed: str
    ok: bool


@dataclass(frozen=True, slots=True)
class Detection:
    profile: HostProfile
    facts: HostFacts
    checks: tuple[Check, ...]
    forced: bool = False

    def table(self) -> str:
        lines = [
            f"profile: {self.profile.name}"
            + ("  (forced by DSEL_PROFILE)" if self.forced else ""),
            "",
            f"{'requirement':<16} {'minimum':<16} {'observed':<34} ok",
            f"{'-' * 16} {'-' * 16} {'-' * 34} --",
        ]
        for check in self.checks:
            lines.append(
                f"{check.requirement:<16} {check.wanted:<16} {check.observed:<34} "
                f"{'yes' if check.ok else 'NO'}"
            )
        p = self.profile
        lines += ["", f"{'tier':<14} {'cpuset':>12} {'cpus':>6} {'memory':>9} {'pids':>6}"]
        lines.append(f"{'-' * 14} {'-' * 12} {'-' * 6} {'-' * 9} {'-' * 6}")
        for tier in p.tiers:
            lines.append(
                f"{tier.name:<14} {tier.cpuset_spec:>12} {tier.cpus:>6g} "
                f"{tier.memory_gib:>8.2f}G {tier.pids_limit:>6}"
            )
        if p.allowance_memory_bytes is not None:
            lines.append(
                f"{'reserved':<14} {_spec(p.reserved_cpuset):>12}   never used by dsel"
            )
            lines.append(
                f"allowance: {p.allowance_logical_cpus} logical CPUs, "
                f"{p.allowance_memory_bytes / GIB:g} GiB memory, "
                f"{(p.allowance_disk_bytes or 0) / 1000**3:g} GB disk"
            )
        lines.append(
            f"ports: bind {p.ports.bind}, prometheus {p.ports.prometheus}, grafana "
            f"{p.ports.grafana}, exporter {p.ports.exporter}"
            + (f" on {p.ports.exporter_bind}" if p.ports.exporter_bind else "")
        )
        lines.append(f"reportable: {str(p.reportable).lower()}")
        return "\n".join(lines)


def _spec(cpus: tuple[int, ...]) -> str:
    from dsel.hostprofile import cpuset_spec

    return cpuset_spec(cpus)


def linux_checks(facts: HostFacts, data: dict[str, object] | None = None) -> tuple[Check, ...]:
    """The minimum-spec table for `linux-shared-8c16t`, row by row."""
    data = data if data is not None else load_linux_file()
    req = data.get("requirements")
    if not isinstance(req, dict):
        raise ProfileError("profile file has no [requirements]")
    min_cores = int(req.get("min_physical_cores", 8))
    min_mem = float(req.get("min_memtotal_gib", 30))
    want_cgroup = str(req.get("cgroup_version", "2"))
    min_docker = int(req.get("min_docker_major", 28))
    mem = facts.memtotal_gib
    major = docker_major(facts.docker_version or "")
    online = facts.logical_cpus
    return (
        Check("os", "Linux", facts.os_name, facts.os_name == "Linux"),
        Check(
            "physical cores",
            f">= {min_cores}",
            f"{facts.physical_cores} ({online} logical online)",
            facts.physical_cores >= min_cores,
        ),
        Check(
            "MemTotal",
            f">= {min_mem:g} GiB",
            f"{mem:.2f} GiB" if mem is not None else "unreadable",
            mem is not None and mem >= min_mem,
        ),
        Check(
            "cgroup",
            f"v{want_cgroup}",
            f"v{facts.cgroup_version} ({facts.cgroup_fs or '?'})",
            facts.cgroup_version == want_cgroup,
        ),
        Check(
            "Docker Engine",
            f">= {min_docker}",
            facts.docker_version or "not responding",
            major is not None and major >= min_docker,
        ),
        Check(
            "nproc",
            "== online CPUs",
            f"{facts.nproc} vs {online}",
            facts.nproc == online,
        ),
    )


def choose(facts: HostFacts | None = None, *, forced: str | None = None) -> Detection:
    """Pick the profile for this host, or refuse with the failed rows named."""
    facts = facts if facts is not None else read_host()
    name = forced or (LOCAL if facts.os_name == "Darwin" else LINUX_SHARED)
    if name == LOCAL:
        checks: tuple[Check, ...] = (
            Check("os", "Darwin", facts.os_name, facts.os_name == "Darwin"),
        )
        return Detection(local_profile(), facts, checks, forced=forced is not None)
    if name != LINUX_SHARED:
        raise ProfileError(f"unknown profile {name!r}; known: {LOCAL}, {LINUX_SHARED}")
    data = load_linux_file()
    checks = linux_checks(facts, data)
    failed = [c for c in checks if not c.ok]
    if failed:
        rows = "\n".join(
            f"  - {c.requirement}: wanted {c.wanted}, observed {c.observed}" for c in failed
        )
        raise ProfileError(f"this host does not meet {LINUX_SHARED}'s minimum spec:\n{rows}")
    profile = linux_shared_profile(
        facts.topology, exporter_bind=facts.bridge_gateway, data=data
    )
    return Detection(profile, facts, checks, forced=forced is not None)
