#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Negative tests for the public commit's machinery (R18's probes; the K
# blueprint's B1a): scripts/prepare_public_repo.sh — the first commit, the
# fast-forward after it and its revert guard — and
# scripts/resolve_public_owner.sh. Each plant must go red, for its own
# reason, and each control must stay green.
#
#   bash scripts/public_commit_probes.sh
#
# It works in a throwaway `git clone --no-hardlinks` of HEAD, in a
# temporary directory, and leaves this checkout as it was: no file, no
# branch and no ref under refs/librerun. The public repository it
# publishes to is a bare repository in that directory, and git is allowed
# the file protocol alone, so nothing reaches a network.
#
# The green controls:
#   - the owner resolved with a probe handle, a site of the probe's own
#     among the rewritten ones, so the script is proved after the real
#     owner is resolved too;
#   - the first commit, every proof printed and the same SHA on a second
#     run; its printed commands, run against the bare repository, leave
#     main alone there, at that SHA;
#   - a candidate on top, and the fast-forward: one parent, the public
#     main, the tree identical and the same SHA twice; its printed
#     commands move main forward in a repository that refuses anything
#     but a fast-forward, and push no tag;
#   - --allow-placeholders on a tree that carries them: a rehearsal, which
#     prints no push;
#   - licensing_probes.sh's development_repository, read from that file:
#     a private repository under another owner is the development
#     repository, a public one never is, and a local run compares the slug.
# The plants, each of which must go red:
#   - a public commit the candidate lacks (the revert guard), which passes
#     once the candidate carries it;
#   - a --base whose tree the public main never had;
#   - a placeholder in the candidate;
#   - the purity canary in the tree, and in PUBLIC_EMAIL in a copy of the
#     script;
#   - the canary as the handle, and the real owner where this is the
#     development repository — which development_repository decides, never
#     check_purity.py's kept digests (#148 item 1: a probe that asks the
#     check under test whether to run skips when the check is broken);
#   - a malformed handle, and an owner site the owner script leaves;
#   - a private repository whose owner is the clone URL's, taken for the
#     development repository;
#   - a rehearsal that prints a push: a copy of the script with its
#     rehearsal switched off, which the rehearsal control's detector must
#     catch.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
root=$(pwd)
failed=0

# Everything this checkout must still be when the probes are done.
state() {
    git rev-parse HEAD
    git status --porcelain=v1 --untracked-files=all
    git branch --list --all
    git for-each-ref refs/librerun
    git worktree list --porcelain
}
before=$(state)

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export GIT_ALLOW_PROTOCOL=file

# The canary, assembled from fragments as licensing_probes.sh assembles
# it: spelled whole, this file would trip the check it tests. The owner
# placeholder is held in a variable, as the two scripts hold it.
canary="$(printf '%s%s' 'purity' 'canary')"
P_OWNER='OWNER'
handle="librerun-probe"

# The real owner, derived at run time and never spelled: in CI from
# $GITHUB_REPOSITORY, locally from the origin remote. A checkout with no
# origin has no owner to derive, and the probes run without one.
slug="${GITHUB_REPOSITORY:-}"
if [ -z "$slug" ]; then
    origin_url="$(git remote get-url origin 2>/dev/null || true)"
    slug="$(sed -E 's#^.*[:/]([^/]+/[^/]+)$#\1#; s#\.git$##' <<< "$origin_url")"
fi
real_owner="" real_repo=""
case "$slug" in
    */*) real_owner="${slug%%/*}" real_repo="${slug#*/}" ;;
esac

# The one rule for "this is the development repository", read from
# licensing_probes.sh rather than copied, and asked here, in this checkout,
# with this run's own event and remote.
rule="$(sed -n '/^development_repository() {$/,/^}$/p' scripts/licensing_probes.sh)"
if [ -z "$rule" ]; then
    echo "::error::development_repository() is not in scripts/licensing_probes.sh; the probes cannot tell whether this is the development repository"
    exit 1
fi
eval "$rule"
dev=0
if dev_why="$(development_repository "$real_repo")"; then
    dev=1
fi

fatal() {
    echo "::error::$1"
    exit 1
}

# expect_red LABEL TEXT COMMAND… — the command must fail, and say TEXT.
expect_red() {
    local label="$1" text="$2" out status=0
    shift 2
    out="$("$@" 2>&1)" || status=$?
    if [ "$status" -eq 0 ]; then
        echo "::error::probe stayed GREEN: $label — the check does not catch what it exists to catch"
        failed=1
    elif ! grep -qF -- "$text" <<< "$out"; then
        echo "::error::probe went red for another reason: $label — it did not say \"$text\""
        printf '%s\n' "$out" | tail -n 6 | sed 's/^/    /'
        failed=1
    else
        echo "red, as it must be: $label"
    fi
}

