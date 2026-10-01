# The distribution surface matrix

Every source of bytes a LibreRun source build fetches, what becomes of those
bytes, and who moves each row next (D27; LIC-13, LIC-18, C-22). A row is a
source of bytes, not a package: one lock is one row, however many packages it
pins.

The matrix rests on [`THIRD_PARTY.md`](../../THIRD_PARTY.md), where each
per-component fact is written once — a version, a licence, a digest — so a
row here cites that file rather than repeating it. LiteLLM is the one
exception (C-21): its row holds the gateway lock's version and the licence
`THIRD_PARTY.md` records for it, and `scripts/check_dependency_identity.py`
holds both to their sources, as it holds every image reference and every
tracked lock to a row here.

The columns (C-22):

- **Component**: what the bytes are, and where their version or digest is
  recorded.
- **Origin**: where a build fetches them from.
- **In the public tree**: what of them the repository a recipient clones
  carries.
- **Downloaded by the recipient**: whether, and by what, a source build fetches
  them.
- **In a local build**: whether they end up in an image the recipient builds.
- **Published by the maintainer**: whether a LibreRun release publishes them. A release
  is source only (L37), so no row says yes.
- **Licence**: the SPDX expression, from `THIRD_PARTY.md`.
- **Action**: who moves the row next: A3, B1b, the maintainer or v1.2.

## Fetch: what a source build downloads

Current, as A3 leaves it. The target is the same with each v1.2 action done.

