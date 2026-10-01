"""The ``librerun`` command: one parser, one subcommand per verb."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from ._common import CliError, find_root, warn


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="librerun",
        description=(
            "The LibreRun CLI. Every command operates on a LibreRun checkout: "
            "the nearest directory above the working directory carrying "
            "compose.yaml and compose.sh, or --root."
        ),
    )
    parser.add_argument("--version", action="version", version=f"librerun {__version__}")
    parser.add_argument("--root", help="the LibreRun checkout to operate on (default: found from the working directory, or LIBRERUN_ROOT)")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    p = sub.add_parser("demo", help="the zero-config demo: write .env, build, start, print the URL and credentials")
    p.add_argument("--env-only", action="store_true", help="write .env (if absent), top up agent keys, and stop")
    # Retired with image publishing (A2): kept hidden so `demo --pull` is
    # refused in one line, rather than as an unknown option, until 1.1.0.
    p.add_argument("--pull", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-wait", action="store_true", help="start and return without waiting for /health")

    p = sub.add_parser("up", help="build and start the stack (platform, viewer, examples, your agents) and wait for it")
    p.add_argument("--no-build", action="store_true", help="start what is built: build nothing, pull nothing")
    p.add_argument("--no-wait", action="store_true", help="start and return without waiting for /health")
    p.add_argument("--quiet", action="store_true", help="skip the summary block")

    p = sub.add_parser("down", help="stop the stack")
    p.add_argument("--volumes", "-v", action="store_true", help="also delete the data volumes (a full reset)")

    p = sub.add_parser("logs", help="the platform's logs (backend and gateway by default)")
    p.add_argument("service", nargs="*", help="compose service names")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--tail", type=int, default=200)

    p = sub.add_parser("init", help="scaffold a new agent under backend/agents from a template")
    p.add_argument("name", help="the agent id: lowercase letters, digits and hyphens (my-agent)")
    p.add_argument("--template", default="langgraph", choices=("langgraph", "container-python", "container-ts"))
    p.add_argument("--name", dest="display_name", help="the display name (default: derived from the id)")

    p = sub.add_parser("run", help="submit an agent's sample and print the run's status and URL")
    p.add_argument("--agent", required=True, help="the agent id")
    p.add_argument("--scenario", help="a scenario id (the file stem) or name; default: the first")
    p.add_argument("--wait", action="store_true", help="follow the run to a terminal state (non-zero on error)")
    p.add_argument("--approve", action="store_true", help="with --wait: approve the run once if it parks at a gate")
    p.add_argument("--timeout", type=float, default=600.0, help="with --wait: seconds to wait (default 600)")
    p.add_argument(
        "--base-url",
        help="where LibreRun answers (default: LIBRERUN_URL, else from BACKEND_PORT in .env, "
        "http://localhost:8000); behind the HTTPS edge, its https origin",
    )
    p.add_argument(
        "--cacert",
        help="a PEM CA file an https base URL is verified against, alone, as curl's --cacert "
        "(default: LIBRERUN_CA_FILE); for the edge's local CA, the root.crt copied out of librerun-edge",
    )
    p.add_argument("--email", help="login (default: LIBRERUN_EMAIL, then INITIAL_ADMIN_EMAIL from .env)")
    secret = p.add_mutually_exclusive_group()
    secret.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password as one line from stdin, or with no echo at a terminal "
        "(default: LIBRERUN_PASSWORD, then INITIAL_ADMIN_PASSWORD from .env)",
    )
    secret.add_argument(
        "--password",
        help="deprecated, goes after this release: the process list shows argv to every "
        "user of the machine — use --password-stdin or LIBRERUN_PASSWORD",
    )

    p = sub.add_parser("battery", help="the conformance battery for one agent (in-process or container)")
    p.add_argument("--agent", help="an agent id under backend/agents or backend/agents/_examples")
    p.add_argument("--url", help="a Run Contract URL to drive instead, as the backend container reaches it")
    p.add_argument("--agent-dir", help="with --url: the agent directory (agent.yaml, scenarios/)")
    p.add_argument("--scenario", help="a scenario file stem (default: the first)")
    p.add_argument("--timeout", type=float, default=600.0, help="the container battery's phase ceiling in seconds")
    p.add_argument("--json", action="store_true", help="print the report as JSON too")

    # No --password, and no abbreviations: `--password` must be refused,
    # not read as `--password-stdin` (K4b).
    p = sub.add_parser(
        "doctor",
        help="what this machine and this checkout can do; fails loudly without Docker or Podman",
        allow_abbrev=False,
    )
    p.add_argument(
        "--base-url",
        help="the backend to check and sign in to (default: LIBRERUN_URL, else from BACKEND_PORT "
        "in .env, http://localhost:8000 — and then only once the engine says this checkout's stack, "
        "or for an https URL its edge, is there)",
    )
    p.add_argument(
        "--cacert",
        help="a PEM CA file an https base URL is verified against, alone, as curl's --cacert "
        "(default: LIBRERUN_CA_FILE); for the edge's local CA, the root.crt copied out of librerun-edge",
    )
    p.add_argument("--email", help="sign in as this user (default: LIBRERUN_EMAIL)")
    p.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password as one line from stdin, or with no echo at a terminal "
        "(default: LIBRERUN_PASSWORD)",
    )

    key = sub.add_parser("key", help="agent gateway keys")
    key_sub = key.add_subparsers(dest="key_command", metavar="<command>")
    key_sub.required = True
    p = key_sub.add_parser("rotate", help="rotate an agent's gateway key (rolling: the old value works until --finish)")
    p.add_argument("agent_id")
    p.add_argument("--finish", action="store_true", help="retire the previous key: remove its line and recreate the gateway alone")
    p.add_argument("--service", help="the compose service to recreate (default: the one labelled librerun.agent_id: <id>)")
    p.add_argument("--no-up", action="store_true", help="edit .env only; do not recreate anything")
    return parser


def dispatch(args) -> int:
    root = find_root(args.root)
    if args.command == "demo":
        from ._stack import cmd_demo

        return cmd_demo(root, args)
    if args.command == "up":
        from ._stack import cmd_up

        return cmd_up(root, args)
    if args.command == "down":
        from ._stack import cmd_down

        return cmd_down(root, args)
    if args.command == "logs":
        from ._stack import cmd_logs

        return cmd_logs(root, args)
    if args.command == "init":
        from ._init import cmd_init

        return cmd_init(root, args)
    if args.command == "run":
        from ._run import cmd_run

        return cmd_run(root, args)
    if args.command == "battery":
        from ._battery import cmd_battery

        return cmd_battery(root, args)
    if args.command == "doctor":
        from ._doctor import cmd_doctor

        return cmd_doctor(root, args)
    if args.command == "key" and args.key_command == "rotate":
        from ._rotate import cmd_key_rotate

        return cmd_key_rotate(root, args)
    raise CliError(f"unknown command {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return dispatch(args)
    except CliError as exc:
        warn(str(exc))
        return exc.code
    except KeyboardInterrupt:
        warn("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
