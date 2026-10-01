<!--
Thanks for contributing to LibreRun. Fill in what applies and delete what
does not — this is a checklist, not a form to satisfy.

Not for vulnerabilities: SECURITY.md has the private route.
-->

## What this changes, and why

<!-- The why is the part reviewers cannot reconstruct from the diff. -->

## Batch

<!--
LibreRun was built in batches, one batch, one pull request, planned in
the development record, which does not ship here. Roadmap work lands
against its issue, labelled v1.1 or v1.2. Link yours, or say "not a
batch" — outside contributions usually are not, and that is fine.
-->

- Issue or batch: <!-- e.g. #123 (v1.1) --> not a batch
- Deviations recorded: <!-- where the batch's plan records them; "no deviations" is a valid entry --> n/a

## How I verified it

<!--
Name the commands you ran and what they said. "Tests pass" is not
verification; `cd backend && pytest` with the count is.
-->

- [ ] `cd backend && pytest`
- [ ] `cd frontend && npm test` and `npm run build` (if the web UI changed)
- [ ] `librerun battery --agent <id>` (if an agent, adapter or template changed)
- [ ] Any CI guard I added or changed was **negative-tested**: I injected the
      violation it exists to catch and watched it go red.

## Gates

<!-- Tick what this change actually keeps green; strike what does not apply. -->

- [ ] **G0 — demo parity**: the bundled agent's intake with PII preview, live progress, the approval gate, the
      report with its citations, PDF export, feedback, the trace link, and
      `scripts/librerun_smoke.py` keyless.
- [ ] **D — the delight gate** still green: `git clone` → demo → login →
      a sample run → report → trace, inside its fifteen-minute budget.
- [ ] **R — release checklist** (`docs/platform/Releasing.md`), for changes that touch packaging,
      publishing or the licence.

## Invariants

- [ ] Every new query filters by `tenant_id`.
- [ ] No unredacted content reaches the database, a log, a span or a provider.
- [ ] The chassis names no specific agent (`chassis-purity`), and zero agents on
      disk still boots.
- [ ] No provider key is read outside `services/gateway/`; no model is named in
      code.
- [ ] I did not weaken a check to make it pass — no loosened pattern, no new
      exclusion, no skipped or quarantined test.

## Developer Certificate of Origin

- [ ] **Every commit is signed off** (`git commit -s`), with a
      `Signed-off-by:` line whose name and email match the author.

By signing off I certify the [DCO 1.1](https://developercertificate.org/): I
wrote this patch or otherwise have the right to submit it under the licence
indicated for the files it changes — AGPL-3.0-only, or Apache-2.0 in the
four directories `NOTICE` lists — and my sign-off is a public, permanent
part of the record. A sign-off is an attestation, not a copyright
assignment: LibreRun asks for no CLA and no assignment.