| Component | Origin | In the public tree | Downloaded by the recipient | In a local build | Published by the maintainer | Licence | Action |
|---|---|---|---|---|---|---|---|
| `backend/requirements.lock.txt`: the chassis's Python packages, pinned by version (`THIRD_PARTY.md`, Python packages) | PyPI | the lock, not the packages | yes, by `backend/Dockerfile` | the backend image | no | each package's, in `THIRD_PARTY.md` | v1.2: hashes, as the gateway's lock has |
| `backend/adapters/requirements.lock.txt` and `backend/adapters/build-constraints.txt`: the LangGraph adapter's tree and its build requirements | PyPI | the locks | yes, by `backend/Dockerfile` | the backend image | no | each package's | v1.2: hashes |
| `services/gateway/requirements.lock.txt`: the gateway's packages, pinned and hashed (A3), resolved from `services/gateway/requirements.txt` | PyPI | the lock and its source | yes, with `--require-hashes`, by `services/gateway/Dockerfile` | the gateway image | no | each package's | a relock is `scripts/lock_requirements.sh`, in a reviewed PR |
| LiteLLM 1.103.1, alone (C-21): `litellm==1.103.1` in the gateway's lock and its `requirements.txt` | PyPI, one wheel per platform | no | yes, as one of the gateway lock's pins | the gateway image, as a library: no extra, no proxy or Enterprise distribution, and the reserved `enterprise/` directory is not in the wheel; the wheel's own cost map serves (`LITELLM_LOCAL_MODEL_COST_MAP=True`) | no | MIT | a new version is a reviewed relock, checked as A3 checked this one (§11) |
| `frontend/package-lock.json`: the web UI's npm packages, pinned with integrity hashes | the npm registry | the lock | yes, by `npm ci` | the web image, as build output | no | each package's | — |
| The unlocked manifests: the examples' and templates' `requirements.txt` and `package.json`, `cli/pyproject.toml`, `sdk/python/librerun-agent/pyproject.toml` and `backend/agents/vita_v1/pyproject.toml` | PyPI and npm | the manifests | yes, when an example or a template is built, or the CLI or SDK installed | that example's or template's image | no | each package's | v1.2: locks for the examples and templates |
| `docker.io/library/python:3.12-slim`, a base image (`THIRD_PARTY.md`, Container images: its tag and digest) | Docker Hub | the reference, by tag and digest | yes, by each Python image's `FROM` | the backend, gateway and Python example and template images | no | PSF-2.0; Debian's packages under their own | a digest moves by `scripts/refresh_image_digests.py`, in a reviewed PR |
| `docker.io/library/node:22-slim`, a base image | Docker Hub | the reference, by tag and digest | yes, by each Node image's `FROM` | the web image and the TypeScript example and template | no | MIT | the same refresh |
| `docker.io/library/postgres:16-alpine`, the database | Docker Hub | the reference, by tag and digest | yes, pulled by compose | no: run as pulled | no | PostgreSQL | the same refresh |
| `docker.io/valkey/valkey:8-alpine`, the cache server under the `redis` name (L38) | Docker Hub | the reference, by tag and digest | yes, pulled by compose | no: run as pulled | no | BSD-3-Clause | the same refresh |
| `docker.io/timberio/vector:0.50.0-alpine`, the telemetry router | Docker Hub | the reference, by tag and digest | yes, pulled by compose | no: run as pulled | no | MPL-2.0 | the same refresh |
| `docker.io/jaegertracing/all-in-one:1.66.0`, the trace viewer (profiles `viewer` and `full`) | Docker Hub | the reference, by tag and digest | under those profiles | no: run as pulled | no | Apache-2.0 | the same refresh |
| `docker.io/otel/opentelemetry-collector:0.159.0` (profiles `obs` and `cribl`) | Docker Hub | the reference, by tag and digest | under those profiles | no: run as pulled | no | Apache-2.0 | the same refresh |
| `docker.io/library/caddy:2.11.4`, T1's proxy: pulled only under `--profile tls`, never built. Its ACME requests are run-time egress, outside this matrix. T2's `edge-control`, the other `tls` service, is the backend's image and adds no row | Docker Hub | the reference, by tag and digest | under `--profile tls` | no: run as pulled | no | Apache-2.0 | the same refresh |
| `en_core_web_lg` 3.8.0, the spaCy model (`THIRD_PARTY.md`, the model's row) | GitHub, spaCy's model releases | no | yes, by `python -m spacy download` | the backend and gateway images | no | MIT | v1.2: fetched by hash |
| Debian packages: `libgomp1`, `libpango-1.0-0`, `libpangoft2-1.0-0`, `libharfbuzz0b`, `libfontconfig1`, `libffi-dev` and `shared-mime-info` (backend); `libgomp1` (gateway) | deb.debian.org | no | yes, by `apt-get` in those two Dockerfiles | the backend and gateway images | no | each package's; their copyright files are in the image | v1.2: by hash, from a dated snapshot |

## Build: what the recipient's build produces

Current and target alike: the recipient builds each first-party image from
the tree (R12: compose builds them, `pull_policy: build`, and never pulls
one), and no LibreRun image is published (L37).

| Component | Origin | In the public tree | Downloaded by the recipient | In a local build | Published by the maintainer | Licence | Action |
|---|---|---|---|---|---|---|---|
| `librerun-backend`, from `backend/Dockerfile`; `edge-control` runs the same image | built from the tree | its Dockerfile and sources | no | yes | no | AGPL-3.0-only, with the carve-outs in `REUSE.toml` | R12 recorded on the candidate (B1b, `source-build`); JR, on the signed tree |
| `librerun-gateway`, from `services/gateway/Dockerfile` | built from the tree | its Dockerfile and sources | no | yes | no | AGPL-3.0-only | R12 recorded on the candidate (B1b, `source-build`); JR, on the signed tree |
| `librerun-web`, from `frontend/Dockerfile`, with `NEXT_TELEMETRY_DISABLED=1`, so `next build` sends no usage report | built from the tree | its Dockerfile and sources | no | yes | no | AGPL-3.0-only | R12 recorded on the candidate (B1b, `source-build`); JR, on the signed tree |
| An example's or a template's agent image | built from the tree | its Dockerfile and sources | no | when the recipient builds it | no | Apache-2.0 for the templates; the example's own | — |

## Cache: what stays between builds

Current and target alike. A build installs with `--no-cache-dir`; the
recipient's engine keeps its own image and layer cache. CI keeps pip's and
npm's caches, keyed on the locks, in GitHub Actions' cache for the
repository, and exports no build cache anywhere (`cache-to` is refused by
`no-publication`).

## Upload: what leaves the build

Current: CI uploads its logs, traces and the browser walk as workflow
artifacts, kept for seven to fourteen days, and publishes nothing:
`release-readiness` → `no-publication` refuses any permission, login, push
or publish that would (R11). Target: the same until the maintainer's own repository,
where a tag makes a GitHub Release carrying the source archives GitHub
attaches, and nothing else (R4, the maintainer's).

## Continuous integration: fetched by the workflows, never shipped

| Component | Origin | In the public tree | Downloaded by the recipient | In a local build | Published by the maintainer | Licence | Action |
|---|---|---|---|---|---|---|---|
| `actions/checkout@v4`, `actions/setup-python@v5`, `actions/setup-node@v4`, `actions/upload-artifact@v4` and `actions/github-script@v7` | GitHub | the references, by tag | no | no | no | MIT | v1.2: pinned by commit |
| `docker.io/library/postgres:16`, CI's database service | Docker Hub | the reference, by tag and digest | no | no | no | PostgreSQL | the same refresh |
| `docker.io/curlimages/curl:8.10.1`, `obs-vendors`' probe of its mock | Docker Hub | the reference, by tag and digest | no | no | no | MIT | the same refresh |
| `docker.io/library/redis:7-alpine`, `dependency-identity`'s proof that Valkey refuses a Redis 7.4 volume (C-18), and nothing else | Docker Hub | the reference, by tag and digest | no | no | no | LicenseRef-RSALv2 OR SSPL-1.0 | goes when the proof does |
| gitleaks 8.30.1, the tree review's scanner, its release tarball's SHA-256 pinned in `scripts/review_public_tree.sh` | GitHub, the project's releases | no | no | no | no | MIT | a new version is a reviewed PR with both SHA-256s |

## Build-time network is not run-time egress (C-09)

A source build fetches every row of the fetch table, from the origins named
there, and a build without that network fails rather than builds something
else. None of it is run-time egress: a running LibreRun's outbound requests
are the flows in [`Security.md`](../platform/Security.md) under "Every flow
that leaves the box", and a keyless run makes one there at most (the Public
Suffix List refresh). Those flows do not loosen because a build fetched its
packages.

## The tree review

What a public repository would carry is the tree `git archive` extracts,
reviewed by `scripts/review_public_tree.sh` (R16's A3 half), which
`release-readiness` → `tree-review` runs on every pull request:

```bash
bash scripts/review_public_tree.sh --install "$HOME/.cache/gitleaks"   # the pinned gitleaks, its SHA-256 checked
GITLEAKS="$HOME/.cache/gitleaks/gitleaks" bash scripts/review_public_tree.sh          # HEAD
GITLEAKS="$HOME/.cache/gitleaks/gitleaks" bash scripts/review_public_tree.sh --probe  # each plant red
```

Its tree rules read what git records, never the working tree:

```bash
git ls-tree -r HEAD | awk '$1 == "160000"'                      # gitlinks: none
git ls-files | grep -E '(^|/)\.gitmodules$'                     # submodules: none
git ls-files -z | xargs -0 git check-attr filter export-ignore export-subst | grep -v unspecified  # none
git grep -l --text -P '\x00' HEAD -- .                          # files with a NUL byte, read whole: none
```
