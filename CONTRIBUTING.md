# Contributing to LibreRun

LibreRun is an educational software environment for teaching the
design, development and operation of AI agents using multiple
agent-development frameworks. At its centre is the platform chassis,
built for educational purposes — intake, the run lifecycle with a human
approval gate, PII redaction before persist, per-step LLM
configuration, reports, tenancy, tracing — and **agents plug into it and
inherit all of it**. It exists to be learned from as much as run, so a
page that teaches something the tree does not do is a defect like any
other. Most contributions are one of four things: an agent or an
example, a framework adapter, a fix in the chassis, or a page of
documentation that was wrong. All four are welcome, and this page is how
to land one.

Contributions come in under a **DCO sign-off**, not a CLA (locked
decision L17). The [Code of Conduct](CODE_OF_CONDUCT.md) applies
everywhere the project happens.

## The development loop

You need a Linux machine with a container engine (Docker or Podman) and
nothing else — macOS and Windows are not supported. Python and Node are
needed only if you run the suites outside a container.

```bash
git clone <this repository> && cd librerun
./scripts/demo.sh                 # writes a demo .env, builds, starts, prints the URL
# or, once the CLI is installed:
pipx install "git+<this repository>#subdirectory=cli"
librerun demo                     # the same thing in Python
librerun up                       # start an existing .env's stack
librerun logs -f backend          # tail a service
librerun down                     # stop
```

`./scripts/demo.sh` and `librerun demo` build every image from this
checkout: a release is source only, so there are no published images to
start from, and nothing is pulled. See
[`docs/platform/Install.md`](docs/platform/Install.md) for the real deployment
modes and [`docs/authoring/Quickstart.md`](docs/authoring/Quickstart.md)
for writing an agent.

Run what CI runs before you push:

```bash
cd backend && pytest              # the chassis suite
cd frontend && npm test           # the web suite
cd frontend && npm run build      # the production build, if you touched it
librerun battery --agent <id>     # the conformance battery for one agent
```

## Adding an example or a template

**An example that is not in the battery matrix is not an example.** The
batteries are the promise that a LibreRun agent written against this
contract keeps working: `backend/adapter_kit/` for in-process agents and
the Run Contract battery for containers. Adding an example (or an
adapter, or a `librerun init` template) means adding it to the matrix
that CI runs — `.github/workflows/container-battery.yml`,
`adapter-battery.yml` or `template-matrix.yml` — in the same pull
request. A new image needs `pull_policy: build` beside its `image:` —
`test_agents_network.py` names the six that build, and a seventh is a
change to that list — and never a line in
[`.github/workflows/release.yml`](.github/workflows/release.yml), which
publishes nothing but the notes.

See [`docs/authoring/Agents_Design.md`](docs/authoring/Agents_Design.md) for the agent
contract and the platform invariants an agent may rely on.

## Never weaken a check to make it pass

This is the rule the project will not bend on, and the one most likely
to send a pull request back.

> A gate that reports success by not looking is worse than no gate. On a
> clean tree a broken checker and a working one are indistinguishable.

So:

- **Never** loosen a pattern, add a path exclusion, skip a test, mark it
  `xfail`, or quarantine it to turn CI green. If a check is wrong, fix
  the check and prove the new one still catches what the old one caught.
- **Every guard you add or touch is negative-tested**: the same job
  injects the exact violation the guard exists to catch and fails if the
  guard stays green. `chassis-purity`, `name-purity`, `publish-purity`
  and the `release-readiness` jobs are all written this way — copy one.
- A flaky test is a bug in the test. "Re-run it" is not a fix.

Two more invariants CI enforces, worth knowing before you write code:

- **Tenant scoping is mandatory** — every database query filters by the
  `tenant_id` in the JWT.
- **PII redaction happens before persist** — unredacted content never
  reaches the database, and the detector fails closed.
