#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Negative tests for R11, the no-publication guard
# (scripts/check_no_publication.py; the K blueprint's §4, A2). Each probe
# plants one capability the guard exists to refuse, in
# .github/workflows/_probe_publication.yml — never committed, because a
# committed workflow runs — runs the real guard, and FAILS unless the guard
# exits 1 naming the probe's file and line. The controls must stay green:
# `pages: write` and `id-token: write` in docs-site.yml, the one workflow
# D21 allows them in; a release that attaches nothing; and the reads and
# the local build the push rules must not mistake for a push.
#
#   bash scripts/publication_probes.sh
#
# Nothing is committed, staged or left behind: a probe file is removed, and
# a tracked file a control would stand in for is restored from a copy,
# including when a probe fails half-way.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
backup=$(mktemp -d)
created=()
restored=()
failed=0
guard="python3 scripts/check_no_publication.py"
W=.github/workflows/_probe_publication.yml
A=.github/actions/_probe_publication/action.yml

cleanup() {
    local f
    for f in "${created[@]:-}"; do
        [ -n "$f" ] || continue
        rm -f -- "$f"
        rmdir -p -- "$(dirname "$f")" 2>/dev/null || true
    done
    for f in "${restored[@]:-}"; do
        [ -n "$f" ] || continue
        cp -p -- "$backup/$(printf '%s' "$f" | tr '/' '%')" "$f"
    done
    created=()
    restored=()
}
trap 'cleanup; rm -rf "$backup"' EXIT

# create PATH CONTENT — a new file the probe removes again.
create() {
    mkdir -p "$(dirname "$1")"
    printf '%s\n' "$2" > "$1"
    created+=("$1")
}

# keep PATH — remember a tracked file so a control can stand in for it.
keep() {
    cp -p -- "$1" "$backup/$(printf '%s' "$1" | tr '/' '%')"
    restored+=("$1")
}

# expect_named LABEL FILE:LINE — the guard must exit 1 and name FILE:LINE.
expect_named() {
    local label="$1" where="$2" out status=0
    out="$($guard 2>&1)" || status=$?
    if [ "$status" -eq 0 ]; then
        echo "::error::probe stayed GREEN: $label — the guard does not catch what it exists to catch"
        failed=1
    elif ! grep -qF -- "$where:" <<< "$out"; then
        echo "::error::probe went red but the guard did not name $where: $label"
        printf '%s\n' "$out" | sed 's/^/    /'
        failed=1
    else
        echo "red at $where, as it must be: $label"
    fi
    cleanup
}

# expect_green LABEL — a control: the guard must pass.
expect_green() {
    local label="$1" out status=0
    out="$($guard 2>&1)" || status=$?
    if [ "$status" -ne 0 ]; then
        echo "::error::control went RED: $label — the guard refuses what it must allow"
        printf '%s\n' "$out" | sed 's/^/    /'
        failed=1
    else
        echo "green, as it must be: $label"
    fi
    cleanup
}

# A workflow whose line 8 is the job's first key after runs-on; STEPS are
# its steps, from line 9 when a job permission is not given.
head='name: probe
on: workflow_dispatch
permissions:
  contents: read
jobs:
  probe:
    runs-on: ubuntu-latest'

steps() { printf '%s\n    steps:\n%s' "$head" "$1"; }
job_permission() { printf '%s\n    permissions:\n      %s\n    steps:\n      - run: echo probe' "$head" "$1"; }

$guard > /dev/null \
    || { echo "::error::the no-publication guard is red on the clean tree; fix that first"; exit 1; }

# The permissions.
create "$W" "$(job_permission 'packages: write')"
expect_named "a job that may write packages" "$W:9"
create "$W" "$(job_permission 'id-token: write')"
expect_named "a job that may mint an OIDC token outside docs-site.yml" "$W:9"
create "$W" "$(job_permission 'attestations: write')"
expect_named "a job that may write attestations" "$W:9"
create "$W" "$(job_permission 'pages: write')"
expect_named "pages: write outside docs-site.yml" "$W:9"
create "$W" "name: probe
on: workflow_dispatch
permissions: write-all
jobs:
  probe:
    runs-on: ubuntu-latest
    steps:
      - run: echo probe"
