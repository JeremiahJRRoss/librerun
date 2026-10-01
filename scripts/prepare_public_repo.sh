#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# ============================================================================
# LibreRun — prepare a commit for the public repository (blueprint S9; the
# K blueprint's L34, L39 and B1a)
#
# LibreRun is published from a public `LibreRun` repository that starts
# from a single parentless commit of the tested tree (L34). The repository
# that prepares a release stays private: its history is the development
# record, and none of it is what a newcomer should meet first.
#
#   ./scripts/prepare_public_repo.sh --ref <sha>          # the first release
#   ./scripts/prepare_public_repo.sh --ref <sha> \
#       --parent refs/librerun/public-main --base <sha>   # every one after it
#   ./scripts/prepare_public_repo.sh --check --ref <sha>  # only the refusals
#   ./scripts/prepare_public_repo.sh --allow-placeholders # a rehearsal
#
# --ref is the resolved candidate's SHA (HEAD when omitted), in the
# repository that prepares it — never a tag, because a tag there would
# start release.yml there. The first release is a parentless commit. Each
# one after it is a single commit on the public `main`, a fast-forward:
# --parent is the public `main`, fetched into refs/librerun/public-main
# (this script fetches nothing), and --base the commit the previous
# release was prepared from. Both or neither.
#
# It writes a git BUNDLE and prints the commands that publish it: one push
# of `main`, never --force, and no tag — the tag is the release (R4), made
# after the publication decision (R19), never part of preparing one. With
# --allow-placeholders it prints no push at all. It never pushes anything,
# and it never creates a branch.
#
# ---------------------------------------------------------------------------
# Three properties this script exists to guarantee
# ---------------------------------------------------------------------------
#
# **The tree is identical.** Release checklist §3.3 item 7: the prepared
# commit must carry the same tree as the candidate that was tested, so
# that what was tested is what gets tagged. That is why this script does
# not edit, filter or substitute anything — `git commit-tree
# <ref>^{tree}` re-parents a tree and changes nothing inside it. The
# placeholders are resolved by an ordinary commit on the release branch
# BEFORE the candidate is cut, which is why the refusal below exists.
#
# **No public commit is reverted.** A contribution merged in the public
# repository comes back to the preparing one as a cherry-pick, before the
# next candidate is cut. So with --parent and --base the script refuses,
# --check included, unless --base's tree is one the public `main` had on
# its first-parent line (a release was prepared from it) and --base is an
# ancestor of --ref, and unless a three-way merge of --ref and the public
# `main` over --base (`git merge-tree --write-tree --merge-base`, git 2.40
# or later) yields --ref's tree exactly. A path it does not is a public
# change the candidate lacks, and each one is named.
#
# **No branch is created.** The standing session guard allows exactly one
# branch, so `git checkout --orphan` and a temporary branch are both out.
# The commit is built as an object, a ref OUTSIDE refs/heads/ is pointed
# at it, that ref is bundled, and the ref is deleted:
#
#     C=$(git commit-tree "$(git rev-parse HEAD^{tree})" -m …)
#     git update-ref refs/librerun/public-release "$C"
#     git bundle create out.bundle refs/librerun/public-release
#     git update-ref -d refs/librerun/public-release
#
# The ref is required, not decoration: `git bundle create` refuses a bare
# commit SHA with "fatal: Refusing to create empty bundle" because it
# cannot advertise an unreferenced object as a bundle head (reproduced on
# git 2.43.0; bundling the ref succeeds). A ref under refs/librerun/ is
# not a branch — `git branch --list --all` does not show it — so the
# guard holds, and the script proves that before it exits.
# ============================================================================
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

ref="HEAD"
out=""
parent=""
base=""
check_only=0
allow_placeholders=0
keep_ref=0
public_ref="refs/librerun/public-release"
# The maintainer's public identity: the author, committer and sign-off
# of every commit this script prepares for the public repository, the
# first among them (L39, and CONTRIBUTING.md's attestation).
PUBLIC_NAME="Jeremiah Ross"
PUBLIC_EMAIL="dev@librerun.dev"

