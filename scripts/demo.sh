#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# ============================================================================
# LibreRun — the zero-config demo (blueprint S3, decision L22)
#
#   git clone … && cd … && ./scripts/demo.sh
#
# On a machine with Docker (or Podman) and nothing else, this writes a demo
# .env if none exists — a generated secret, a generated admin password,
# demo mode, the stub LLM, the bundled agent and the examples, Jaeger — then
# builds and starts the stack, waits for the backend, and prints the URL,
# the credentials and the trace viewer's address.
#
# Options:
#   --env-only   write .env (if absent), top up the keys of any agent it
#                predates, and stop before anything is built or started
#   --no-wait    start and return without waiting for /health
#
# Demo mode is not for production: delete .env, or set APP_SECRET_KEY and
# unset LIBRERUN_DEMO, to leave it.
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

# Compose's environment is the one this script inherited. Snapshot it
# here, before anything below is assigned, and resolve every name from
# the snapshot: bash scopes variables dynamically, so a function's
# `local value` or this file's own variables would otherwise stand in
# for the caller's (Codex on PR #51). The script's own names are all
# demo_* / _env_* / _dotenv_* and are never exported; the only exports
# it adds are the three derived values further down.
for demo_snap in $(compgen -e); do
    [[ "$demo_snap" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    printf -v "_env_${demo_snap}" '%s' "${!demo_snap}"
done
unset demo_snap

demo_env_only=0
demo_wait=1
for demo_arg in "$@"; do
    case "$demo_arg" in
        --env-only) demo_env_only=1 ;;
        --pull)
            # Retired with image publishing (A2; L37, D26): a release is
            # source only. Refused, before anything is written, until 1.1.0.
            echo "demo.sh: --pull is gone: a LibreRun release is source only, and ./scripts/demo.sh builds it" >&2
            exit 2
            ;;
        --no-wait) demo_wait=0 ;;
        -h|--help)
            # The comment block BETWEEN the two banner lines, found by
            # reading them — not lines 2..23, which stopped being the
            # help the first time a line was inserted above it.
            awk '/^# =+$/ { n++; next } n == 1 && /^#/ { sub(/^# ?/, ""); print } n >= 2 { exit }' "$0"
            exit 0
            ;;
        *)
            echo "demo.sh: unknown option '$demo_arg' (try --help)" >&2
            exit 2
            ;;
    esac
done

# Hex from the kernel's entropy: works on Linux with no openssl,
# no python — "Docker and nothing else" is the contract.
random_hex() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }

# A Fernet key (K6, D33): 32 random bytes as url-safe base64 — 44
# characters ending in '=', the format the secrets store's key list takes.
# `random_hex 32` is 64 hex characters, which names 32 bytes and is not a
# Fernet key, so the backend would refuse to boot on it (K6-01).
random_fernet_key() { head -c 32 /dev/urandom | base64 | tr '+/' '-_'; }

