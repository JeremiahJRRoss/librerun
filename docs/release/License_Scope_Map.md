# The licence scope map

Which licence covers which part of LibreRun, why, and what the tree can
and cannot prove about where its material came from. `REUSE.toml` is
the same map in machine-readable form, `NOTICE` is the notice that
travels with the code, and `scripts/check_licensing.py` holds all three
to each other.

## The decisions this map carries out

Jeremiah Ross decided these on 2026-09-23. Where an older document
in this repository says otherwise, this map and those decisions govern,
and the older document is a record of what was decided before.

1. First-party material is licensed under **AGPL-3.0-only** — the GNU
   Affero General Public License, version 3 only — wherever it can
   properly be licensed so. There is no additional term: no
   non-commercial clause, no field-of-use limit, no restriction beyond
   the AGPL's own.
2. The first-party copyright claim is Jeremiah Ross's, and it is
   qualified. `NOTICE` states it:

   > Copyright © 2026 Jeremiah Ross, to the extent copyright subsists in
   > first-party material and such copyright is owned by Jeremiah Ross.
   > No copyright is claimed in AI-generated material that is not
   > eligible for copyright protection under applicable law.

   and, separately, the grant:

   > To the extent copyright subsists, copyrightable first-party material
   > owned by Jeremiah Ross is licensed under AGPL-3.0-only.

3. Much of the tree was produced by AI coding agents at Jeremiah Ross's direction.
   Nothing in the tree says he typed it, and no AI tool is described as
   holding any right in it.
4. Whether a particular AI-generated portion is protected by copyright is
   not decided file by file. That uncertainty is accepted, and it does
   not hold up publication.
5. Third-party material keeps its own licence and notices.
6. The name is governed separately from the licence (`TRADEMARKS.md`).
7. Contributions come in under a DCO sign-off, which is an attestation
   and not an assignment; the maintainer signs off as
   `Jeremiah Ross <dev@librerun.dev>`.
8. No organisation is the owner, publisher, licensor or maintainer of
   LibreRun because of where the development repository was kept.

## The map

| Path | Licence | Why |
|---|---|---|
| everything first-party not listed below | AGPL-3.0-only | decision 1 |
| `sdk/` | Apache-2.0 | an agent imports the SDK into its own process |
| `backend/adapters/` | Apache-2.0 | a framework adapter runs the author's graph |
| `cli/src/librerun/templates/` | Apache-2.0 | `librerun init` copies a template into the author's agent |
| `backend/agents/_examples/` | Apache-2.0 | an author starts from an example |
| `backend/alembic/README`, `backend/alembic/script.py.mako` | MIT | Alembic's scaffold, copied unmodified |
| `backend/alembic.ini`, `backend/alembic/env.py` | MIT AND AGPL-3.0-only | generated from Alembic's scaffold, then edited here |
| `CODE_OF_CONDUCT.md` | CC-BY-4.0 | the Contributor Covenant 2.1 |
| `.gitignore` | CC0-1.0 AND AGPL-3.0-only | mostly GitHub's gitignore template |
| a directory under `backend/agents/` other than `vita_v1/` and `_examples/` | not covered | `librerun init` scaffolds a user's own agent there; it is its author's to license |

The four Apache-2.0 directories are the carve-out Jeremiah Ross approved on
2026-09-23. They keep an agent's author free to license their agent as
they choose: the SDK, the templates and the examples are the parts an
agent imports or starts from across a process boundary, and none of them
hands it the AGPL. The adapters are Apache-2.0 as their own text, and
they run inside a LibreRun process: the LangGraph adapter imports the
chassis's protocol module (`backend/app/agents/protocol.py`, AGPL-3.0-only,
an interface module of dataclasses) at import time, so it does not run
without the chassis. An agent that the chassis runs in-process, or that
talks to it over HTTP, SSE or MCP, is a separate question the licence
answers, not this map; the map describes the boundaries and does not
settle the legal analysis.

**Documentation** under `docs/` is first-party material and is
AGPL-3.0-only like the code. **Generated files** — `docs/api/openapi.yaml`,
`docs/authoring/Manifest.md`, `backend/db/schema.sql` and the lockfiles —
are first-party output; a lockfile records third-party names, versions
and hashes, and none of that is relicensed by being recorded.

**Packages and images** state an AND of the licences of the files they
carry: the CLI is `AGPL-3.0-only AND Apache-2.0` because it ships the
templates; the backend image is `AGPL-3.0-only AND Apache-2.0 AND MIT`
because it carries the adapters, the examples and Alembic's scaffold;
the web and gateway images are `AGPL-3.0-only`; the SDK, the adapter and
the example images are `Apache-2.0`.

**Binaries.** LibreRun is distributed as source: the release is the
repository at a tag, and its archive carries `LICENSE`, `NOTICE`,
`LICENSES/`, `TRADEMARKS.md`, `REUSE.toml` and `THIRD_PARTY.md` (the
`bundle` rule holds that). The images and wheels a build produces carry
licence metadata but no licence files, and the project publishes none of
them (L37): the source-only batch (A2) deleted the publishing jobs from
`.github/workflows/release.yml`, and `scripts/check_no_publication.py`
(R11) refuses one that comes back. A binary channel, if one is ever decided,
must first put the licence files, the third-party notices its contents
require (`THIRD_PARTY.md` notes the ones inside some wheels) and a
pointer to the Corresponding Source into what it ships.