while [ $# -gt 0 ]; do
    case "$1" in
        --ref) ref="${2:?--ref needs a revision}"; shift 2 ;;
        --ref=*) ref="${1#*=}"; shift ;;
        --out) out="${2:?--out needs a path}"; shift 2 ;;
        --out=*) out="${1#*=}"; shift ;;
        --parent) parent="${2:?--parent needs a commit}"; shift 2 ;;
        --parent=*) parent="${1#*=}"; shift ;;
        --base) base="${2:?--base needs a commit}"; shift 2 ;;
        --base=*) base="${1#*=}"; shift ;;
        --check|--check-placeholders) check_only=1; shift ;;
        --allow-placeholders) allow_placeholders=1; shift ;;
        --keep-ref) keep_ref=1; shift ;;
        -h|--help)
            awk '/^# =+$/ { n++; next } n == 1 && /^#/ { sub(/^# ?/, ""); print } n >= 2 { exit }' "$0"
            exit 0 ;;
        *) echo >&2 "prepare_public_repo.sh: unknown option '$1' (try --help)"; exit 2 ;;
    esac
done

if [ "${parent:+set}" != "${base:+set}" ]; then
    echo >&2 "prepare_public_repo.sh: --parent and --base go together: the public main, and the commit the previous release was prepared from (try --help)"
    exit 2
fi

commit_sha=$(git rev-parse --verify "${ref}^{commit}")
tree_sha=$(git rev-parse --verify "${ref}^{tree}")
version=$(git show "${ref}:VERSION" | tr -d '[:space:]')

echo "source ref : ${ref}  (${commit_sha})"
echo "tree       : ${tree_sha}"
echo "version    : ${version}"
if [ -n "$parent" ]; then
    parent_sha=$(git rev-parse --verify "${parent}^{commit}")
    base_sha=$(git rev-parse --verify "${base}^{commit}")
    echo "parent     : ${parent}  (${parent_sha}) — the public main"
    echo "base       : ${base}  (${base_sha}) — the previous release's source"
fi
echo

# ---------------------------------------------------------------------------
# Refusal 1 — unresolved placeholders
# ---------------------------------------------------------------------------
# The tokens are ASSEMBLED rather than spelled out, exactly as
# publish-purity assembles its pattern: written literally, this script
# and every document that mentions it would be reported as placeholder
# sites forever, and the only fix would be to exclude them — the files
# where a real placeholder could then hide. Assembled, there are no
# exclusions at all.
P_OWNER='OWNER'
P_COPY="COPYRIGHT_$(printf 'HOLDER')"
P_CONDUCT="CONDUCT_$(printf 'CONTACT')"
P_SECURITY="SECURITY_$(printf 'CONTACT')"
# The holder and the two contacts are resolved (NOTICE names Jeremiah
# Ross; both contacts are dev@librerun.dev). Their tokens stay in the
# pattern so that neither can come back unnoticed. The owner token after
# an `@` is matched alone as well as before a slash: CODEOWNERS names the
# maintainer's personal account, which has no teams.
placeholder_pattern="github\.com/${P_OWNER}/|@${P_OWNER}([^A-Za-z0-9_-]|\$)|${P_COPY}|${P_CONDUCT}|${P_SECURITY}"

