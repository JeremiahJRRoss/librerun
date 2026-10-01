#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# R16's A3 half (LIC-13, C-08, C-23, P03): review the tree a public
# repository would carry, before anyone makes one of it.
#
# The tree reviewed is `git archive <ref>`, extracted: the tree
# prepare_public_repo.sh re-parents unchanged. History is not scanned; a
# public repository starts from a parentless commit (R1). A path marked
# export-ignore or export-subst is refused outright, since then the
# archive would not be the tree. On the copy it runs:
#
#   * gitleaks, pinned below by version and by the SHA-256 its release
#     lists, with --redact and no call to any provider (trufflehog makes
#     one by default), printing rule, file and line only — never a value;
#   * the tree rules, none broken on the day they were written: no gitlink
#     or .gitmodules, no LFS pointer or filter=lfs, no file with a NUL
#     byte, no cache or build output, no archive or compiled object, and
#     no licence file outside the root and LICENSES/.
#
#   bash scripts/review_public_tree.sh                # HEAD
#   bash scripts/review_public_tree.sh --ref <sha>    # another commit or tree
#   bash scripts/review_public_tree.sh --probe        # each plant must go red
#   bash scripts/review_public_tree.sh --install DIR  # fetch the pinned gitleaks
#
# The scan runs $GITLEAKS, or gitleaks on PATH, and refuses any version but
# the pinned one. A finding is a live credential or a fixture. A live one
# stops the work and goes to JR by rule and path, to rotate (C-23). A
# fixture gets an entry in .gitleaks.toml naming its file and its rule,
# never a directory, which this also holds. --probe plants, in trees
# written beside HEAD and never checked out, a token made at run time and
# each violation, and fails unless each is red for its own plant and the
# token is in no output.
set -euo pipefail

GITLEAKS_VERSION=8.30.1
# The SHA-256 gitleaks_8.30.1_checksums.txt lists for each tarball.
declare -A GITLEAKS_SHA256=(
    [linux_x64]=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
    [linux_arm64]=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080
)
CACHE_OR_BUILD='(^|/)(__pycache__|node_modules|\.next|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.tox|\.venv|venv|htmlcov|coverage|dist|build|[^/]*\.egg-info)/'
ARCHIVE_OR_OBJECT='\.(zip|tar|tgz|gz|bz2|xz|zst|7z|rar|whl|egg|jar|war|so|dylib|dll|exe|o|a|obj|lib|pyc|pyo|class|wasm)$'
LICENCE_FILE='(^|/)(licen[cs]e|copying|unlicense)([.-][a-z0-9.+-]*)?$'

cd "$(git rev-parse --show-toplevel)"

usage() {
    sed -n '/^#   bash/p' "$0" | sed 's/^# *//' >&2
    exit 2
}

platform() {
    case "$(uname -s)-$(uname -m)" in
        Linux-x86_64) echo linux_x64 ;;
        Linux-aarch64 | Linux-arm64) echo linux_arm64 ;;
        *) echo "::error::no gitleaks is pinned for $(uname -s)-$(uname -m)" >&2; return 1 ;;
    esac
}

install_gitleaks() {
    local dir="$1" plat tarball want got
    plat="$(platform)"
    tarball="gitleaks_${GITLEAKS_VERSION}_${plat}.tar.gz"
    want="${GITLEAKS_SHA256[$plat]}"
    mkdir -p "$dir"
    curl -fsSL --retry 3 -o "$dir/$tarball" \
        "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/${tarball}"
    got="$(sha256sum "$dir/$tarball" | cut -d' ' -f1)"
    if [ "$got" != "$want" ]; then
        rm -f "$dir/$tarball"
        echo "::error::$tarball is not the release's: SHA-256 $got, and the pin says $want" >&2
        return 1
    fi
    tar -xzf "$dir/$tarball" -C "$dir" gitleaks
    rm -f "$dir/$tarball"
    echo "installed gitleaks $GITLEAKS_VERSION for $plat at $dir/gitleaks, its SHA-256 the pin's"
}