# need LABEL COMMAND… — a control: the command must pass. Its output is
# kept in $last for the checks after it; the probes cannot go on without it.
need() {
    local label="$1" status=0
    shift
    last="$("$@" 2>&1)" || status=$?
    if [ "$status" -ne 0 ]; then
        printf '%s\n' "$last" | tail -n 12 | sed 's/^/    /'
        fatal "control went RED: $label"
    fi
    echo "green, as it must be: $label"
}

# commit MESSAGE — everything in the clone's working tree, committed.
commit() {
    git add -A
    git commit -q -m "$1"
    git rev-parse HEAD
}

# commit_of OUTPUT — the SHA prepare_public_repo.sh printed.
commit_of() {
    sed -nE 's/^  commit     : ([0-9a-f]{40})$/\1/p' <<< "$1"
}

# proved LABEL OUTPUT PROOF… — each proof line is in the output.
proved() {
    local label="$1" out="$2" proof
    shift 2
    for proof in "$@"; do
        grep -qF -- "$proof" <<< "$out" || fatal "$label: no proof reads \"$proof\""
    done
    echo "green, as it must be: $label"
}

# commands_of OUTPUT — the commands printed for the release owner.
commands_of() {
    sed -n '/^== to publish/,$p' <<< "$1" | grep -E '^       git ' || true
}

# printed LABEL OUTPUT SHA HOW — the printed commands check the bundle,
# take its commit into a bare repository (HOW: `init` or `clone`), and
# push main alone, once: no other ref, no mirror, no tag, never --force.
printed() {
    local label="$1" cmds
    cmds="$(commands_of "$2")"
    grep -qE '^ +git bundle verify ' <<< "$cmds" || fatal "$label: no git bundle verify"
    grep -qE "^ +git $4 --bare " <<< "$cmds" || fatal "$label: no git $4 --bare"
    [ "$(grep -cE '^ +git push ' <<< "$cmds" || true)" = 1 ] || fatal "$label: not exactly one git push"
    grep -qxF "       git push ${public_url} ${3}:refs/heads/main" <<< "$cmds" \
        || fatal "$label: the push is not ${3}:refs/heads/main to the public repository"
    if grep -qE -- '--force|(^| )-f( |$)|--mirror|--tags|--all|--delete|refs/tags/|git tag' <<< "$cmds"; then
        fatal "$label: a forced, mirrored, tag or other push is printed"
    fi
    echo "green, as it must be: $label"
}

# run_printed OUTPUT DIR — the printed commands, run from DIR with the
# public repository's URL replaced by the bare one here. The line that
# reads the candidate's tree runs in the preparing repository, and the
# proofs have checked that already.
run_printed() {
    mkdir -p "$2"
    { commands_of "$1" | grep -vF '# here' || true; } | sed "s#${public_url}#${public}#g" > "$2/commands.sh"
    grep -qF "$public" "$2/commands.sh" || fatal "no printed command names the public repository"
    (cd "$2" && bash -euo pipefail commands.sh) > "$2/log" 2>&1 \
        || { sed 's/^/    /' "$2/log"; fatal "the printed commands failed (above)"; }
}

# public_refs — every ref the public repository holds.
public_refs() {
    git -C "$public" for-each-ref --format='%(refname) %(objectname)'
}

# prints_no_push OUTPUT — the detector a rehearsal is held to.
prints_no_push() {
    if grep -qE '^ +git push |^== to publish' <<< "$1"; then
        echo "a push is printed"
        return 1
    fi
}

# wrote_nothing LABEL — a refused handle left the working tree alone.
wrote_nothing() {
    if [ -n "$(git status --porcelain)" ]; then
        echo "::error::$1: the refusal wrote to the tree"
        git status --short | sed 's/^/    /'
        git reset -q --hard
        failed=1
    fi
}

# dev_repo EVENT SLUG NAME — development_repository's answer in a subshell
# with that event (none when empty) and that slug.
dev_repo() {
    (
        if [ -n "$1" ]; then export GITHUB_EVENT_PATH="$1"; else unset GITHUB_EVENT_PATH; fi
        export GITHUB_REPOSITORY="$2"
        development_repository "$3"
    ) > /dev/null
}