interpolate() {
    # Compose's interpolation for an unquoted or double-quoted .env value:
    # ${VAR}, $VAR, ${VAR:-default}, ${VAR-default}, ${VAR:+alt}, ${VAR+alt}
    # (${VAR:?err} and ${VAR?err} read as ${VAR} — Compose itself refuses
    # `up` on those), defaults nested, $$ a literal dollar. Names resolve
    # against the environment accumulated so far — the inherited
    # environment, then the .env lines already read — and a substituted
    # value is not rescanned.
    local s="$1" out="" i=0 n c rest expr name op arg val isset depth j
    n=${#s}
    while [ "$i" -lt "$n" ]; do
        c="${s:$i:1}"
        if [ "$c" != '$' ]; then out+="$c"; i=$((i + 1)); continue; fi
        rest="${s:$((i + 1))}"
        if [ "${rest:0:1}" = '$' ]; then out+='$'; i=$((i + 2)); continue; fi
        if [ "${rest:0:1}" = '{' ]; then
            depth=0; j=0
            while [ "$j" -lt "${#rest}" ]; do
                case "${rest:$j:1}" in
                    '{') depth=$((depth + 1)) ;;
                    '}') depth=$((depth - 1)); [ "$depth" -eq 0 ] && break ;;
                esac
                j=$((j + 1))
            done
            if [ "$depth" -ne 0 ]; then out+='$'; i=$((i + 1)); continue; fi
            expr="${rest:1:$((j - 1))}"
            i=$((i + j + 2))
            if [[ "$expr" =~ ^([A-Za-z_][A-Za-z0-9_]*)(:?[-+?])?(.*)$ ]]; then
                name="${BASH_REMATCH[1]}"; op="${BASH_REMATCH[2]:-}"; arg="${BASH_REMATCH[3]:-}"
                if env_is_set "$name"; then val=$(env_value "$name"); isset=1; else val=""; isset=0; fi
                case "$op" in
                    ":-") [ -n "$val" ] || val=$(interpolate "$arg") ;;
                    "-")  [ "$isset" = 1 ] || val=$(interpolate "$arg") ;;
                    ":+") if [ -n "$val" ]; then val=$(interpolate "$arg"); else val=""; fi ;;
                    "+")  if [ "$isset" = 1 ]; then val=$(interpolate "$arg"); else val=""; fi ;;
                    *) : ;;
                esac
                out+="$val"
            else
                out+="\${${expr}}"
            fi
            continue
        fi
        if [[ "$rest" =~ ^([A-Za-z_][A-Za-z0-9_]*) ]]; then
            name="${BASH_REMATCH[1]}"
            if env_is_set "$name"; then val=$(env_value "$name"); else val=""; fi
            out+="$val"; i=$((i + 1 + ${#name})); continue
        fi
        out+='$'; i=$((i + 1))
    done
    printf '%s' "$out"
}

load_dotenv() {
    # Read .env once, top to bottom, as Compose's dotenv parser does: each
    # value is expanded against the environment accumulated so far (the
    # inherited environment first, then the lines above it — never its
    # own line or a later one), then stored as _dotenv_KEY; a later line
    # for the same key replaces an earlier one. A line is KEY=value — or
    # the YAML-style KEY: value the same parser takes — with an optional
    # `export ` prefix; blank and # lines are skipped. A double-quoted
    # value ends at its closing quote (\" and \\ unescaped) and is
    # interpolated, a single-quoted one is literal, an unquoted one ends
    # at ` #` and is interpolated; whatever follows a closing quote is a
    # comment. No `source`: bash would evaluate $(...), backticks and ;
    # in a password.
    local line key value
    [ -f .env ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        line="${line#"${line%%[![:space:]]*}"}"
        case "$line" in ''|'#'*) continue ;; esac
        if [[ "$line" =~ ^export[[:space:]]+(.*)$ ]]; then line="${BASH_REMATCH[1]}"; fi
        [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*[=:](.*)$ ]] || continue
        key="${BASH_REMATCH[1]}"; value="${BASH_REMATCH[2]}"
        value="${value#"${value%%[![:space:]]*}"}"
        if [[ "$value" =~ ^\"((\\.|[^\"\\])*)\" ]]; then
            value="${BASH_REMATCH[1]}"
            value="${value//\\\"/\"}"; value="${value//\\\\/\\}"
            value=$(interpolate "$value")
        elif [[ "$value" =~ ^\'([^\']*)\' ]]; then
            value="${BASH_REMATCH[1]}"
        else
            value="${value%% #*}"; value="${value%"${value##*[![:space:]]}"}"
            value=$(interpolate "$value")
        fi
        printf -v "_dotenv_${key}" '%s' "$value"
    done < .env
}

env_exported() {
    # Compose's "set" for the shell half: KEY is in the environment this
    # script inherited (the snapshot at the top), even empty.
    local demo_var="_env_$1"
    [ -n "${!demo_var+x}" ]
}

env_is_set() {
    # Compose's "set" for ${KEY-default} and ${KEY+alt}: the variable is
    # in the inherited environment (even empty) or was read from .env.
    local demo_var="_dotenv_$1"
    env_exported "$1" || [ -n "${!demo_var+x}" ]
}

env_value() {
    # The value Compose will use for KEY, resolved as Compose resolves it:
    # the inherited environment when it carries the name — even empty, so
    # a caller's `BACKEND_PORT=8001 ./scripts/demo.sh` outranks the file
    # and `BACKEND_PORT= ./scripts/demo.sh` falls through to the compose
    # default exactly as `${BACKEND_PORT:-8000}` does — else the value
    # load_dotenv read from .env, else empty.
    local demo_env="_env_$1" demo_file="_dotenv_$1"
    if [ -n "${!demo_env+x}" ]; then printf '%s' "${!demo_env}"; return; fi
    if [ -n "${!demo_file+x}" ]; then printf '%s' "${!demo_file}"; fi
}

trim() {
    # Surrounding whitespace off, as the backend strips its settings.
    local demo_s="$1"
    demo_s="${demo_s#"${demo_s%%[![:space:]]*}"}"
    printf '%s' "${demo_s%"${demo_s##*[![:space:]]}"}"
}

is_true() {
    # The spellings the backend's boolean settings accept (pydantic):
    # 1, true, yes, on, t, y — in any case.
    case "$(trim "$1" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on|t|y) return 0 ;;
        *) return 1 ;;
    esac
}

# Every bundled agent's gateway key (blueprint S4a, D10), generated the
# same way APP_SECRET_KEY is.
#
# These have to exist BEFORE `up`: compose expands
# ${LIBRERUN_AGENT_KEY_<ID>:?...} in agents.compose.yaml while no
# LibreRun service is running, so nothing inside the platform can mint
# them at start-up. The gateway registers whatever it finds at boot.
#
# <ID> is the agent id normalised for the environment — upper-cased with
# every character outside [A-Z0-9] replaced by _ — because compose
# variable names admit no hyphens.
agent_key_lines() {
    local dir id var
    echo "# Per-agent gateway keys (blueprint S4a). One per bundled agent;"
    echo "# compose.sh derives agent-keys.env from these lines and the environment."
    for dir in backend/agents/*/ backend/agents/_examples/*/; do
        [ -f "${dir}agent.yaml" ] || continue
        case "$(basename "$dir")" in _*) continue ;; esac
        id=$(sed -n 's/^id:[[:space:]]*["'"'"']\{0,1\}\([A-Za-z0-9_-]*\)["'"'"']\{0,1\}[[:space:]]*$/\1/p' "${dir}agent.yaml" | head -1)
        [ -n "$id" ] || continue
        var="LIBRERUN_AGENT_KEY_$(printf '%s' "$id" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9' '_')"
        echo "${var}=lr_agent_$(random_hex 24)"
    done
}

# The upgrade half of the same idea (blueprint S5-R). An .env written
# before an agent existed carries no key for it, and compose expands
# ${LIBRERUN_AGENT_KEY_<ID>:?...} BEFORE it filters by profile — so
# `git pull && ./scripts/demo.sh` on a working demo would stop with a
# named error about a variable the operator never chose to omit. That
# error is right for a key someone deleted and wrong for an agent that
# is simply new, and the two are indistinguishable from inside compose.
#
# So: append a line per agent this file has none for, and never touch a
# line it already has. A key already in .env (or in the environment) is
# somebody's decision — possibly a rotation in progress — and rewriting
# it would break every container still holding the old value.
provision_missing_agent_keys() {
    local line name missing=()
    while IFS= read -r line; do
        case "$line" in LIBRERUN_AGENT_KEY_*) ;; *) continue ;; esac
        name="${line%%=*}"
        if env_is_set "$name"; then continue; fi
        missing+=("$line")
    done < <(agent_key_lines)
    [ "${#missing[@]}" -gt 0 ] || return 0
    {
        echo ""
        echo "# Added by scripts/demo.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ): agents that"
        echo "# this file had no key for. Existing keys are never rewritten."
        printf '%s\n' "${missing[@]}"
    } >> .env
    # The in-memory view of .env too, so anything below that resolves a
    # value sees what compose will see when it reads the file itself.
    for line in "${missing[@]}"; do
        printf -v "_dotenv_${line%%=*}" '%s' "${line#*=}"
    done
    echo "demo.sh: provisioned ${#missing[@]} new agent key(s) in .env: ${missing[*]%%=*}"
}

# The same top-up for the secrets store's key (K6, D33), so a demo .env
# written before the store existed can take a secret in Admin -> Settings.
# Only in demo mode — a real deployment chooses its own key, or none — and
# never over a choice already made: a key, an explicit blank (which means
# "unconfigured") or a _FILE, in the file or in the environment.
provision_store_key() {
    is_true "$(env_value LIBRERUN_DEMO)" || return 0
    if env_is_set LIBRERUN_BACKEND_SECRETS_KEY || env_is_set LIBRERUN_BACKEND_SECRETS_KEY_FILE; then
        return 0
    fi
    # Owner-only before the key goes in, as for gateway.env below (the
    # review after K7): appending keeps the file's mode, and an .env written
    # by hand is usually readable by every local account. A file this user
    # may not make owner-only gets no key.
    if ! chmod 600 .env 2>/dev/null; then
        echo "demo.sh: .env is not yours to make owner-only, so the secrets store key was not added: chmod 600 .env and run this again, or add LIBRERUN_BACKEND_SECRETS_KEY yourself" >&2
        return 0
    fi
    local key
    key=$(random_fernet_key)
    {
        echo ""
        echo "# Added by scripts/demo.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ): the secrets store key (K6)."
        echo "LIBRERUN_BACKEND_SECRETS_KEY=${key}"
    } >> .env
    printf -v "_dotenv_LIBRERUN_BACKEND_SECRETS_KEY" '%s' "$key"
    echo "demo.sh: provisioned the secrets store key in .env (LIBRERUN_BACKEND_SECRETS_KEY)"
}

# The gateway's store key (K7, D33), so a provider key pasted in Admin ->
# Settings has somewhere to be kept. It goes in the demo's own gateway.env —
# the file the gateway alone reads — and never in .env or compose's
# `environment:` block, where the backend would receive it or a blank would
# override the file. Only in demo mode, only for the file compose reads by
# default (LIBRERUN_GATEWAY_ENV_FILE unset: an operator who names another
# file keeps it themselves), and never over a choice already made: the key
# or its _FILE on a line of gateway.env, blank included, which means
# "unconfigured". The key differs from the backend's by construction: two
# draws of 32 random bytes.
provision_gateway_env() {
    is_true "$(env_value LIBRERUN_DEMO)" || return 0
    env_is_set LIBRERUN_GATEWAY_ENV_FILE && return 0
    local key
    key=$(random_fernet_key)
    if [ ! -f gateway.env ]; then
        # Owner-only from the first byte, like .env: it holds a key.
        ( umask 077 && cat > gateway.env <<GATEWAY_ENV
# Written by scripts/demo.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ): the demo's gateway.env,
# read by the gateway alone. It holds the key the provider keys pasted in
# Admin -> Settings are sealed with (K7); add a provider key below it, as
# gateway.env.example shows, or paste one in the admin page.
LIBRERUN_GATEWAY_SECRETS_KEY=${key}
GATEWAY_ENV
        )
        chmod 600 gateway.env
        echo "demo.sh: wrote gateway.env (the gateway's store key, mode 600)"
        return 0
    fi
    if grep -Eq '^[[:space:]]*(export[[:space:]]+)?LIBRERUN_GATEWAY_SECRETS_KEY(_FILE)?[[:space:]]*=' gateway.env; then
        return 0
    fi
    # Owner-only before the key goes in (Codex on #170): appending keeps the
    # file's mode, and a gateway.env copied from the example is usually
    # readable by every local account. A file this user may not make
    # owner-only gets no key.
    if ! chmod 600 gateway.env 2>/dev/null; then
        echo "demo.sh: gateway.env is not yours to make owner-only, so the gateway's store key was not added: chmod 600 gateway.env and run this again, or add LIBRERUN_GATEWAY_SECRETS_KEY yourself" >&2
        return 0
    fi
    {
        echo ""
        echo "# Added by scripts/demo.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ): the gateway's store key (K7)."
        echo "LIBRERUN_GATEWAY_SECRETS_KEY=${key}"
    } >> gateway.env
    echo "demo.sh: provisioned the gateway's store key in gateway.env (LIBRERUN_GATEWAY_SECRETS_KEY)"
}

demo_created_env=0
if [ ! -f .env ]; then
    demo_created_env=1
    demo_admin_password="Demo-$(random_hex 8)!1"
    # Owner-only from the first byte: the file holds the secret and the
    # admin password, and the usual umask 022 would hand it to every
    # local account (Codex on PR #51).
    ( umask 077 && cat > .env <<ENV
# Written by scripts/demo.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ): the zero-config
# demo (LIBRERUN_DEMO=true). Delete this file and run the script again for
# fresh credentials. To leave demo mode: set a real APP_SECRET_KEY, unset
# LIBRERUN_DEMO, set LIBRERUN_STUB_LLM=false and add provider keys — see
# .env.example for every setting.
APP_ENV=development
APP_SECRET_KEY=$(random_hex 32)
# The key the secrets set in Admin -> Settings are sealed with (K6).
LIBRERUN_BACKEND_SECRETS_KEY=$(random_fernet_key)
LIBRERUN_DEMO=true
LIBRERUN_STUB_LLM=true
INITIAL_ADMIN_EMAIL=admin@librerun.example
INITIAL_ADMIN_PASSWORD=${demo_admin_password}
# The bundled agent and the examples side by side.
LIBRERUN_AGENTS_PATH=agents:agents/_examples
# Vector forwards traces to the bundled Jaeger (viewer profile).
VECTOR_VIEWER=1
# "View trace" links point at that Jaeger (the shipped default is off:
# no link until a viewer is really there).
TRACE_VIEWER=jaeger
# The demo shows prompts and completions in the trace viewer; the shipped
# default is NO_CONTENT.
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_AND_EVENT
$(agent_key_lines)
ENV
    )
    chmod 600 .env
    echo "demo.sh: wrote .env (demo mode, generated credentials, mode 600)"
else
    echo "demo.sh: using the existing .env"
fi

# Read what the file says before anything asks it a question, and top up
# the keys of any agent it predates.
load_dotenv
provision_missing_agent_keys
provision_store_key
provision_gateway_env

if [ "$demo_env_only" = 1 ]; then
    echo "demo.sh: --env-only — run ./scripts/demo.sh again to start the stack"
    exit 0
fi

demo_compose="./compose.sh --profile app --profile viewer --profile demo"

# Built from this checkout, from cache when nothing changed; nothing is
# pulled (A2, #133).
demo_up_args="up -d --build"

# The host ports — each a bare port or a Compose host binding such as
# 127.0.0.1:8001 — resolved as Compose will resolve them (shell, then
# .env, then the compose default) BEFORE the build, because three values
# follow them and are otherwise fixed at the default ports: the API URL baked
# into the frontend bundle, the origin the backend's CORS allows, and the
# base of the "View trace" links. Each is derived from its port and
# exported for Compose unless the caller supplied it (shell or .env); an
# explicitly empty value is not a URL and counts as unsupplied.
bind_port() {
    # The port of a Compose host binding "[HOST:]PORT" (the short ports
    # syntax compose.yaml expands to "[HOST:]PORT:8000").
    printf '%s' "${1##*:}"
}
bind_host() {
    # The URL host for that binding: the address when one is given —
    # what the backend is reachable at — and localhost for none, a
    # wildcard (0.0.0.0, [::]) or loopback (127.0.0.1, [::1]). Loopback
    # is .env.example's default since T1, and its CORS line names the web
    # UI as http://localhost:3000; http://127.0.0.1:3000 is another origin,
    # whose every API call that exact list would refuse.
    local demo_h
    case "$1" in
        *:*) demo_h="${1%:*}"
             case "$demo_h" in ""|0.0.0.0|"[::]"|127.0.0.1|"[::1]") printf 'localhost' ;; *) printf '%s' "$demo_h" ;; esac ;;
        *) printf 'localhost' ;;
    esac
}
demo_backend_bind=$(trim "$(env_value BACKEND_PORT)"); demo_backend_bind="${demo_backend_bind:-8000}"
demo_backend_port=$(bind_port "$demo_backend_bind"); demo_backend_host=$(bind_host "$demo_backend_bind")
demo_frontend_bind=$(trim "$(env_value FRONTEND_PORT)"); demo_frontend_bind="${demo_frontend_bind:-3000}"
demo_frontend_port=$(bind_port "$demo_frontend_bind"); demo_frontend_host=$(bind_host "$demo_frontend_bind")
# The viewer's mapping in compose.yaml carries its own 127.0.0.1 host,
# so JAEGER_UI_PORT is a bare port.
demo_jaeger_ui_port=$(trim "$(env_value JAEGER_UI_PORT)"); demo_jaeger_ui_port="${demo_jaeger_ui_port:-16686}"
derive_unless_supplied() {
    # $1 key, $2 the derived value, $3 the port it follows, $4 the compose default
    local key="$1" value="$2"
    if [ -n "$(env_value "$key")" ]; then return; fi
    export "$key=$value"
    if [ "$value" != "$4" ]; then echo "demo.sh: ${key}=${value}   (follows ${3})"; fi
}
derive_unless_supplied NEXT_PUBLIC_API_URL "http://${demo_backend_host}:${demo_backend_port}/api/v1" BACKEND_PORT "http://localhost:8000/api/v1"
derive_unless_supplied APP_CORS_ORIGINS "http://${demo_frontend_host}:${demo_frontend_port}" FRONTEND_PORT "http://localhost:3000"
derive_unless_supplied TRACE_VIEWER_BASE_URL "http://localhost:${demo_jaeger_ui_port}" JAEGER_UI_PORT "http://localhost:16686"

