#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
# Run Alembic migrations before starting the app.
# This ensures the app_settings table (and any future migrations) exist
# in both development and staging/production container deployments.
set -e

# Self-heal the ./data/logs bind mount, then drop privileges. Docker
# creates a missing bind-mount directory as root:root, so on every
# fresh checkout the non-root 'librerun' user could not write the JSON
# log file and the app silently degraded to stderr-only logging until
# an operator chown'd the host directory by hand. The compose
# deployment therefore starts this container as root (user: "0:0" in
# compose.yaml — the image itself defaults to USER librerun), the branch
# below fixes ownership of the mount, and the script immediately
# re-execs itself as 'librerun' — the standard pattern the official
# postgres/redis images use. Everything below the re-exec
# (migrations, bootstrap, uvicorn) runs as 'librerun', exactly as before.
if [ "$(id -u)" = "0" ]; then
    mkdir -p /app/data/logs /app/data/state
    # The state volume (agents' file-shaped state, blueprint S2) may hold
    # files an operator copied in as root — `docker cp` copies as root —
    # so it gets the same ownership repair as the logs.
    chown -Rh librerun:librerun /app/data/state 2>/dev/null || true
    # -R: repair FILES too, not just the directory — deployments that
    # previously ran with Podman's ':U' mount flag (or any run that
    # wrote as root) can hold a root-owned backend.jsonl that would
    # survive a directory-only chown and still block librerun's append.
    # -h keeps the recursion from ever dereferencing a symlink.
    chown -Rh librerun:librerun /app/data/logs 2>/dev/null || true
    # 0755, not 0750: this is a HOST-SHARED bind mount — compose.yaml
    # promises operators they can tail ./data/logs/*.jsonl from the
    # host (e.g. a Cribl Edge agent), and those readers do not share
    # librerun's numeric uid/gid. Write access stays librerun-only; the files
    # are PII-redacted before anything is written.
    chmod 0755 /app/data/logs 2>/dev/null || true
    # The HTTPS edge's control volume (K blueprint T2), mounted in the
    # `edge-control` service alone — the backend never mounts it (L42, D44
    # refined). The directory, not its files: edge-control replaces a file
    # by renaming a new one over it, which the directory's owner may do,
    # and the edge, which runs as root, reads whatever it writes.
    if [ -d /control ]; then
        chown librerun:librerun /control 2>/dev/null || true
    fi
    # Both re-exec paths below preserve the environment (setpriv changes
    # only credentials; su -p is explicitly env-preserving), so HOME
    # would stay '/root' after the drop. The old `USER librerun` Dockerfile
    # directive used to set HOME=/app from librerun's passwd entry, and
    # libraries resolve '~' against $HOME: asyncpg stats
    # ~/.postgresql/postgresql.key while parsing the DSN, which as
    # librerun-with-HOME=/root raises PermissionError and kills every DB
    # connection (migrations, bootstrap, logins). Restore librerun's real
    # home (and identity vars) explicitly before dropping.
    export HOME=/app USER=librerun LOGNAME=librerun
    if command -v setpriv >/dev/null 2>&1; then
        exec setpriv --reuid=librerun --regid=librerun --init-groups "$0" "$@"
    fi
    # Fallback for images without util-linux; -p preserves the process
    # environment (APP_*, DATABASE_URL, ... must survive the drop).
    exec su -p -s /bin/sh librerun -c 'exec "$0" "$@"' -- "$0" "$@"
fi

# Command dispatch — the image wires this script as ENTRYPOINT with
# CMD "serve". The default falls through to migrations + bootstrap +
# uvicorn below; any other argv (`docker run <image> alembic ...`, a
# compose `command:`, Kubernetes args) is exec'd verbatim here,
# already demoted to 'librerun' by the branch above.
if [ "$#" -gt 0 ] && [ "$1" != "serve" ]; then
    exec "$@"
fi

echo "Running database migrations..."
cd /app
# A failed migration stops the container (K blueprint T1). It used to be
# swallowed ("DB may not be ready yet. Continuing..."), which booted a
# backend on a stale schema that every table since 0002 meets at request
# time. Compose already holds this container until Postgres is healthy, and
# `restart: unless-stopped` turns the exit into a retry; the error is above.
if ! alembic upgrade head 2>&1; then
    echo "ERROR: Alembic migrations failed; the backend does not start on a stale schema." >&2
    exit 1
fi

# Idempotent admin/customer bootstrap. Reads INITIAL_ADMIN_* and
# INITIAL_USER_* env vars; either pair blank => skipped silently. The
# script always overwrites the password hash from the env value, so
# rotating a password is just a .env edit + container restart. We don't
# fail the container on a non-zero exit because it would block uvicorn
# from starting if the DB hadn't fully come up yet — the script's own log
# events make it obvious what happened.
echo "Bootstrapping admin/customer users (if configured)..."
python -m app.scripts.bootstrap_admin || echo "WARNING: bootstrap_admin exited non-zero. Continuing..."

# Best-effort residual: normally the root branch above has already
# fixed ownership before dropping to 'librerun'. This remains for runs that
# never start as root (a compose `user:` override, dev-style
# invocations) — there mkdir/chmod may fail with EPERM, which must not
# kill the container ('set -e' is active): the app itself falls back to
# stderr-only logging and prints a log_file_unwritable warning.
echo "Ensuring log directory exists..."
mkdir -p /app/data/logs 2>/dev/null || true
chmod 0755 /app/data/logs 2>/dev/null \
  || echo "WARNING: could not chmod /app/data/logs (root-owned bind mount?). Continuing..."

echo "Starting uvicorn..."
# --proxy-headers states intent (T1): uvicorn has it on by default, and it
# believes X-Forwarded-For and X-Forwarded-Proto only from the addresses in
# FORWARDED_ALLOW_IPS (default 127.0.0.1 and ::1) — compose sets that to
# the HTTPS edge's address, and that value is the trust.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