# ---------------------------------------------------------------------------
# The throwaway clone, and the public repository
# ---------------------------------------------------------------------------
repo="$work/repo"
public="$work/public.git"
# The bundles go where a pasted command must quote them: a space and a
# quote in the directory's name, so the printed commands, run below,
# prove each path is printed as one word.
bundles="$work/the owner's bundles"
mkdir -p "$bundles"
git clone -q --no-hardlinks --no-checkout "$root" "$repo"
git -C "$repo" fetch -q "$root" HEAD
git -C "$repo" checkout -q --detach FETCH_HEAD
git init -q --bare --initial-branch=main "$public"
git -C "$public" config receive.denyNonFastForwards true
git -C "$public" config receive.denyDeletes true
cd "$repo"
git config user.name "Probe"
git config user.email "probe@librerun.invalid"
git config commit.gpgsign false

# ---------------------------------------------------------------------------
# The owner, resolved with a probe handle
# ---------------------------------------------------------------------------
mkdir -p docs
printf 'see https://github.com/%s/LibreRun/blob/main/README.md\n/probe/  @%s\n' "$P_OWNER" "$P_OWNER" \
    > docs/_probe_owner.md
commit "probe: an owner site of the probe's own" > /dev/null

for bad in "-${handle}" "${handle}-" "librerun--probe" "librerun_probe" \
        "$(printf 'a%.0s' $(seq 40))" ""; do
    expect_red "a malformed handle ('${bad}')" "is not a GitHub user name" \
        bash scripts/resolve_public_owner.sh "$bad"
    wrote_nothing "a malformed handle"
done
expect_red "the canary as the handle" "publish-purity flags the handle given" \
    bash scripts/resolve_public_owner.sh "$canary"
wrote_nothing "the canary as the handle"
if [ "$dev" = 1 ]; then
    echo "::notice::taken for the development repository ($dev_why); its owner is tried as the handle"
    if [ -z "$real_owner" ]; then
        echo "::error::this is the development repository, but no owner could be derived to try"
        failed=1
    else
        expect_red "the development repository's owner as the handle" "publish-purity flags the handle given" \
            bash scripts/resolve_public_owner.sh "$real_owner"
        wrote_nothing "the development repository's owner as the handle"
    fi
else
    echo "::notice::not the development repository; its owner is not tried as the handle"
fi

need "the owner resolved with a probe handle" bash scripts/resolve_public_owner.sh "$handle"
if ! grep -qxF "see https://github.com/${handle}/LibreRun/blob/main/README.md" docs/_probe_owner.md \
        || ! grep -qxF "/probe/  @${handle}" docs/_probe_owner.md; then
    fatal "the owner script did not rewrite the probe's own sites"
fi
c1=$(commit "probe: the owner resolved")
need "no placeholder is left in the resolved candidate" \
    bash scripts/prepare_public_repo.sh --check --ref "$c1"
# The public repository, as the resolved README.md's clone line names it:
# the probe handle's where the owner was a placeholder, and the real
# handle's where it was resolved before these probes ran.
public_url=$(sed -nE '/^git clone https:\/\/github\.com\//{s#^git clone (https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+\.git)( .*)?$#\1#p;q;}' \
    README.md)
public_owner=$(sed -E 's#^https://github\.com/([^/]+)/.*#\1#' <<< "$public_url")
[ -n "$public_url" ] || fatal "README.md has no clone line naming the public repository"

# ---------------------------------------------------------------------------
# The first commit
# ---------------------------------------------------------------------------
need "the first commit" bash scripts/prepare_public_repo.sh --ref "$c1" --out "$bundles/first.bundle"
first="$last"
proved "the first commit's proofs" "$first" "identical to ${c1}" "one commit, no parents" \
    "identity   : Jeremiah Ross <dev@librerun.dev> — author, committer and sign-off" \
    "branches   : unchanged" "alone, with everything it needs"
p1=$(commit_of "$first")
need "the first commit, prepared again" bash scripts/prepare_public_repo.sh --ref "$c1" --out "$work/first-again.bundle"
[ "$(commit_of "$last")" = "$p1" ] || fatal "the first commit is not reproducible: a second run printed another SHA"
echo "green, as it must be: the same SHA on a second run"
printed "the first commit's commands" "$first" "$p1" init
run_printed "$first" "$work/owner-1"
[ "$(public_refs)" = "refs/heads/main $p1" ] || fatal "the public repository holds '$(public_refs)', not main alone at $p1"
echo "green, as it must be: the printed commands publish main alone, at the first commit"

