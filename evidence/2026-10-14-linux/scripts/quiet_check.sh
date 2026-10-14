#!/usr/bin/env bash
# Read-only co-tenant quiet check, recorded before any container-starting step.
# Reads only: /proc, docker stats --no-stream, ps, ss. Changes nothing.
set -u
echo "== quiet check $(date -u +%FT%TZ)"
echo "loadavg: $(cat /proc/loadavg)"
grep -E 'MemAvailable|SwapTotal|SwapFree' /proc/meminfo | tr -s ' '
echo "-- docker stats --no-stream (all running containers)"
docker stats --no-stream --format '{{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}} pids={{.PIDs}}'
echo "-- native co-tenants (ps, cpu% since start / rss)"
ps -C ollama -o pid=,pcpu=,rss=,etime=,comm= 2>/dev/null || echo "ollama: not running"
echo "-- listeners on gateway ports from the brief (8443 API, 9091 gateway Prometheus, 3300)"
ss -ltnH '( sport = :8443 or sport = :9091 or sport = :3300 )' | awk '{print $4}' || true
echo "(none listed = no LLM gateway on this host)"
echo "-- per-CPU busy over 2 s, reserved cores 0-3,8-11 vs dsel cores 4-7,12-15"
read -ra a < <(awk '/^cpu[0-9]/{printf "%s:%s:%s ", $1, $2+$3+$4+$7+$8, $2+$3+$4+$5+$6+$7+$8}' /proc/stat)
sleep 2
read -ra b < <(awk '/^cpu[0-9]/{printf "%s:%s:%s ", $1, $2+$3+$4+$7+$8, $2+$3+$4+$5+$6+$7+$8}' /proc/stat)
for i in "${!a[@]}"; do
  IFS=: read -r n b1 t1 <<<"${a[$i]}"; IFS=: read -r _ b2 t2 <<<"${b[$i]}"
  dt=$((t2 - t1)); [ "$dt" -gt 0 ] && printf "%s=%d%% " "$n" $(( 100 * (b2 - b1) / dt ))
done; echo
