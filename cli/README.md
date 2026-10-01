# The `librerun` CLI

The command line for a LibreRun checkout: the zero-config demo, the
stack, a new agent from a template, a run, the conformance battery, a
health report, and the rotation of an agent's gateway key. Standard
library only, so it installs in seconds:

```bash
pipx install "git+https://github.com/JeremiahJRRoss/librerun#subdirectory=cli"
cd <your LibreRun checkout>
librerun --help
```

| Command | What it does |
|---|---|
| `librerun demo` | the zero-config demo: writes `.env`, builds, starts, waits, prints the URL and the credentials |
| `librerun up` / `down` / `logs` | the stack under compose, with the platform, viewer, example and agent profiles |
| `librerun init <name> --template langgraph \| container-python \| container-ts` | a new agent under `backend/agents/`, with its compose service and gateway key for the container templates |
| `librerun run --agent <id> [--scenario <id>] [--wait] [--email <address> --password-stdin]` | submit a sample and print the run's status and URL; non-zero on `error`. Signs in as `--email` with the password on stdin, or `LIBRERUN_EMAIL` and `LIBRERUN_PASSWORD`, else the demo's `.env` admin; `--password` is deprecated, because argv is in the process list |
| `librerun battery --agent <id>` / `--url <url> --agent-dir <dir>` | the conformance battery: in-process for a `python-package` agent, the Run Contract battery for a container |
| `librerun doctor [--email <address> --password-stdin] [--base-url <url>]` | engine, checkout, `.env` (or the keys arriving through the shell — `sops exec-env`), `gateway.env` (or the file `LIBRERUN_GATEWAY_ENV_FILE` names), agents, keys, ports, backend, gateway, trace endpoint — fails loudly without Docker or Podman. Given credentials (or `LIBRERUN_EMAIL` and `LIBRERUN_PASSWORD`), it signs in and says who you are and whether you administer the platform; it takes no password on argv, and sends one only to `--base-url` or to the stack the engine says is this checkout's, once `/api/v1/meta` answers as LibreRun |
| `librerun key rotate <id> [--finish]` | rotate an agent's gateway key with a rolling changeover; refuses a key the shell exports, because the environment outranks `.env` |

`docs/authoring/Quickstart.md` in the repository is the hour path that
uses every one of them.