gitleaks_bin() {
    local bin="${GITLEAKS:-gitleaks}" version
    if ! version="$("$bin" version 2>/dev/null)"; then
        echo "::error::no gitleaks at '$bin': run with --install DIR and GITLEAKS=DIR/gitleaks" >&2
        return 1
    fi
    if [ "${version#v}" != "$GITLEAKS_VERSION" ]; then
        echo "::error::gitleaks $version is not the pinned $GITLEAKS_VERSION" >&2
        return 1
    fi
    printf '%s\n' "$bin"
}

# attr TREE ATTRIBUTE — "path<TAB>value" for each path in TREE whose
# ATTRIBUTE is set, as TREE's own .gitattributes set it.
attr() {
    git ls-tree -r -z --name-only "$1" \
        | git check-attr --source="$1" -z --stdin "$2" \
        | tr '\0' '\n' | paste - - - \
        | awk -F'\t' '$3 != "unspecified" && $3 != "unset" { print $1 "\t" $3 }'
}

# paths TREE — every file path in TREE, one a line.
paths() {
    git ls-tree -r --name-only "$1"
}

# nul_files DIR — each regular file under DIR with a NUL byte anywhere in
# it, relative to DIR. Symbolic links are skipped: their content is a path.
nul_files() {
    python3 - "$1" <<'PY'
import os, sys
root = sys.argv[1]
for top, dirs, files in os.walk(root):
    dirs.sort()
    for name in sorted(files):
        path = os.path.join(top, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            while chunk := fh.read(1 << 20):
                if b"\0" in chunk:
                    print(os.path.relpath(path, root))
                    break
PY
}

# The gitleaks report, as "rule file:line", read by Python so that no value
# in it is ever printed; it exits 1 on a report it cannot read.
report_findings() {
    python3 - "$1" <<'PY'
import json, sys
try:
    findings = json.load(open(sys.argv[1])) or []
    lines = [f"{f['RuleID']} {f['File'].removeprefix('./')}:{f['StartLine']}" for f in findings]
except Exception as err:  # any shape but gitleaks' own is unreadable, never clean
    print(f"::error::the gitleaks report could not be read ({type(err).__name__})")
    sys.exit(1)
print("\n".join(lines))
PY
}

# The allowlist rule: every [[allowlists]] entry names rules and single
# files, anchored and literal, on top of gitleaks' default rules.
check_allowlist() {
    python3 - "$1" <<'PY'
import re, sys, tomllib
try:
    config = tomllib.load(open(sys.argv[1], "rb"))
except (OSError, tomllib.TOMLDecodeError) as err:
    print(f".gitleaks.toml: it could not be read ({type(err).__name__})")
    sys.exit(1)
bad = []
if config.get("extend", {}).get("useDefault") is not True:
    bad.append("it does not extend gitleaks' default rules ([extend] useDefault = true)")
for key in ("allowlist", "rules"):
    if key in config:
        bad.append(f"it has a top-level [{key}]: a fixture is one [[allowlists]] entry, a file and a rule")
for i, entry in enumerate(config.get("allowlists", []), 1):
    if not entry.get("targetRules"):
        bad.append(f"allowlist {i} names no rule (targetRules)")
    if not entry.get("paths") or set(entry) - {"description", "targetRules", "paths"}:
        bad.append(f"allowlist {i} is not a file and a rule: only description, targetRules and paths")
    for p in entry.get("paths") or []:
        if not re.fullmatch(r"\^(?:[\w-]|\\\.|/)+\$", p):
            bad.append(f"allowlist {i}: {p!r} is not one file, anchored and literal")
for b in bad:
    print(f".gitleaks.toml: {b}")
sys.exit(1 if bad else 0)
PY
}

# review TREE — exits 0 when clean; names rule, file and line for each finding.
review() (
    tree="$1"
    found=0
    work="$(mktemp -d)"
    trap 'rm -rf "$work"' EXIT
    # Each step's own status is checked. This runs inside `if` or `||`,
    # where bash ignores `set -e`, and a listing that failed would read as
    # a tree with nothing in it: a review that passed by not looking.
    fail() { echo "::error::the review could not $1, so it cannot call the tree clean"; exit 1; }

    { attr "$tree" export-ignore > "$work/export" && attr "$tree" export-subst >> "$work/export"; } \
        || fail "read the tree's export attributes (git check-attr --source needs git 2.40 or later)"
    while IFS=$'\t' read -r path value; do
        echo "archive: $path is marked $value, so the archive would not be the tree"
        found=1
    done < "$work/export"
    [ "$found" = 0 ] || exit 1

    git ls-tree -r "$tree" > "$work/entries" || fail "list the tree"
    while IFS= read -r entry; do
        [ "${entry%% *}" = 160000 ] || continue
        echo "tree: ${entry#*$'\t'} is a gitlink (a submodule)"
        found=1
    done < "$work/entries"
    paths "$tree" > "$work/paths" || fail "list the tree's paths"
    while IFS= read -r path; do
        case "/$path" in
            */.gitmodules) echo "tree: $path declares a submodule"; found=1 ;;
        esac
        if printf '%s\n' "$path" | grep -qiE "$CACHE_OR_BUILD"; then
            echo "tree: $path is cache or build output"; found=1
        fi
        if printf '%s\n' "$path" | grep -qiE "$ARCHIVE_OR_OBJECT"; then
            echo "tree: $path is an archive or a compiled object"; found=1
        fi
        if printf '%s\n' "$path" | grep -qiE "$LICENCE_FILE"; then
            case "$path" in
                */*) case "$path" in
                         LICENSES/*) ;;
                         *) echo "tree: $path is a licence file outside the root and LICENSES/"; found=1 ;;
                     esac ;;
            esac
        fi
    done < "$work/paths"
    attr "$tree" filter > "$work/filter" || fail "read the tree's filter attributes"
    while IFS=$'\t' read -r path value; do
        [ "$value" = lfs ] || continue
        echo "tree: $path is stored by filter=lfs"
        found=1
    done < "$work/filter"

    mkdir "$work/tree" && git archive --format=tar "$tree" | tar -x -C "$work/tree" \
        || fail "extract the tree"
    # Every file read whole: git's own binary test (`git diff --numstat`)
    # looks at the first 8000 bytes only, so a NUL further in passed it.
    nul_files "$work/tree" > "$work/nul" || fail "read the tree's files for NUL bytes"
    while IFS= read -r path; do
        echo "tree: $path has a NUL byte"
        found=1
    done < "$work/nul"
    # grep exits 1 when nothing matches, and 2 when it could not look.
    rc=0
    grep -rlI '^version https://git-lfs.github.com/spec/v1' "$work/tree" > "$work/lfs" || rc=$?
    [ "$rc" -le 1 ] || fail "search the tree for LFS pointers"
    while IFS= read -r path; do
        echo "tree: ${path#"$work/tree/"} is a Git LFS pointer"
        found=1
    done < "$work/lfs"

    if [ -f "$work/tree/.gitleaks.toml" ] && ! check_allowlist "$work/tree/.gitleaks.toml"; then
        found=1
    fi

    bin="$(gitleaks_bin)"
    if ! (cd "$work/tree" && "$bin" dir . --redact --no-banner --no-color --exit-code 0 \
            --report-format json --report-path "$work/report.json") > "$work/gitleaks.log" 2>&1; then
        echo "::error::gitleaks did not run; its log is not shown, since it may quote the tree"
        exit 1
    fi
    if ! report_findings "$work/report.json" > "$work/findings"; then
        cat "$work/findings"
        fail "read the gitleaks report"
    fi
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        echo "gitleaks: $line"
        found=1
    done < "$work/findings"
    exit "$found"
)

# plant BASE PATH CONTENT [MODE] — the id of a tree that is BASE with one
# entry more (or replaced), written to the object store, never checked out.
plant() {
    local base="$1" path="$2" content="$3" mode="${4:-100644}" index object
    index="$(mktemp -u)"
    GIT_INDEX_FILE="$index" git read-tree "$base"
    if [ "$mode" = 160000 ]; then
        object="$(git rev-parse HEAD)"
    elif [ "$mode" = nul ]; then
        # A shell variable cannot hold a NUL byte, so this blob is written here.
        object="$(printf 'a\0b\n' | git hash-object -w --stdin)"
        mode=100644
    elif [ "$mode" = latenul ]; then
        # 9000 bytes of text, then a NUL: past the 8000 git's binary test reads.
        object="$( { head -c 9000 /dev/zero | tr '\0' a; printf '\0\n'; } | git hash-object -w --stdin)"
        mode=100644
    else
        object="$(printf '%s' "$content" | git hash-object -w --stdin)"
    fi
    GIT_INDEX_FILE="$index" git update-index --add --cacheinfo "$mode,$object,$path"
    GIT_INDEX_FILE="$index" git write-tree
    rm -f "$index"
}

probe() {
    local base token out failed=0 attrs leaks nl=$'\n'
    base="$(git rev-parse "$1^{tree}")"
    review "$base" > /dev/null || {
        echo "::error::the review is red on $1 itself; fix that first"; exit 1; }
    token="ghp_$(head -c 512 /dev/urandom | tr -dc 'A-Za-z0-9' | head -c 36)"
    out="$(mktemp)"
    attrs="$(git show "$base:.gitattributes" 2>/dev/null || true)"
    leaks="$(git show "$base:.gitleaks.toml" 2>/dev/null || printf '[extend]\nuseDefault = true')"
    # expect_red LABEL WHAT TREE — the review of TREE must fail, naming WHAT.
    expect_red() {
        local label="$1" what="$2" planted="$3"
        if review "$planted" > "$out" 2>&1; then
            echo "::error::plant stayed GREEN: $label — the review does not catch it"
            failed=1
        elif ! grep -qF -- "$what" "$out"; then
            echo "::error::red, but not for the plant: $label (the findings do not name '$what')"
            failed=1
        else
            echo "red, as it must be: $label"
        fi
        if grep -qF -- "$token" "$out"; then
            echo "::error::the token was printed: $label"
            failed=1
        fi
    }
    expect_red "gitleaks: a token made at run time" "docs/_probe_token.md" \
        "$(plant "$base" docs/_probe_token.md "the token is $token$nl")"
    expect_red "tree: a gitlink" "vendor/_probe_module is a gitlink" \
        "$(plant "$base" vendor/_probe_module "" 160000)"
    expect_red "tree: a .gitmodules" "docs/.gitmodules declares" \
        "$(plant "$base" docs/.gitmodules "[submodule \"x\"]${nl}	path = x${nl}	url = ../x${nl}")"
    expect_red "tree: a Git LFS pointer" "docs/_probe_pointer.txt is a Git LFS pointer" \
        "$(plant "$base" docs/_probe_pointer.txt "version https://git-lfs.github.com/spec/v1${nl}oid sha256:0${nl}size 1${nl}")"
    expect_red "tree: filter=lfs" "docs/_probe.lfs is stored by filter=lfs" \
        "$(plant "$(plant "$base" .gitattributes "$attrs${nl}*.lfs filter=lfs diff=lfs merge=lfs -text${nl}")" docs/_probe.lfs "x$nl")"
    expect_red "tree: a NUL byte" "docs/_probe_nul.txt has a NUL byte" \
        "$(plant "$base" docs/_probe_nul.txt "" nul)"
    expect_red "tree: a NUL byte past git's first 8000" "docs/_probe_late_nul.txt has a NUL byte" \
        "$(plant "$base" docs/_probe_late_nul.txt "" latenul)"
    expect_red "tree: cache output" "backend/__pycache__/_probe.txt is cache" \
        "$(plant "$base" backend/__pycache__/_probe.txt "x$nl")"
    expect_red "tree: build output" "frontend/.next/_probe.txt is cache" \
        "$(plant "$base" frontend/.next/_probe.txt "x$nl")"
    expect_red "tree: an archive" "docs/_probe.zip is an archive" \
        "$(plant "$base" docs/_probe.zip "x$nl")"
    expect_red "tree: a compiled object" "backend/_probe.pyc is an archive or a compiled object" \
        "$(plant "$base" backend/_probe.pyc "x$nl")"
    expect_red "tree: a licence file outside the root" "frontend/LICENSE is a licence file" \
        "$(plant "$base" frontend/LICENSE "x$nl")"
    expect_red "archive: export-ignore" "README.md is marked" \
        "$(plant "$base" .gitattributes "$attrs${nl}README.md export-ignore${nl}")"
    expect_red ".gitleaks.toml: an allowlist naming a directory" "is not one file" \
        "$(plant "$base" .gitleaks.toml "$leaks${nl}${nl}[[allowlists]]${nl}targetRules = [\"generic-api-key\"]${nl}paths = ['''^backend/tests/.*''']${nl}")"
    expect_red ".gitleaks.toml: an allowlist naming no rule" "names no rule" \
        "$(plant "$base" .gitleaks.toml "$leaks${nl}${nl}[[allowlists]]${nl}paths = ['''^README\\.md\$''']${nl}")"
    # A review that cannot read its scanner's report must say so, never
    # "clean": a stand-in gitleaks, the pinned version by its own word,
    # writes a report that is not JSON, then JSON of a shape gitleaks
    # never writes (a file named by a number).
    local stub report label
    stub="$(mktemp -d)"
    printf '%s\n' '#!/usr/bin/env bash' \
        "[ \"\$1\" = version ] && { echo $GITLEAKS_VERSION; exit 0; }" \
        "while [ \$# -gt 0 ]; do [ \"\$1\" = --report-path ] && cp \"$stub/report\" \"\$2\"; shift; done" \
        > "$stub/gitleaks"
    chmod +x "$stub/gitleaks"
    for report in '{not json' '[{"RuleID": "x", "File": 5, "StartLine": 1}]'; do
        label="gitleaks: a report the review cannot read ($report)"
        printf '%s' "$report" > "$stub/report"
        if GITLEAKS="$stub/gitleaks" review "$base" > "$out" 2>&1; then
            echo "::error::plant stayed GREEN: $label"
            failed=1
        elif ! grep -qF "could not be read" "$out"; then
            echo "::error::red, but not for the plant: $label"
            failed=1
        else
            echo "red, as it must be: $label"
        fi
    done
    rm -rf "$stub"
    rm -f "$out"
    if [ "$failed" = 1 ]; then
        echo "::error::a plant stayed green, went red for another reason, or printed the token (above): the review is broken, not the tree"
        exit 1
    fi
    echo "every plant went red, and the token was in no output"
}

ref=HEAD
mode=review
while [ $# -gt 0 ]; do
    case "$1" in
        --ref) ref="${2:?--ref needs a commit or tree}"; shift 2 ;;
        --probe) mode=probe; shift ;;
        --install) install_gitleaks "${2:?--install needs a directory}"; exit ;;
        *) usage ;;
    esac
done

case "$mode" in
    probe) probe "$ref" ;;
    review)
        tree="$(git rev-parse "$ref^{tree}")"
        files="$(paths "$tree" | wc -l)"
        if [ "$files" = 0 ]; then
            echo "::error::$ref has no file: a review that read nothing proved nothing"; exit 1
        fi
        if review "$tree"; then
            echo "clean: $ref ($files files) — gitleaks $GITLEAKS_VERSION found nothing, and every tree rule holds"
        else
            echo "::error::the tree a public repository would carry has the findings above (rule, file and line; no value is printed)"
            exit 1
        fi
        ;;
esac
