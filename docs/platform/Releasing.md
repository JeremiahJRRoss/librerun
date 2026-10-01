# Releasing LibreRun

*The release procedure: the version, the tag, what a release publishes,
and the checks that stand in its way. A release is source only, nothing
published names the development repository, and the first public release
was a beta.*

A release is a **tag** in the public repository, and what it publishes
is **source**. The GitHub Release is made by
`.github/workflows/release.yml` from that tag, with the notes and the
source archives GitHub attaches to every release — no image, no wheel,
no package (§3) — and nothing else can start it. There is no publish
button, no manual dispatch, and no way to publish from a branch. The
repository that prepares a release tags nothing: its candidate is a SHA
on `main`, and a tag there would start `release.yml` there (§2).

```
  VERSION  ──► the number every artifact carries
     │
     ▼
  a pull request   ──► the normal checks run on the merge commit: the candidate
     │
     ▼
  prepare_public_repo.sh --ref <sha>  ──► one commit on the public main (§5, §6)
     │
     ▼
  in the public repository: git tag -a v<VERSION>, pushed there
     │
     ▼
  release-gate ──────────────► release-notes
   tag==VERSION                  GitHub Release: the notes and
   no-publication guard          GitHub's source archives,
   CI on this sha                no file attached
```

---

## 1. VERSION is the only number

`VERSION` at the repository root holds the canonical **semver** spelling
— `1.1.0-beta.1`, `1.0.0`. Nothing derives it at run time; every artifact
writes it down, and `scripts/check_single_version.py` (the
`release-readiness / single-version` job) asserts the copies agree.

Two spellings, because two ecosystems disagree and neither is wrong:

| Where | Spelling | Example |
|---|---|---|
| `VERSION`, the web UI's `package.json` and its lock's two root lines, the backend's and the gateway's `version.py` | semver | `1.1.0-beta.1` |
| a Python distribution's `pyproject.toml` and its package's `__version__` | canonical PEP 440 | `1.1.0b1` |

`pip` normalises the first form into the second anyway; writing the
normalised form is how `pip show librerun` and `librerun --version` end
up saying the same thing. The check converts by a table (D38): `alpha`
or `a` is `a`, `beta` or `b` is `b`, `rc` is `rc`, the dot dropped —
`1.1.0-beta.1` is `1.1.0b1`, `1.0.0-rc1` is `1.0.0rc1`. A pre-release
outside it, `1.1.0-preview.1` say, stops the check and names its label,
because PEP 440 reads it too and pip prints it respelled (`1.1.0rc1`):
the distributions would say one version and `pip show` another. The
same goes for `1.1.0beta.1` in a distribution, valid PEP 440 that pip
prints as `1.1.0b1`.

**Every** `pyproject.toml` and `package.json` in the tree is either
pinned to VERSION or exempt **by name, with a reason**, in that script.
A package added without that decision fails the check — which is the
point: the failure is the decision being asked for. The bundled demo
agent is exempt on purpose: an agent is a plug-in with its own release
line, and tying its version to the platform's would say the two ship
together forever.

To bump: edit `VERSION`, run `python3 scripts/check_single_version.py`,
and fix everything it names. In `frontend/`, `npm version <VERSION>
--no-git-tag-version` writes `package.json` and both of its lock's root
lines.

## 2. Cutting a release

A release is cut in the repository that prepares it and published in the
public repository. **Nothing is tagged in the preparing repository:** its
candidate is a SHA on `main`, and a `v*` tag there would start
`release.yml` there. The tag is made in the public repository alone,
after the publication decision (R19), on the commit prepared from that
SHA.

1. **Land everything** the release contains, through pull requests, and
   **bump `VERSION`** in its own, merged.
2. **The candidate is the merge commit's SHA** on `main`, with every
   check green on it (R6) and its gates recorded (§5 step 1).
3. **Prepare the public commit** from that SHA:
   `scripts/prepare_public_repo.sh --ref <sha>`, a parentless commit for
   the first release (§5) and a fast-forward of the public `main` for
   each one after it (§6). It pushes nothing; it prints the one push.
