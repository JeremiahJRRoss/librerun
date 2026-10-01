#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# The Developer Certificate of Origin 1.1, enforced (blueprint S9, L17):
# LibreRun takes contributions under a DCO sign-off and no CLA, and a
# sign-off that is not checked is a sign-off nobody writes.
#
#     .github/dco_check.sh <exclude-ref> <head-ref>
#
# It checks every commit reachable from <head-ref> and NOT from
# <exclude-ref>. Written as `rev-list head --not exclude` rather than
# `exclude..head` on purpose: when a pull request merges its base branch
# in, the base's own commits become reachable from head, and a range
# anchored to a stale base sha would demand a sign-off from whoever wrote
# them — someone who is not the author of this pull request and cannot
# fix it. Excluding the base branch as it stands NOW asks each author for
# exactly the commits they are contributing.
#
# Merge commits are checked like any other. A merge can carry conflict
# resolutions — authored lines, by the person who made it — and a rule
# with an exemption in it is a rule with a place to hide. `git merge
# --signoff` writes the trailer; CONTRIBUTING.md says so.
set -euo pipefail

exclude="${1:?usage: dco_check.sh <exclude-ref> <head-ref>}"
head_ref="${2:?usage: dco_check.sh <exclude-ref> <head-ref>}"

fail=0
checked=0
while read -r sha; do
    [ -n "$sha" ] || continue
    checked=$((checked + 1))
    author_name=$(git log -1 --format='%an' "$sha")
    author_email=$(git log -1 --format='%ae' "$sha")
    subject=$(git log -1 --format='%s' "$sha")
    wanted="Signed-off-by: ${author_name} <${author_email}>"
    # -x: the whole line, so a sign-off mentioned inside prose does not
    # count. -F: literal, because a name may contain regex characters.
    # -i: emails are case-insensitive and people capitalise the trailer
    # in every way there is. Trailing whitespace is stripped first so a
    # trailer with a stray space still counts as one.
    #
    # A HERE-STRING, not a pipe into `grep -q`. `-q` exits at the first
    # match, the writer upstream gets SIGPIPE, and under `set -o
    # pipefail` the pipeline then reports failure — so a sign-off that IS
    # there reads as missing. It is a race with the message's length: it
    # passed on every commit locally and failed in CI on the longest one,
    # with `sed: couldn't flush stdout: Broken pipe` printed above the
    # error. A here-string has no pipe and no race.
    body=$(git log -1 --format='%B' "$sha" | sed -e 's/[[:space:]]*$//')
    if grep -qixF "$wanted" <<< "$body"; then
        continue
    fi
    fail=1
    echo "  ${sha}  ${subject}"
    echo "      author: ${author_name} <${author_email}>"
    echo "      wanted: ${wanted}"
done < <(git rev-list "$head_ref" --not "$exclude")

if [ "$fail" = 1 ]; then
    cat >&2 <<'MSG'
::error::A commit in this pull request has no Signed-off-by line matching its author (listed above). LibreRun takes contributions under the Developer Certificate of Origin 1.1 (https://developercertificate.org/) — no CLA, no copyright assignment, but the sign-off is required and is not waived. Fix it with:  git commit --amend -s --no-edit  (the last commit),  git rebase --signoff <base>  (every commit on the branch), or  git merge --signoff  (a merge commit). The name and email in the trailer must be the commit author's own: the DCO is a statement by the author about their own right to contribute, so a sign-off naming somebody else is not one.
MSG
    exit 1
fi
echo "DCO: ${checked} commit(s), every one signed off by its author"
