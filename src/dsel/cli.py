"""The `dsel` command line.

The full subcommand tree is declared from the first phase so the shape of the
harness is visible, but an unimplemented subcommand exits non-zero naming the
phase that will build it. A stub that exits 0 would be a command that lies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from dsel.hostprofile import HostProfile
from dsel.version import provenance

app = typer.Typer(
    name="dsel",
    help="Datastore selection decision harness: gate, measure, weigh.",
    no_args_is_help=True,
    add_completion=False,
)

# Subcommand -> the phase of PLAN.md that implements it. Kept honest as phases
# land: `run` needs an adapter and a workload spec to drive, which arrive at
# S13/S14, so pointing it at S10 once the driver exists would be a lie.
_PENDING: dict[str, str] = {
    "gate": "G1",
    "plan": "S6",
    "run": "S14",
    "score": "G2",
    "verify": "G3",
    "report": "G2",
}


def _require_docker() -> None:
    """Fail with a sentence rather than a stack trace.

    `env` and `budget` are the two commands a new reader is most likely to run
    first, and both need the daemon. A traceback here reads as "the harness is
    broken" when the actual state is "Docker Desktop is not started".
    """
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        typer.secho(
            "docker is not on PATH. Install Docker Desktop: https://docs.docker.com/desktop/",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    probe = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        typer.secho(
            "the Docker daemon is not responding. Start Docker Desktop and try "
            "again; this command reads the VM's own CPU and memory limits.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)


def _not_implemented(command: str) -> None:
    phase = _PENDING[command]
    typer.secho(
        f"dsel {command}: not implemented (phase {phase})",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=2)


def _profile(name: str | None) -> HostProfile:
    """The named profile, or the detected one. Refusals are sentences, not traces."""
    from dsel.hostprofile import ProfileError, active
    from dsel.hostprofile.detect import choose

    try:
        return choose(forced=name).profile if name else active()
    except ProfileError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(str(provenance()))
        raise typer.Exit(code=0)


@app.callback()
def root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Print the harness version, git commit and dirty flag.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Datastore selection decision harness."""


@app.command()
def env(
    image: Annotated[
        str,
        typer.Option(help="Probe image; pinned to its index digest before use."),
    ] = "python:3.13-slim",
    probe: Annotated[
        bool,
        typer.Option("--probe/--no-probe", help="Run the vCPU speed probe."),
    ] = True,
    repeats: Annotated[int, typer.Option(help="Probe repeats per vCPU.")] = 6,
    interference: Annotated[
        bool,
        typer.Option(
            "--interference/--no-interference",
            help="Sweep cross-cpuset interference (~2 min). Both directions.",
        ),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option(help="Write the manifest here instead of stdout."),
    ] = None,
    profile_name: Annotated[
        str | None,
        typer.Option("--profile", help="local | linux-shared-8c16t (default: detect)."),
    ] = None,
) -> None:
    """Capture the environment and probe per-vCPU speed (PLAN.md S1)."""
    _require_docker()
    from dsel.audit.environment import build_manifest, resolve_image
    from dsel.audit.interference import reasons_for, sweep, to_record
    from dsel.audit.vcpu_probe import run_probe

    profile = _profile(profile_name)
    shared = profile.allowance_memory_bytes is not None
    # On a shared host every probe container is capped and labelled like any
    # other dsel container; on `local` the probes run exactly as before.
    caps: tuple[str, ...] = (
        (
            "--memory",
            "512m",
            "--memory-swap",
            "512m",
            "--pids-limit",
            "256",
            "--label",
            "com.dsel.managed=true",
            "--label",
            "com.dsel.run=env",
        )
        if shared
        else ()
    )
    pin = resolve_image(image)
    reasons: list[str] = []
    probe_result = None
    if probe:
        if shared:
            probe_result, reasons = run_probe(
                pin,
                repeats=repeats,
                cpuset=profile.probe_cpuset,
                engine_cpuset=profile.engine.cpuset,
                driver_cpuset=profile.driver.cpuset,
                caps=caps,
            )
        else:
            probe_result, reasons = run_probe(pin, repeats=repeats)

    records = []
    if interference:
        measured, contended = profile.interference_pair
        sweeps = [
            sweep(pin, measured, contended, caps=caps),
            sweep(pin, contended, measured, caps=caps),
        ]
        reasons += reasons_for(sweeps)
        records = [to_record(s) for s in sweeps]

    manifest = build_manifest(pin, reasons, profile if shared or profile_name else None)

    updates: dict[str, object] = {}
    if probe_result is not None:
        updates["vcpu_probe"] = probe_result
    if records:
        updates["cpuset_interference"] = records
    if updates:
        manifest = manifest.model_copy(update=updates)
    payload = manifest.model_dump_json(indent=2)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
        typer.echo(f"wrote {out}")
    else:
        typer.echo(payload)