echo "== placeholders still in the tree =="
if git grep -nE "$placeholder_pattern" "$ref" > /tmp/librerun_placeholders.$$ 2>/dev/null; then
    sed 's/^/  /' /tmp/librerun_placeholders.$$
    count=$(wc -l < /tmp/librerun_placeholders.$$)
    rm -f /tmp/librerun_placeholders.$$
    echo
    if [ "$allow_placeholders" = 1 ]; then
        echo "  ${count} placeholder reference(s) — a rehearsal (--allow-placeholders):"
        echo "  the commit is prepared and proved, and no push is printed."
    else
        echo "::error::${count} placeholder reference(s) are still in the tree (above)."
        cat >&2 <<'MSG'

  These are resolved by an ordinary commit on the release branch, BEFORE the
  candidate is cut — never by this script, because the release checklist
  requires the published commit to carry the SAME TREE as the candidate that
  was tested (blueprint §3.3 item 7), and an edit here would break that.

    * the public repository's owner, the maintainer's personal account,
      in documentation URLs and CODEOWNERS.
    * the copyright holder and the conduct and security contacts are
      resolved already (NOTICE; CODE_OF_CONDUCT.md; SECURITY.md). A match
      on one of them means it has come back: undo that edit.

  Resolve them, merge, re-cut the candidate, and run this again; the owner's
  are written by scripts/resolve_public_owner.sh <handle>. To rehearse on a
  tree that still carries them, pass --allow-placeholders: it prepares the
  commit and prints no push.
MSG
        exit 1
    fi
else
    rm -f /tmp/librerun_placeholders.$$
    echo "  none — every placeholder has a real value."
fi
echo

# ---------------------------------------------------------------------------
# Refusal 2 — the tree names this repository
# ---------------------------------------------------------------------------
# publish-purity runs on every pull request, but the thing being published
# is a TREE AT A REF, which may not be the tree CI last looked at. Check
# the one that is about to be published.
echo "== publish-purity on ${ref} =="
purity_tree=$(mktemp -d)
trap 'rm -rf "$purity_tree"' EXIT
git archive "$ref" | tar -x -C "$purity_tree"
(
    cd "$purity_tree"
    git init -q .
    git add -A
    # The checker reads the index and the working tree, so a throwaway
    # repository over the extracted tree is exactly the right input — and
    # it checks what will be published rather than what is checked out.
    bash .github/publish_purity.sh
) || {
    echo "::error::The tree at ${ref} names this repository or its owner path (above). It cannot be published — that is gap I6, and the public reader cannot open the private repository those references point at."
    exit 1
}
echo

# ---------------------------------------------------------------------------
# Refusal 3 — a public commit the candidate lacks (the revert guard)
# ---------------------------------------------------------------------------
# The public `main` may carry commits made there — a contribution merged in
# the public repository — and the new commit's tree is the candidate's,
# whole. A change the candidate never brought back would be reverted by
# publishing it, silently. Three-way merging the candidate and the public
# `main` over the previous release's source finds exactly those changes:
# where the candidate already carries them, the merge is the candidate.
if [ -n "$parent" ]; then
    echo "== the public main's commits, carried by ${ref} =="
    # `--merge-base` came to `git merge-tree --write-tree` in git 2.40.
    read -r git_major git_minor < <(git version | sed -nE 's/^git version ([0-9]+)\.([0-9]+).*/\1 \2/p') || true
    if [ -z "${git_minor:-}" ] || [ "$git_major" -lt 2 ] || { [ "$git_major" -eq 2 ] && [ "$git_minor" -lt 40 ]; }; then
        echo "::error::--parent and --base need git 2.40 or later (git merge-tree --write-tree --merge-base); this is $(git version)."
        exit 1
    fi
    base_tree=$(git rev-parse --verify "${base_sha}^{tree}")
    # Read whole, then searched: `git log | grep -q` under pipefail fails
    # when grep exits at a match and git log takes SIGPIPE (dco_check.sh).
    published_trees=$(git log --first-parent --format=%T "$parent_sha")
    if ! grep -qxF "$base_tree" <<< "$published_trees"; then
        echo "::error::--base ${base} (${base_sha}) has a tree the public main never had on its first-parent line: no release was prepared from it. --base is the commit the previous release was prepared from, in this repository."
        exit 1
    fi
    if ! git merge-base --is-ancestor "$base_sha" "$commit_sha"; then
        echo "::error::--base ${base} (${base_sha}) is not an ancestor of ${ref}: the candidate does not descend from the previous release's source, so the merge over it would compare unrelated work."
        exit 1
    fi
    merge_status=0
    merged=$(git merge-tree --write-tree --name-only --no-messages \
        --merge-base="$base_sha" "$commit_sha" "$parent_sha") || merge_status=$?
    if [ "$merge_status" -gt 1 ]; then
        echo "::error::git merge-tree failed (exit ${merge_status}); nothing is known about what the public main carries."
        exit 1
    fi
    merged_tree=$(head -n 1 <<< "$merged")
    if [ "$merge_status" -eq 1 ]; then
        tail -n +2 <<< "$merged" | sed '/^$/d; s/^/  conflicted  /'
        echo "::error::The public main and ${ref} changed the same lines differently since --base (above). Bring each public change back as a cherry-pick, re-cut the candidate, and run this again: a release never reverts a public commit that was not brought back."
        exit 1
    fi
    if [ "$merged_tree" != "$tree_sha" ]; then
        git diff-tree -r --name-status "$tree_sha" "$merged_tree" \
            | sed -E 's/^A\t/  added there    /; s/^D\t/  deleted there  /; s/^M\t/  changed there  /; s/^([A-Z][0-9]*)\t/  \1  /'
        echo "::error::The public main carries changes ${ref} lacks (above): publishing it would revert them. Bring each one back as a cherry-pick with its author and sign-off, re-cut the candidate, and run this again."
        exit 1
    fi
    echo "  every public commit since --base is in ${ref}: the merge over it is ${ref}'s own tree"
    echo