4. **Push `main`** to the public repository, as printed, and let CI run
   there.
5. **Tag the pushed commit there**, annotated — a release should carry
   who cut it and when — and push the tag to the public repository:

   ```bash
   git tag -a v<VERSION> -m "LibreRun <VERSION>" <prepared-sha>
   git push <the public repository> v<VERSION>
   ```

   `release.yml` makes the notes-only GitHub Release from it once every
   check on that commit is green. The first release is the exception:
   the public repository has run no CI yet, so its pre-release is made by
   hand (§5 step 8).

A tag is permanent. If a release is wrong, cut the next one — never move
a tag. `gh release create` in the last job fails rather than replacing an
existing release, for the same reason.

**What a pre-release is for.** A version with a hyphen — a beta,
`1.1.0-beta.1` — is a pre-release, and its GitHub Release is marked one
(§3). It exists to be tested, not to run in production: the next
pre-release may change anything in it, so back up before each upgrade.
Each one's announcement, under `docs/release/`, says what it promises
and what it does not do yet.

## 3. What a release is

**The tag, the notes and GitHub's archives.** A release is the
repository at a tag (L37). `release-notes` makes the GitHub Release:
notes from `CHANGELOG.md` (`scripts/release_notes.py`), marked
pre-release for any tag with a `-` in it, and no file attached. The
source archives GitHub makes for every release are all it carries, and
they carry `LICENSE`, `NOTICE` and the rest the licence scope map's
`bundle` rule holds the tree to.

**No image, wheel or package.** Nothing is built for a release, and
nothing is pushed to a registry or a package index. Whoever runs
LibreRun builds it from the tag:

```bash
./scripts/demo.sh                 # every image, built from this checkout
librerun demo                     # the same, from the CLI
```

