"""Environment capture (findings.md 8.5).

Not bureaucracy: exp07 showed the same spec yields a different candidate set on
macOS than on Linux CI, because mongo:8 refuses kernel >= 6.19. Two bundles with
identical spec hashes and different results are inexplicable without the kernel
version recorded, so the capture list is part of the result, not metadata.

Facts are gathered from three vantage points, which do not agree and are not
meant to: the macOS host, the Docker daemon, and the inside of a container --
the last being the only one that sees what an engine will see.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from dsel.audit.models import (
    DaemonCapture,
    HostCapture,
    ImagePin,
    Manifest,
    VmCapture,
)
from dsel.hostprofile import HostProfile
from dsel.version import provenance

_TIMEOUT_S = 30.0


class CaptureError(RuntimeError):
    """Environment capture failed in a way that must not be silently absorbed."""


def _run(args: list[str], *, stdin: str | None = None, timeout: float = _TIMEOUT_S) -> str:
    result = subprocess.run(
        args,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise CaptureError(
            f"{' '.join(args[:3])}... exited {result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout


def _sysctl_int(name: str) -> int | None:
    try:
        return int(_run(["sysctl", "-n", name]).strip())
    except (CaptureError, ValueError, subprocess.SubprocessError, OSError):
        return None


def _sysctl_str(name: str) -> str | None:
    try:
        value = _run(["sysctl", "-n", name]).strip()
    except (CaptureError, subprocess.SubprocessError, OSError):
        return None
    return value or None


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _root_device_rotational() -> tuple[str | None, str | None]:
    """The root filesystem's type and its block device's `rotational` flag.

    Read-only: `findmnt` names the source, `/sys/block/<disk>/queue/rotational`
    says whether the kernel thinks it spins. A partition (`nvme0n1p2`) is mapped
    to its parent disk through `/sys/class/block/<part>/..`.
    """
    try:
        raw = _run(["findmnt", "-no", "SOURCE,FSTYPE", "/"]).split()
    except (CaptureError, subprocess.SubprocessError, OSError):
        return None, None
    if len(raw) < 2:
        return None, None
    source, fstype = raw[0], raw[1]
    name = Path(source).name
    block = Path("/sys/class/block") / name
    disk = name
    if (block / "partition").exists():
        disk = block.resolve().parent.name
    return fstype, _read_text(f"/sys/block/{disk}/queue/rotational")


def _bracketed(value: str | None) -> str | None:
    if value is None:
        return None
    start, end = value.find("["), value.find("]")
    return value[start + 1 : end] if 0 <= start < end else value


def capture_linux_host() -> HostCapture:
    """A Linux host, read from /proc and /sys. Nothing is written."""
    from dsel.hostprofile.detect import read_host

    facts = read_host()
    fstype, rotational = _root_device_rotational()
    zones: dict[str, str] = {}
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        kind = _read_text(str(zone / "type"))
        temp = _read_text(str(zone / "temp"))
        if kind and temp:
            zones[kind] = temp
    return HostCapture(
        os_name=platform.system(),
        os_version=_os_release() or platform.release(),
        arch=platform.machine(),
        cpu_brand=facts.cpu_model,
        logical_cpus=facts.logical_cpus or None,
        physical_cpus=facts.physical_cores or None,
        memory_bytes=facts.memtotal_kib * 1024 if facts.memtotal_kib else None,
        kernel_release=facts.kernel,
        smt_active=facts.smt_active,
        cpu_governor=facts.governor,
        cpu_scaling_driver=_read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_driver"),
        transparent_hugepage=facts.thp,
        root_fs_type=fstype,
        root_device_rotational=rotational,
        thermal_zones=zones,
    )


def _os_release() -> str | None:
    text = _read_text("/etc/os-release") or ""
    for line in text.splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


REASON_FREQUENCY_SCALING = "cpu_frequency_scaling_enabled"
REASON_SMT = "smt_active"
REASON_NON_SSD_ROOT = "root_device_rotational"


def host_deviation_reasons(host: HostCapture) -> list[str]:
    """Reasons a bare-metal Linux host's envelope deviates, from facts already read.

    Recorded, never corrected: the governor and SMT belong to the host's owner.
    A `powersave` or `schedutil` governor means clock speed follows load and
    temperature, so two cells minutes apart need not have run at one frequency;
    SMT means a tier's two logical CPUs share one core's execution units.
    """
    reasons: list[str] = []
    if host.cpu_governor is not None and host.cpu_governor != "performance":
        reasons.append(REASON_FREQUENCY_SCALING)
    if host.smt_active:
        reasons.append(REASON_SMT)
    if host.root_device_rotational == "1":
        reasons.append(REASON_NON_SSD_ROOT)
    return reasons


def capture_host() -> HostCapture:
    """The host. On macOS it does not run the engine, but it schedules the VM."""
    if platform.system() == "Linux":
        return capture_linux_host()
    return HostCapture(
        os_name=platform.system(),
        os_version=platform.mac_ver()[0] or platform.release(),
        arch=platform.machine(),
        cpu_brand=_sysctl_str("machdep.cpu.brand_string"),
        logical_cpus=_sysctl_int("hw.ncpu"),
        physical_cpus=_sysctl_int("hw.physicalcpu"),
        performance_cores=_sysctl_int("hw.perflevel0.logicalcpu"),
        efficiency_cores=_sysctl_int("hw.perflevel1.logicalcpu"),
        memory_bytes=_sysctl_int("hw.memsize"),
    )


_DAEMON_FORMAT = (
    "{{.ServerVersion}}\n{{.KernelVersion}}\n{{.OperatingSystem}}\n"
    "{{.Driver}}\n{{.NCPU}}\n{{.MemTotal}}\n{{.CgroupVersion}}\n{{.CgroupDriver}}"
)


def capture_daemon() -> DaemonCapture:
    """What the Docker daemon reports. Requires a running daemon."""
    raw = _run(["docker", "info", "--format", _DAEMON_FORMAT]).splitlines()
    if len(raw) < 8:
        raise CaptureError(f"docker info returned {len(raw)} fields, expected 8")
    compose: str | None = None
    if shutil.which("docker"):
        try:
            compose = _run(["docker", "compose", "version", "--short"]).strip()
        except (CaptureError, subprocess.SubprocessError, OSError):
            compose = None
    return DaemonCapture(
        server_version=raw[0],
        kernel_version=raw[1],
        operating_system=raw[2],
        storage_driver=raw[3],
        ncpu=int(raw[4]),
        mem_total_bytes=int(raw[5]),
        cgroup_version=raw[6] or None,
        cgroup_driver=raw[7] or None,
        compose_version=compose,
    )


# Reads the VM's own view. Runs inside a container because that is the only
# vantage point that sees the kernel the engine will see. Kept to `cat` so it
# works in any image with a shell -- no interpreter assumed.
_VM_SCRIPT = r"""
set -u
echo "kernel_release=$(cat /proc/sys/kernel/osrelease 2>/dev/null || echo unknown)"
echo "thp=$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || echo unavailable)"
echo "swappiness=$(cat /proc/sys/vm/swappiness 2>/dev/null || echo unavailable)"
echo "numa_nodes=$(ls -d /sys/devices/system/node/node[0-9]* 2>/dev/null | wc -l)"
echo "cpuinfo_processors=$(grep -c ^processor /proc/cpuinfo 2>/dev/null || echo 0)"
echo "shm_bytes=$(df -B1 /dev/shm 2>/dev/null | awk 'NR==2{print $2}')"
echo "controllers=$(cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null || echo unavailable)"
for d in /sys/block/*/queue/rotational; do
  [ -e "$d" ] || continue
  dev=$(echo "$d" | cut -d/ -f4)
  echo "rotational.$dev=$(cat "$d")"
done
"""


def capture_vm(image: ImagePin, caps: list[str] | None = None) -> VmCapture:
    """Read the VM's kernel state from inside a container pinned by digest.

    `caps` are the profile's container limits. On a shared host even a
    two-second `cat` container is pinned to dsel's cores with a memory and
    pids ceiling; nothing dsel starts runs unbounded.
    """
    ref = f"{image.reference.split(':')[0]}@{image.index_digest}"
    out = _run(
        ["docker", "run", "--rm", "-i", *(caps or []), ref, "sh", "-s"], stdin=_VM_SCRIPT
    )
    fields: dict[str, str] = {}
    rotational: dict[str, str] = {}
    for line in out.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.startswith("rotational."):
            rotational[key.removeprefix("rotational.")] = value
        else:
            fields[key] = value

    def as_int(key: str) -> int | None:
        raw = fields.get(key, "")
        return int(raw) if raw.isdigit() else None

    controllers = fields.get("controllers", "")
    return VmCapture(
        kernel_release=fields.get("kernel_release", "unknown"),
        cgroup_controllers=controllers.split() if controllers != "unavailable" else [],
        transparent_hugepage=fields.get("thp") or None,
        swappiness=as_int("swappiness"),
        numa_nodes=as_int("numa_nodes"),
        shm_size_bytes=as_int("shm_bytes"),
        cpuinfo_processors=as_int("cpuinfo_processors"),
        rotational=rotational,
    )


def _cache_path() -> Path:
    """Where resolved index -> platform digest mappings are remembered."""
    base = os.environ.get("DSEL_CACHE_DIR")
    root = Path(base) if base else Path.home() / ".cache" / "dsel"
    return root / "digests.json"


def _load_cache() -> dict[str, dict[str, str]]:
    path = _cache_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _store_cache(cache: dict[str, dict[str, str]]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass  # a cache that cannot be written is a slow run, not a wrong one


def _local_index_digest(reference: str) -> str | None:
    """Read the image's index digest from the local daemon, no registry call."""
    try:
        raw = _run(
            ["docker", "image", "inspect", reference, "--format", "{{json .Descriptor}}"],
            timeout=60.0,
        )
    except (CaptureError, subprocess.SubprocessError, OSError):
        return None
    try:
        digest = json.loads(raw).get("digest")
    except json.JSONDecodeError:
        return None
    return digest if isinstance(digest, str) and digest.startswith("sha256:") else None


def _platform_from_registry(reference: str) -> tuple[str, str]:
    """Ask the registry which platform manifest this host should run."""
    raw = _run(["docker", "buildx", "imagetools", "inspect", reference, "--raw"], timeout=120.0)
    index = json.loads(raw)
    want_os, want_arch = "linux", _host_docker_arch()
    for entry in index.get("manifests", []):
        spec = entry.get("platform") or {}
        os_name, arch = spec.get("os"), spec.get("architecture")
        if os_name in (None, "unknown") or arch in (None, "unknown"):
            continue  # attestation manifests, not runnable images
        if os_name == want_os and arch == want_arch:
            variant = spec.get("variant")
            return entry["digest"], f"{os_name}/{arch}" + (f"/{variant}" if variant else "")
    raise CaptureError(f"{reference}: no {want_os}/{want_arch} manifest in the index")


def resolve_image(reference: str, allow_pull: bool = True) -> ImagePin:
    """Resolve a tag to its index digest and this host's platform digest.

    The invariant: the spec pins the *index* digest, the manifest records the
    resolved *platform* digest. `docker image inspect --format {{.Id}}` returns
    the index digest on this host, which is the trap -- so the platform digest
    comes from the index's manifest list, skipping `unknown/unknown`
    attestation entries.

    Resolution is local-first. The index digest is read from the daemon, and the
    index -> platform mapping is cached under it. That mapping is between two
    content addresses, so it is immutable and can never go stale -- and it keeps
    a run from spending a Docker Hub rate-limit token to learn what it already
    knows.
    """
    index_digest = _local_index_digest(reference)
    if index_digest is None:
        if not allow_pull:
            raise CaptureError(f"{reference} is not present locally and pulling is disabled")
        _run(["docker", "pull", "--quiet", reference], timeout=600.0)
        index_digest = _local_index_digest(reference)
        if index_digest is None:
            raise CaptureError(f"{reference}: no index digest after pull")

    cache = _load_cache()
    hit = cache.get(index_digest)
    if hit and "platform_digest" in hit and "platform" in hit:
        return ImagePin(
            reference=reference,
            index_digest=index_digest,
            platform_digest=hit["platform_digest"],
            platform=hit["platform"],
        )

    platform_digest, resolved = _platform_from_registry(reference)
    cache[index_digest] = {"platform_digest": platform_digest, "platform": resolved}
    _store_cache(cache)
    return ImagePin(
        reference=reference,
        index_digest=index_digest,
        platform_digest=platform_digest,
        platform=resolved,
    )


def _host_docker_arch() -> str:
    machine = platform.machine().lower()
    return {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(
        machine, machine
    )


def capture(
    image: ImagePin, caps: list[str] | None = None
) -> tuple[HostCapture, DaemonCapture, VmCapture]:
    """Capture all three vantage points."""
    return capture_host(), capture_daemon(), capture_vm(image, caps)


def build_manifest(
    image: ImagePin,
    deviation_reasons: list[str],
    profile: HostProfile | None = None,
) -> Manifest:
    """Assemble the S1 portion of the run manifest.

    The profile's name, its `reportable` stamp and its standing deviation
    reasons go in before anything is measured. With no profile the manifest is
    exactly what it was before profiles existed: `profile=local`, unreportable.
    """
    caps = (
        profile.driver.docker_flags(cpus=1.0, memory_bytes=256 * 1024**2) if profile else None
    )
    host, daemon, vm = capture(image, caps if profile and profile.name != "local" else None)
    prov = provenance()
    reasons = set(deviation_reasons) | set(profile.deviation_reasons if profile else ())
    return Manifest(
        profile=profile.name if profile is not None else "local",
        reportable=profile.reportable if profile is not None else False,
        captured_at=datetime.now(UTC),
        envelope_deviation_reasons=sorted(reasons),
        harness_version=prov.version,
        harness_commit=prov.commit,
        harness_dirty=prov.dirty,
        host=host,
        daemon=daemon,
        vm=vm,
    )
