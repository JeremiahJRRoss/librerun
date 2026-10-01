#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# ============================================================================
# LibreRun — write the public repository's owner into the tree (the K
# blueprint's B1a; LIC-12, L39)
#
#   ./scripts/resolve_public_owner.sh <handle>
#
# The public repository belongs to the maintainer's personal GitHub account,
# and until its handle is given the tree carries a placeholder in its place:
# in every `github.com/<placeholder>/` URL, and as `@<placeholder>` in
# .github/CODEOWNERS. This rewrites each of them to the handle, and
# CODEOWNERS names that one user and no team, since a personal account has
# none. The holder and both contacts are resolved already (NOTICE,
# CODE_OF_CONDUCT.md, SECURITY.md), and the changelog is dated by the
# release, not here, so the owner is all this writes.
#
# Run it on a branch of the repository that prepares the release, on a
# clean tree; read `git diff`, rewrite by hand the prose that explains the
# placeholder (docs/platform/Releasing.md §5 lists it), commit signed off
# and merge by a pull request. Then `prepare_public_repo.sh --check` says
# no placeholder is left. It never commits, fetches or pushes.
#
# It refuses a handle GitHub would not take as a user name — 1 to 39
# letters, digits or single inner hyphens — and one publish-purity flags:
# the public owner's handle names nothing of the development repository's
# owner (L39). It exits 1 naming any owner site it leaves, such as a team
# (`@<placeholder>/…`) or a URL with no path after the owner.
#
# The placeholder is ASSEMBLED, as prepare_public_repo.sh assembles it, so
# this file is no site of its own and stays in the tree after use:
# public_commit_probes.sh plants a site to prove it still resolves one.
# ============================================================================
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

case "${1:-}" in
    -h|--help)
        awk '/^# =+$/ { n++; next } n == 1 && /^#/ { sub(/^# ?/, ""); print } n >= 2 { exit }' "$0"
        exit 0 ;;
esac
if [ $# -ne 1 ]; then
    echo >&2 "usage: resolve_public_owner.sh <handle>   (try --help)"
    exit 2
fi
handle="$1"
P_OWNER='OWNER'

# A refusal never repeats the handle: the one publish-purity flags would
# spell the development repository's owner in the log.
if ! [[ "$handle" =~ ^[A-Za-z0-9]([A-Za-z0-9]|-[A-Za-z0-9])*$ ]] || [ "${#handle}" -gt 39 ]; then
    echo "::error::The handle given is not a GitHub user name: 1 to 39 letters, digits or single inner hyphens, with no hyphen first or last."
    exit 1
fi
if [ "${handle,,}" = "${P_OWNER,,}" ]; then
    echo "::error::The handle given is the placeholder itself."
    exit 1
fi
if ! python3 scripts/check_purity.py --text "$handle" > /dev/null; then
    echo "::error::publish-purity flags the handle given (L39): the public repository's owner must name nothing of the development repository's owner. Nothing was written."
    exit 1
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "::error::The tracked files have uncommitted changes. Run this on a clean tree, so that git diff shows its edits alone. Nothing was written."
    exit 1
fi

# Each site, and what it becomes: an owner in a URL, before a slash; an
# owner after `@` that is not the start of a team or a longer name.
url="github\.com/${P_OWNER}/"
at="@${P_OWNER}([^A-Za-z0-9_/-]|\$)"
# Anything else that still names the placeholder as an owner is a site
# left, and prepare_public_repo.sh would refuse it: a team, or a URL whose
# owner has no path after it.
left="github\.com/${P_OWNER}([^A-Za-z0-9_-]|\$)|@${P_OWNER}([^A-Za-z0-9_-]|\$)"

files=0
sites=0
while IFS= read -r -d '' path; do
    if [ -L "$path" ] || [ ! -f "$path" ]; then
        continue
    fi
    n=$(grep -cE "${url}|${at}" -- "$path" || true)
    sed -i -E \
        -e "s#${url}#github.com/${handle}/#g" \
        -e "s#@${P_OWNER}([^A-Za-z0-9_/-])#@${handle}\\1#g" \
        -e "s#@${P_OWNER}\$#@${handle}#" \
        -- "$path"
    files=$((files + 1))
    sites=$((sites + n))
    echo "  resolved  ${path}  (${n} line(s))"
done < <(git grep -lIzE "${url}|${at}" || true)

remaining=$(git grep -nIE "$left" || true)
if [ -n "$remaining" ]; then
    printf '%s\n' "$remaining" | sed 's/^/  left      /'
    echo "::error::$(printf '%s\n' "$remaining" | wc -l) owner site(s) are left (above): each is a team or a URL this script does not rewrite. A personal account has no teams; resolve each by hand, or remove it."
    exit 1
fi
if [ "$files" -eq 0 ]; then
    echo "no owner placeholder is left in the tracked tree: nothing to write."
    exit 0
fi
echo
echo "resolved: ${sites} line(s) in ${files} file(s); no owner placeholder is left."
echo "Next: git diff; the prose that explains the placeholder, by hand"
echo "(docs/platform/Releasing.md §5); commit it signed off, open the pull"
echo "request and merge it; then scripts/prepare_public_repo.sh --check."