Every first-party image carries `pull_policy: build` and a local name,
`localhost/librerun/…`, so compose builds it from the checkout — from
cache when nothing changed — and never pulls it (#133).
`LIBRERUN_IMAGE_PREFIX` and `LIBRERUN_IMAGE_TAG` name the build, and may
name a registry of the operator's own to push it to; compose still
builds. `--pull` is refused in one line until 1.1.0, when it goes.

**The CLI and the SDK install from the tree**, never from a package
index: `pipx install ./cli` for `librerun` (or `pipx install
"git+<the repository>#subdirectory=cli"`), and `pip install
"./sdk/python/librerun-agent[uvicorn,otel]"` for the SDK
([`docs/authoring/SDK.md`](../authoring/SDK.md)).

**R11 holds it.** `scripts/check_no_publication.py` reads every workflow
and local action, and fails naming the file and line of any capability
to publish: a registry login, a push or cache export, a package
publish, a release asset, a permission that could publish or sign
(`write-all`, or `packages`, `attestations` or `id-token` write; `pages:
write` and `id-token: write` belong to `docs-site.yml` alone, D21), a
workflow with no top-level `permissions:`, and any secret but
`GITHUB_TOKEN`. A step behind `if: false` counts: a dormant step is one
edit from running. So does a push spelled another way — a registry
exporter (`--output type=registry`), `buildx imagetools create`, a
registry client's copy — and a key however YAML quotes it
(`"packages": write`). The `no-publication` job runs it on every pull
request, with `scripts/publication_probes.sh` planting each capability
to prove it bites, and `release-gate` runs it again on the tag.

**A new channel needs its own decision.** An image, a wheel or a package
manager is a binary channel, and L37 opens none. One would first need a
decision recorded beside L37, the licence files and third-party notices
inside what it ships (the licence scope map's "Binaries"), and the guard
changed in the same pull request — never a step added and left off.

## 4. The checks that stand in the way

`.github/workflows/release-readiness.yml` runs on every pull request, and
the release gate runs three of them again on the tagged commit:

| Job | What it refuses |
|---|---|
| `dco` | a commit with no `Signed-off-by` matching its author (L17: a DCO, no CLA) |
| `spdx` | a script header, package manifest or image label that disagrees with the licence scope map (`REUSE.toml`); a `LICENSE` or `NOTICE` that is not what it must be; a stale licence statement; a dependency or image missing from `THIRD_PARTY.md`; `reuse lint` (`scripts/check_licensing.py`) |
| `single-version` | an artifact that disagrees with `VERSION`, the web UI's lock among them; a pre-release outside D38's scheme, or spelled as pip rewrites it (§1); a package that is neither pinned nor exempt |
| `publish-purity` | a tracked file, path or symlink target that names this repository or its owner, in any spelling (the check keeps digests, not names) |
| `no-publication` | a workflow or local action that could publish: a registry login, a push, a package publish, a release asset, a permission that could publish or sign, a workflow with no `permissions:`, a secret but `GITHUB_TOKEN` (R11, §3) |
| `public-commit` | the machinery of §5 and §6 as the tree carries it (R18): `scripts/public_commit_probes.sh`, in a throwaway clone, resolves the owner with a probe handle, prepares the first commit and a fast-forward and publishes both to a local bare repository, then plants what `prepare_public_repo.sh` and `resolve_public_owner.sh` must refuse — a public commit the candidate lacks, a `--base` the public `main` never had, a placeholder, the purity canary in the tree and in the identity, a flagged or malformed handle, a rehearsal that prints a push |
| `dependency-identity` | an image the tree builds from, runs or tests on without a tag, tagged `latest`, without its `sha256` digest or not fully qualified; a workflow `docker run` that names its image by a literal; an image or lock the distribution surface matrix has no row for, or a LiteLLM row that is not the gateway lock's version and licence (R14: `scripts/check_dependency_identity.py`, planted by `scripts/dependency_identity_probes.sh`); and a Valkey that starts on a volume Redis 7.4 wrote, on a throwaway volume (C-18) |
| `tree-review` | in the tree a public repository would carry (`git archive`), a secret gitleaks finds — pinned by the SHA-256 its release lists, printing rule, file and line only — a gitlink, `.gitmodules`, LFS pointer, file with a NUL byte, cache or build output, archive or compiled object, or licence file outside the root and `LICENSES/`; an allowlist entry that names a directory (R16's scan: `scripts/review_public_tree.sh`, and its `--probe`) |

Each one **negative-tests itself**: the same job injects the violation it
exists to catch and fails if the check stays green. On a clean tree a
broken checker and a working one are indistinguishable, and these eight
run on a clean tree nearly every time.

## 5. The public repository and the first publication (L34)

LibreRun is published from a public repository,
<https://github.com/JeremiahJRRoss/librerun>, that starts from a single
parentless commit of the tested tree. The repository that prepares a
release stays private as the development record: its history carries
the demo agent's name and years of decisions that no longer apply, and
that is not what a newcomer should meet first.

So **no tracked file may name this repository or its owner** —
`publish-purity` (L39). Documentation URLs name the public repository
instead; anything that needs the preparing repository's own answer
derives it at run time from the checkout's own remote.

Every placeholder is resolved. `NOTICE` names Jeremiah Ross (L36, in the
qualified form it gives), and both contacts are `dev@librerun.dev`. The
public owner is the maintainer's personal account, `JeremiahJRRoss`,
whose handle carries no company name (L39): B1b wrote it into every site
with `scripts/resolve_public_owner.sh JeremiahJRRoss` — each
`github.com/<owner>/` URL, and `CODEOWNERS` as `@JeremiahJRRoss`, one
user and no team, since a personal account has none — and named the
repository `librerun` in each URL by hand. The four tokens stay in the
refusal pattern, so none can come back unnoticed:

```bash
./scripts/prepare_public_repo.sh --check --ref <sha>   # none — every placeholder has a real value
```

A placeholder is resolved by an **ordinary reviewed commit in the
repository that prepares the release, before the candidate is cut** —
never by the publishing step, because the published commit must carry
the *same tree* as the candidate that was tested (R7, R18), and an edit
at publication time would break that. `resolve_public_owner.sh <handle>`
refuses a handle GitHub would not take as a user name and one
`publish-purity` flags, and exits 1 naming any site it leaves.

### Preparing the commit

```bash
./scripts/prepare_public_repo.sh --ref <sha>                  # the first release
./scripts/prepare_public_repo.sh --ref <sha> \
    --parent refs/librerun/public-main --base <previous-sha>  # every one after it (§6)
```

`--ref` is the resolved candidate's SHA, in the repository that prepares
it, never a tag. The script refuses on an unresolved placeholder and on
a tree that names this repository; with `--parent` and `--base`, also on
a public commit the candidate lacks (§6). Then it builds the commit as
an object, points a ref *outside* `refs/heads/` at it, bundles that ref,
deletes the ref, and prints the commands that publish it: the bundle
checked with `git bundle verify` and fetched into a bare repository, then
one push, `<sha>:refs/heads/main` — never `--force`, never a mirror, and
no tag, because the tag is the release (R4), made after the publication
decision (R19) and never while preparing one (C-04). It creates no branch
and pushes nothing: publishing is a person's action. With
`--allow-placeholders` it prepares and proves the commit on a tree that
still carries placeholders and prints no push at all: a rehearsal.

It proves, before it exits, rather than claiming:

- the prepared commit's tree **equals** the source ref's tree (R7 — what
  was tested is what gets published);
- the commit has **no parents** — or, in §6's mode, exactly one, the
  public `main`;
- its author, committer and sign-off are **`Jeremiah Ross
  <dev@librerun.dev>`**, whatever the local git identity, and
  `publish-purity` passes on them (L39);
- **no branch was created**, and the bundle carries the new commit alone.

The commit is reproducible: the same ref produces the same SHA, so the
one the script printed can be checked against the one being pushed.
`release-readiness` → `public-commit` runs all of it on every pull
request, in a throwaway clone (§4).

### The first publication — the release owner's procedure

The release owner runs it, in this order; no session runs any of it.
Each record it names goes in the release owner's own evidence register,
never the tree.

1. **The repository that prepares it.** Once B1b, the plan's last batch,
   has merged, the tree moves to a private repository of the release
   owner's own (the K blueprint's §7 item 18). The move is written in the
   evidence register, never the tree, since this repository's remote
   names its owner (L39). The owner is resolved already, in the
   candidate itself (above): README.md's clone URL names the release
   owner's own account, so the real-name probes of `publish-purity` and
   `public-commit` take that private repository for the public owner's,
   never for this one. There, by a reviewed pull request: the maintainer
   attestation signed (R13) in a commit authored as the public identity,
   since the `dco` job builds the sign-off it wants from the author
   (`.github/dco_check.sh:33-36`) — on a branch:

   ```bash
   git -c user.name='Jeremiah Ross' -c user.email=dev@librerun.dev \
       commit -s -m 'Sign the maintainer attestation' docs/release/License_Scope_Map.md
   ```

   — and, for squat defence if wanted, a Docker Hub namespace (PyPI and
   npm reserve a name only by publishing, which L37 rules out). What
   merges there is a **new candidate**: every §3.6 gate runs on it again
   and is recorded, R6, R7 and R18 among them.
2. **A private workspace** (R10), cloned from that repository. Its
   absolute path and `git remote -v` are written in the evidence register
   before anything runs. It is a `--no-local` clone with its push URL
   disabled (`git remote set-url --push origin DISABLED`); the resolved
   candidate is checked out and `git status --porcelain` is empty; it
   holds no production `.env`, credential, engine context, volume or
   cloud resource (C-01). There: `prepare_public_repo.sh --check --ref
   <sha>`, `.github/publish_purity.sh` and `review_public_tree.sh --ref
   <sha>` (A3's); D1 from source on the release owner's machine, then
   `./compose.sh down -v`; and `prepare_public_repo.sh --ref <sha>` run
   twice, printing the same SHA, which is recorded. A cleanup the workspace shows is needed lands in the
   preparing repository as a reviewed pull request, never from the
   workspace, and the candidate is re-cut and its gates run again — so
   both paths prepare one tree and one commit (C-02).
3. **R19**, the publication decision, on those records.
4. **`JeremiahJRRoss/librerun` created empty** — no README, licence or
   `.gitignore`, which would commit — private while it is checked, and
   with Actions disabled (C-04).
5. **`main` alone pushed from the bundle**, as the script printed it: no
   mirror-push, no other branch or tag of the workspace, and no tag yet.
6. **The checks there:** `git ls-remote` shows `main` alone at the SHA; a
   fresh clone's tree is the candidate's; `check_no_publication.py`,
   `publish_purity.sh` and `review_public_tree.sh` pass on it;
   `CODEOWNERS` shows no error; and the workflows are read.
7. **A personal account's settings:** the repository made public; private
   vulnerability reporting on (`SECURITY.md`'s first route depends on
   it); the `good first issue` and roadmap labels created (gap I4); rulesets
   refusing force-push and deletion on `main`, and update and deletion on
   `v*` tags; no secret and no variable — `PYPI_PUBLISH`,
   `RELEASE_PLATFORMS` and trusted publishing retire with L37; code-owner
   review not required while one person owns every path, since GitHub
   never counts an author's own approval. An organisation differs only in
   that `CODEOWNERS` may name a team and Actions follow its policy.
8. **The first pre-release, by hand**, with Actions still off:
   `release.yml`'s gate refuses a commit no CI ran on in that repository
   (`.github/workflows/release.yml:107-110`), and the first push ran none. An annotated `v<VERSION>` on the SHA,
   pushed; then, as `release.yml` titles every later one, with the notes
   `scripts/release_notes.py v<VERSION>` makes from the candidate and no
   file argument (R4):

   ```bash
   gh release create v<VERSION> --title "LibreRun <VERSION>" \
       --notes-file notes.md --verify-tag --prerelease
   ```
9. **Actions enabled**, for the actions the workflows name.
10. **After publication:** D1 from a public clone on a clean machine, a
    newcomer's walk (D12), and R17's Source link at the tag; the records
    back in the development record by pull request; the roadmap issues
    opened afresh in the public repository from public-safe text, since
    GitHub does not transfer an issue from a private repository to a
    public one and a development issue may name this repository (L39);
    and the two repository secrets the retired Groundcover workflow read
    deleted from this one.

## 6. Every release after the first

Each release is prepared in the repository the previous one was prepared
in, as one commit on the public `main`: a fast-forward, never a new
history.

1. **Fetch the public `main`** into the preparing repository, outside
   `refs/heads/` (the script fetches nothing):

   ```bash
   git fetch <the public repository> +refs/heads/main:refs/librerun/public-main
   ```
2. **Prepare the fast-forward**, `--base` being the commit the previous
   release was prepared from:

   ```bash
   ./scripts/prepare_public_repo.sh --ref <sha> \
       --parent refs/librerun/public-main --base <previous-sha>
   ```

   It refuses, `--check` included, unless `--base`'s tree is one the
   public `main` had on its first-parent line and `--base` is an ancestor
   of `--ref`, and unless `git merge-tree --write-tree --merge-base=<base>
   <ref> <parent>` yields `--ref`'s tree exactly, naming each path that
   differs: **a release never reverts a public commit that was not
   brought back.** It needs git 2.40 or later, and checks. The commit's
   one parent is the public `main`, and the bundle carries it alone, the
   public `main` its prerequisite.
3. **Push `main`** from a bare clone of the public repository, as
   printed: a fast-forward, never `--force`.
4. **CI in the public repository**, then **the tag**, pushed there, from
   which `release.yml` makes the notes-only pre-release (§2 step 5).

**Contributions come back by cherry-pick.** A contribution merged in the
public repository comes back to the preparing repository as an ordinary
pull request, each of its commits cherry-picked (`git cherry-pick -x`)
with its author and `Signed-off-by` intact, which `dco` checks again. The
contributor keeps their copyright, and a sign-off attests and assigns
nothing ([`CONTRIBUTING.md`](../../CONTRIBUTING.md), "The DCO sign-off";
C-13). The next `--parent` run proves it came back: until then the merge
over `--base` is not the candidate's tree, and the script names the
paths.
