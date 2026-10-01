# LibreRun — CentOS Stream 10 & Ubuntu 26.04 Setup

This is the **OS layer** for two specific distributions. LibreRun runs on
Linux only — macOS and Windows are not supported — and these two have
their own page. The chassis's own steps — `.env` reference, run modes,
the keyless demo, agents — live in
[`Install.md`](Install.md) and are unchanged; this guide adds what each OS
needs around them: packages, container runtime, SELinux/AppArmor,
firewalls. Where a step is identical on both distributions it appears
once, marked *(both)*.

The architecture is the same everywhere: **Postgres 16, Valkey 8 and the
Vector telemetry router always run as containers**, orchestrated by
Docker Compose or Podman Compose through `compose.sh` — nothing
database-related is installed natively on the host. The backend and
frontend run either as local processes (**development mode**) or as
containers too (**staging mode**).

**Version floors vs. what each OS provides:**

| Requirement | Floor | CentOS Stream 10 | Ubuntu 26.04 |
|---|---|---|---|
| Python | 3.12+ | 3.12 (default `python3`) ✓ | 3.13-series ✓ |
| Node.js | 22.12+ | 22.x from `dnf` ✓ | 22.x from `apt` ✓, or NodeSource 22 (see §3) |
| Container runtime | Docker 24+ / Podman 4.5+ | Podman 5 (native) | Docker CE |

> Distribution archives drift between releases. The **checkpoint
> commands in §1 and §3 are authoritative** over any version named in
> prose here — if a checkpoint passes, proceed; if it fails, install the
> alternative that section names.

---

## 1. System preparation

**CentOS Stream 10:**

```bash
sudo dnf -y upgrade
sudo dnf -y install git
sudo dnf -y install epel-release      # EPEL carries podman-compose
```

**Ubuntu 26.04:**

```bash
sudo apt-get update && sudo apt-get -y upgrade
sudo apt-get -y install git curl ca-certificates
```

> **Checkpoint:** `python3 --version` must report **3.12 or newer**.
> EL10 ships 3.12; Ubuntu 26.04 ships a newer 3.13-series — both satisfy
> the project's `requires-python = ">=3.12"`. The shipped container
> image and CI run 3.12; to match them exactly on Ubuntu, install
> `python3.12` from the deadsnakes PPA and use it for the venv in §5 of
> `Install.md`.

## 2. Container runtime

`compose.sh` auto-detects Docker or Podman (force one with
`COMPOSE_ENGINE=docker` or `=podman`), and `compose.yaml` uses
fully-qualified image names, so both runtimes work without registry
configuration. Use each distribution's native choice:

**CentOS Stream 10 — Podman (native):**

```bash
sudo dnf -y install podman podman-compose
podman --version        # 5.x
```

Rootless Podman with SELinux **enforcing** works untouched: every bind
mount in `compose.yaml` already carries the `:Z` relabel flag, and the
backend entrypoint chowns the log mount to the container user at
startup. If EPEL's
`podman-compose` is missing from your mirror's snapshot, the fallback is
`pip install --user podman-compose`.

On a server, rootless containers stop when your session ends unless
lingering is enabled:

```bash
loginctl enable-linger $USER
```

**Ubuntu 26.04 — Docker CE** (official repository):

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL -o /etc/apt/keyrings/docker.asc \
  https://download.docker.com/linux/ubuntu/gpg
echo "deb [arch=$(dpkg --print-architecture) \
signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu \
$(. /etc/os-release && echo $VERSION_CODENAME) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update
sudo apt-get -y install docker-ce docker-ce-cli containerd.io \
  docker-compose-plugin
sudo usermod -aG docker $USER     # then log out and back in
```

Archive alternative with no third-party repository:
`sudo apt-get -y install docker.io docker-compose-v2`. Podman also works
on Ubuntu (`apt-get install podman podman-compose`) if you prefer
daemonless.

## 3. Host packages for development mode

Needed only when the backend and frontend run as local processes.
Staging mode installs all of this inside the images — skip to §4 if
you'll run everything in containers.

**CentOS Stream 10** (the dnf translation of `Install.md`'s
Ubuntu/Debian list, plus fonts):

```bash
# Toolchain + Python headers (native-extension insurance)
sudo dnf -y install gcc gcc-c++ make python3-devel python3-pip libffi-devel

# WeasyPrint's rendering stack — see the warning below
sudo dnf -y install pango harfbuzz fontconfig shared-mime-info libgomp \
                    dejavu-sans-fonts dejavu-serif-fonts

