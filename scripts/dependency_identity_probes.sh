#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Negative tests for R14's guard, scripts/check_dependency_identity.py.
# Each probe plants the exact violation a rule exists to catch, runs the
# real guard, and FAILS unless the guard goes red naming the planted file;
# then it puts the tree back. On a clean tree a broken guard and a working
# one look identical, so this is how the guard proves it still looks.
#
#   bash scripts/dependency_identity_probes.sh           # every probe
#   bash scripts/dependency_identity_probes.sh images    # the images rule
#   bash scripts/dependency_identity_probes.sh matrix    # the matrix rule
#
# Nothing is committed and no branch is created. A tracked file a probe
# edits is restored from a copy, including when a probe fails half-way,
# and the end compares `git status` with the start.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
which="${1:-all}"
backup=$(mktemp -d)
restored=()
failed=0
before="$(git status --porcelain=v1 --untracked-files=all | sha256sum)"
guard="python3 scripts/check_dependency_identity.py"
out="$backup/out"

cleanup() {
    local f
    for f in "${restored[@]:-}"; do
        [ -n "$f" ] || continue
        cp -p -- "$backup/$(printf '%s' "$f" | tr '/' '%')" "$f"
    done
    restored=()
}
trap 'cleanup; rm -rf "$backup"' EXIT

# keep PATH — remember a tracked file so the probe can edit it.
keep() {
    cp -p -- "$1" "$backup/$(printf '%s' "$1" | tr '/' '%')"
    restored+=("$1")
}

# edit PATH SED — keep PATH, apply SED to it, and insist that it changed:
# a probe whose plant matched nothing would test nothing.
edit() {
    local path="$1" script="$2"
    keep "$path"
    sed -i -E "$script" "$path"
    if cmp -s "$path" "$backup/$(printf '%s' "$path" | tr '/' '%')"; then
        echo "::error::the probe's edit changed nothing in $path ($script): the probe is stale"
        failed=1
    fi
}

# expect_red LABEL WHAT RULE — the guard's RULE must fail, naming WHAT.
expect_red() {
    local label="$1" what="$2" rule="$3"
    if $guard "$rule" > "$out" 2>&1; then
        echo "::error::probe stayed GREEN: $label — the guard does not catch what it exists to catch"
        failed=1
    elif ! grep -qF -- "$what" "$out"; then
        echo "::error::red, but not for the plant: $label (the findings do not name '$what')"
        failed=1
    else
        echo "red, as it must be: $label"
    fi
    cleanup
}

