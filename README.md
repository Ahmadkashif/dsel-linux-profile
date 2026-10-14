# dsel

**Which datastore should we build this on?** Describe the workload; `dsel` provisions each
candidate in Docker, drives real load at it, and shows where each one bends and where it
breaks — with the raw evidence to re-check every number it prints.

<!-- honesty:begin -->
## What this harness will and will not claim

`dsel` produces **mechanisms, relationships and scaling curves** — where the knee is, how
throughput moves as connections rise, what saturates first and why. It does **not** produce
reportable capacity numbers. Every run on this machine is stamped `profile=local`,
`envelope_deviation=true` and `reportable=false`, and `dsel verify` refuses to sign a report
carrying `reportable=false`. A claim of the form "this serves N orders per second" is not
something this build will emit, because that number would not be true on any other hardware.

That refusal is enforced by code, not by discipline. The target machine is a single Apple
M5 laptop running Docker Desktop: cores are shared between the engine, the load driver, the
app tier and the observability stack, the filesystem is a VM's, and at least one candidate
engine cannot start here at all. Those are the conditions under which mechanisms remain
learnable and absolute numbers do not. What transfers to real hardware is the shape of the
curve and the identity of the bottleneck — carried across by changing a resource envelope,
not by rewriting the harness.
<!-- honesty:end -->

## Why it exists

Choosing a datastore is normally done from vendor benchmarks, blog posts and the opinion of
whoever argues hardest. All three are unfalsifiable: you cannot re-run them, they were not
measured on your workload, and nothing in them says when they stop being true.

`dsel` is the other thing. It is a **decision harness, not a benchmark suite** — it answers
"which of {X, Y, Z} should we build on?", so it optimises for fair, comparable, *ordinal*
numbers rather than absolute peaks. Everything it reports is reproducible from an audit
bundle by someone who was not there.

The hard part is not generating load. It is **not fooling yourself while you do it**, and
most of this codebase is that. A sample of what the acceptance criteria caught in the
build, each of which would otherwise have silently corrupted a result:

| What looked fine | What was actually happening |
|---|---|
| A clean 15% throughput drop as connections rose | The laptop warming up. Rung order was a proxy for elapsed time |
| pgbench's reported latency | 88% of it was pgbench waiting for its own scheduler |
| Sub-millisecond latencies from the driver | `time.sleep` overshoots ~50% here, adding 3.7 ms to every measurement |
| The app tier's own cost, measured at 1 µs | The span started after the framework had already done its work |
| A saturation gate that opened and closed | CPU sampled after each step ended, mixing load with idle |
| Percentiles matching a reference implementation | They didn't — until compared at cumulative count instead of nominal percentile |

Nothing above was found by review. Each was found because a phase's acceptance criterion
was run as a command and failed.

## How it works

`GATE → MEASURE → WEIGH`

1. **Gate.** A candidate that fails a hard requirement — transactional scope, durability,
   query capability, licence policy, environment feasibility — is excluded and *never
   benchmarked*. Exclusion is not a low score.
2. **Measure.** Survivors × scenarios × rates × repeats, frozen up front. Each cell:
   provision → health-gate → load → run → collect → teardown, every phase emitting
   evidence. Open-loop load, HdrHistogram latency, validity gates that **invalidate rather
   than caveat**.
3. **Weigh.** Reference-anchored normalisation (never min-max), explicit vetoes, attributed
   weights, a sensitivity sweep. Measured and human-judged criteria are scored separately
   and rendered apart, and a human signs the decision — the harness never picks the winner
   on its own.

## Getting started

**Prerequisites**

