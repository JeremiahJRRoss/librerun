#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
# ============================================================================
# LibreRun — the HTTPS edge's start (K blueprint T2; decisions L42, L43, D44)
#
# compose.yaml runs this in the `edge` service as
# `/bin/sh /etc/caddy/edge-start.sh`, mounted read-only, so it needs no
# execute bit. At every start, choice or not, it writes what the
# environment says into the control volume, then hands over to Caddy with
# the image's own command:
#
#   /control/pki.global.caddy  the environment's `ca env-<12 hex>` entry,
#                              rewritten (an issuer id from the SHA-256 of
#                              LIBRERUN_TLS_CA's certificate file), or
#                              dropped when LIBRERUN_TLS_CA is blank; the
#                              CAs loaded on Application Settings stay
#   /control/env.caddy         the environment's selection: `import
#                              tls_environment` (LIBRERUN_TLS), or the
#                              internal issuer on LIBRERUN_TLS_CA's CA
#   /control/tls.caddy         `import /control/env.caddy` while no choice
#                              is recorded; a choice made on Application
#                              Settings (/control/choice) is left as it is
#
# LIBRERUN_TLS_CA is `/certs/<ca.crt> /certs/<ca.key>`: the environment's
# way to keep one root across a restore, since the edge's volumes are in no
# backup (L42). A file it names that is missing stops the start here,
# naming the path — a pki entry naming files that are gone makes `caddy
# run` panic. The directories come from EDGE_CONTROL_DIR and EDGE_CERT_DIR,
# default /control and /certs, which compose never sets: the guard test
# runs this on a temporary directory with a stand-in `caddy`. Busybox's
# sh, sha256sum, cut, awk and mv, as the pinned image carries them.
# ============================================================================
set -eu

control="${EDGE_CONTROL_DIR:-/control}"
certs="${EDGE_CERT_DIR:-/certs}"
ca="${LIBRERUN_TLS_CA:-}"

fail() {
    echo "edge-start: $*" >&2
    exit 1
}

mkdir -p "$control"

# ---- The environment's CA, if LIBRERUN_TLS_CA names one --------------------
env_id=""
env_entry=""
if [ -n "$(printf '%s' "$ca" | tr -d ' \t')" ]; then
    # Word splitting is the point: two paths, separated by blanks, and
    # never a glob.
    set -f
    # shellcheck disable=SC2086
    set -- $ca
    set +f
    [ "$#" -eq 2 ] || fail "LIBRERUN_TLS_CA must name two files, '$certs/<ca.crt> $certs/<ca.key>'; it names $# word(s)"
    ca_cert="$1"
    ca_key="$2"
    for path in "$ca_cert" "$ca_key"; do
        case "$path" in
            "$certs"/*) ;;
            *) fail "LIBRERUN_TLS_CA names $path, which is not under $certs (LIBRERUN_TLS_CERT_DIR's mount)" ;;
        esac
        case "$path" in
            *'{'* | *'}'* | *'"'* | *'#'*) fail "LIBRERUN_TLS_CA names $path, which a Caddyfile cannot hold" ;;
        esac
        [ -f "$path" ] || fail "LIBRERUN_TLS_CA names $path, which does not exist: the edge does not start on a CA it cannot load"
    done
    env_id="env-$(sha256sum "$ca_cert" | cut -c1-12)"
    env_entry="$(printf '\tca %s {\n\t\troot {\n\t\t\tcert %s\n\t\t\tkey %s\n\t\t}\n\t}' "$env_id" "$ca_cert" "$ca_key")"
fi

# ---- /control/pki.global.caddy: the environment's entry rewritten ----------
# One `pki` block, written by this script and by `edge-control` alike: an
# entry per CA, `\tca <id> {` to its `\t}`. Every entry but the
# environment's is kept as it is.
pki="$control/pki.global.caddy"
kept=""
if [ -f "$pki" ]; then
    kept="$(awk '
        /^pki \{$/ { inside = 1; next }
        inside && /^\}$/ { inside = 0; next }
        !inside { next }
        /^\tca env-/ { skip = 1 }
        skip { if ($0 ~ /^\t\}$/) skip = 0; next }
        { print }
    ' "$pki")"
fi
if [ -n "$env_entry" ] || [ -n "$kept" ]; then
    {
        echo "# Every CA the edge may name, in one pki block (config/Caddyfile says why)."
        echo "# The env- entry is config/edge-start.sh's, from LIBRERUN_TLS_CA; a loaded-"
        echo "# entry is edge-control's, from Application Settings."
        echo "pki {"
        if [ -n "$env_entry" ]; then printf '%s\n' "$env_entry"; fi
        if [ -n "$kept" ]; then printf '%s\n' "$kept"; fi
        echo "}"
    } > "$pki.start"
    mv "$pki.start" "$pki"
else
    rm -f "$pki"
fi

# ---- /control/env.caddy: the environment's selection -----------------------
if [ -n "$env_id" ]; then
    printf 'tls {\n\tissuer internal {\n\t\tca %s\n\t}\n}\n' "$env_id" > "$control/env.caddy.start"
else
    printf 'import tls_environment\n' > "$control/env.caddy.start"
fi
mv "$control/env.caddy.start" "$control/env.caddy"

# ---- /control/tls.caddy: the environment's, unless a choice is recorded ----
if [ -f "$control/choice" ] && [ -f "$control/tls.caddy" ]; then
    echo "edge-start: a certificate chosen on Application Settings is in effect ($control/choice); \"Use the environment's setting\" there returns to LIBRERUN_TLS${env_id:+ and LIBRERUN_TLS_CA}"
else
    if [ -f "$control/choice" ]; then
        # A choice with no selection to go with it cannot be served: the
        # environment's applies, and the stale record goes.
        echo "edge-start: $control/choice has no $control/tls.caddy beside it; the environment's setting applies" >&2
        rm -f "$control/choice"
    fi
    printf 'import %s/env.caddy\n' "$control" > "$control/tls.caddy.start"
    mv "$control/tls.caddy.start" "$control/tls.caddy"
fi

exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
