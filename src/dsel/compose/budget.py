"""Pre-flight resource budget (PLAN.md S2).

Build this first so the arithmetic is visible on day one rather than discovered
at S14. Nothing here starts a container: it adds up what a configuration would
demand and refuses combinations the machine cannot honour, showing the sums.

On `profile=local` the budget is the VM's, not the host's: `docker info` reports
7.75 GiB on the Mac while the host has 16 GiB, and it is the VM that has to fit
everything. On a shared Linux host (`linux-shared-8c16t`, 2026-10-14) it is
neither: `docker info` there is the whole machine -- 16 threads and 31 GiB on the
reference host -- and most of that belongs to other work. The budget is then the
profile's *dsel allowance* (12 GiB, four physical cores), and a plan that would
fit the host but not the allowance is refused with both figures shown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from dsel.hostprofile import (
    LOCAL_DRIVER_CPUSET,
    LOCAL_ENGINE_CPUSET,
    LOCAL_HOST_CPUSET,
    HostProfile,
    cpuset_spec,
)

GIB = 1024**3

# PLAN.md's locked slice table: the `local` profile's values, kept importable
# under their historical names.
HOST_CPUSET = LOCAL_HOST_CPUSET
ENGINE_CPUSET = LOCAL_ENGINE_CPUSET
DRIVER_CPUSET = LOCAL_DRIVER_CPUSET

# The VM keeps its own kernel, the daemon and containerd out of the allocatable
# pool. PLAN.md budgets "6.5 of 7.75 GiB", implying a 1.25 GiB reserve -- but
# `docker info` reports 7.748 GiB, not 7.75, so a literal 1.25 GiB reserve puts
# PLAN.md's own slice table 2 MiB over the line. 1 GiB is the round number that
# fits the table with 0.25 GiB to spare and still refuses deep observability.
VM_RESERVE_BYTES = 1024 * 1024 * 1024  # 1 GiB


class Observability(StrEnum):
    """How much of the observability stack is running."""

    NONE = "none"
    LIGHT = "light"  # prometheus + grafana, scraping metrics.ndjson only
    DEEP = "deep"  # + cAdvisor, node-exporter, per-engine exporters


@dataclass(frozen=True, slots=True)
class Component:
    """One container's claim on the machine."""

    name: str
    cpuset: tuple[int, ...]
    cpus: float
    memory_bytes: int

    @property
    def memory_gib(self) -> float:
        return self.memory_bytes / GIB


class BudgetError(RuntimeError):
    """A configuration the machine cannot honour. Carries the arithmetic."""


@dataclass(frozen=True, slots=True)
class Budget:
    """What the machine has to give."""

    total_vcpus: int
    total_memory_bytes: int
    reserve_bytes: int = VM_RESERVE_BYTES
    # vCPUs on which oversubscription is by design (local: the host slice).
    shared_cpuset: tuple[int, ...] = HOST_CPUSET
    # When set, the budget is an allowance on a shared host: no component may
    # be pinned outside these CPUs, and `total_memory_bytes` is the allowance,
    # not the machine. `host_memory_bytes` is shown beside it, never used.
    allowed_cpuset: tuple[int, ...] | None = None
    host_memory_bytes: int | None = None
    label: str = "VM"

    @property
    def allocatable_bytes(self) -> int:
        return self.total_memory_bytes - self.reserve_bytes

    @classmethod
    def for_profile(cls, profile: HostProfile, *, ncpu: int, mem_total_bytes: int) -> Budget:
        """The budget a profile is held to. `ncpu`/`mem_total_bytes` are `docker info`'s.

        `local` budgets against the VM, less its reserve -- unchanged. A profile
        with an allowance budgets against the allowance and nothing else.
        """
        if profile.allowance_memory_bytes is None:
            return cls(total_vcpus=ncpu, total_memory_bytes=mem_total_bytes)
        return cls(
            total_vcpus=ncpu,
            total_memory_bytes=profile.allowance_memory_bytes,
            reserve_bytes=0,
            shared_cpuset=profile.shared_cpuset,
            allowed_cpuset=profile.dsel_cpuset,
            host_memory_bytes=mem_total_bytes,
            label="dsel allowance",
        )