- **The chassis knows nothing about any specific agent** — no agent id,
  no agent-shaped field, no import of agent code from chassis code. Zero
  agents on disk is a healthy configuration.

## Batches, and how this repository is organised

LibreRun was built in **batches**, one batch, one pull request, each
batch named in its title (`K6: …`); the plans that ran them stay in the
development record and do not ship here, and
[`CHANGELOG.md`](CHANGELOG.md) records what each one changed. What comes
next is the roadmap
in [`docs/release/v1.1.0-beta.1.md`](docs/release/v1.1.0-beta.1.md#roadmap);
a change toward a roadmap item names it, and its issue when there is one.
If your change belongs to none — most outside contributions do not —
that is fine; say what it fixes instead.

- A change that departs from a documented decision says so in the pull
  request, and in the page that documents the decision.
- `CHANGELOG.md` gets an entry under `[Unreleased]` for anything a user
  would notice; start that section at the top if the newest one is a
  release.

## Pull requests

1. Branch, commit with a message that says **why**, and sign off (below).
2. Fill in the pull-request template: what changed, how you verified it,
   the gate checkboxes.
3. CI must be green. Every check in `.github/workflows/` runs on every
   pull request; the release workflow additionally runs on `v*` tags.
4. Review is by the owners in [`.github/CODEOWNERS`](.github/CODEOWNERS).
   Expect questions about invariants rather than style — formatting is
   the tools' job.

## The DCO sign-off (required)

LibreRun uses the [Developer Certificate of
Origin](https://developercertificate.org/) 1.1. There is no CLA and no
copyright assignment: you keep your copyright, and you certify that you
have the right to contribute the code under the project's licence.

Every commit must carry a `Signed-off-by` trailer whose name and email
match the commit author:

```
Signed-off-by: Ada Lovelace <ada@example.com>
```

`git commit -s` adds it from your `user.name` and `user.email`. To fix a
branch that is missing it:

```bash
git commit --amend -s --no-edit                      # the last commit
git rebase --signoff origin/main                     # every commit on the branch
git merge --signoff origin/main                      # a merge is checked like any commit
```

The `release-readiness / dco` job checks every commit in a pull request
and fails with the offending commit's hash. That check is a legal
requirement, not a style preference — it is not waived.

By signing off you certify the DCO's four clauses, in short: (a) you
created the contribution and have the right to submit it under the
licence indicated for the files it changes; or (b) it is based on
earlier work under a suitable open-source licence and you have the
right to submit it, with your changes, under that licence; or (c)
someone who certified (a), (b) or (c) gave it to you and you have not
changed it; and (d) the contribution and your sign-off are public and
kept in the record for good. The licence indicated for a file is the
one the [licence scope map](docs/release/License_Scope_Map.md) gives
its path.

A sign-off is a certification, not a transfer. You keep whatever
copyright you hold in your contribution, and a `Signed-off-by` line does
not make it the maintainer's or anyone else's property.

A sign-off is a person's certification, and a coding assistant cannot
give one. If a tool produced part of your change, you sign off for the
whole change, and only if you can make the DCO's certifications for it.

The maintainer, Jeremiah Ross, signs off as:

```
Signed-off-by: Jeremiah Ross <dev@librerun.dev>
```

## Licence

A contribution is licensed under the licence of the files it changes:
`AGPL-3.0-only` for most of the tree, and `Apache-2.0` in the four
directories the [licence scope map](docs/release/License_Scope_Map.md)
lists (`sdk/`, `backend/adapters/`, `cli/src/librerun/templates/`,
`backend/agents/_examples/`). A new file takes the licence of its path.

There is no copyright assignment. If the project ever needed one, that
would be a separate, explicit process that you would agree to on its
own; nothing on this page is one.

The licence covers the software, not the name: what a fork may call
itself is in [`TRADEMARKS.md`](TRADEMARKS.md).

## Reporting a vulnerability

Not here, and not in a public issue. [`SECURITY.md`](SECURITY.md) has the
private route.
