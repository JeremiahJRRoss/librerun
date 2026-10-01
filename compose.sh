#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# ============================================================================
# LibreRun — Compose wrapper (auto-detects Docker or Podman)
#
# Usage:
#   ./compose.sh up -d                    # start infra (postgres + redis)
#   ./compose.sh --profile app up -d      # start infra + backend + frontend
#   ./compose.sh down -v                  # tear down everything
#   ./compose.sh logs -f backend          # tail backend logs
#   ./compose.sh ps                       # list running containers
#
# Override detection:
#   COMPOSE_ENGINE=podman ./compose.sh up -d
#   COMPOSE_ENGINE=docker ./compose.sh up -d
# ============================================================================

set -euo pipefail

# --- The images ------------------------------------------------------------
# Every first-party service builds from this checkout and is never pulled
# (A2, #133; L37, D26): compose.yaml gives each `pull_policy: build` and
# names its image `${LIBRERUN_IMAGE_PREFIX:-localhost/librerun}/…`, a local
# name, so the most a missing image can reach is the loopback address
# (podman's `missing` default, under `up --no-build`). Nothing here derives
# a registry: a release is source only. An operator who sets
# LIBRERUN_IMAGE_PREFIX names a registry of their own to push to, and
# compose still builds.

# --- Detect compose engine ---
detect_engine() {
    # Respect explicit override
    if [[ -n "${COMPOSE_ENGINE:-}" ]]; then
        echo "$COMPOSE_ENGINE"
        return
    fi

    # Prefer docker if available and daemon is running
    if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
        echo "docker"
        return
    fi

    # Fall back to podman
    if command -v podman &>/dev/null; then
        echo "podman"
        return
    fi

    # Docker is installed and did not answer. That is the likeliest first-run
    # state — the Docker service not started yet, or a user outside the
    # `docker` group — and telling that person to install Docker sends them
    # the wrong way. Found by S10's first-contact walk of the README.
    if command -v docker &>/dev/null; then
        echo >&2 "Error: docker is installed but its daemon is not answering (\`docker info\` failed)."
        echo >&2 "Start the Docker service (sudo systemctl start docker)"
        echo >&2 "and check that your user is in the docker group. \`docker info\` shows the reason."
        exit 1
    fi

    echo >&2 "Error: neither docker nor podman found. Install one of them first."
    exit 1
}

ENGINE=$(detect_engine)

# --- Build the compose command ---
case "$ENGINE" in
    docker)
        # Docker Compose V2 (plugin) is "docker compose"
        # Docker Compose V1 (standalone) is "docker-compose"
        if docker compose version &>/dev/null 2>&1; then
            CMD="docker compose"
        elif command -v docker-compose &>/dev/null; then
            CMD="docker-compose"
        else
            echo >&2 "Error: docker is installed but docker compose plugin is not."
            echo >&2 "Install it: https://docs.docker.com/compose/install/"
            exit 1
        fi
        ;;
    podman)
        if command -v podman-compose &>/dev/null; then
            CMD="podman-compose"
        elif podman compose version &>/dev/null 2>&1; then
            CMD="podman compose"
        else
            echo >&2 "Error: podman is installed but podman-compose is not."
            echo >&2 "Install it: pip install podman-compose"
            exit 1
        fi
        ;;
    *)
        echo >&2 "Error: unknown engine '$ENGINE'. Use 'docker' or 'podman'."
        exit 1
        ;;
esac