# Node.js 22 (satisfies the 22.12+ floor) + Postgres client for checks
sudo dnf -y install nodejs npm postgresql
```

**Ubuntu 26.04** (the list `Install.md` documents, verbatim, plus fonts
and Node):

```bash
sudo apt-get -y install python3-venv python3-dev build-essential \
  libgomp1 libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
  libfontconfig1 libffi-dev shared-mime-info

# Fonts (minimal/server installs) + Postgres client
sudo apt-get -y install fonts-dejavu-core postgresql-client

# Node: the floor is 22.12, which 26.04's archive 22.x meets — verify it
# before trusting it (`apt-cache policy nodejs`). NodeSource 22.x is deterministic:
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get -y install nodejs
```

> **The quiet PDF failure** (same trap `Install.md` documents): without
> the Pango/HarfBuzz/fontconfig libraries, PDF export does not fail — it
> **silently downgrades to HTML**. The run still reports `complete`, and
> the person who asked for a PDF downloads an HTML file
> (`weasyprint_failed` in the backend log is the confirmation). And on a
> fontless minimal server the PDF renders but with empty glyphs — that
> is what the DejaVu packages are for; Ubuntu desktop installs already
> have fonts, minimal server images may not.

> **Checkpoint:** `node --version` ≥ 22.12 · `python3 --version` ≥ 3.12
> · `psql --version` ≥ 16.

## 4. Clone, configure, start infrastructure *(both)*

Follow `Install.md` §1–§3. Condensed:

```bash
git clone https://github.com/JeremiahJRRoss/librerun.git
cd librerun
cp .env.example .env
# Edit .env — for the keyless demo: APP_SECRET_KEY, the INITIAL_* pairs,
# LIBRERUN_STUB_LLM=true, and (dev mode) OTEL_DEBUG=true.

# For real model calls, the provider keys — in their OWN file, which the
# gateway service alone loads (Install.md §1, decision L28). Skip it for
# the keyless demo: the file is optional and the stack boots without it.
cp gateway.env.example gateway.env && chmod 600 gateway.env
# Edit gateway.env — OPENAI_API_KEY / ANTHROPIC_API_KEY / GOOGLE_AI_API_KEY.

mkdir -p data/logs      # bind-mounted log dir; create as your user, not root
./compose.sh up -d
./compose.sh ps         # librerun-postgres, -redis, -vector → healthy
```

The three `.env` rules from `Install.md` apply verbatim on both systems:
exactly **one** `.env`, at the repository root; the password inside
`DATABASE_URL` must match `POSTGRES_PASSWORD`; and never `source .env`
into a shell. `gateway.env` is the one other file, it holds the provider
keys and nothing else, and the same three rules apply to it.

On **CentOS Stream 10 with SELinux enforcing**, nothing extra is needed
for either file: both are read by the compose CLI on the host and by the
container runtime, never bind-mounted into a container, so no `:Z`
relabel and no `container_file_t` context is involved. Keep both
`chmod 600` and owned by the user who runs `./compose.sh` — on a rootful
Docker install that is the user invoking `sudo`, whose `$PWD` compose
reads them from.

> **Checkpoint:**
> `psql postgresql://librerun:librerun_dev_pw@localhost:5432/librerun -c '\dt'`
> lists tables. Without the host client:
> `./compose.sh exec postgres psql -U librerun -d librerun -c '\dt'`.

## 5. Run and verify *(both)*

Both run modes are exactly `Install.md` §5 — development mode's
first-time backend setup (venv, `pip install -r requirements.txt`, the
spaCy model, Alembic, `bootstrap_admin`, then uvicorn) plus
`npm install && npm run dev`, or staging mode's
`./compose.sh --profile app up -d --build`.

Then verify end to end with `Install.md` §2's smoke gate — collect the
credentials with the `read -rs` prompts documented there (never inline,
never from `source .env`):

```bash
python3 scripts/librerun_smoke.py \
  --base-url http://localhost:8000 --agent vita-v1 \
  --email="$LR_USER" --password="$LR_PW" \
  --admin-email="$LR_ADMIN" --admin-password="$LR_APW"
```

Exit code 0 means the stack booted, the agent was discovered, both
phases ran through the human gate, progress streamed, the report exists,
and the run carries a trace id — in development mode that last assertion
needs `OTEL_DEBUG=true` in `.env`.

## 6. OS specifics: security and firewalls

### CentOS Stream 10