fi

if [ "$check_only" = 1 ]; then
    echo "--check: the refusals passed; nothing written."
    exit 0
fi

# ---------------------------------------------------------------------------
# The commit, the ref, the bundle
# ---------------------------------------------------------------------------
out="${out:-librerun-public-${version}.bundle}"

if [ -n "$parent" ]; then
    message="LibreRun ${version}

The tree of this commit is the tree of the release candidate that was
tested, byte for byte. Its one parent is the public main it follows, and
every change published there since the previous release was carried by
the candidate before it was cut.

Signed-off-by: ${PUBLIC_NAME} <${PUBLIC_EMAIL}>"
else
    message="LibreRun ${version}

The first commit of the public LibreRun repository.

LibreRun is an educational software environment for teaching the
design, development and operation of AI agents using multiple
agent-development frameworks. At its centre is a platform chassis,
built for educational purposes: intake, a run lifecycle with a human
approval gate, PII redaction before anything is persisted, per-step
model configuration through one gateway, reports, multi-tenant auth and
tracing. An agent plugs in and inherits all of it; Docker Compose or
Podman Compose orchestrates it on Linux; and the whole of it is there
to be learned from as much as run.

This tree is published from a private development repository, as a
single commit rather than its history: the history is a different
project's, under a different name, and re-publishing it would hand a
newcomer years of decisions that no longer apply instead of the software
they came for. Nothing is omitted from the TREE — it is byte-for-byte
the tree of the tested release candidate, which is what the release
checklist requires.

AGPL-3.0-only, with four Apache-2.0 directories and the third-party
material NOTICE lists (LICENSE, NOTICE,
docs/release/License_Scope_Map.md). The name is governed separately
(TRADEMARKS.md). Contributions come in under a DCO sign-off
(CONTRIBUTING.md).

Signed-off-by: ${PUBLIC_NAME} <${PUBLIC_EMAIL}>"
fi