@dataclass(frozen=True, slots=True)
class BudgetPlan:
    """A proposed set of components, checkable before anything starts."""

    budget: Budget
    components: list[Component] = field(default_factory=list)

    @property
    def memory_claimed(self) -> int:
        return sum(c.memory_bytes for c in self.components)

    def cpu_claimed_on(self, vcpu: int) -> float:
        """Total CPU quota claimed by components whose cpuset includes `vcpu`.

        A component's quota is spread across its cpuset, so a 4.0-quota
        container on a 4-vCPU set claims 1.0 per vCPU.
        """
        total = 0.0
        for component in self.components:
            if vcpu in component.cpuset and component.cpuset:
                total += component.cpus / len(component.cpuset)
        return total

    def arithmetic(self) -> str:
        """The sums, rendered. Shown whenever the plan is refused."""
        lines = [
            f"{'component':<26} {'cpuset':>10} {'cpus':>6} {'memory':>10}",
            f"{'-' * 26} {'-' * 10} {'-' * 6} {'-' * 10}",
        ]
        for c in sorted(self.components, key=lambda c: c.name):
            # The real cpuset, not min-max: "4-13" for 4-5,12-13 would name six
            # CPUs the component is not pinned to, four of them reserved.
            cpuset = cpuset_spec(c.cpuset) if c.cpuset else "-"
            lines.append(f"{c.name:<26} {cpuset:>10} {c.cpus:>6.1f} {c.memory_gib:>9.2f}G")
        claimed = self.memory_claimed / GIB
        allocatable = self.budget.allocatable_bytes / GIB
        total = self.budget.total_memory_bytes / GIB
        reserve = self.budget.reserve_bytes / GIB
        if self.budget.allowed_cpuset is None:
            basis = f"   (VM {total:.2f}G - {reserve:.2f}G reserve)"
        else:
            host = (self.budget.host_memory_bytes or 0) / GIB
            basis = f"   ({self.budget.label} {total:.2f}G; host {host:.2f}G is not the budget)"
        lines += [
            f"{'-' * 26} {'-' * 10} {'-' * 6} {'-' * 10}",
            f"{'claimed':<26} {'':>10} {'':>6} {claimed:>9.2f}G",
            f"{'allocatable':<26} {'':>10} {'':>6} {allocatable:>9.2f}G" + basis,
            f"{'headroom':<26} {'':>10} {'':>6} {allocatable - claimed:>9.2f}G",
        ]
        if self.budget.allowed_cpuset is not None:
            cpu_claimed = sum(c.cpus for c in self.components)
            lines.append(
                f"{'cpu quota claimed':<26} {'':>10} {cpu_claimed:>6.1f}"
                f"   on {len(self.budget.allowed_cpuset)} allowed logical CPUs "
                f"({cpuset_spec(self.budget.allowed_cpuset)})"
            )
        over = [
            (v, self.cpu_claimed_on(v))
            for v in range(self.budget.total_vcpus)
            if self.cpu_claimed_on(v) > 1.0
        ]
        if over:
            lines.append("oversubscribed vCPUs:")
            lines += [
                f"  vcpu {v}: {claim:.2f} quota claimed (1.00 available)" for v, claim in over
            ]
        return "\n".join(lines)

    def check(self) -> None:
        """Raise `BudgetError` with the arithmetic if this cannot be honoured."""
        problems: list[str] = []

        if self.memory_claimed > self.budget.allocatable_bytes:
            over = (self.memory_claimed - self.budget.allocatable_bytes) / GIB
            problems.append(
                f"memory: claims {self.memory_claimed / GIB:.2f}G against "
                f"{self.budget.allocatable_bytes / GIB:.2f}G allocatable, over by {over:.2f}G"
                + (f" ({self.budget.label})" if self.budget.allowed_cpuset is not None else "")
            )

        if self.budget.allowed_cpuset is not None:
            allowed = set(self.budget.allowed_cpuset)
            for component in self.components:
                forbidden = sorted(set(component.cpuset) - allowed)
                if forbidden:
                    problems.append(
                        f"{component.name}: cpuset {cpuset_spec(component.cpuset)} uses "
                        f"{cpuset_spec(tuple(forbidden))}, outside the dsel allowance "
                        f"{cpuset_spec(self.budget.allowed_cpuset)} (reserved for the host)"
                    )

        for component in self.components:
            outside = [v for v in component.cpuset if v >= self.budget.total_vcpus]
            if outside:
                problems.append(
                    f"{component.name}: cpuset references vCPU {outside} but the VM has "
                    f"{self.budget.total_vcpus} (0-{self.budget.total_vcpus - 1})"
                )
            if component.cpuset and component.cpus > len(component.cpuset):
                problems.append(
                    f"{component.name}: --cpus {component.cpus} exceeds its cpuset width "
                    f"{len(component.cpuset)}; quota can never be met"
                )

        oversubscribed = [
            (v, self.cpu_claimed_on(v))
            for v in range(self.budget.total_vcpus)
            if self.cpu_claimed_on(v) > 1.0
        ]
        # The shared slice (local: vCPUs 0-1 -- host, app tier, observability)
        # is oversubscribed by design, so it is recorded rather than refused.
        hard = [(v, c) for v, c in oversubscribed if v not in self.budget.shared_cpuset]
        if hard:
            detail = ", ".join(f"vcpu {v} at {c:.2f}" for v, c in hard)
            problems.append(f"cpu: measurement vCPUs oversubscribed ({detail})")

        if problems:
            raise BudgetError(
                "this configuration does not fit:\n  - "
                + "\n  - ".join(problems)
                + "\n\n"
                + self.arithmetic()
            )