- **SELinux — leave it enforcing.** Nothing to configure: the compose
  bind mounts are already labeled (`:Z`). If a future edit adds a
  bind mount and Podman answers *permission denied*, the fix is adding
  `:Z` to that mount — not `setenforce 0`.
- **firewalld — nothing for local use.** Every port publishes on
  loopback by default — the backend's and the web UI's too, since T1 —
  and firewalld does not filter loopback. Browsing from another machine
  is the only case that needs a port opened, and it is the HTTPS edge's
  (`Install.md`, "HTTPS at the edge"; check its staging checklist first —
  real secrets, the name browsers use):

  ```bash
  sudo firewall-cmd --permanent --add-port=8443/tcp   # LIBRERUN_HTTPS_PORT; 443/tcp if you publish that
  sudo firewall-cmd --reload
  ```

  Not 3000 and 8000: they are plain HTTP, and the binding, not the
  firewall, is what keeps them on loopback — `compose.sh` refuses the
  `tls` profile while either is published elsewhere, because Docker's
  publishing does not pass through firewalld's zones.

- **Rootless Podman.** All published ports are ≥ 3000 (the edge's is
  8443), so no privileged-port sysctl is needed; publishing the edge on
  443 is the one case that needs
  `sudo sysctl net.ipv4.ip_unprivileged_port_start=443` (persisted in
  `/etc/sysctl.d/`), or a firewalld forward from 443 to 8443. Subordinate ID ranges are created
  automatically for locally added users; a directory-service user
  hitting a subuid error needs
  `sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $USER`,
  then `podman system migrate`.

### Ubuntu 26.04

- **AppArmor** — nothing to configure; Docker's default profile covers
  this stack. The SELinux `:Z` mount flags in `compose.yaml` are
  inert no-ops here (see `Install.md`'s Podman notes — remove them only
  if a permission error actually names them).
- **ufw — and the Docker caveat.** ufw is off by default; nothing is
  needed for local use. If you enable it, know that **Docker-published
  ports bypass ufw** (Docker programs iptables ahead of it), so
  `ufw deny` would not shield ports 3000/8000 had they been published on
  every interface. That is why the binding is the guard: the app
  publishes on loopback by default, as the infra ports always have, and
  `compose.sh` refuses the `tls` profile unless it still does. Other
  machines reach LibreRun through the HTTPS edge (`--profile tls`,
  `Install.md`, "HTTPS at the edge") — allow its port, `sudo ufw allow
  8443/tcp` (or 443) — and the two defaults hold the rest:

  ```bash
  BACKEND_PORT=127.0.0.1:8000
  FRONTEND_PORT=127.0.0.1:3000
  ```

### Both

**Do not install Valkey, Redis or Postgres natively.** The stack expects
them as containers, and the compose file already runs the Valkey 8 and
Postgres 16 images, each pinned by digest. (EL10 no longer ships a `redis`
package at all — Valkey is its in-distro replacement — and none of that
matters here.)

## 7. Test suites *(both, optional)*

Infra containers must be up (the backend tests use `localhost:5432` and
`6379`):

```bash
cd backend  && source .venv/bin/activate && pytest
cd frontend && npm test
```

Set `LIBRERUN_STUB_LLM=false` in `.env` before `pytest` — `Install.md`
§2 explains why stub mode fails 24 provider-boundary tests in a way that
looks like a code defect and isn't.

## 8. Troubleshooting (OS-specific)

| Symptom | Cause and fix |
|---------|---------------|
| PDF export arrives as HTML | WeasyPrint's libraries or fonts missing — install the §3 list; confirm via `weasyprint_failed` in the backend log. |
| `compose.sh`: "podman-compose is not" / "compose plugin is not" | §2 incomplete. EL10: EPEL + `podman-compose` (or `pip install --user podman-compose`). Ubuntu: `docker-compose-plugin` (or `docker-compose-v2`). |
| `docker: permission denied` (Ubuntu) | Not in the `docker` group yet — `usermod -aG docker $USER`, then log out and back in. |
| Containers gone after logout (EL10) | Rootless session ended — `loginctl enable-linger $USER`. |
| Bind-mount "permission denied" (EL10) | A mount lost its `:Z` label — re-add it. Keep SELinux enforcing. |
| Node build/test errors (Ubuntu) | Archive Node below the 22.12 floor — install NodeSource 22.x (§3), `rm -rf frontend/node_modules`, reinstall. |

Everything else — login failures, trace assertions, port conflicts,
database resets — is OS-independent and lives in `Install.md`
§Troubleshooting.
