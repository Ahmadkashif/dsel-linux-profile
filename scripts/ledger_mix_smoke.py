#!/usr/bin/env python3
"""Push the `ledger-mix` workload through a host profile, end to end.

    python scripts/ledger_mix_smoke.py --out runs

Provision -> envelope and knob readback -> load -> low-rate ramp ->
`metrics.ndjson` -> write-path check -> label-scoped teardown. Afterwards,
independently of this process:

    uv run dsel watch --replay runs/<run-id> --state-out ... --screen-out ...
    python scripts/rederive_landmarks.py runs/<run-id>/metrics.ndjson --table --thermal

It is a smoke, not a measurement. The default rates (50-400/s) sit far below
what one Postgres core serves, so the ramp is expected to report no knee and no
collapse; what it proves is that every stage of the pipeline runs under the
profile's caps and that the file re-derives what the run reported.

Safety on a shared host, all from the profile:
* the engine is pinned to the engine tier's whole cores, with a quota, memory
  (swap = memory) and pids ceiling, and published on loopback only;
* this process -- the load driver -- pins itself to the driver tier's cores;
* the driver connects to the engine's bridge address rather than the published
  port, so Docker's userland proxy (unpinned, owned by the daemon) carries none
  of the load;
* `dsel budget` arithmetic is checked before anything starts and printed;
* teardown is label-scoped, registered for exit and SIGINT/SIGTERM.
"""

from __future__ import annotations

