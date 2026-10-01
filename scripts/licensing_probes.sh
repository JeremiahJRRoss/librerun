#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Negative tests for the licensing and purity checks (docs/release/
# License_Scope_Map.md). Each probe injects the exact violation a rule
# exists to catch, runs the real checker, and FAILS if the checker stays
# green; then it puts the tree back. On a clean tree a broken checker and
# a working one look identical, so this is how the checks prove they
# still look.
#
#   bash scripts/licensing_probes.sh              # every probe
#   bash scripts/licensing_probes.sh licensing    # the check_licensing.py rules
#   bash scripts/licensing_probes.sh reuse        # reuse lint (needs `reuse`)
#   bash scripts/licensing_probes.sh purity       # publish-purity
#
# Nothing is committed and no branch is created. A probe file is staged
# only with `git add -N` (or `git add` for a symlink, whose blob must be
# real) and unstaged again; a tracked file a probe edits is restored from
# a copy, including when a probe fails half-way.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
which="${1:-all}"
backup=$(mktemp -d)
created=()
restored=()
failed=0

cleanup() {
    local f
    for f in "${created[@]:-}"; do
        [ -n "$f" ] || continue
        git rm -q --cached --force -r -- "$f" >/dev/null 2>&1 || true
        rm -rf -- "$f"
    done
    for f in "${restored[@]:-}"; do
        [ -n "$f" ] || continue
        cp -p -- "$backup/$(printf '%s' "$f" | tr '/' '%')" "$f"
    done
    created=()
    restored=()
}
trap 'cleanup; rm -rf "$backup"' EXIT

# create PATH CONTENT — a new file, staged with intent-to-add.
create() {
    mkdir -p "$(dirname "$1")"
    printf '%s\n' "$2" > "$1"
    git add -N -- "$1"
    created+=("$1")
}

# keep PATH — remember a tracked file so the probe can edit it.
keep() {
    cp -p -- "$1" "$backup/$(printf '%s' "$1" | tr '/' '%')"
    restored+=("$1")
}

# expect_red LABEL COMMAND… — the command must FAIL on the injected violation.
expect_red() {
    local label="$1"
    shift
    if "$@" > /dev/null 2>&1; then
        echo "::error::probe stayed GREEN: $label — the check does not catch what it exists to catch"
        failed=1
    else
        echo "red, as it must be: $label"
    fi
    cleanup
}