# ---------------------------------------------------------------------------
# A contribution merged in the public repository, and the revert guard
# ---------------------------------------------------------------------------
git clone -q "$public" "$work/contributor"
(
    cd "$work/contributor"
    printf 'contributed in the public repository\n' > CONTRIBUTED.md
    printf '\n' >> README.md
    git add -A
    git -c user.name=Contributor -c user.email=contributor@librerun.invalid commit -q -s \
        -m "A contribution merged in the public repository"
    git push -q origin HEAD:refs/heads/main
)
p2=$(git -C "$public" rev-parse refs/heads/main)
git fetch -q "$public" "+refs/heads/main:refs/librerun/public-main"

printf 'work in the preparing repository\n' > PRIVATE_WORK.md
c2=$(commit "probe: work in the preparing repository")
expect_red "a public commit the candidate lacks (the revert guard)" "added there    CONTRIBUTED.md" \
    bash scripts/prepare_public_repo.sh --check --ref "$c2" --parent refs/librerun/public-main --base "$c1"

git cherry-pick "$p2" > /dev/null
c3=$(git rev-parse HEAD)
[ "$(git show -s --format='%an' "$c3")" = "Contributor" ] || fatal "the cherry-pick lost the contributor's authorship"

# ---------------------------------------------------------------------------
# The fast-forward
# ---------------------------------------------------------------------------
need "the fast-forward, once the candidate carries the public commit" \
    bash scripts/prepare_public_repo.sh --ref "$c3" --parent refs/librerun/public-main --base "$c1" \
    --out "$bundles/second.bundle"
second="$last"
proved "the fast-forward's proofs" "$second" "identical to ${c3}" \
    "one commit on ${p2} — exactly one parent, a fast-forward" \
    "identity   : Jeremiah Ross <dev@librerun.dev> — author, committer and sign-off" \
    "branches   : unchanged" "alone; its prerequisite is the public main"
p3=$(commit_of "$second")
need "the fast-forward, prepared again" bash scripts/prepare_public_repo.sh --ref "$c3" \
    --parent refs/librerun/public-main --base "$c1" --out "$work/second-again.bundle"
[ "$(commit_of "$last")" = "$p3" ] || fatal "the fast-forward is not reproducible: a second run printed another SHA"
echo "green, as it must be: the same SHA on a second run"
printed "the fast-forward's commands" "$second" "$p3" clone
run_printed "$second" "$work/owner-2"
[ "$(public_refs)" = "refs/heads/main $p3" ] || fatal "the public repository holds '$(public_refs)', not main alone at $p3"
[ "$(git -C "$public" rev-parse "${p3}^@")" = "$p2" ] || fatal "the public main's new commit is not on the old one"
[ "$(git -C "$public" rev-parse "${p3}^{tree}")" = "$(git rev-parse "${c3}^{tree}")" ] \
    || fatal "the public main's tree is not the candidate's"
echo "green, as it must be: the printed commands move main forward, one parent, the candidate's tree, no tag"

expect_red "a --base whose tree the public main never had" "never had on its first-parent line" \
    bash scripts/prepare_public_repo.sh --check --ref "$c3" --parent refs/librerun/public-main --base "$c2"
expect_red "--parent without --base" "--parent and --base go together" \
    bash scripts/prepare_public_repo.sh --check --ref "$c3" --parent refs/librerun/public-main
# A git before 2.40 has no `merge-tree --merge-base`: a stand-in that
# answers `git version` as 2.39 and hands everything else to git.
mkdir -p "$work/old-git"
printf '#!/usr/bin/env bash\nif [ "$1" = version ]; then echo "git version 2.39.5"; exit 0; fi\nexec %q "$@"\n' \
    "$(command -v git)" > "$work/old-git/git"
chmod +x "$work/old-git/git"
expect_red "a git older than 2.40" "need git 2.40 or later" \
    env PATH="$work/old-git:$PATH" bash scripts/prepare_public_repo.sh --check --ref "$c3" \
    --parent refs/librerun/public-main --base "$c1"

# ---------------------------------------------------------------------------
# A placeholder, and the canary in the tree and in the identity
# ---------------------------------------------------------------------------
printf 'https://github.com/%s/LibreRun/issues\n' "$P_OWNER" > docs/_probe_placeholder.md
c4=$(commit "probe: a placeholder")
expect_red "a placeholder in the candidate" "placeholder reference(s) are still in the tree" \
    bash scripts/prepare_public_repo.sh --check --ref "$c4"
git reset -q --hard "$c3"