# --- Derive agent-keys.env (blueprint S4a, D10; K3) ---
#
# The gateway's per-agent keys live in .env beside everything else, but
# the gateway must receive THOSE LINES AND NO OTHERS: it holds the
# provider credentials, and a whole-.env env_file would hand it
# APP_SECRET_KEY and the INITIAL_* bootstrap credentials too. So they are
# derived into their own file here, before every command, by a reader
# that never evaluates the file — a plain line match, not `source`, so a
# value containing $(...), backticks or ; is data rather than a command.
#
# Two sources since K3, read in compose's own order: the ENVIRONMENT
# first, then .env. Compose reads the shell before the file when it
# expands `${LIBRERUN_AGENT_KEY_<ID>:?}` for an agent container, so a key
# set in the shell is the one that container presents — and it has to be
# the one the gateway registers, or every call from that agent is refused
# with a 401 that reads like a bad key rather than like a file the
# gateway never saw. It is also what lets a deployment run with no .env
# on disk at all: `sops exec-env` hands this script the decrypted values
# through the environment (docs/platform/Install.md, "Encrypting .env at rest").
#
# Two agents sharing a key value would make a presented key ambiguous, so
# that stops the derivation by name, BEFORE `up` — the database's unique
# index would otherwise catch it later, as a constraint name in a boot
# log nobody is watching. The check spans both sources.
derive_agent_keys() {
    local env_file=".env"
    local out="agent-keys.env"
    local name value environment=""
    # The environment's lines, collected by name rather than by parsing
    # `env` output. A shell value may hold anything and a file line may
    # not: one with a newline in it would write a second line into the
    # gateway's file, so it is refused by name. Exported variables only —
    # those are the ones compose sees.
    for name in $(compgen -e LIBRERUN_AGENT_KEY_ 2>/dev/null || true); do
        value="${!name}"
        case "$value" in
            *$'\n'*)
                echo >&2 "Error: $name (environment) holds a newline; an agent key is one line."
                exit 3 ;;
        esac
        environment+="${name}=${value}"$'\n'
    done
    local -a sources=(src=environment -)
    if [[ -f "$env_file" ]]; then
        sources+=(src=.env "$env_file")
    fi
    printf '%s' "$environment" | awk -F= '
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*LIBRERUN_AGENT_KEY_[A-Z0-9_]+[[:space:]]*=/ {
            name = $1
            value = substr($0, index($0, "=") + 1)
            if (src != "environment") {
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
                gsub(/^"|"$/, "", value)
                gsub(/^\x27|\x27$/, "", value)
            }
            if (value == "") next
            # The stand-in placeholder_missing_agent_keys exports so that
            # an unprovisioned agent does not block an infra-only command.
            # It must never reach the gateway: a registered key is a
            # credential, and this one is a public string. The call order
            # at the bottom of this script keeps it out of the environment
            # when this runs; this line keeps it out of the file if that
            # order ever changes.
            if (value == "unprovisioned-agent-key-run-scripts-demo-sh") next
            # The first source seen owns the name: the environment
            # outranks the file, as it does for compose itself.
            if (name in from) next
            from[name] = src
            if (value in owner && owner[value] != name) {
                printf("Error: %s (%s) and %s (%s) carry the same agent key value. A key names exactly one agent; give each its own.\n", owner[value], from[owner[value]], name, src) > "/dev/stderr"
                exit 3
            }
            owner[value] = name
            print name "=" value
        }
    ' "${sources[@]}" > "$out.tmp" || { rm -f "$out.tmp"; exit 3; }
    mv "$out.tmp" "$out"
    chmod 600 "$out" 2>/dev/null || true
}

# --- Keep an unprovisioned checkout usable (blueprint S4a) ---
#
# An agent fragment spells its key `${LIBRERUN_AGENT_KEY_<ID>:?...}` so
# an unprovisioned agent stops `up` with a named error instead of
# starting a container whose every model call is refused — which looks
# like a model outage rather than a missing line in .env.
#
# Compose interpolates the WHOLE file before it filters by profile,
# though, so left alone that error would also block `./compose.sh up -d`
# — the infra-only command a developer runs to get Postgres and Redis
# for a local uvicorn, starting no agent container at all. And
# `./compose.sh down`, and `ps`, and `logs`.
#
# So: when the command requests none of the profiles a given agent
# container lives under, that agent's missing key gets a placeholder
# that cannot authenticate anything (no row hashes to it, so every call
# is refused `401 agent_key_invalid`). When one IS requested, that
# agent's variable stays unset and the named error fires — which is the
# case the gate is about.
#
# Per agent, not per file: with two agents installed, starting one must
# not demand the other's key, or `--profile demo up` would be blocked by
# an agent the command never asked to start — the same defect as above,
# one level in.