## Third-party material in the tree

- **Alembic's scaffold** — `backend/alembic/README` and `script.py.mako`
  are byte-identical to Alembic 1.20.0's templates, and `alembic.ini`
  and `env.py` were generated from them and then edited. Copyright
  2009-2026 Michael Bayer; MIT (`LICENSES/MIT.txt`).
- **The Contributor Covenant 2.1** — `CODE_OF_CONDUCT.md`, under
  CC BY 4.0 (`LICENSES/CC-BY-4.0.txt`), with its own attribution section.
- **GitHub's gitignore template** — most of `.gitignore`, dedicated to
  the public domain under CC0 1.0 (`LICENSES/CC0-1.0.txt`).

Nothing else was found to be third-party: no vendored package, no
binary, no font, image or icon set, no copied snippet. The components a
build *downloads* are not in the tree; `THIRD_PARTY.md` lists them.

## Provenance

What the repository shows, and what it cannot show:

- **Who made it.** The version history carries two kinds of identity:
  Jeremiah Ross's, under more than one name and e-mail, and AI coding agents'. A
  full-history blame of every first-party file finds no line from any
  other person. A commit's author field records the tool or account that
  made the commit, not who holds a right in its content: most commits
  under Jeremiah Ross's identity carry an AI co-author trailer, and many AI-authored
  commits carry Jeremiah Ross's direction in their messages.
- **Material from before this repository.** The first commit that
  imported the code (2026-08-13) brought in work developed earlier the
  same year under earlier names of the project; its history before that
  date is not in this repository. Some of it came from sub-projects
  that carried their own notices at the time:
  - an Apache-2.0 notice naming that project's contributors. Jeremiah Ross
    confirmed on 2026-09-23 that they were he alone, working with AI
    tools, so the code five files carry from them is first-party:
    `backend/app/observability/otel_init.py` and `span_enricher.py`,
    and `frontend/src/lib/toast.tsx`, `api.ts` and `auth.tsx`;
  - GPL-3.0: only two `import` lines survive, which are not protectable
    expression;
  - a "proprietary, not for redistribution" marking: nothing survives.
- **Other AI output.** A read-only review written by another AI model
  sits in the development record; code review
  comments from an AI reviewer appear on pull requests; and the demo
  agent's test fixtures hold model output captured from a test run.
  None of it is claimed as protected by copyright beyond what NOTICE
  qualifies.

### Open

- The attestation below is unsigned.

## The maintainer's attestation

Unsigned. Jeremiah Ross signs it by replacing "Unsigned" with his name and the date
in a commit he makes and signs off himself
(`Signed-off-by: Jeremiah Ross <dev@librerun.dev>`). Gate R13 reads it.

> I, Jeremiah Ross, state that, to the best of my knowledge:
>
> 1. I hold whatever copyright subsists in the first-party material of
>    this repository, except where this map or `NOTICE` records another
>    holder. Much of that material was produced by AI coding agents at
>    my direction, and I claim no copyright in AI-generated material that
>    is not eligible for protection.
> 2. No other person contributed material to this repository, except as
>    this map records.
> 3. No employer, client or other party holds a right in the first-party
>    material that prevents licensing it as this map says.
> 4. The third-party material in the repository is what `NOTICE` lists;
>    nothing else was copied into it from elsewhere.
> 5. No copy of this repository was distributed under an earlier
>    licence (GPL-3.0 until 2026-09-21, Apache-2.0 after).

## The checks that hold this

`scripts/check_licensing.py` (the `spdx` job of `release-readiness`)
runs six rules, each derived from `REUSE.toml`, and
`scripts/licensing_probes.sh` shows each one failing on the violation it
exists to catch:

| Rule | Fails when |
|---|---|
| `headers` | a script has no SPDX header, or any header disagrees with the map |
| `metadata` | a package manifest or image label does not name exactly the licences of the files it carries |
| `notice` | `LICENSE` is not the FSF's AGPL-3.0 text; `NOTICE` lacks the qualified statement, the grant, a carve-out, a third-party notice or a licence text it points at |
| `statements` | a current document says the project is Apache-2.0, or that a company holds its copyright or its name |
| `bundle` | a file a source archive must carry is untracked or export-ignored |
| `third-party` | a locked or declared dependency or an image has no row in `THIRD_PARTY.md`, or a first-party file carries a copyright notice the map does not account for |

`reuse lint` checks the map itself, and `publish-purity` keeps the
development repository's identity out of the tree, holding digests
rather than names so that the check does not spell what it forbids.

Records this map supersedes, kept as written: CHANGELOG's 1.0.0
section. The retired 1.0 plan and the session prompts that ran it, which
carried the earlier licence clause, stay in the development record and
do not ship here.
