# Evidence: the Linux host profile, 2026-10-14

Captured on a shared bare-metal Linux host (Intel i7-10700K, 8 cores / 16 threads, 31.06 GiB,
Ubuntu 24.04.5, kernel 7.0.0-34, cgroup v2, Docker Engine 29.8.1). Every file is the output of
the command written at its top. Nothing on the host was changed; every container dsel started
was capped, labelled `com.dsel.managed=true` and removed by that label.

**Redacted for the public copy:** home-directory paths (`~`, `<topic>`, `<repo>`), the local
user name (`<user>`), the docker bridge gateway address (`<docker-bridge-gateway>`), and the
process and container listings of the host's other workloads. Nothing else was edited.

| File | What it shows |
|---|---|
| `toolchain/01..03-*.log` | Fresh clone at 32b167b: `uv sync --frozen`, ruff, ruff format, mypy green; `pytest tests/unit` **1 failed, 234 passed** (macOS errno numbers) |
| `toolchain/11-toolchain-final.log` | The branch head: ruff, ruff format, mypy --strict clean, **298 passed** |
| `host/10-host-hw.log` | `lscpu -e`, meminfo, kernel, cgroup, THP, governor, `vm.max_map_count` (1048576), root fs |
| `host/11-docker.log`, `host/12-cotenants.log` | `docker info`, free ports (19090/13000/19464), co-tenant services |
| `live/01-dsel-profile.*` | Detection: minimum-spec checks and the tiers mapped from `lscpu -e` |
| `live/02-dsel-budget.txt` | The allowance budget: default fits (10.50G/12.00G); deep observability and a 10 GiB engine **refused with the arithmetic** |
| `live/03,05b,06,07-quiet-check*.txt` | Read-only co-tenant check before each container-starting step |
| `live/04-image-pulls.txt` | Anonymous pulls and the index digests recorded |
| `live/05a-*` | First `dsel env`: a false `heterogeneous_cores` (49.6%) from comparing set totals; fixed in a0ea142 |
| `live/05b-*` | `dsel env` manifest: profile, `reportable=false`, deviation reasons, host facts, vCPU probe on dsel cores only |
| `live/06-q9b-mongo8.txt` | MongoDB 8 refuses kernel 7.0 (SERVER-121912) in one throwaway container |
| `live/07-*` | The `ledger-mix` smoke: plan, run log (readback, ramp, write-path check, teardown), per-op latency |
| `live/08-tui-replay*` | `dsel watch --replay` final state and screen |
| `live/09a/09b-rederive.txt` | Re-derivation from `metrics.ndjson` before and after the multi-op fix (d49ec3c) |
| `live/10-leftovers.txt` | Zero dsel containers, networks, volumes; images kept and their sizes |
| `run-20261014T190715Z-fea3b5/` | The smoke's run directory: manifest, `metrics.ndjson` (543 records), shards, `.hlog` histograms, summary |
| `scripts/` | The read-only quiet check and the MongoDB 8 check, as run |

## Commit hashes in these files

The evidence was captured in a local clone whose history continued upstream's. This public
repository imports upstream as one commit and replays the same changes on top, so the trees
are identical but the hashes differ. Hashes printed inside the evidence map as follows:

| In the evidence (local) | In this repository | Change |
|---|---|---|
| `3e9507e` | `6521f2d` | platform digest by index digest; `dsel profile`, `dsel budget`, first `dsel env` ran here |
| `a0ea142` | `7d3c6e5` | per-vCPU heterogeneity; the second `dsel env` ran here |
| `f3e7055` | `6e7a0fe` | `ledger-mix` added; **the smoke ran here** (its `manifest.json` says `harness_commit: f3e7055`, `harness_dirty: false`) |
| `d49ec3c` | `20a2721` | multi-op re-derivation fix; `09b-rederive.txt` and the TUI replay ran here or at `f3e7055` as stated in each file |
| `3d62246` | `e5d851f` | `toolchain/11-toolchain-final.log` |