import argparse
import json
import resource
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for candidate in (REPO_ROOT / "src", REPO_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from dsel.audit.environment import build_manifest, resolve_image  # noqa: E402
from dsel.compose.budget import GIB, Budget, Observability, plan_for  # noqa: E402
from dsel.driver.calibrate import psql  # noqa: E402
from dsel.driver.pool import plan_workers, run_pool  # noqa: E402
from dsel.driver.ramp import Ramp, RampPlan, summarise  # noqa: E402
from dsel.driver.transport import PostgresFactory  # noqa: E402
from dsel.hostprofile import active, pin_current_process  # noqa: E402
from dsel.live.merge import find_shards, merge_records, sort_key  # noqa: E402
from dsel.live.ndjson import ShardWriter, dumps  # noqa: E402
from dsel.live.sampler.containers import ContainerSampler  # noqa: E402
from dsel.live.sampler.host import HostSampler  # noqa: E402
from dsel.runtime.docker import provision, read_back, verify_storage, wait_healthy  # noqa: E402
from dsel.runtime.paths import RunLayout, new_run_id  # noqa: E402
from dsel.runtime.teardown import Teardown, list_managed  # noqa: E402
from dsel.workloads import ledger_mix as lm  # noqa: E402

ENGINE_IMAGE = "postgres:18"
PROBE_IMAGE = "python:3.13-slim"
DATA_DIR = "/var/lib/postgresql/data"
READY = ["pg_isready", "-U", "postgres", "-q"]

# Every knob set explicitly and read back from the running engine.
KNOBS: dict[str, str] = {
    "shared_buffers": "256MB",
    "max_connections": "50",
    "synchronous_commit": "on",
    "fsync": "on",
    "max_wal_size": "1GB",
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {message}", flush=True)


def bridge_address(container: str) -> str:
    out = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
            container,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    if not out:
        raise RuntimeError(f"{container} has no bridge address")
    return out[0]


def copy_accounts(container: str, scale: int) -> None:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "--env",
            "PGPASSWORD=dsel",
            container,
            "psql",
            "-U",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "\\copy ledger_accounts (id, balance) FROM STDIN",
        ],
        input=lm.accounts_copy_text(scale),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"COPY failed: {result.stderr.strip()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "runs")
    parser.add_argument("--rates", default="50,100,200,400", help="offered rates per second")
    parser.add_argument("--duration", type=float, default=15.0, help="seconds per rate")
    parser.add_argument("--warmup", type=float, default=3.0)
    parser.add_argument("--workers", type=int, default=0, help="0 = driver tier width")
    parser.add_argument("--scale", type=int, default=1, help="x100k accounts")
    parser.add_argument("--engine-cpus", type=float, default=2.0)
    parser.add_argument("--engine-memory-gib", type=float, default=2.0)
    args = parser.parse_args(argv)

    profile = active()
    rates = tuple(float(r) for r in args.rates.split(","))
    workers = args.workers or len(profile.driver.cpuset)
    envelope = profile.envelope(
        "engine",
        cpus=args.engine_cpus,
        memory_bytes=int(args.engine_memory_gib * GIB),
        pids_limit=512,
    )

    # The budget first: nothing starts unless the arithmetic says it fits.
    from dsel.audit.environment import capture_daemon

    daemon = capture_daemon()
    plan = plan_for(
        profile,
        Budget.for_profile(profile, ncpu=daemon.ncpu, mem_total_bytes=daemon.mem_total_bytes),
        with_app_tier=False,
        observability=Observability.NONE,
        engine_memory_bytes=envelope.memory_bytes,
    )
    plan.check()
    log(f"profile {profile.name}; budget fits:\n{plan.arithmetic()}")

    pinned = pin_current_process(profile.driver.cpuset)
    log(
        f"driver: this process {'pinned to' if pinned else 'NOT pinned (no affinity call)'} "
        f"{profile.driver.cpuset_spec}, {workers} workers"
    )

    layout = RunLayout.for_run(new_run_id(), base=args.out)
    layout.create()
    run_id = layout.run_id
    teardown = Teardown(run_id).register()
    log(f"run {run_id} -> {layout.root}")
    summary: dict[str, object] = {"run_id": run_id, "profile": profile.name}

    try:
        manifest = build_manifest(resolve_image(PROBE_IMAGE), [], profile)
        layout.manifest.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")

        pin = resolve_image(ENGINE_IMAGE)
        log(f"engine {pin.reference} index {pin.index_digest} platform {pin.platform_digest}")
        command: list[str] = []
        for key, value in KNOBS.items():
            command += ["-c", f"{key}={value}"]
        container = provision(
            pin,
            envelope,
            run_id,
            data_dir=DATA_DIR,
            container_port=5432,
            host_port=free_port(),
            env={"POSTGRES_PASSWORD": "dsel", "PGDATA": f"{DATA_DIR}/pgdata"},
            command=command,
            bind=profile.ports.bind,
        )
        waited = wait_healthy(container, READY)
        log(f"healthy after {waited:.1f}s: {container.name}")

        readback = read_back(container)
        envelope.verify(readback)
        fstype = verify_storage(container)
        knobs_read = {k: psql(container.name, f"SHOW {k}") for k in KNOBS}
        mismatched = {k: v for k, v in knobs_read.items() if v != KNOBS[k]}
        if mismatched:
            raise RuntimeError(f"engine knobs did not read back: {mismatched}")
        log(
            f"readback ok: cpuset {readback.host_cpuset}, cpu.max {readback.cpu_max}, "
            f"memory.max {readback.memory_max}, pids.max {readback.pids_max}, "
            f"nproc {readback.nproc_visible}, data fs {fstype!r}, knobs {knobs_read}"
        )
        summary["readback"] = {
            "cpuset_effective": list(readback.cpuset_effective),
            "host_cpuset": readback.host_cpuset,
            "cpu_max": readback.cpu_max,
            "memory_max": readback.memory_max,
            "memory_swap": readback.host_memory_swap,
            "pids_max": readback.pids_max,
            "nproc_visible": readback.nproc_visible,
            "data_fstype": fstype,
            "knobs": knobs_read,
        }

        psql(container.name, lm.DDL)
        copy_accounts(container.name, args.scale)
        psql(container.name, "ANALYZE")
        opening = int(psql(container.name, "SELECT sum(balance) FROM ledger_accounts"))
        if opening != lm.opening_total(args.scale):
            raise RuntimeError(f"loaded total {opening} != index-derived {lm.opening_total()}")
        log(f"loaded {args.scale * lm.ACCOUNTS_PER_SCALE} accounts, opening total {opening}")

        host = bridge_address(container.name)
        dsn = f"postgresql://postgres:dsel@{host}:5432/postgres"
        factory = PostgresFactory(dsn=dsn, scale=args.scale, statements=lm.STATEMENTS)

        container_writer = ShardWriter(layout.shards, "container-0")
        host_writer = ShardWriter(layout.shards, "host-0")
        containers = ContainerSampler(container_writer, [container.name]).start()
        thermal = HostSampler(host_writer, (*profile.engine.cpuset, *profile.driver.cpuset))
        thermal.start()

        ramp_plan = RampPlan(
            run_dir=layout.root,
            ops=lm.OPS,
            rates_per_s=rates,
            use_case="ledger",
            engine="postgres",
            scenario=lm.NAME,
            duration_s=args.duration,
            warmup_s=args.warmup,
            workers=workers,
            seed=lm.SEED,
        )
        unique_ops = tuple(dict.fromkeys(lm.OPS))
        steps = []
        issued: list[int] = []
        errors = 0
        try:
            for index, rate in enumerate(rates):
                specs = plan_workers(
                    layout.root / f"step{index}",
                    ramp_plan.cell_for(index),
                    lm.OPS,
                    rate_per_s=rate,
                    duration_s=args.duration,
                    workers=workers,
                    warmup_s=args.warmup,
                    seed=lm.SEED + index,
                )
                result = run_pool(specs, factory, ops=unique_ops)
                step = summarise(
                    RampPlan(run_dir=ramp_plan.run_dir, ops=unique_ops, rates_per_s=rates),
                    rate,
                    result,
                )
                steps.append(step)
                issued += [w.issued for w in result.workers]
                errors += result.errors
                log(
                    f"step {index}: offered {rate:.0f}/s achieved "
                    f"{step.achieved_rate_per_s:.1f}/s p50 {step.p50_us:.0f}us p99 "
                    f"{step.p99_us:.0f}us errors {step.errors} worker cpu "
                    f"{step.max_worker_cpu:.0%} {step.verdict}"
                )
        finally:
            containers.stop()
            thermal.stop()
            container_writer.close()
            host_writer.close()

        ramp = Ramp(steps=tuple(steps), ops=unique_ops)
        log("ramp, as the run computed it in memory:\n" + ramp.table())

        # The write path: the engine must hold exactly what the driver says it wrote.
        expected = lm.expected_writes(issued)
        entries = int(psql(container.name, "SELECT count(*) FROM ledger_entries"))
        closing = int(psql(container.name, "SELECT sum(balance) FROM ledger_accounts"))
        write_check = {
            "errors": errors,
            "entries_expected": expected.entries,
            "entries_in_engine": entries,
            "balance_increment_expected": expected.balance_increment,
            "balance_increment_in_engine": closing - opening,
            "holds": errors == 0
            and entries == expected.entries
            and closing - opening == expected.balance_increment,
        }
        log(f"write-path check: {write_check}")

        records = list(merge_records(find_shards(layout.shards)))
        for index in range(len(rates)):
            records.extend(merge_records(find_shards(layout.root / f"step{index}" / "shards")))
        records.sort(key=sort_key)
        layout.metrics.write_text("".join(dumps(r) + "\n" for r in records), encoding="utf-8")
        log(f"{len(records)} records -> {layout.metrics}")

        usage_self = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        usage_children = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        summary |= {
            "engine": {
                "image": pin.reference,
                "index_digest": pin.index_digest,
                "platform_digest": pin.platform_digest,
                "cpuset": envelope.cpuset_spec,
                "cpus": envelope.cpus,
                "memory_bytes": envelope.memory_bytes,
                "pids_limit": envelope.pids_limit,
            },
            "driver": {
                "cpuset": profile.driver.cpuset_spec,
                "pinned": pinned,
                "workers": workers,
                "peak_rss_kib_supervisor": usage_self,
                "peak_rss_kib_largest_worker": usage_children,
            },
            "workload": {
                "name": lm.NAME,
                "ops": list(lm.OPS),
                "statements": dict(lm.STATEMENTS),
                "seed": lm.SEED,
                "rates": list(rates),
                "duration_s": args.duration,
                "warmup_s": args.warmup,
            },
            "landmarks_in_memory": {
                "knee_rate_per_s": ramp.knee_rate_per_s,
                "collapse_rate_per_s": ramp.collapse_rate_per_s,
                "max_sustainable_rate_per_s": ramp.max_sustainable_rate_per_s,
            },
            "steps": [
                {
                    "offered": s.offered_rate_per_s,
                    "achieved": s.achieved_rate_per_s,
                    "completed": s.completed,
                    "errors": s.errors,
                    "p50_us": s.p50_us,
                    "p99_us": s.p99_us,
                    "verdict": s.verdict,
                    "max_worker_cpu": s.max_worker_cpu,
                }
                for s in steps
            ],
            "write_check": write_check,
            "records": len(records),
        }
    finally:
        teardown.run()
        left = {kind: list_managed(run_id, kind) for kind in ("container", "volume", "network")}
        summary["leftover_after_teardown"] = left
        (layout.root / "smoke-summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        log(f"teardown done; leftovers by label {left}")
    return 0 if summary.get("write_check", {}).get("holds") else 1  # type: ignore[union-attr]


if __name__ == "__main__":
    raise SystemExit(main())
