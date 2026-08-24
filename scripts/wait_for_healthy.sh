#!/usr/bin/env bash
#
# Polls `docker compose ps` until every started service is either healthy
# or has exited 0 (the latter is expected for the one-shot `migrate`
# service), or until WAIT_TIMEOUT_SECONDS elapses.

set -euo pipefail

TIMEOUT="${WAIT_TIMEOUT_SECONDS:-180}"
INTERVAL=3
elapsed=0

echo "Waiting up to ${TIMEOUT}s for services to become healthy..."

while true; do
  status_json="$(docker compose ps --format json)"

  not_ready="$(echo "${status_json}" | python3 -c '
import json
import sys

raw = sys.stdin.read()
services = []
try:
    parsed = json.loads(raw)
    services = parsed if isinstance(parsed, list) else [parsed]
except json.JSONDecodeError:
    for line in raw.splitlines():
        line = line.strip()
        if line:
            services.append(json.loads(line))

not_ready = []
for svc in services:
    name = svc.get("Service", svc.get("Name", "unknown"))
    state = svc.get("State", "")
    health = svc.get("Health", "")
    exit_code = svc.get("ExitCode", 0)

    if state == "exited":
        if exit_code == 0:
            continue
        not_ready.append(f"{name} (exited with code {exit_code})")
        continue

    if health in ("", "healthy"):
        continue

    not_ready.append(f"{name} (health={health or \"none\"}, state={state})")

print("\n".join(not_ready))
')"

  if [ -z "${not_ready}" ]; then
    echo "All services healthy (or exited 0, as expected for one-shot jobs)."
    exit 0
  fi

  if [ "${elapsed}" -ge "${TIMEOUT}" ]; then
    echo "Timed out after ${TIMEOUT}s waiting for:"
    echo "${not_ready}"
    docker compose ps
    exit 1
  fi

  sleep "${INTERVAL}"
  elapsed=$((elapsed + INTERVAL))
done