- [uv](https://docs.astral.sh/uv/) — the only one you install; it fetches Python 3.13 itself
- Docker Desktop, running, for anything that provisions an engine
- `docker login` — anonymous Docker Hub pull limits will otherwise bite partway through a run

```bash
uv sync --frozen          # environment from the committed lock
uv run dsel --version     # version, git commit, dirty flag
uv run dsel --help        # the whole subcommand tree
uv run pytest tests/unit -q
```

`tests/unit` needs nothing but Python and runs in about two seconds.

**With Docker running:**

```bash
uv run dsel budget                 # does this machine fit the plan? shows the arithmetic
uv run dsel env --no-probe         # capture host, daemon and VM; write the run manifest
uv run pytest tests/integration -q # provisions real containers; several minutes
```

**Watch a run, live or after the fact:**

```bash
uv run dsel watch --run runs/<run-id>      # live terminal dashboard
uv run dsel watch --replay runs/<run-id>   # same screen, from the finished run
```

**Re-derive the findings from the evidence, with no daemon and no harness state:**

```bash
python scripts/rederive_landmarks.py runs/<run-id>/metrics.ndjson --table
python scripts/analyse_backend_growth.py runs/<run-id>/metrics.ndjson --table
```

That last pair is the point of the whole design. If a number cannot be recovered from the
file, it was never evidence — it was something held in the memory of the process that
produced it, and nobody outside that process could ever check it.

## What works today

Built and accepted, against Postgres 18:

| Area | State |
|---|---|
| Environment capture, vCPU probe, cross-cpuset interference sweep | ✅ `dsel env` |
| Pre-flight budget: cpuset and memory arithmetic before anything starts | ✅ `dsel budget` |
| Provisioning, health gate, envelope readback, label-scoped teardown | ✅ |
| `metrics.ndjson` schema, sharded writers, deterministic merge | ✅ committed JSON Schema, drift-checked |
| Live TUI, and a replay that lands on the identical screen | ✅ `dsel watch` |
| Prometheus + Grafana, four dashboards, enforced series budget | ✅ `observability/` |
| Open-loop driver, verified against the Java HdrHistogram reference | ✅ |
| Rate ramp; calibration against `pgbench -R` | ✅ |
| FastAPI app tier, its saturation ceiling, and driver→app→DB (PATH B) | ✅ |
| Phenomena re-derived from `metrics.ndjson` alone | ✅ `scripts/` |
| Connection ramp and refusal attribution | ✅ |

Not built yet — these subcommands exit non-zero and name the phase that will build them,
rather than exiting 0 and doing nothing:

| Command | Waiting on |
|---|---|
| `dsel gate` | G1 — capability tables, SPDX per digest, environment feasibility |
| `dsel plan` | S6 — freeze the run matrix from a workload spec |
| `dsel run` | S14 — needs an adapter and a spec to drive |
| `dsel score` / `dsel report` | G2 — scoring and the decision report |
| `dsel verify` | G3 — audit bundle verification |

Also outstanding: **S16–S18(b)**, the ≥1 h memory-growth soak — the machinery is built and
unit-tested but the hour-long acceptance run has not been completed. Valkey, ClickHouse,
OpenSearch and MongoDB adapters (W1–W4) are not started. `PLAN.md` is the source of truth
for phase status; a phase is finished only when its `Accept:` line has been run as a
command and passed.

## Layout

| Path | What it is |
|---|---|
| `src/dsel/` | The harness. `live/` records, `phenomena/` derives, and the two never mix |
| `tests/unit/` | No Docker, no network, ~2 s |
| `tests/integration/` | Real containers; marked `slow`, skipped when Docker is absent |
| `scripts/` | Standalone derivation from a metrics file — no daemon, no harness state |
| `observability/` | Prometheus config and the four provisioned Grafana dashboards |
| `images/` | Digest-pinned Dockerfiles for the load driver and the app tier |
| `schema/` | The committed `metrics.ndjson` JSON Schema, CI drift-checked |
| `PLAN.md` | The build plan: locked decisions, phases, acceptance criteria, and what each one actually measured |
| `CLAUDE.md` | The invariants. Read before changing anything |
| `datastore-selection-automation/` | The completed research this is built from. `findings.md` is the specification; `phase-3/` is its verification evidence |

## The rules that do not bend

Each was demonstrated by experiment, not argued, and violating one silently invalidates
results. `CLAUDE.md` has the full list with its evidence; the ones that most often surprise
people:

- **Gate before measuring.** A candidate that fails a hard requirement is never benchmarked.
- **Named volumes only.** Bind mounts raised run-to-run variance from 1.3% to 18% and
  reported 8.19 kB for 6.44 GB of writes.
- **Pin the OCI *index* digest; record the resolved platform digest.** Platform digests are
  architecture-locked and make one spec unrunnable across a dev Mac and amd64 CI.
- **Set every knob explicitly, then read it back from the running engine.** No Docker flag
  combination makes CPU and memory detection agree across APIs.
- **Reference-anchored normalisation, never min-max.** Min-max reversed the winner when an
  irrelevant candidate was added.
- **Open loop, always, for any latency claim.** Store raw histograms, never just summaries.
- **Invalidate rather than report with a caveat.**
- **The dev machine cannot produce reportable numbers.** Linux CI is the only source of
  results; this laptop is for validating the pipeline.

## Contributing

`uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest tests/unit -q`

British spelling throughout. Absolute dates, never relative. Every version and licence claim
carries the date it was verified. A claim is backed by a log line or explicitly marked
**UNVERIFIED** — do not soften a claim the logs support, and do not harden one that is
marked unverified.