$demo_compose $demo_up_args

demo_health_url="http://${demo_backend_host}:${demo_backend_port}/api/v1/health"

fetch() {
    if command -v curl >/dev/null 2>&1; then curl -fsS --max-time 5 "$1"
    elif command -v wget >/dev/null 2>&1; then wget -qO- --timeout=5 "$1"
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c 'import sys, urllib.request; sys.stdout.write(urllib.request.urlopen(sys.argv[1], timeout=5).read().decode())' "$1"
    else return 3
    fi
}

meta_json() {
    # GET /api/v1/meta — the backend's own account of the demo: from the
    # host when it can speak HTTP, else from inside the backend container
    # (python is there). Empty when neither answers.
    local demo_rc=0 demo_body
    demo_body=$(fetch "http://${demo_backend_host}:${demo_backend_port}/api/v1/meta" 2>/dev/null) || demo_rc=$?
    if [ "$demo_rc" = 3 ]; then
        demo_body=$($demo_compose exec -T backend python3 -c 'import sys, urllib.request; sys.stdout.write(urllib.request.urlopen("http://127.0.0.1:8000/api/v1/meta", timeout=5).read().decode())' 2>/dev/null) || demo_body=""
    elif [ "$demo_rc" != 0 ]; then
        demo_body=""
    fi
    printf '%s' "$demo_body"
}