# Reproducible: re-running this on the same ref produces the same commit
# SHA, so JR can check the SHA this script printed against the one they
# are about to push. The identity is fixed here rather than read from
# `git config`: the public commit's author, committer and sign-off are
# the maintainer's public identity, never whatever identity the machine
# running this happens to carry (a development e-mail, or a coding
# assistant's).
GIT_AUTHOR_DATE=$(git show -s --format=%aI "$commit_sha")
GIT_COMMITTER_DATE="$GIT_AUTHOR_DATE"
GIT_AUTHOR_NAME="$PUBLIC_NAME"
GIT_AUTHOR_EMAIL="$PUBLIC_EMAIL"
GIT_COMMITTER_NAME="$PUBLIC_NAME"
GIT_COMMITTER_EMAIL="$PUBLIC_EMAIL"
export GIT_AUTHOR_DATE GIT_COMMITTER_DATE GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL \
    GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL

if [ -n "$parent" ]; then
    public_commit=$(git commit-tree "$tree_sha" -p "$parent_sha" -m "$message")
else
    public_commit=$(git commit-tree "$tree_sha" -m "$message")
fi
git update-ref "$public_ref" "$public_commit"

rm -f "$out"
if [ -n "$parent" ]; then
    # The new commit alone: the public main is the bundle's prerequisite.
    git bundle create "$out" "$public_ref" "^${parent_sha}" >/dev/null
else
    git bundle create "$out" "$public_ref" >/dev/null
fi
if [ "$keep_ref" = 0 ]; then
    git update-ref -d "$public_ref"
fi

# ---------------------------------------------------------------------------
# Prove the properties rather than asserting them in a comment
# ---------------------------------------------------------------------------
echo "== what was produced =="
printf '  commit     : %s\n' "$public_commit"
printf '  bundle     : %s (%s bytes)\n' "$out" "$(wc -c < "$out")"

published_tree=$(git rev-parse "${public_commit}^{tree}")
if [ "$published_tree" != "$tree_sha" ]; then
    echo "::error::The prepared commit's tree ($published_tree) is not the source tree ($tree_sha). Release checklist §3.3 item 7 requires them to be identical; do not push this."
    exit 1
fi
printf '  tree       : %s — identical to %s (checklist §3.3 item 7)\n' "$published_tree" "$ref"

read -r -a line <<< "$(git rev-list --parents -n 1 "$public_commit")"
if [ -z "$parent" ]; then
    if [ "${#line[@]}" != "1" ]; then
        echo "::error::The prepared commit has parents; it must be parentless (a clean history)."
        exit 1
    fi
    printf '  history    : one commit, no parents\n'
else
    if [ "${#line[@]}" != "2" ] || [ "${line[1]}" != "$parent_sha" ]; then
        echo "::error::The prepared commit's parents are '${line[*]:1}'; it must have exactly one, the public main ${parent_sha}."
        exit 1
    fi
    printf '  history    : one commit on %s — exactly one parent, a fast-forward\n' "$parent_sha"
fi

want="${PUBLIC_NAME} <${PUBLIC_EMAIL}>"
author=$(git show -s --format='%an <%ae>' "$public_commit")
committer=$(git show -s --format='%cn <%ce>' "$public_commit")
signoff=$(git show -s --format='%(trailers:key=Signed-off-by,valueonly)' "$public_commit" | sed '/^$/d')
if [ "$author" != "$want" ] || [ "$committer" != "$want" ] || [ "$signoff" != "$want" ]; then
    echo "::error::The prepared commit's author ($author), committer ($committer) and sign-off ($signoff) must all be $want (L39). Do not push this."
    exit 1
fi
if ! python3 scripts/check_purity.py --text "$author $committer $signoff" >/dev/null; then
    echo "::error::The prepared commit's identity names this repository's owner (L39). Do not push this."
    exit 1
fi
printf '  identity   : %s — author, committer and sign-off\n' "$want"

branches=$(git branch --list --all)
if grep -q "public-release" <<< "$branches"; then
    echo "::error::A branch named for the public release exists. This script must not create one; remove it and report the bug."
    exit 1
fi
printf '  branches   : unchanged — %s is not a branch\n' "$public_ref"

heads=$(git bundle list-heads "$out")
if [ "$heads" != "${public_commit} ${public_ref}" ]; then
    echo "::error::The bundle carries '${heads}', not ${public_commit} as ${public_ref} alone."
    exit 1
