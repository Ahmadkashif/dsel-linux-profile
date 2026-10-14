#!/usr/bin/env bash
# Q9b: does MongoDB 8 start on this kernel? One throwaway container, no host change.
# Engine-tier cores, 1 GiB memory (swap = memory), pids-limit 512, no published
# ports, labelled com.dsel.managed=true, removed by label at the end.
set -u
DIGEST=sha256:d0c8f51bb63fb1f992f4ff05293adb3f0967aaaae32c3e5fa567907800819b3b   # mongo:8 index, pulled 2026-10-14
RUN=q9b-$(date -u +%Y%m%dT%H%M%SZ)
NAME=dsel-$RUN-mongo
echo "== $(date -u +%FT%TZ) kernel $(uname -r) run $RUN"
docker run --detach --name "$NAME" \
  --label com.dsel.managed=true --label com.dsel.run="$RUN" \
  --cpuset-cpus 4-5,12-13 --cpus 1 --memory 1073741824 --memory-swap 1073741824 \
  --pids-limit 512 "mongo@$DIGEST" >/dev/null
for i in $(seq 1 30); do
  state=$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' "$NAME")
  case "$state" in exited*) break;; esac
  if docker exec "$NAME" mongosh --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | grep -q '^1$'; then
    state="running-and-answering-ping"; break
  fi
  sleep 1
done
echo "state after ${i}s: $state"
echo "-- docker logs (FATAL / kernel / SERVER-121912 lines; last 15 lines otherwise)"
docker logs "$NAME" 2>&1 | grep -Ei 'fatal|kernel|121912|refus|abort' | head -20
echo "-- tail"
docker logs "$NAME" 2>&1 | tail -5 | cut -c1-400
# The image declares /data/db and /data/configdb as VOLUMEs; Docker creates them
# anonymous and unlabelled, so they are removed with their container (-v), which
# is the only scope that names them without touching anything else.
VOLS=$(docker inspect -f '{{range .Mounts}}{{.Name}} {{end}}' "$NAME")
docker rm --force --volumes $(docker ps -aq --filter label=com.dsel.run="$RUN") >/dev/null
echo "leftover containers for $RUN: $(docker ps -aq --filter label=com.dsel.run="$RUN" | wc -l)"
left=0; for v in $VOLS; do docker volume inspect "$v" >/dev/null 2>&1 && left=$((left+1)); done
echo "leftover anonymous volumes of that container: $left"
