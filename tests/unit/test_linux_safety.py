"""The shared-host safety rules, checked without a daemon (added 2026-10-14).

* every published port is bound to loopback;
* `dsel budget` on the Linux profile is held to the dsel allowance, not to the
  host total, and shows both figures when it refuses;
* no module pins a container to a literal cpuset any more.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Any

import pytest

from dsel.audit.environment import host_deviation_reasons
from dsel.audit.models import HostCapture
from dsel.compose.budget import (
    GIB,
    Budget,
    BudgetError,
    BudgetPlan,
    Component,
    Observability,
    plan_for,
)
from dsel.compose.observability import COMPOSE_FILE, compose_env, ports_for, render_config
from dsel.hostprofile import HostProfile, linux_shared_profile, local_profile
from tests.unit.test_hostprofile import REFERENCE

SRC = Path(__file__).resolve().parents[2] / "src" / "dsel"
HOST_MEM = 33348857856  # docker info MemTotal on the reference host, 31.06 GiB


@pytest.fixture
def linux() -> HostProfile:
    return linux_shared_profile(REFERENCE, exporter_bind="172.17.0.1")


def linux_budget(profile: HostProfile) -> Budget:
    return Budget.for_profile(profile, ncpu=16, mem_total_bytes=HOST_MEM)


# --- budget ---------------------------------------------------------------------------


def test_linux_budget_is_the_allowance_not_the_host(linux: HostProfile) -> None:
    b = linux_budget(linux)
    assert b.allocatable_bytes == 12 * GIB
    assert b.host_memory_bytes == HOST_MEM


def test_the_default_linux_plan_fits(linux: HostProfile) -> None:
    plan = plan_for(
        linux, linux_budget(linux), with_app_tier=True, observability=Observability.LIGHT
    )
    plan.check()
    assert plan.memory_claimed == int(10.5 * GIB)


def test_deep_observability_is_refused_against_the_allowance(linux: HostProfile) -> None:
    """The Linux S2 acceptance case: 6 + 2 + 1 + 3.5 = 12.5 GiB > 12 GiB, on a
    host whose `docker info` would have said 31 GiB was available."""
    plan = plan_for(
        linux, linux_budget(linux), with_app_tier=True, observability=Observability.DEEP
    )
    with pytest.raises(BudgetError) as exc:
        plan.check()
    message = str(exc.value)
    assert "over by 0.50G (dsel allowance)" in message
    assert "dsel allowance 12.00G; host 31.06G is not the budget" in message
    for expected in ("engine", "driver", "claimed", "allocatable", "headroom"):
        assert expected in message


def test_an_engine_asking_for_more_than_its_share_is_refused(linux: HostProfile) -> None:
    plan = plan_for(
        linux,
        linux_budget(linux),
        with_app_tier=True,
        observability=Observability.LIGHT,
        engine_memory_bytes=10 * GIB,
    )
    with pytest.raises(BudgetError, match="over by 2.50G"):
        plan.check()


def test_the_same_plan_would_have_fitted_the_host(linux: HostProfile) -> None:
    """Which is exactly why `docker info` is not the budget on a shared host."""
    host_view = Budget(
        total_vcpus=16, total_memory_bytes=HOST_MEM, shared_cpuset=linux.shared_cpuset
    )
    BudgetPlan(
        host_view,
        plan_for(
            linux, linux_budget(linux), with_app_tier=True, observability=Observability.DEEP
        ).components,
    ).check()


def test_a_component_on_a_reserved_core_is_refused(linux: HostProfile) -> None:
    plan = BudgetPlan(linux_budget(linux), [Component("intruder", (0, 8), 1.0, GIB)])
    with pytest.raises(BudgetError, match="outside the dsel allowance"):
        plan.check()


def test_the_arithmetic_shows_real_cpusets_not_min_max(linux: HostProfile) -> None:
    plan = plan_for(
        linux, linux_budget(linux), with_app_tier=True, observability=Observability.LIGHT
    )
    text = plan.arithmetic()
    assert "4-5,12-13" in text and "4-13" not in text
    assert "on 8 allowed logical CPUs (4-7,12-15)" in text


# --- loopback publishing ------------------------------------------------------------------


def test_provision_publishes_on_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    from dsel.audit.models import ImagePin
    from dsel.runtime import docker as runtime_docker

    calls: list[list[str]] = []

    def fake(args: list[str], timeout: float = 300.0) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "cid\n", "")

    monkeypatch.setattr(runtime_docker, "_docker", fake)
    pin = ImagePin(
        reference="postgres:18", index_digest="sha256:" + "a" * 64,
        platform_digest="sha256:" + "b" * 64, platform="linux/amd64",
    )  # fmt: skip
    runtime_docker.provision(
        pin,
        local_profile().envelope("engine"),
        "run-1",
        data_dir="/data",
        container_port=5432,
        host_port=15432,
    )
    run = next(c for c in calls if c[0] == "run")
    assert run[run.index("--publish") + 1] == "127.0.0.1:15432:5432"


def test_app_tier_publishes_on_loopback_on_its_tier(
    monkeypatch: pytest.MonkeyPatch, linux: HostProfile
) -> None:
    from dsel.app import stack

    calls: list[list[str]] = []

    def fake(args: list[str], timeout: float = 900.0) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(stack, "_docker", fake)
    stack.start_app("dsel-app:local", "run-1", 18000, profile=linux)
    run = next(c for c in calls if c[0] == "run")
    assert run[run.index("--publish") + 1] == "127.0.0.1:18000:8000"
    assert run[run.index("--cpuset-cpus") + 1] == "7,15"
    assert run[run.index("--pids-limit") + 1] == "1024"


def test_observability_compose_binds_every_port_to_loopback() -> None:
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    lines = text.splitlines()
    port_lines = [
        lines[i + 1].strip() for i, line in enumerate(lines) if line.strip() == "ports:"
    ]
    assert len(port_lines) == 2
    for line in port_lines:
        assert line.startswith('- "${DSEL_BIND:-127.0.0.1}:'), line
    assert text.count('cpuset: "${DSEL_OBS_CPUSET:-0-1}"') == 2
    assert 'cpuset: "0-1"' not in text


def test_compose_env_carries_the_profile(linux: HostProfile) -> None:
    env = compose_env("run-1", ports_for(linux), linux)
    assert env["DSEL_BIND"] == "127.0.0.1"
    assert env["DSEL_OBS_CPUSET"] == "7,15"
    assert env["DSEL_PROMETHEUS_PORT"] == "19090"
    assert env["DSEL_GRAFANA_PORT"] == "13000"
    assert env["DSEL_EXPORTER_PORT"] == "19464"


def test_local_compose_env_keeps_its_ports() -> None:
    env = compose_env("run-1", ports_for(local_profile()))
    assert (env["DSEL_PROMETHEUS_PORT"], env["DSEL_GRAFANA_PORT"]) == ("9090", "3000")
    assert env["DSEL_BIND"] == "127.0.0.1"


def test_prometheus_stamps_follow_the_profile(linux: HostProfile) -> None:
    config = render_config("run-1", ports_for(linux), linux.name, linux.reportable)[
        "prometheus/prometheus.yml"
    ]
    assert "profile: linux-shared-8c16t" in config
    assert 'reportable: "false"' in config
    assert "host.docker.internal:19464" in config
    assert "$" not in config


# --- no literal cpusets left ------------------------------------------------------------


def _literal_cpuset_args(path: Path) -> list[str]:
    """`"--cpuset-cpus", "<literal>"` pairs in any list or call in a module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        elements: list[Any] = []
        if isinstance(node, ast.List | ast.Tuple):
            elements = list(node.elts)
        for first, second in zip(elements, elements[1:], strict=False):
            if (
                isinstance(first, ast.Constant)
                and first.value == "--cpuset-cpus"
                and isinstance(second, ast.Constant)
            ):
                found.append(f"{path.name}:{second.lineno} {second.value!r}")
    return found