backend_healthy_per_engine() {
    # The engine-agnostic fallback when nothing on the host can speak HTTP:
    # the backend service's compose healthcheck (a python probe of /health
    # inside the container — compose.yaml), which Docker and Podman alike
    # show as "(healthy)" in `compose ps`. It is a CURRENT signal: a
    # container that crash-loops, or is still starting after a restart, is
    # never healthy whatever an older log line says, and a backend that
    # was already up satisfies it too. The output goes to a file first:
    # under `pipefail`, `grep -q` closing the pipe early would make
    # `compose ps` die of SIGPIPE.
    local tmp
    tmp=$(mktemp)
    $demo_compose ps backend > "$tmp" 2>/dev/null || true
    if grep -q "(healthy)" "$tmp"; then rm -f "$tmp"; return 0; fi
    rm -f "$tmp"
    return 1
}

if [ "$demo_wait" = 1 ]; then
    echo "demo.sh: waiting for ${demo_health_url} …"
    demo_deadline=$((SECONDS + 300))
    demo_have_http=1
    while :; do
        if [ "$demo_have_http" = 1 ]; then
            demo_rc=0
            demo_out=$(fetch "$demo_health_url" 2>/dev/null) || demo_rc=$?
            if [ "$demo_rc" = 0 ]; then
                echo "demo.sh: backend is up: ${demo_out}"
                break
            elif [ "$demo_rc" = 3 ]; then
                echo "demo.sh: no curl/wget/python3 here — watching the backend's health status in the engine instead"
                demo_have_http=0
            fi
        elif backend_healthy_per_engine; then
            echo "demo.sh: backend is up (healthy per the engine)"
            break
        fi
        if [ "$SECONDS" -ge "$demo_deadline" ]; then
            echo "demo.sh: the backend did not come up within 5 minutes" >&2
            $demo_compose ps
            $demo_compose logs --no-color --tail 40 backend >&2
            exit 1
        fi
        sleep 2
    done