images() {
    edit services/gateway/Dockerfile '0,/^(FROM [^ @]+)@sha256:[0-9a-f]{64}/s//\1/'
    expect_red "images: a FROM with no digest" "services/gateway/Dockerfile" images

    edit frontend/Dockerfile '0,/^(FROM docker\.io\/library\/node):[^@ ]+@/s//\1:latest@/'
    expect_red "images: a FROM tagged latest" "frontend/Dockerfile" images

    edit backend/agents/_examples/echo_container/Dockerfile 's/^FROM docker\.io\/library\//FROM /'
    expect_red "images: a FROM by a short name" "backend/agents/_examples/echo_container/Dockerfile" images

    edit cli/src/librerun/templates/container-ts/Dockerfile '0,/^(FROM [^ @]+)@sha256:[0-9a-f]{64}/s//\1/'
    expect_red "images: a template's FROM with no digest" "cli/src/librerun/templates/container-ts/Dockerfile" images

    edit compose.yaml '0,/^(\s+image: docker\.io\/library\/postgres:[^@ ]+)@sha256:[0-9a-f]{64}/s//\1/'
    expect_red "images: a compose image with no digest" "compose.yaml" images

    edit compose.yaml '0,/^(\s+image: docker\.io\/valkey\/valkey:[^@ ]+@sha256:[0-9a-f]{63})[0-9a-f]/s//\1/'
    expect_red "images: a digest one hex digit short" "compose.yaml" images

    edit agents.compose.yaml '0,/^(\s+)image: \$\{LIBRERUN_IMAGE_PREFIX.*librerun-example-echo.*$/s//\1image: docker.io\/library\/redis:7-alpine/'
    expect_red "images: a third-party image in agents.compose.yaml" "agents.compose.yaml" images

    edit cli/src/librerun/templates/_fragment.yaml '0,/^(\s+)pull_policy: build$/s//\1image: docker.io\/library\/python:3.12-slim/'
    expect_red "images: an image in the compose fragment librerun init renders" "cli/src/librerun/templates/_fragment.yaml" images

    # Read by name, not by the one spelling this tree uses: a checkout
    # whose only build file is a Containerfile and whose only compose
    # file is dev.compose.yml, each naming an image with no digest.
    local scratch
    scratch="$(mktemp -d)"
    git -C "$scratch" init -q
    mkdir -p "$scratch/.github/workflows"
    printf 'FROM docker.io/library/python:3.12-slim\n' > "$scratch/Containerfile"
    printf 'services:\n  cache:\n    image: docker.io/valkey/valkey:8-alpine\n' > "$scratch/dev.compose.yml"
    printf 'on: push\njobs: {}\n' > "$scratch/.github/workflows/w.yml"
    git -C "$scratch" add -A
    if $guard images --root "$scratch" > "$out" 2>&1; then
        echo "::error::probe stayed GREEN: a Containerfile and dev.compose.yml — the guard does not read them"
        failed=1
    elif ! grep -qF "Containerfile:1:" "$out" || ! grep -qF "dev.compose.yml:3:" "$out"; then
        echo "::error::red, but not for the plant: a Containerfile and dev.compose.yml (the findings do not name both)"
        failed=1
    else
        echo "red, as it must be: a Containerfile and dev.compose.yml, each read by its name"
    fi
    rm -rf "$scratch"

    edit .github/workflows/database-parity.yml '0,/^(\s+image: [^@ ]+)@sha256:[0-9a-f]{64}/s//\1/'
    expect_red "images: a workflow image: with no digest" ".github/workflows/database-parity.yml" images

    edit .github/workflows/obs-vendors.yml '0,/^(\s+VECTOR_IMAGE: [^@ ]+)@sha256:[0-9a-f]{64}/s//\1/'
    expect_red "images: a workflow …_IMAGE with no digest" ".github/workflows/obs-vendors.yml" images

    edit .github/workflows/obs-vendors.yml '0,/"\$CURL_IMAGE"/s//docker.io\/curlimages\/curl:8.10.1/'
    expect_red "images: a workflow docker run naming its image by a literal" "names its image" images

    edit .github/workflows/release-readiness.yml '0,/"\$REDIS7_IMAGE"/s//docker.io\/library\/redis:7-alpine/'
    expect_red "images: the RDB proof's docker run naming redis by a literal" ".github/workflows/release-readiness.yml" images
}

matrix() {
    edit docs/release/Distribution_Surface_Matrix.md '/docker\.io\/timberio\/vector:/d'
    expect_red "matrix: an image the matrix has no row for" "docker.io/timberio/vector" matrix

    # The CI database's row, while compose's postgres:16-alpine row stays:
    # a row for one tag must not stand in for a tag it begins with.
    edit docs/release/Distribution_Surface_Matrix.md '/^\| `docker\.io\/library\/postgres:16`,/d'
    expect_red "matrix: postgres:16, with only postgres:16-alpine's row" "docker.io/library/postgres:16 is in no row" matrix

    edit docs/release/Distribution_Surface_Matrix.md '/services\/gateway\/requirements\.lock\.txt/d'
    expect_red "matrix: a lock the matrix has no row for" "services/gateway/requirements.lock.txt" matrix

    edit services/gateway/requirements.lock.txt 's/^litellm==([0-9]+)\.([0-9]+)\.([0-9]+)/litellm==\1.\2.9\3/'
    expect_red "matrix: the lock's LiteLLM is not the matrix's" "no LiteLLM row holds" matrix

    edit THIRD_PARTY.md 's/^\| litellm \| ([^|]+) \| MIT \|/| litellm | \1 | Apache-2.0 |/'
    expect_red "matrix: the LiteLLM row's licence is not THIRD_PARTY.md's" "no LiteLLM row holds" matrix

    local empty
    empty="$(mktemp -d)"
    git -C "$empty" init -q
    if $guard --root "$empty" > "$out" 2>&1; then
        echo "::error::probe stayed GREEN: a checkout with nothing to read — the guard proved nothing"
        failed=1
    else
        echo "red, as it must be: a checkout with nothing to read"
    fi
    rm -rf "$empty"
}

$guard > /dev/null || {
    echo "::error::the guard is red on the clean tree; fix that first"; exit 1; }

case "$which" in
    images) images ;;
    matrix) matrix ;;
    all) images; matrix ;;
    *) echo "usage: $0 [images|matrix|all]" >&2; exit 2 ;;
esac

if [ "$(git status --porcelain=v1 --untracked-files=all | sha256sum)" != "$before" ]; then
    echo "::error::the tree is not as it was: git status differs from the start"
    failed=1
fi
if [ "$failed" = 1 ]; then
    echo "::error::a probe stayed green, went red for another reason, or planted nothing (above): the guard is broken, not the tree"
    exit 1
fi
echo "every probe went red, and the tree is as it was"
