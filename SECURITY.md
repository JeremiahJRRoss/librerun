# Security policy

## Reporting a vulnerability

**Do not open a public issue for a vulnerability.** Report it privately:

1. **GitHub private vulnerability reporting** (preferred) — on the
   repository, *Security* → *Report a vulnerability*. It creates a
   private advisory that only the maintainers and you can read, and it
   is the route that needs no address and no waiting for one. If that
   button is not there, the setting has not been enabled yet and route 2
   is the one to use.
2. Email **dev@librerun.dev**.

Please include: what you did, what happened, what you expected, the
version or commit, and whether you have a proof of concept. A short
reproduction is worth more than a long description. If you have a patch,
say so — but send it privately, not as a pull request, until the
advisory is published.

We aim to acknowledge a report within **3 working days** and to have an
assessment within **10**. Those are the targets we hold ourselves to,
not a contractual guarantee: this is an open-source project, and a
promise it cannot keep is worth less than a target it is measured
against. If a report turns out to be a bug rather than a vulnerability
we will say so and move it into the open, with your agreement. We will
credit you in the advisory unless you ask us not to.

We ask for **90 days** before public disclosure, or until a fix ships if
that is sooner. If you believe a vulnerability is being exploited, say
so in the report and we will work to your timetable rather than ours.

Report privately, please, even for what looks minor: a leak of another
tenant's data through a query that forgot its `tenant_id` filter, or
unredacted content reaching the database, are the two failures this
platform exists to prevent, and either is a vulnerability here.

## What is in scope

The chassis and everything shipped in this repository: the backend, the
web UI, the LLM gateway, the SDK, the CLI, the adapters, the example
agents, the container and compose configuration, and the CI workflows.

Particularly interesting to us:

- **Tenant isolation.** Any path that reads or writes data across
  tenants — a query without its `tenant_id` filter, a token accepted for
  the wrong tenant, an id that is guessable and unscoped.
- **The PII boundary.** Unredacted content reaching the database, the
  logs, a trace, or a model provider. The detector is designed to fail
  closed; a way past it is a vulnerability.
- **Credential handling.** The gateway is the only process that holds a
  provider key. A path that gets one into the backend, into an agent, or
  into a log, span or error message is a vulnerability. So is a way to
  use one agent's gateway key as another's.
- **The agent network boundary.** Container agents sit on an internal network
  with no route off the host. Escaping it without `network.egress: true`
  in the manifest is a vulnerability.
- **The approval gate.** Anything that advances a run past the human
  gate without a human.

## What is not in scope

- Findings that need a valid administrator credential to reach: an admin
  can configure the deployment, and that is the design.
- The **demo mode** defaults (`LIBRERUN_DEMO=true`): generated
  credentials printed to the terminal, a default secret key, a trace
  viewer open on localhost. Demo mode says loudly that it is not for
  production; `docs/platform/Install.md` has the production checklist. A way to
  get a *production* configuration to behave like demo mode **is** in
  scope.
- Denial of service by sheer volume against a deployment you control,
  vulnerabilities in a dependency with no path to exploit here (tell us
  anyway, but as an issue), and reports produced by a scanner with no
  demonstrated impact.
- Anything requiring physical access to the host, or a compromised host.

## Supported versions

| Version | Supported |
|---|---|
| `1.1.0-beta.N` | The newest beta alone: a fix ships as the next beta, or in 1.1.0 — never as a patch to an earlier beta |
| `1.0.0` | No — certified on 2026-09-22 and never published |
| `< 1.0` | No |

From 1.1.0, the current minor and the one before it get security fixes,
released as a patch version. Every fix is announced in the GitHub
advisory and recorded in `CHANGELOG.md`.

## Hardening

`docs/platform/Install.md` carries the production checklist — the secrets that
must not stay at their demo values, the encrypted-at-rest options
(`sops` and `age`), and how secrets are partitioned by process so that
only the gateway ever holds a provider credential. The security model itself —
the trust boundaries, tenancy, PII, where each secret lives and every
flow that leaves the box — is
[`docs/platform/Security.md`](docs/platform/Security.md), written at S8.

---

**dev@librerun.dev** reaches the maintainer, Jeremiah Ross. GitHub
private vulnerability reporting needs no address, once it is enabled on
the public repository.