fi
if [ -n "$parent" ]; then
    printf '  bundle     : %s alone; its prerequisite is the public main\n' "$public_commit"
else
    printf '  bundle     : %s alone, with everything it needs\n' "$public_commit"
fi
echo

# ---------------------------------------------------------------------------
# The release owner's commands
# ---------------------------------------------------------------------------
# A rehearsal prints none: its tree still carries placeholders, or was
# asked to be treated as if it did.
if [ "$allow_placeholders" = 1 ]; then  # the rehearsal
    echo "== a rehearsal (--allow-placeholders): no push is printed =="
    echo
    echo "  The commit above is prepared and proved. Resolve the placeholders"
    echo "  and prepare the candidate again without --allow-placeholders for"
    echo "  the commands that publish it (docs/platform/Releasing.md §5)."
    exit 0
fi

# shell_word STRING — STRING as one shell word, quoted only when it must
# be: a command printed for a person to paste is split by their shell, so
# a path with a space or a metacharacter in it is single-quoted.
shell_word() {
    case "$1" in
        '' | *[!A-Za-z0-9_./:@%+=,-]*) printf "'%s'" "${1//\'/\'\\\'\'}" ;;
        *) printf '%s' "$1" ;;
    esac
}

bundle=$(shell_word "$(cd "$(dirname "$out")" && pwd)/$(basename "$out")")
tree_of_ref=$(shell_word "${commit_sha}^{tree}")
# The public repository, as README.md's clone line at the ref names it:
# the placeholder refusal above means its owner is resolved by now. Read
# whole, so a README with no such line leaves the placeholder text below.
readme=$(git show "${ref}:README.md" 2>/dev/null || true)
public_url=$(sed -nE '/^git clone https:\/\/github\.com\//{s#^git clone (https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+\.git)( .*)?$#\1#p;q;}' <<< "$readme")
if [ -z "$public_url" ]; then
    public_url="<the public repository's clone URL, as README.md's clone line names it>"
fi

if [ -z "$parent" ]; then
    cat <<COMMANDS
== to publish (yours to run: this script pushes nothing) ==

  1. In a new, empty bare repository, check the bundle and take its commit:

       git init --bare librerun-public.git && cd librerun-public.git
       git bundle verify ${bundle}
       git fetch ${bundle} ${public_ref}:${public_ref}

  2. Push main, and nothing else — no other ref, no mirror, never --force:

       git push ${public_url} ${public_commit}:refs/heads/main

  3. Check what arrived, before anyone else sees it:

       git ls-remote ${public_url}
       # HEAD and refs/heads/main at ${public_commit}, and nothing else

     and that the tree is the one that was tested:

       git rev-parse ${tree_of_ref}          # here
       # ${tree_sha}

  No tag: the tag is the release (R4), made after the publication decision
  (R19) and never while preparing one. docs/platform/Releasing.md §5 has the
  whole sequence.
COMMANDS
else
    cat <<COMMANDS
== to publish (yours to run: this script pushes nothing) ==

  1. In a bare clone of the public repository, which holds the public main
     the bundle needs, check the bundle and take its commit:

       git clone --bare ${public_url} librerun-public.git && cd librerun-public.git
       git bundle verify ${bundle}
       git fetch ${bundle} ${public_ref}:${public_ref}

  2. Push main, and nothing else: a fast-forward of the public main from
     ${parent_sha}, never --force:

       git push ${public_url} ${public_commit}:refs/heads/main

  3. Check what arrived:

       git ls-remote ${public_url} refs/heads/main
       # ${public_commit}	refs/heads/main

     and that the tree is the one that was tested:

       git rev-parse ${tree_of_ref}          # here
       # ${tree_sha}

  No tag: the tag is the release (R4), pushed once CI in the public
  repository is green on this commit. docs/platform/Releasing.md §6 has the
  whole sequence.
COMMANDS
fi
