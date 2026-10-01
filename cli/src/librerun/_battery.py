"""``battery``: the conformance battery, for an agent id or a URL.

Two batteries, one command (gap C2):

* a ``python-package`` agent runs the adapter battery
  (``backend/adapter_kit``): schema-valid output, streamed progress,
  spans, output that survives persistence — driven by
  ``python -m adapter_kit.in_process`` (S6);
* a ``container`` agent, or ``--url``, runs the Run Contract battery
  (``python -m adapter_kit.run_contract``): ``/healthz``, token binding,
  progress, exactly one ``completed``, an output the chassis walk
  accepts.

Both run **inside a one-off backend container** — ``compose run --rm
--no-deps --name librerun-battery-<pid> backend python -m adapter_kit.…``
— because that is where the chassis's Python lives (the CLI installs
nothing but itself) and the only place a container agent's address
resolves: agent containers sit on the internal ``agents`` network and
publish no port. The ``--name`` is not cosmetic: a one-off container
does NOT answer to the service name, so it is the only address an agent
can use to call the driver back (``driver_container_name``). The agent's source
directory is bind-mounted over the image's copy, so an in-process
battery tests the file you just edited, with no rebuild in between; a
container is the artifact, so after an edit ``librerun up`` rebuilds it
first and the battery drives the running container.
"""
from __future__ import annotations

from pathlib import Path

import os

from . import _compose
from ._agents import find_agent, service_for
from ._common import CliError, say, warn


def driver_container_name() -> str:
    """The name the DRIVER's own container answers to on the agents network.

    `compose run` does NOT give a one-off container the service's
    network aliases — `docker compose run --help` carries
    `--use-aliases` precisely because that is opt-in, and this CLI has
    never passed it. So `backend` resolves, from inside an agent, to
    the LONG-RUNNING backend service container, where nothing is
    listening on the driver's ephemeral MCP port.

    That is the whole of the bug: `backend:8000` worked, because that
    really is the long-running backend serving the chassis's MCP, while
    `backend:<ephemeral>` was refused — one name, two containers, and
    the battery advertised the one it was not in. Every agent driven by
    `librerun battery` therefore reported `mcp: skip`, and the callback
    the battery exists to exercise was never exercised on this path.

    A container name IS resolvable on a user-defined network, so the
    driver is given one and advertises that. Unique per process, since
    two batteries may run at once and a name collision would fail the
    `compose run` outright.

    NOT `--use-aliases`: that would put a second container behind the
    `backend` name, Docker would answer with both addresses, and the
    agent would reach the driver only some of the time. A flaky pass is
    worse than an honest skip.
    """
    return f"librerun-battery-{os.getpid()}"

CONTAINER_AGENTS = "/app/agents"


def _mounted_dir(root: Path, summary) -> tuple[str, str]:
    host = str(summary.directory.resolve())
    inside = f"{CONTAINER_AGENTS}/{summary.directory.name}"
    if not host.startswith(str((root / "backend" / "agents").resolve())):
        # An agent outside backend/agents (a custom LIBRERUN_AGENTS_PATH)
        # is still mounted, under its own name, and the driver is told
        # where.
        inside = f"/mnt/agent/{summary.directory.name}"
    return host, inside


def cmd_battery(root: Path, args) -> int:
    if not args.agent and not args.url:
        raise CliError("say which agent: --agent <id>, or --url <url> --agent-dir <dir>")
    extra = []
    if args.scenario:
        extra += ["--scenario", args.scenario]
    if args.json:
        extra.append("--json")
    # `run.mcp.url` is served by the driver, and the driver runs in a
    # ONE-OFF container of its own (`_run_in_backend`). So the host an
    # agent must use to reach it is that container's name — not
    # `127.0.0.1`, which from inside the agent is the agent, and not
    # `backend`, which names the long-running service container the
    # driver is not in. See `driver_container_name`. Container-to-
    # container on the `agents` network, so no published port is
    # involved. The in-process battery never sees this flag.
    driver = driver_container_name()
    container_mcp = ["--mcp-advertise-host", driver]

    if args.url:
        if not args.agent_dir:
            raise CliError("--url needs --agent-dir <dir>: the battery reads the manifest and the scenario from the agent's directory")
        agent_dir = Path(args.agent_dir).expanduser().resolve()
        if not (agent_dir / "agent.yaml").is_file():
            raise CliError(f"{agent_dir} carries no agent.yaml")
        inside = f"/mnt/agent/{agent_dir.name}"
        say(f"container battery against {args.url} (the URL must resolve from inside the backend container: a compose service name, such as http://my-agent:8090)")
        module = ["python", "-m", "adapter_kit.run_contract", "--url", args.url,
                  "--agent-dir", inside, "--timeout", str(args.timeout),
                  *container_mcp, *extra]
        return _run_in_backend(root, str(agent_dir), inside, module, name=driver)

    summary = find_agent(root, args.agent)
    if summary is None:
        raise CliError(
            f"no agent with id {args.agent!r} under backend/agents or "
            f"backend/agents/_examples. `librerun init` creates one; "
            f"`librerun doctor` lists the ones on disk."
        )
    host, inside = _mounted_dir(root, summary)
    if summary.runtime == "container":
        service = service_for(root, summary.id)
        if service is None:
            raise CliError(
                f"{summary.id} is a container agent but no service in "
                f"agents.compose.yaml carries the label librerun.agent_id: "
                f"{summary.id}; pass --url http://<service>:8090 --agent-dir {host}"
            )
        url = f"http://{service.name}:8090"
        say(f"container battery: {summary.id} at {url} (the running container — after editing the agent, `librerun up` rebuilds it first)")
        say("the span check is reported `skip` here: the container exports to the chassis relay, not to a recorder; .github/workflows/container-battery.yml holds the exporting half")
        say("the mcp check reports what your agent asked the chassis for over the run.mcp.url this battery advertises; `skip` means it never called, which the contract allows")
        module = ["python", "-m", "adapter_kit.run_contract", "--url", url,
                  "--agent-dir", inside, "--timeout", str(args.timeout),
                  *container_mcp, *extra]
        return _run_in_backend(root, host, inside, module, name=driver)
    say(f"adapter battery: {summary.id} ({summary.runtime}), from the source under {host}")
    module = ["python", "-m", "adapter_kit.in_process", "--agent-dir", inside, *extra]
    # No name: the in-process battery serves no callback, so there is
    # nothing for an agent to reach and nothing to advertise.
    return _run_in_backend(root, host, inside, module)


def _run_in_backend(
    root: Path, host: str, inside: str, module: list[str], name: str | None = None
) -> int:
    # `-T`: no TTY, so the output is the driver's own lines and nothing
    # else; `--no-deps`: the stack is already up, and the battery must not
    # start half of it; `--rm`: nothing to clean up afterwards.
    # `--name`: so the agent can reach the driver's MCP endpoint, which
    # the compose service name does not (`driver_container_name`).
    named = ["--name", name] if name else []
    result = _compose.run(
        root, "run", "--rm", "--no-deps", "-T", *named, "-v", f"{host}:{inside}:ro",
        "backend", *module, check=False,
    )
    if result.returncode == 0:
        say("battery: green")
    else:
        warn(f"battery: red (exit {result.returncode}) — the reasons are in the report above")
    return result.returncode