def local_profile(
    *,
    with_app_tier: bool,
    observability: Observability,
    engine_memory_bytes: int = 3 * GIB,
    driver_memory_bytes: int = 1 * GIB,
    app_memory_bytes: int = 1 * GIB,
) -> list[Component]:
    """The `profile=local` component set from PLAN.md's slice table."""
    components = [
        Component("engine", ENGINE_CPUSET, 4.0, engine_memory_bytes),
        Component("driver", DRIVER_CPUSET, 4.0, driver_memory_bytes),
    ]
    if with_app_tier:
        components.append(Component("app-tier", HOST_CPUSET, 2.0, app_memory_bytes))
    if observability is Observability.LIGHT:
        # prometheus + grafana, consuming metrics.ndjson through the exporter.
        components.append(Component("observability-light", HOST_CPUSET, 1.0, 1536 * 1024**2))
    elif observability is Observability.DEEP:
        # + cAdvisor, node-exporter and a per-engine exporter per candidate.
        components.append(Component("observability-deep", HOST_CPUSET, 2.0, 3584 * 1024**2))
    return components


def plan_local(
    budget: Budget, *, with_app_tier: bool, observability: Observability
) -> BudgetPlan:
    """Assemble and return the `profile=local` plan (unchecked)."""
    return BudgetPlan(
        budget=budget,
        components=local_profile(with_app_tier=with_app_tier, observability=observability),
    )


# Deep observability adds cAdvisor, node-exporter and per-engine exporters.
DEEP_OBSERVABILITY_BYTES = 3584 * 1024**2
DEEP_OBSERVABILITY_CPUS_FACTOR = 2.0


def profile_components(
    profile: HostProfile,
    *,
    with_app_tier: bool,
    observability: Observability,
    engine_memory_bytes: int | None = None,
    driver_memory_bytes: int | None = None,
) -> list[Component]:
    """A profile's component set. For `local` this is exactly `local_profile`."""
    if profile.allowance_memory_bytes is None:
        return local_profile(
            with_app_tier=with_app_tier,
            observability=observability,
            engine_memory_bytes=engine_memory_bytes or 3 * GIB,
            driver_memory_bytes=driver_memory_bytes or 1 * GIB,
        )
    e, d, a, o = profile.engine, profile.driver, profile.app, profile.observability
    components = [
        Component("engine", e.cpuset, e.cpus, engine_memory_bytes or e.memory_bytes),
        Component("driver", d.cpuset, d.cpus, driver_memory_bytes or d.memory_bytes),
    ]
    if with_app_tier:
        components.append(Component("app-tier", a.cpuset, a.cpus, a.memory_bytes))
    if observability is Observability.LIGHT:
        components.append(Component("observability-light", o.cpuset, o.cpus, o.memory_bytes))
    elif observability is Observability.DEEP:
        components.append(
            Component(
                "observability-deep",
                o.cpuset,
                o.cpus * DEEP_OBSERVABILITY_CPUS_FACTOR,
                DEEP_OBSERVABILITY_BYTES,
            )
        )
    return components


def plan_for(
    profile: HostProfile,
    budget: Budget,
    *,
    with_app_tier: bool,
    observability: Observability,
    engine_memory_bytes: int | None = None,
    driver_memory_bytes: int | None = None,
) -> BudgetPlan:
    """Assemble a profile's plan (unchecked)."""
    return BudgetPlan(
        budget=budget,
        components=profile_components(
            profile,
            with_app_tier=with_app_tier,
            observability=observability,
            engine_memory_bytes=engine_memory_bytes,
            driver_memory_bytes=driver_memory_bytes,
        ),
    )
