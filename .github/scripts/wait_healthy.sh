#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Wait for one compose service's container to report healthy.
#
#   wait_healthy.sh <service> [seconds]
#
# `docker compose up -d` returns when the container is CREATED, not when
# the process inside it is answering. A step that read the environment
# straight after `up` would pass on a container that then crash-looped,
# which is usually the thing the caller is testing for.
#
# The container is found by compose's OWN label rather than by
# `docker compose ps`, and that is load-bearing: `compose.yaml`
# `include:`s `agents.compose.yaml`, whose services interpolate
# `${LIBRERUN_AGENT_KEY_<ID>:?}`. Compose resolves that at PARSE time for
# every subcommand, `ps` included and whatever profiles are selected, so
# on a checkout whose `.env` carries no agent keys — a plain
# `cp .env.example .env`, which is exactly what one caller is testing —
# `docker compose ps` exits with an interpolation error and this wait
# would fail for a reason that has nothing to do with the service.
# `docker ps --filter label=…` asks the daemon instead and parses no
# compose file at all.
#
# Exits non-zero with the container's log tail if it never becomes
# healthy, or if it exits first. A service with no healthcheck is an
# error rather than an instant pass: "no health to report" must not read
# as healthy.
set -euo pipefail

service="${1:?usage: wait_healthy.sh <service> [seconds]}"
deadline=$(( $(date +%s) + ${2:-120} ))
state=unknown
health=unknown

while :; do
    id="$(docker ps -aq --filter "label=com.docker.compose.service=${service}" | head -1)"
    if [ -n "$id" ]; then
        state="$(docker inspect -f '{{.State.Status}}' "$id" 2>/dev/null || echo missing)"
        health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id" 2>/dev/null || echo none)"
        if [ "$health" = "none" ]; then
            echo "::error::$service declares no healthcheck, so this wait cannot tell running from working"
            exit 2
        fi
        if [ "$health" = "healthy" ]; then
            echo "$service is healthy"
            exit 0
        fi
        if [ "$state" = "exited" ] || [ "$state" = "dead" ]; then
            echo "::error::$service is $state before ever becoming healthy"
            docker logs --tail 80 "$id" 2>&1 || true
            exit 1
        fi
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "::error::$service never became healthy (state=$state health=$health)"
        [ -n "${id:-}" ] && docker logs --tail 80 "$id" 2>&1 || true
        exit 1
    fi
    sleep 3
done