# Emits one `profiles|VARIABLE` line per agent key the fragment needs,
# with `profiles` the comma-separated list the owning service declares
# (empty when it declares none, i.e. the service always starts).
agent_key_requirements() {
    [ -f agents.compose.yaml ] || return 0
    awk '
        # A service name: exactly two spaces of indent, then a name.
        /^  [A-Za-z0-9_.-]+:[[:space:]]*$/ {
            svc = $1; sub(/:$/, "", svc)
            prof[svc] = ""; inprof = 0
            next
        }
        svc == "" { next }
        # profiles, inline (["a", "b"]) or as a block list.
        /profiles:/ {
            inprof = 0
            if (match($0, /\[[^]]*\]/)) {
                p = substr($0, RSTART + 1, RLENGTH - 2)
                gsub(/["\047[:space:]]/, "", p)
                prof[svc] = p
            } else {
                prof[svc] = ""
                inprof = 1
            }
            next
        }
        inprof && /^[[:space:]]*-[[:space:]]*[^[:space:]]/ {
            v = $0
            sub(/^[[:space:]]*-[[:space:]]*/, "", v)
            gsub(/["\047]/, "", v)
            sub(/[[:space:]].*$/, "", v)
            if (v != "") prof[svc] = (prof[svc] == "" ? v : prof[svc] "," v)
            next
        }
        { inprof = 0 }
        # Collected, not printed: a key may be declared ABOVE the
        # profiles that decide whether it is needed, and attributing it
        # to the profiles seen so far would make it unconditional.
        match($0, /\$\{LIBRERUN_AGENT_KEY_[A-Z0-9_]*/) {
            v = substr($0, RSTART + 2, RLENGTH - 2)
            if (!(svc in keys)) { order[++n] = svc; keys[svc] = v }
            else if (index("," keys[svc] ",", "," v ",") == 0) keys[svc] = keys[svc] "," v
        }
        END {
            for (i = 1; i <= n; i++) {
                s = order[i]
                m = split(keys[s], k, ",")
                for (j = 1; j <= m; j++) print prof[s] "|" k[j]
            }
        }
    ' agents.compose.yaml | sort -u
}

placeholder_missing_agent_keys() {
    local requested=" $* " profiles var profile asked
    while IFS='|' read -r profiles var; do
        [ -n "$var" ] || continue
        # No profile means the service always starts, so its key is
        # always required: never placeholder it.
        asked=0
        [ -n "$profiles" ] || asked=1
        for profile in $(printf '%s' "$profiles" | tr ',' ' '); do
            case "$requested" in
                *" --profile $profile "*|*" --profile=$profile "*) asked=1 ;;
            esac
        done
        if [ "$asked" -eq 1 ]; then continue; fi
        if [ -z "${!var:-}" ] && ! grep -q "^[[:space:]]*${var}=" .env 2>/dev/null; then
            export "$var=unprovisioned-agent-key-run-scripts-demo-sh"
        fi
    done <<EOF
$(agent_key_requirements)
EOF
}

# --- The loopback guard (K blueprint T1; decisions L35, D37) ---
#
# The `tls` profile puts an HTTPS edge in front of LibreRun, and that is
# only true if nothing else answers off this host: a plain port published
# on every interface is a way around the edge. The binding is the guard,
# because nothing else can be. Docker publishes a port with its own
# iptables rules, ahead of ufw's and firewalld's, so a host firewall does
# not close it; and no override file can remove a publish (compose merges
# `ports:`, and `!override` needs Compose 2.24.4 and podman-compose 1.4.0,
# above docs/platform/Install.md's floors).
#
# So when a command requests the profile — a `--profile` argument, or
# COMPOSE_PROFILES (the environment, even empty, else the env file), naming
# `tls` or `*` (every profile) — four values must hold, each resolved as
# compose resolves it: the environment wins even when empty, then the env
# file (.env, or the `--env-file` arguments), last assignment winning, then
# compose.yaml's own default, which is also what a blank value becomes:
#
#   BACKEND_PORT, FRONTEND_PORT  127.0.0.1:<port> or [::1]:<port>
#   NEXT_PUBLIC_API_URL          /api/v1 — the browser calls the edge's own
#                                origin; an absolute http:// URL is blocked
#                                as mixed content on an https page
#   BACKEND_INTERNAL_URL         http://backend:8000 — the fourth line: a
#                                start WITHOUT the profile (`librerun up`,
#                                `librerun demo`, ./scripts/demo.sh) then
#                                still serves the UI on plain loopback,
#                                through the web UI's own /api/v1 rewrite
#
# Otherwise it names each line to set and exits 4 (1 and 3 are taken)
# before anything runs or is written — agent-keys.env included. Docker
# Compose reads COMPOSE_PROFILES only when no --profile is given, and
# podman-compose below 1.6 not at all; this reads it either way, so its
# refusal errs toward the binding. scripts/demo.sh and the CLI reach
# compose through this script, so they inherit it.
#
# tls_env_file_value NAME FILE… prints `=<value>` for the last assignment
# of NAME in the files, in compose's dotenv syntax (an `export ` prefix,
# `=` or `: `, quotes, an unquoted value's ` #` comment), and nothing when
# no file assigns it. It never evaluates a line.
tls_env_file_value() {
    local name="$1" file
    shift
    for file in "$@"; do
        [ -f "$file" ] || continue
        awk -v name="$name" '
            {
                line = $0
                sub(/\r$/, "", line)
                sub(/^[ \t]+/, "", line)
                if (line ~ /^#/) next
                sub(/^export[ \t]+/, "", line)
                if (!match(line, /^[A-Za-z0-9_.-]+[ \t]*[=:]/)) next
                key = substr(line, 1, RLENGTH - 1)
                sub(/[ \t]+$/, "", key)
                if (key != name) next
                value = substr(line, RLENGTH + 1)
                sub(/^[ \t]+/, "", value)
                quote = substr(value, 1, 1)
                if ((quote == "\"" || quote == "\047") && (end = index(substr(value, 2), quote)) > 0) {
                    value = substr(value, 2, end - 1)
                } else {
                    sub(/[ \t]+#.*$/, "", value)
                    sub(/[ \t]+$/, "", value)
                }
                found = "=" value
            }
            END { if (found != "") print found }
        ' "$file"
    done | tail -n 1
}

tls_loopback_guard() {
    local arg previous="" requested="" profiles found name value origin default want port
    local -a files=() problems=()
    for arg in "$@"; do
        case "$previous" in
            --profile) case ",$arg," in *,tls,*|*,\*,*) requested=1 ;; esac ;;
            --env-file) files+=("$arg") ;;
        esac
        case "$arg" in
            --profile=*) case ",${arg#--profile=}," in *,tls,*|*,\*,*) requested=1 ;; esac ;;
            --env-file=*) files+=("${arg#--env-file=}") ;;
        esac
        previous="$arg"
    done
    [ "${#files[@]}" -gt 0 ] || files=(.env)
    if [ -n "${COMPOSE_PROFILES+set}" ]; then
        profiles="$COMPOSE_PROFILES"
    else
        found=$(tls_env_file_value COMPOSE_PROFILES "${files[@]}")
        profiles="${found#=}"
    fi
    case ",$(printf '%s' "$profiles" | tr -d ' \t')," in *,tls,*|*,\*,*) requested=1 ;; esac
    [ -n "$requested" ] || return 0

    for name in BACKEND_PORT FRONTEND_PORT NEXT_PUBLIC_API_URL BACKEND_INTERNAL_URL; do
        # compose.yaml's own default for the name: what an unset or blank value
        # becomes. Read from the file, so it cannot drift; a comment never counts.
        default=$(sed -n "/^[[:space:]]*#/!s/.*\${$name:-\([^}]*\)}.*/\1/p" compose.yaml 2>/dev/null | head -n 1)
        if [ -n "${!name+set}" ]; then
            value="${!name}"
            origin="this shell's environment, which outranks ${files[*]}"
        else
            found=$(tls_env_file_value "$name" "${files[@]}")
            if [ -n "$found" ]; then
                value="${found#=}"
                origin="${files[*]}"
            else
                value=""
                origin="compose.yaml's default"
            fi
        fi
        if [ -z "$value" ]; then
            value="$default"
            [ "$origin" = "compose.yaml's default" ] || origin="blank in $origin, so compose.yaml's default"
        fi
        case "$name" in
            BACKEND_PORT|FRONTEND_PORT)
                if [[ "$value" =~ ^(127\.0\.0\.1|\[::1\]):[0-9]+$ ]]; then continue; fi
                port=""
                [[ "$value" =~ ^([^:]*:)*([0-9]+)$ ]] && port="${BASH_REMATCH[2]}"
                [ -n "$port" ] || port="${default##*:}"
                want="127.0.0.1:$port"
                ;;
            NEXT_PUBLIC_API_URL)
                want="/api/v1"
                [ "$value" = "$want" ] && continue
                ;;
            BACKEND_INTERNAL_URL)
                want="http://backend:8000"
                [ "$value" = "$want" ] && continue
                ;;
        esac
        problems+=("$(printf '  %-40s (it is %s, from %s)' "$name=$want" "${value:-blank}" "$origin")")
    done
    [ "${#problems[@]}" -gt 0 ] || return 0

    {
        echo "compose.sh: the tls profile is requested, and it puts an HTTPS edge in front of LibreRun —"
        echo "so nothing else may answer off this host, and the web UI must call the edge's own origin."
        echo "Refused (exit 4); nothing was started. Set these lines in ${files[*]} (or export them: the"
        echo "environment outranks the file):"
        echo
        printf '%s\n' "${problems[@]}"
        echo
        echo "The binding is the guard: Docker publishes a port with its own iptables rules, ahead of"
        echo "ufw's and firewalld's, so a host firewall does not close it, and no override file can"
        echo "remove a publish. After changing NEXT_PUBLIC_API_URL or BACKEND_INTERNAL_URL, rebuild the"
        echo "web UI (up -d --build): both are baked into it at build time, and a bundle that calls an"
        echo "absolute http:// URL from an https page is blocked by the browser as mixed content."
        echo "docs/platform/Install.md, \"HTTPS at the edge\"."
    } >&2
    exit 4
}

# The guard FIRST: a refused command changes nothing, agent-keys.env
# included. Then derive FIRST, placeholder SECOND — the order is
# load-bearing since K3: the derivation reads the environment, and the
# placeholder is exported into it. The reverse order would hand the gateway
# a public string as a registered credential (the derivation also refuses
# that value by name).
tls_loopback_guard "$@"
derive_agent_keys
placeholder_missing_agent_keys "$@"

echo "Using: $CMD"
exec $CMD -f compose.yaml "$@"