printf 'see the %s notes\n' "$canary" > docs/_probe_canary.md
c5=$(commit "probe: the canary")
expect_red "the purity canary in the tree" "names this repository or its owner path" \
    bash scripts/prepare_public_repo.sh --check --ref "$c5"
git reset -q --hard "$c3"

sed "s/^PUBLIC_EMAIL=\"dev@librerun.dev\"\$/PUBLIC_EMAIL=\"dev@${canary}.dev\"/" \
    scripts/prepare_public_repo.sh > "$work/prepare_identity.sh"
cmp -s "$work/prepare_identity.sh" scripts/prepare_public_repo.sh \
    && fatal "PUBLIC_EMAIL's line is not where the probe looks: the copy is unchanged and would prove nothing"
expect_red "the purity canary in the identity (PUBLIC_EMAIL, in a copy of the script)" \
    "identity names this repository's owner" \
    bash "$work/prepare_identity.sh" --ref "$c1" --out "$work/identity.bundle"

# ---------------------------------------------------------------------------
# An owner site the owner script leaves
# ---------------------------------------------------------------------------
printf '/probe/  @%s/maintainers\n' "$P_OWNER" > docs/_probe_team.md
commit "probe: a team as the owner" > /dev/null
expect_red "an owner site the owner script leaves (a team)" "owner site(s) are left" \
    bash scripts/resolve_public_owner.sh "$handle"
git reset -q --hard "$c3"

# ---------------------------------------------------------------------------
# The rehearsal
# ---------------------------------------------------------------------------
need "a rehearsal: --allow-placeholders on a tree that carries them" \
    bash scripts/prepare_public_repo.sh --ref "$c4" --allow-placeholders --out "$work/rehearsal.bundle"
if ! prints_no_push "$last" > /dev/null; then
    fatal "the rehearsal printed a push"
fi
echo "green, as it must be: the rehearsal prints no push"
sed 's/^if \[ "$allow_placeholders" = 1 \]; then  # the rehearsal$/if false; then  # the rehearsal/' \
    scripts/prepare_public_repo.sh > "$work/prepare_rehearsal.sh"
cmp -s "$work/prepare_rehearsal.sh" scripts/prepare_public_repo.sh \
    && fatal "the rehearsal's branch is not where the probe looks: the copy is unchanged and would prove nothing"
rehearsal="$(bash "$work/prepare_rehearsal.sh" --ref "$c4" --allow-placeholders --out "$work/rehearsal-2.bundle" 2>&1 || true)"
expect_red "a rehearsal that prints a push" "a push is printed" prints_no_push "$rehearsal"

# ---------------------------------------------------------------------------
# development_repository — the rule both probe scripts ask
# ---------------------------------------------------------------------------
# Here README.md's clone URL names the public owner, as a resolved tree's
# does. Events carry the repository's privacy and owner, as GitHub's do.
event() {
    printf '{"repository": {"private": %s, "owner": {"login": "%s"}}}\n' "$1" "$2" > "$work/event-$3.json"
    echo "$work/event-$3.json"
}
if dev_repo "$(event true someone-else private)" "someone-else/elsewhere" "elsewhere"; then
    echo "green, as it must be: a private repository under another owner is the development repository"
else
    fatal "the rule does not take a private repository under another owner for the development repository"
fi
expect_red "a private repository whose owner is the clone URL's, taken for the development repository" "" \
    dev_repo "$(event true "$public_owner" handle)" "${public_owner}/elsewhere" "elsewhere"
if dev_repo "$(event false someone-else public)" "someone-else/LibreRun" "LibreRun"; then
    fatal "the rule takes a public repository for the development repository"
fi
echo "green, as it must be: a public repository is never the development repository"
if dev_repo "" "someone-else/elsewhere" "elsewhere" \
        && ! dev_repo "" "${public_owner}/elsewhere" "elsewhere" \
        && ! dev_repo "" "someone-else/LibreRun" "LibreRun"; then
    echo "green, as it must be: with no event, the slug is compared with the clone URL's"
else
    fatal "with no event, the rule does not compare the slug's owner and name with the clone URL's"
fi

# ---------------------------------------------------------------------------
cd "$root"
after=$(state)
if [ "$before" != "$after" ]; then
    echo "::error::this checkout changed while the probes ran:"
    diff <(printf '%s\n' "$before") <(printf '%s\n' "$after") | sed 's/^/    /' || true
    failed=1
fi
if [ "$failed" = 1 ]; then
    echo "::error::a probe stayed green, or went red for another reason (above): that check is broken, not the tree"
    exit 1
fi
echo "every probe went red, and the tree is as it was"