expect_named "permissions: write-all" "$W:3"
create "$W" "name: probe
on: workflow_dispatch
jobs:
  probe:
    runs-on: ubuntu-latest
    steps:
      - run: echo probe"
expect_named "a workflow with no top-level permissions" "$W:1"
# YAML reads a key however it is spelled, and so does the guard.
create "$W" "$(job_permission '"packages": write')"
expect_named "a quoted permission key" "$W:9"
create "$W" "$(job_permission "id-token: 'write'")"
expect_named "a quoted permission value" "$W:9"
create "$W" "$(printf '%s\n    permissions: {"attestations": "write"}\n    steps:\n      - run: echo probe' "$head")"
expect_named "a permission in a flow mapping" "$W:8"

# A login — a dormant one too: a step behind if: false is one edit from running.
create "$W" "$(steps '      - if: false
        uses: docker/login-action@v3')"
expect_named "a registry login action behind if: false" "$W:10"
create "$W" "$(steps '      - run: echo "$T" | podman login ghcr.io -u x --password-stdin')"
expect_named "podman login" "$W:9"
create "$A" "name: probe
runs:
  using: composite
  steps:
    - shell: bash
      run: echo \"\$T\" | docker login -u x --password-stdin"
expect_named "a login in a local action" "$A:6"
create "$W" "$(steps '      - {uses: docker/login-action@v3}')"
expect_named "a login action in a flow mapping" "$W:9"

# A push, and a cache export.
create "$W" "$(steps '      - uses: docker/build-push-action@v6
        with:
          push: true')"
expect_named "push: true" "$W:11"
create "$W" "$(steps '      - uses: docker/build-push-action@v6
        with:
          cache-to: type=gha,mode=max')"
expect_named "cache-to:" "$W:11"
create "$W" "$(steps '      - run: docker buildx build --push -t librerun .')"
expect_named "docker buildx build --push" "$W:9"
create "$W" "$(steps '      - run: docker image push librerun')"
expect_named "docker image push" "$W:9"
create "$W" "$(steps '      - run: git push origin HEAD')"
expect_named "git push" "$W:9"
create "$W" "$(steps '      - run: docker compose --profile app push')"
expect_named "a push after an option's value" "$W:9"
create "$W" "$(steps '      - run: ./compose.sh --profile app push')"
expect_named "a push by this tree's compose wrapper" "$W:9"
create "$W" "$(steps '      - uses: docker/build-push-action@v6
        with:
          "push": true')"
expect_named "a quoted push: key" "$W:11"
create "$W" "$(steps '      - run: nerdctl push librerun')"
expect_named "nerdctl push" "$W:9"
# A registry write that never says push.
create "$W" "$(steps '      - run: docker buildx build --output type=registry,name=ghcr.io/acme/app:latest .')"
expect_named "a registry exporter (--output type=registry)" "$W:9"
create "$W" "$(steps '      - uses: docker/build-push-action@v6
        with:
          outputs: type=image,name=librerun,push=true')"
expect_named "an image exporter with push=true" "$W:11"
create "$W" "$(steps '      - run: docker buildx imagetools create -t librerun:multi librerun:amd64 librerun:arm64')"
expect_named "buildx imagetools create" "$W:9"
create "$W" "$(steps '      - run: skopeo copy docker-archive:librerun.tar docker://registry.example/librerun')"
expect_named "skopeo copy" "$W:9"
create "$W" "$(steps '      - run: crane copy registry.example/a registry.example/b')"
expect_named "crane copy" "$W:9"