@app.command()
def budget(
    app_tier: Annotated[bool, typer.Option("--app-tier/--no-app-tier")] = True,
    observability: Annotated[str, typer.Option(help="none | light | deep")] = "light",
    profile_name: Annotated[
        str | None,
        typer.Option("--profile", help="local | linux-shared-8c16t (default: detect)."),
    ] = None,
    engine_memory_gib: Annotated[
        float | None, typer.Option(help="Ask for this much engine memory instead.")
    ] = None,
    driver_memory_gib: Annotated[
        float | None, typer.Option(help="Ask for this much driver memory instead.")
    ] = None,
) -> None:
    """Check a configuration against the machine before anything starts (S2).

    On `local` the budget is the Docker Desktop VM. On a shared host it is the
    profile's dsel allowance -- not `docker info`, which there is the whole host.
    """
    _require_docker()
    from dsel.audit.environment import capture_daemon
    from dsel.compose.budget import GIB, Budget, BudgetError, Observability, plan_for

    profile = _profile(profile_name)
    daemon = capture_daemon()
    typer.echo(f"profile: {profile.name}")
    plan = plan_for(
        profile,
        Budget.for_profile(profile, ncpu=daemon.ncpu, mem_total_bytes=daemon.mem_total_bytes),
        with_app_tier=app_tier,
        observability=Observability(observability),
        engine_memory_bytes=int(engine_memory_gib * GIB) if engine_memory_gib else None,
        driver_memory_bytes=int(driver_memory_gib * GIB) if driver_memory_gib else None,
    )
    try:
        plan.check()
    except BudgetError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(plan.arithmetic())
    typer.secho("\nfits", fg=typer.colors.GREEN)


@app.command()
def profile(
    profile_name: Annotated[
        str | None,
        typer.Option("--profile", help="Force a profile instead of detecting one."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Detect the host profile, read-only, and show its minimum-spec checks and tiers."""
    import dataclasses
    import json

    from dsel.hostprofile import ProfileError
    from dsel.hostprofile.detect import choose

    try:
        detection = choose(forced=profile_name)
    except ProfileError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    if as_json:
        payload = {
            "profile": detection.profile.name,
            "reportable": detection.profile.reportable,
            "checks": [dataclasses.asdict(c) for c in detection.checks],
            "tiers": {
                t.name: {
                    "cpuset": t.cpuset_spec,
                    "cpus": t.cpus,
                    "memory_bytes": t.memory_bytes,
                    "pids_limit": t.pids_limit,
                }
                for t in detection.profile.tiers
            },
            "reserved_cpuset": list(detection.profile.reserved_cpuset),
            "facts": {
                k: v for k, v in dataclasses.asdict(detection.facts).items() if k != "topology"
            },
            "topology": {str(k): list(v) for k, v in detection.facts.topology.items()},
        }
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        typer.echo(detection.table())


@app.command()
def gate() -> None:
    """Exclude candidates that fail a hard requirement, before any container starts."""
    _not_implemented("gate")


@app.command()
def plan() -> None:
    """Freeze the run matrix: survivors x scenarios x rates x repeats."""
    _not_implemented("plan")


@app.command()
def run() -> None:
    """Provision, load, measure and tear down each cell of the run matrix."""
    _not_implemented("run")


@app.command()
def score() -> None:
    """Reference-anchored normalisation, vetoes, weighting, sensitivity."""
    _not_implemented("score")


@app.command()
def verify() -> None:
    """Verify an audit bundle; refuse to sign anything stamped reportable=false."""
    _not_implemented("verify")


@app.command()
def report() -> None:
    """Render the decision report, measured and judged criteria kept apart."""
    _not_implemented("report")


@app.command()
def watch(
    replay: Annotated[
        Path | None,
        typer.Option(help="Replay a finished run directory instead of tailing a live one."),
    ] = None,
    run: Annotated[
        Path | None, typer.Option(help="Run directory to tail while it is being written.")
    ] = None,
    idle_timeout: Annotated[
        float | None, typer.Option(help="Exit after this many idle seconds (live mode).")
    ] = None,
    state_out: Annotated[
        Path | None,
        typer.Option(help="Write the final screen state as canonical JSON (S8a check)."),
    ] = None,
    screen_out: Annotated[
        Path | None,
        typer.Option(help="Write the final rendered screen as plain text (S8a check)."),
    ] = None,
    prometheus_port: Annotated[
        int | None,
        typer.Option(help="Also expose the run's metrics for Prometheus on this port."),
    ] = None,
) -> None:
    """Live TUI over the run's metrics stream; --replay re-runs a finished run."""
    import json

    from dsel.live.state import snapshot
    from dsel.live.tui import screen_text, watch_live, watch_replay

    target = replay or run
    if target is None:
        typer.secho("give --replay <run-dir> or --run <run-dir>", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    if not target.is_dir():
        typer.secho(f"{target} is not a directory", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    if prometheus_port is not None:
        # A second consumer of the same stream, never a second sampling path (D3).
        from dsel.live.exporter import serve

        addr = _profile(None).ports.exporter_bind
        serve(target, prometheus_port, addr=addr)
        typer.echo(f"exporting on http://{addr or '127.0.0.1'}:{prometheus_port}/metrics")
    state = watch_replay(target) if replay else watch_live(target, idle_timeout_s=idle_timeout)
    # Both dumps come from the same functions the screen uses, so a diff of two
    # sessions is a diff of what was actually shown (PLAN.md S8a).
    if state_out is not None:
        state_out.write_text(
            json.dumps(snapshot(state), sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
    if screen_out is not None:
        screen_out.write_text(screen_text(state), encoding="utf-8")
    typer.echo(
        f"\nfinal: {state.records_seen:,} records, "
        f"{len(state.ops)} ops, {len(state.containers)} containers, "
        f"worst verdict {state.worst_verdict}"
    )


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
