"""``key rotate``: the D10 rotation of an agent's gateway key, as a command.

An env-provisioned key is rotated in ``.env`` and made effective by
recreating the two containers that read it — never through the admin
page, whose rotate action is for admin-issued keys (blueprint S4a, S6):

1. ``librerun key rotate <id>`` moves the current value to
   ``LIBRERUN_AGENT_KEY_<ID>_PREVIOUS``, writes a fresh value on the main
   line, and runs ``up -d gateway <service>``: the gateway re-derives
   ``agent-keys.env`` and accepts BOTH values, the agent container is
   recreated with the new one. The service is resolved from the
   fragment's ``librerun.agent_id`` label — an id alone cannot name a
   service — or given as ``--service``. Named explicitly, so the service's
   own profile is activated: a bare ``up -d`` would leave a profiled
   container on its old environment.
2. ``librerun key rotate <id> --finish`` removes the ``_PREVIOUS`` line and
   runs ``up -d gateway`` alone: the agent's environment has not changed,
   so compose leaves its container as it is; the gateway re-derives its
   file and the old value is refused from that boot.

The suffix is ``_PREVIOUS`` as the gateway reads it
(``services/gateway/gateway/config.py``): the variable is
``LIBRERUN_AGENT_KEY_<ID>_PREVIOUS``, a separate variable rather than a
suffix on the id, because a suffix would collide with a legitimate id
such as ``foo-previous``.

Both steps edit ``.env`` and nothing else, so both refuse when the shell
exports either variable: since K3 the environment outranks the file for
compose and ``compose.sh`` alike, and a rotation written under an
exported name would recreate two containers on the old key and report
success. A deployment that runs with no plaintext ``.env`` at all
(``sops exec-env``, docs/platform/Install.md "Encrypting .env at rest") rotates
the value in the encrypted file and recreates the containers from there.
"""
from __future__ import annotations

from pathlib import Path

from . import _compose
from ._agents import find_agent, service_for
from ._common import CliError, say, validate_agent_id
from ._env import (
    AGENT_KEY_PREVIOUS_SUFFIX,
    KEY_VALUE_PREFIX,
    DotEnv,
    agent_key_variable,
    mint_agent_key,
)


def prefix_of(value: str) -> str:
    """The eight characters after ``lr_agent_`` — what the admin page
    shows, and all this command ever prints of a key."""
    body = value.strip()
    if body.startswith(KEY_VALUE_PREFIX):
        body = body[len(KEY_VALUE_PREFIX) :]
    return body[:8]


def _refuse_when_the_shell_outranks_the_file(env: DotEnv, agent_id: str, *names: str) -> None:
    """The environment wins over ``.env`` (K3), so an edit to the file
    under an exported name changes nothing a container sees — refuse
    rather than report a rotation that did not happen."""
    exported = [name for name in names if env.in_environment(name)]
    if exported:
        raise CliError(
            f"{' and '.join(exported)} exported in this shell: the environment "
            f"outranks .env for compose and compose.sh alike (K3), so an edit to "
            f".env would change nothing the gateway or the container sees. Unset "
            f"it here and run again — or, when the keys come from `sops exec-env` "
            f"(no plaintext .env on disk), rotate the value in the encrypted file "
            f"and recreate the gateway and {agent_id}'s container from there."
        )


def rotate(env: DotEnv, agent_id: str) -> tuple[str, str]:
    """Edit the text: old value to ``_PREVIOUS``, a fresh one on the main
    line. Returns ``(variable, new value)``. Pure — nothing is written."""
    variable = agent_key_variable(agent_id)
    previous = variable + AGENT_KEY_PREVIOUS_SUFFIX
    _refuse_when_the_shell_outranks_the_file(env, agent_id, variable, previous)
    current = env.values().get(variable, "")
    if not env.file_has(variable) or not current.strip():
        raise CliError(
            f"{variable} is not provisioned in .env, so there is nothing to "
            f"rotate. `librerun up` provisions a key for every agent on disk."
        )
    if env.file_has(previous):
        raise CliError(
            f"a rotation of {agent_id} is already in flight: {previous} is set. "
            f"Finish it first — `librerun key rotate {agent_id} --finish` — "
            f"once every container has picked up the current key."
        )
    fresh = mint_agent_key()
    env.set(previous, current.strip())
    env.set(variable, fresh)
    return variable, fresh


def finish(env: DotEnv, agent_id: str) -> str:
    """Edit the text: drop the ``_PREVIOUS`` line. Returns the variable
    removed. Pure — nothing is written."""
    variable = agent_key_variable(agent_id)
    previous = variable + AGENT_KEY_PREVIOUS_SUFFIX
    _refuse_when_the_shell_outranks_the_file(env, agent_id, variable, previous)
    if not env.file_has(previous):
        raise CliError(
            f"no rotation of {agent_id} is in flight: {previous} is not set in "
            f".env. `librerun key rotate {agent_id}` starts one."
        )
    env.remove(previous)
    return previous


def resolve_service(root: Path, agent_id: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    service = service_for(root, agent_id)
    if service is not None:
        return service.name
    summary = find_agent(root, agent_id)
    if summary is not None and summary.runtime != "container":
        raise CliError(
            f"{agent_id} is a {summary.runtime} agent: it runs inside the "
            f"backend and holds no gateway key, so there is nothing to rotate."
        )
    raise CliError(
        f"no service in agents.compose.yaml carries the label "
        f"librerun.agent_id: {agent_id}, so the container to recreate cannot "
        f"be named from the id. Pass --service <name>."
    )


def cmd_key_rotate(root: Path, args) -> int:
    agent_id = validate_agent_id(args.agent_id)
    env = DotEnv(root / ".env")
    if not env.exists:
        raise CliError(
            "no .env in this checkout, so there is no key line to rotate. "
            "`librerun up` writes one; when the keys come from `sops exec-env` "
            "(no plaintext .env on disk — docs/platform/Install.md, \"Encrypting .env at "
            "rest\"), rotate the value in the encrypted file and recreate the "
            "gateway and the agent's container from there."
        )
    if args.finish:
        previous = finish(env, agent_id)
        env.write()
        say(f"removed {previous} from .env")
        if args.no_up:
            say("--no-up: run `librerun up` (or `./compose.sh up -d gateway`) to make the gateway refuse the old key")
            return 0
        # The gateway alone: the agent's environment is unchanged, so
        # compose would not recreate it and need not. --no-build: a key
        # changes the environment, not the image, and every service builds
        # on `up` otherwise (pull_policy: build, #133).
        _compose.run(root, "up", "-d", "--no-build", "gateway")
        say(f"gateway recreated: the previous key of {agent_id} is refused from now on")
        return 0

    service = resolve_service(root, agent_id, args.service)
    variable, fresh = rotate(env, agent_id)
    env.write()
    say(
        f"rotated {variable}: new key {KEY_VALUE_PREFIX}{prefix_of(fresh)}… on the "
        f"main line, the old value kept as {variable}{AGENT_KEY_PREVIOUS_SUFFIX}"
    )
    if args.no_up:
        say(f"--no-up: run `./compose.sh up -d gateway {service}` to apply it")
        return 0
    _compose.run(root, "up", "-d", "--no-build", "gateway", service)
    say(
        f"gateway and {service} recreated: both keys work until "
        f"`librerun key rotate {agent_id} --finish` retires the old one"
    )
    return 0