# A package publish.
create "$W" "$(steps '      - uses: pypa/gh-action-pypi-publish@release/v1')"
expect_named "the PyPI publish action" "$W:9"
create "$W" "$(steps '      - run: python -m twine upload dist/*')"
expect_named "twine upload" "$W:9"
create "$W" "$(steps '      - run: python -m twine --repository-url https://upload.pypi.org/legacy/ upload dist/*')"
expect_named "twine upload after an option's value" "$W:9"
create "$W" "$(steps '      - run: npm publish --access public')"
expect_named "npm publish" "$W:9"
create "$W" "$(steps '      - run: uv publish')"
expect_named "uv publish" "$W:9"

# A release asset.
create "$W" "$(steps '      - run: gh release upload v1.1.0 dist/librerun.whl')"
expect_named "gh release upload" "$W:9"
create "$W" "$(steps '      - run: |
          gh release create "$TAG" \
            --title "LibreRun" \
            --notes-file notes.md \
            dist/librerun.whl')"
expect_named "a file after gh release create's tag, on a continued line" "$W:13"
create "$W" "$(steps '      - run: gh release create "$TAG" --notes-file notes.md $asset')"
expect_named "a bare variable where an asset would stand" "$W:9"
create "$W" "$(steps '      - uses: softprops/action-gh-release@v2')"
expect_named "a third-party release action" "$W:9"

# A secret.
create "$W" "$(steps '      - run: echo ${{ secrets.PYPI_TOKEN }}')"
expect_named "a secrets expression" "$W:9"
create "$W" "$(steps "      - run: echo \${{ secrets['NPM_TOKEN'] }}")"
expect_named "a secrets expression, bracketed" "$W:9"
create "$W" "name: probe
on: workflow_dispatch
permissions:
  contents: read
jobs:
  probe:
    uses: ./.github/workflows/unit-suites.yml
    secrets: inherit"
expect_named "secrets: inherit" "$W:8"
create "$W" "name: probe
on: workflow_dispatch
permissions:
  contents: read
jobs:
  probe:
    uses: ./.github/workflows/unit-suites.yml
    \"secrets\": inherit"
expect_named "a quoted secrets: inherit" "$W:8"
create "$W" "$(steps "      - run: echo '\${{ toJSON(secrets) }}'")"
expect_named "the whole secrets context" "$W:9"

# A guard that read nothing has proved nothing.
if $guard --root "$backup" > /dev/null 2>&1; then
    echo "::error::probe stayed GREEN: the guard passed a tree with no workflow in it"
    failed=1
else
    echo "red, as it must be: a tree with no workflow"
fi

# The controls.
if [ -e .github/workflows/docs-site.yml ]; then
    keep .github/workflows/docs-site.yml
fi
create .github/workflows/docs-site.yml "name: docs-site
on: workflow_dispatch
permissions:
  contents: read
jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      pages: write
      id-token: write
    steps:
      - uses: actions/deploy-pages@v4"
expect_green "pages: write and id-token: write in docs-site.yml (D21)"
create "$W" "$(steps '      - run: |
          gh release create "$TAG" \
            --title "LibreRun $VERSION" \
            --notes-file notes.md \
            $flags \
            --prerelease="$PRERELEASE" \
            --verify-tag')"
expect_green "a release with notes and no file"
create "$W" "$(steps '      - uses: docker/build-push-action@v6
        with: {context: ., push: false, load: true, cache-from: type=registry,ref=registry.example/cache}
      - run: docker buildx build --output type=docker -t librerun .
      - run: docker buildx imagetools inspect registry.example/librerun
      - run: skopeo inspect docker://registry.example/librerun && crane digest registry.example/librerun
      - run: echo ${{ secrets['"'"'GITHUB_TOKEN'"'"'] }} ${{ inputs.secrets }}')"
expect_green "reads and a local build: push: false, a registry cache read, the docker exporter, inspect, digest, the token"

if [ "$failed" = 1 ]; then
    echo "::error::a probe stayed green or went red wrongly (above): the guard is broken, not the tree"
    exit 1
fi
echo "every probe went red, and the tree is as it was"