fi

# Every line below states what THIS .env says, and the credentials are
# printed only when this invocation generated them — an existing,
# hand-written .env may carry a real secret, real keys and a live LLM.
demo_admin_email=$(env_value INITIAL_ADMIN_EMAIL)
demo_mode=$(env_value LIBRERUN_DEMO)
demo_stub=$(env_value LIBRERUN_STUB_LLM)
demo_viewer=$(trim "$(env_value TRACE_VIEWER)" | tr '[:upper:]' '[:lower:]')
# Whether "View trace" links render is the backend's call, not the viewer
# name's: a preset needs a base URL, custom/langsmith a template, and an
# admin may have overridden all three at runtime — /meta reports the
# effective answer (trace_viewer_configured), the preset rendering the
# links (trace_viewer) and whether the environment or a runtime override
# configured it (trace_viewer_source). A destination is printed only
# when the environment is the source — it is then the base URL for the
# presets or the template for custom/langsmith, the very values the
# backend read; a runtime override's URL is not a public fact (Codex on
# PR #51).
demo_meta=$(meta_json)
if [[ "$demo_meta" =~ \"trace_viewer_configured\"[[:space:]]*:[[:space:]]*(true|false) ]]; then
    demo_links="${BASH_REMATCH[1]}"
else
    demo_links="unknown"
fi
demo_effective_viewer=""
if [[ "$demo_meta" =~ \"trace_viewer\"[[:space:]]*:[[:space:]]*\"([a-z]+)\" ]]; then
    demo_effective_viewer="${BASH_REMATCH[1]}"
fi
demo_viewer_source=""
if [[ "$demo_meta" =~ \"trace_viewer_source\"[[:space:]]*:[[:space:]]*\"(env|runtime)\" ]]; then
    demo_viewer_source="${BASH_REMATCH[1]}"
fi
demo_viewer_base=$(trim "$(env_value TRACE_VIEWER_BASE_URL)")
demo_viewer_base="${demo_viewer_base:-http://localhost:${demo_jaeger_ui_port}}"
demo_viewer_template=$(trim "$(env_value TRACE_VIEWER_URL_TEMPLATE)")
# The preset the backend reports is its normalized reading of the same
# value (trimmed, lowercased); when the environment is the source the
# two agree. The destination follows the backend's precedence: an
# explicit TRACE_VIEWER_URL_TEMPLATE wins over any preset (printed with
# {base} filled in), else the preset's base URL is the viewer's home.
demo_viewer_name="${demo_effective_viewer:-$demo_viewer}"
if [ -n "$demo_viewer_template" ]; then
    demo_viewer_dest="${demo_viewer_template//\{base\}/"${demo_viewer_base%/}"}"
else
    case "$demo_viewer_name" in
        custom|langsmith) demo_viewer_dest="<TRACE_VIEWER_URL_TEMPLATE is not set>" ;;
        *) demo_viewer_dest="${demo_viewer_base%/}" ;;
    esac
fi

echo
echo "LibreRun is up."
echo
echo "  Open      http://${demo_frontend_host}:${demo_frontend_port}"
echo "  Sign in   ${demo_admin_email:-<INITIAL_ADMIN_EMAIL is not set in .env>}"
if [ "$demo_created_env" = 1 ]; then
    echo "  Password  ${demo_admin_password}   (generated by this run, saved in .env)"
else
    echo "  Password  the INITIAL_ADMIN_PASSWORD in your existing .env"
fi
echo
echo "  API       http://${demo_backend_host}:${demo_backend_port}/api/v1"
case "$demo_links" in
    true)
        if [ "$demo_viewer_source" = "env" ]; then
            echo "  Traces    ${demo_viewer_dest}   (${demo_viewer_name} — \"View trace\" on any run)"
        else
            # The links open a viewer an admin configured at runtime; its
            # address is in the admin settings, not in .env.
            echo "  Traces    \"View trace\" links open the ${demo_effective_viewer:-viewer} configured in the admin settings (a runtime override of .env)"
        fi ;;
    false)
        if [ "${demo_effective_viewer:-${demo_viewer:-off}}" = "off" ]; then
            echo "  Traces    none (TRACE_VIEWER=off) — set jaeger with VECTOR_VIEWER=1 for \"View trace\" links"
        else
            echo "  Traces    none: TRACE_VIEWER=${demo_effective_viewer:-$demo_viewer}, but the backend reports no usable viewer (/api/v1/meta) — the presets need TRACE_VIEWER_BASE_URL, custom/langsmith a TRACE_VIEWER_URL_TEMPLATE"
        fi ;;
    *)
        echo "  Traces    TRACE_VIEWER=${demo_viewer:-off}; GET /api/v1/meta reports whether \"View trace\" links render (trace_viewer_configured)" ;;
esac
echo
if is_true "$demo_mode"; then
    echo "  Demo mode (LIBRERUN_DEMO=true): not for production; the UI says so."
else
    echo "  LIBRERUN_DEMO is not true (shell or .env): this is a regular deployment, not the demo."
fi
if is_true "$demo_stub"; then
    echo "  The LLM is a stub answering from fixtures: no provider calls, no cost."
else
    echo "  LIBRERUN_STUB_LLM is not true (shell or .env): runs call the configured providers."
fi
if [ "$demo_created_env" = 1 ]; then
    echo "  The secret and the password above were generated by this run."
fi
echo
echo "  Stop      ./compose.sh --profile app --profile viewer down"
echo "  Reset     ./compose.sh --profile app --profile viewer down -v   (deletes all data)"
echo