def test_no_module_pins_a_container_to_a_literal_cpuset() -> None:
    offenders = [hit for p in sorted(SRC.rglob("*.py")) for hit in _literal_cpuset_args(p)]
    assert not offenders, offenders


# --- deviation reasons from host facts ------------------------------------------------------


def test_host_deviation_reasons_are_read_not_assumed() -> None:
    host = HostCapture(
        os_name="Linux", os_version="x", arch="x86_64",
        cpu_governor="powersave", smt_active=True, root_device_rotational="0",
    )  # fmt: skip
    assert host_deviation_reasons(host) == ["cpu_frequency_scaling_enabled", "smt_active"]
    quiet = HostCapture(
        os_name="Linux", os_version="x", arch="x86_64",
        cpu_governor="performance", smt_active=False, root_device_rotational="0",
    )  # fmt: skip
    assert host_deviation_reasons(quiet) == []


# --- digest resolution ----------------------------------------------------------------------


def test_repository_strips_tag_and_digest_but_not_a_registry_port() -> None:
    from dsel.audit.environment import repository

    assert repository("postgres:18") == "postgres"
    assert repository("prom/prometheus@sha256:" + "a" * 64) == "prom/prometheus"
    assert repository("registry:5000/team/pg:18") == "registry:5000/team/pg"


def test_platform_lookup_asks_about_the_index_held_not_the_tag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tag can move after the pull; the platform digest must belong to the
    index digest recorded beside it."""
    import json

    from dsel.audit import environment

    held = "sha256:" + "1" * 64
    asked: list[str] = []
    monkeypatch.setenv("DSEL_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(environment, "_local_index_digest", lambda ref: held)

    def fake_run(args: list[str], **_: object) -> str:
        asked.append(args[4])
        index = {
            "manifests": [
                {
                    "digest": "sha256:" + "2" * 64,
                    "platform": {"os": "linux", "architecture": "amd64"},
                },
                {
                    "digest": "sha256:" + "3" * 64,
                    "platform": {"os": "linux", "architecture": "arm64"},
                },
            ]
        }
        return json.dumps(index)

    monkeypatch.setattr(environment, "_run", fake_run)
    monkeypatch.setattr(environment, "_host_docker_arch", lambda: "amd64")
    pin = environment.resolve_image("postgres:18")
    assert asked == [f"postgres@{held}"]
    assert pin.index_digest == held and pin.platform_digest == "sha256:" + "2" * 64


# --- vCPU probe on unequal sets ---------------------------------------------------------------


def test_heterogeneity_is_judged_per_vcpu_when_the_sets_differ_in_width() -> None:
    """Four identical engine CPUs against two identical driver CPUs are not
    heterogeneous, whatever their totals say."""
    from dsel.audit.models import VcpuSpeed
    from dsel.audit.vcpu_probe import REASON_HETEROGENEOUS, analyse

    speeds = [VcpuSpeed(vcpu=c, median_ips=1e6, min_ips=1e6, max_ips=1e6, samples=5)
              for c in (4, 5, 6, 12, 13, 14)]  # fmt: skip
    result = analyse(speeds, (4, 5, 12, 13), (6, 14))
    assert result.set_difference_pct == 0.0
    assert REASON_HETEROGENEOUS not in result.reasons