# development_repository REPO — whether this is the development repository,
# decided from what check_purity.py does not hold (#148 item 1). The public
# repository is the one README.md's clone URL names: its owner a
# placeholder until the owner is resolved, the maintainer's handle after.
# In CI the event's own record of the repository decides. A public
# repository is never this one: the public repository and any fork of it
# are public. A private one is, unless its owner is the clone URL's: the
# tree is resolved and published from a private repository of the public
# handle's own (the K blueprint's §7 item 18), and that one is not the
# development repository, whose owner the clone URL never names (L39).
# With no event to read (a local run), the slug is compared with the
# public one: this is the development repository when neither its owner
# nor REPO, its name, is the public repository's. An answer that cannot
# be read — no owner, no clone URL — falls back to the rule without it.
# Prints the reason when the answer is yes.
development_repository() {
    local private="" owner="" public_owner="" public_name="" slug=""
    read -r public_owner public_name < <(sed -nE \
        's#^git clone https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)\.git( .*)?$#\1 \2#p' \
        README.md 2>/dev/null | head -n 1) || true
    if [ -n "${GITHUB_EVENT_PATH:-}" ] && [ -r "$GITHUB_EVENT_PATH" ]; then
        read -r private owner < <(python3 -c 'import json, sys
event = json.load(open(sys.argv[1], encoding="utf-8"))
repository = event.get("repository") or {}
print(repository.get("private", ""), (repository.get("owner") or {}).get("login", ""))' \
            "$GITHUB_EVENT_PATH" 2>/dev/null) || true
    fi
    case "$private" in
        True)
            if [ -n "$owner" ] && [ -n "$public_owner" ] && [ "${owner,,}" = "${public_owner,,}" ]; then
                return 1
            fi
            echo "the event says this repository is private, and its owner is not the one README.md's clone URL names"
            return 0 ;;
        False) return 1 ;;
    esac
    [ -n "$1" ] || return 1
    slug="${GITHUB_REPOSITORY:-}"
    if [ -z "$slug" ]; then
        slug="$(git remote get-url origin 2>/dev/null || true)"
        slug="$(sed -E 's#^.*[:/]([^/]+/[^/]+)$#\1#; s#\.git$##' <<< "$slug")"
    fi
    public_name="${public_name:-LibreRun}"
    [ "${1,,}" != "${public_name,,}" ] || return 1
    case "$slug" in
        */*) owner="${slug%%/*}" ;;
        *) owner="" ;;
    esac
    if [ -n "$owner" ] && [ -n "$public_owner" ] && [ "${owner,,}" = "${public_owner,,}" ]; then
        return 1
    fi
    echo "neither the owner nor the name is the public repository's, and no event said whether it is private"
}

licensing() {
    local c="python3 scripts/check_licensing.py"
    python3 scripts/check_licensing.py > /dev/null \
        || { echo "::error::the licensing checks are red on the clean tree; fix that first"; exit 1; }

    create scripts/_probe_no_header.sh "$(printf '#!/usr/bin/env bash\necho probe')"
    expect_red "headers: a script with no SPDX header" $c headers

    create scripts/_probe_apache.sh "$(printf '#!/usr/bin/env bash\n# SPDX-License-Identifier: Apache-2.0\necho probe')"
    expect_red "headers: an Apache-2.0 header outside the carve-outs" $c headers

    keep cli/pyproject.toml
    sed -i 's/^license = "AGPL-3.0-only AND Apache-2.0"$/license = "AGPL-3.0-only"/' cli/pyproject.toml
    expect_red "metadata: the CLI's manifest drops the templates' Apache-2.0" $c metadata

    keep frontend/Dockerfile
    sed -i 's/image.licenses="AGPL-3.0-only"/image.licenses="Apache-2.0"/' frontend/Dockerfile
    expect_red "metadata: the web image's label says Apache-2.0" $c metadata

    keep NOTICE
    sed -i 's/, to the extent copyright subsists in/ in/' NOTICE
    expect_red "notice: NOTICE drops the qualification from the copyright statement" $c notice

    keep LICENSE
    printf '\n' >> LICENSE
    expect_red "notice: LICENSE is not the FSF's AGPL-3.0 text byte for byte" $c notice

    create docs/_probe_stale.md "LibreRun is released under the Apache License 2.0."
    expect_red "statements: a document says the project is Apache-2.0" $c statements

    # #148 item 5: the wordings #147 removed — the release notes' old
    # footer, and the whole-tree claim the 1.0 announcement and the PRD made.
    create docs/_probe_stale.md "Licensed under Apache-2.0 (LICENSE, NOTICE); the name and logo are reserved."
    expect_red "statements: a sentence opens with licensed under Apache (#148 item 5)" $c statements

    create docs/_probe_stale.md "Apache-2.0, across the whole tree (LICENSE, NOTICE)."
    expect_red "statements: Apache-2.0 across the whole tree (#148 item 5)" $c statements

    keep .gitattributes
    printf 'NOTICE export-ignore\n' >> .gitattributes
    expect_red "bundle: NOTICE would be left out of a source archive" $c bundle

    # #148 item 2: export-ignore on a DIRECTORY drops every file under it.
    # The second form is set on nothing `git check-attr` is asked about —
    # the directory and each file in it read "unspecified" — and `git
    # archive` still leaves LICENSES/ out.
    keep .gitattributes
    printf 'LICENSES export-ignore\n' >> .gitattributes
    expect_red "bundle: LICENSES/ export-ignored as a directory (#148 item 2)" $c bundle

    keep .gitattributes
    printf 'LICENSES/ export-ignore\n' >> .gitattributes
    expect_red "bundle: LICENSES/ export-ignored by a trailing-slash pattern (#148 item 2)" $c bundle

    keep backend/requirements.lock.txt
    printf 'zzz-probe-package==0.0.1\n' >> backend/requirements.lock.txt
    expect_red "third-party: a locked dependency with no THIRD_PARTY.md row" $c third-party

    # A3: a row keys on its digest, so a reference whose digest moved while
    # its THIRD_PARTY.md cell did not is caught (a refresh rewrites both).
    keep compose.yaml
    sed -i -E '0,/(image: docker\.io\/library\/postgres:[^@ ]+@sha256:)[0-9a-f]{64}/s//\1'"$(printf '0%.0s' $(seq 64))"'/' compose.yaml
    expect_red "third-party: a digest THIRD_PARTY.md does not record" $c third-party

    create docs/_probe_vendored.md "Copyright (c) 2019 Probe Vendor, Inc. All rights reserved."
    expect_red "third-party: a foreign copyright notice REUSE.toml does not account for" $c third-party
}

reuse_probe() {
    command -v reuse > /dev/null || { echo "::error::reuse is not installed"; exit 1; }
    reuse lint > /dev/null || { echo "::error::reuse lint is red on the clean tree; fix that first"; exit 1; }
    create scripts/_probe_gpl.sh "$(printf '#!/usr/bin/env bash\n# SPDX-License-Identifier: GPL-2.0-only\necho probe')"
    expect_red "reuse: a file declares a licence whose text is not in LICENSES/" reuse lint
}

purity() {
    local c="python3 scripts/check_purity.py"
    python3 scripts/check_purity.py > /dev/null \
        || { echo "::error::publish-purity is red on the clean tree; fix that first"; exit 1; }
    # The canary, assembled from fragments: spelled whole, this file would
    # trip the check it tests.
    local canary
    canary="$(printf '%s%s' 'purity' 'canary')"

    create docs/_probe_purity.md "see https://example.org/$canary/notes"
    expect_red "purity: the canary in a file's contents" $c

    create "docs/$(printf '%s-%s' 'PURITY' 'CANARY')_probe/notes.md" "nothing to see here"
    expect_red "purity: the canary in a PATH, upper-case and hyphenated, with clean contents" $c

    # A real `git add`: an intent-to-add entry carries the EMPTY blob, and
    # the symlink's target is its blob.
    ln -s "../$(printf '%s_%s' 'purity' 'canary')/notes.md" docs/_probe_purity_link.md
    git add -- docs/_probe_purity_link.md
    created+=("docs/_probe_purity_link.md")
    expect_red "purity: the canary in a SYMLINK TARGET" $c

    create docs/_probe_purity.md "the acme${canary}io bucket"
    expect_red "purity: the canary run together with other words" $c

    # The real names, derived at run time — never spelled here: in CI from
    # $GITHUB_REPOSITORY, locally from the origin remote. They run only
    # where this IS the development repository, and development_repository
    # decides that from nothing the check under test holds (#148 item 1).
    # Asked of check_purity.py, as it once was, one deleted digest or one
    # dropped length skipped every probe below and left this job green
    # while the gate passed a tree naming the owner. Anywhere else (the
    # public repository, a fork of it) only the canary runs, and says so.
    local slug="${GITHUB_REPOSITORY:-}"
    if [ -z "$slug" ]; then
        slug="$(git remote get-url origin 2>/dev/null | sed -E 's#^.*[:/]([^/]+/[^/]+)$#\1#; s#\.git$##')"
    fi
    local owner="" repo=""
    case "$slug" in
        */*) owner="${slug%%/*}" repo="${slug#*/}" ;;
    esac
    local why
    if why="$(development_repository "$repo")"; then
        echo "::notice::taken for the development repository ($why); the real-name probes run"
        if [ -z "$owner" ] || [ -z "$repo" ]; then
            echo "::error::this is the development repository, but no owner/repository slug could be derived to probe with"
            failed=1
            return
        fi
        # Each kept name must be flagged on its own. If either stays green
        # here, check_purity.py has lost its digest or its length — the
        # gate is off — or this private repository is not the development
        # repository and the probe cannot tell; either way, a person looks.
        expect_red "purity: the owner's name, alone (#148 item 1)" $c --text "$owner"
        expect_red "purity: the repository's name, alone (#148 item 1)" $c --text "$repo"
        create docs/_probe_purity.md "see the $repo notes for the history"
        expect_red "purity: the repository's name alone, in a file's contents (#148 item 1)" $c
        local first="${owner%%-*}" rest="${owner#*-}"
        create docs/_probe_purity.md "https://github.com/$slug/blob/main/README.md"
        expect_red "purity: the development repository's URL" $c
        create docs/_probe_purity.md "ghcr.io/${owner,,}/librerun-backend:1.0.0"
        expect_red "purity: the owner in a lower-cased image path" $c
        create docs/_probe_purity.md "Copyright 2026 $first $rest, Inc. All rights reserved."
        expect_red "purity: the owner's name written as prose" $c
        create docs/_probe_purity.md "write to someone@${first,,}.${rest,,}"
        expect_red "purity: the owner's e-mail domain" $c
        create docs/_probe_purity.md "reviewed by @jr_${first}"
        expect_red "purity: the owner's name inside a handle" $c
    else
        echo "::notice::not the development repository; only the canary probes ran"
    fi
}

case "$which" in
    licensing) licensing ;;
    reuse) reuse_probe ;;
    purity) purity ;;
    all) licensing; reuse_probe; purity ;;
    *) echo "usage: $0 [licensing|reuse|purity|all]" >&2; exit 2 ;;
esac

if [ "$failed" = 1 ]; then
    echo "::error::a probe stayed green (above): that check is broken, not the tree"
    exit 1
fi
echo "every probe went red, and the tree is as it was"
