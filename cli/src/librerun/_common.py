"""What every command shares: the checkout, the error type, the output."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# The manifest id charset (``backend/app/agents/manifest.py``), repeated
# rather than imported: the CLI runs where the chassis is not installed.
AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
AGENT_ID_MAX = 50

# The files that make a directory a LibreRun checkout for this CLI: the
# compose file it wraps and the wrapper that derives agent-keys.env.
ROOT_MARKERS = ("compose.yaml", "compose.sh")


class CliError(Exception):
    """A failure the user can act on. The message is printed on its own
    line prefixed ``librerun:``; ``code`` is the exit status."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def find_root(explicit: str | None = None) -> Path:
    """The LibreRun checkout to operate on.

    ``--root`` first, then ``LIBRERUN_ROOT``, then the nearest ancestor of
    the working directory (itself included) carrying ``compose.yaml`` and
    ``compose.sh``. A CLI installed with pipx has no checkout of its own,
    so it must be told — or find — the one it is meant to drive.
    """
    candidate = explicit or os.environ.get("LIBRERUN_ROOT")
    if candidate:
        root = Path(candidate).expanduser().resolve()
        missing = [m for m in ROOT_MARKERS if not (root / m).is_file()]
        if missing:
            raise CliError(
                f"{root} is not a LibreRun checkout: {', '.join(missing)} missing"
            )
        return root
    here = Path.cwd().resolve()
    for directory in (here, *here.parents):
        if all((directory / m).is_file() for m in ROOT_MARKERS):
            return directory
    raise CliError(
        f"not inside a LibreRun checkout: no compose.yaml and compose.sh in "
        f"{here} or above it. cd into the clone, or pass --root <path>."
    )


def say(message: str = "") -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"librerun: {message}", file=sys.stderr, flush=True)


def validate_agent_id(agent_id: str) -> str:
    """The manifest's rule for an id, applied before anything is written."""
    if not AGENT_ID.match(agent_id) or len(agent_id) > AGENT_ID_MAX:
        raise CliError(
            f"{agent_id!r} is not a valid agent id: lowercase letters, digits "
            f"and hyphens, starting with a letter or digit, at most "
            f"{AGENT_ID_MAX} characters (the manifest's own rule)"
        )
    if agent_id.endswith("-previous"):
        # The gateway refuses to provision such an id from the environment:
        # its key variable would be spelled like another agent's rotation
        # variable (services/gateway/gateway/keys.py).
        raise CliError(
            f"{agent_id!r} cannot be used: an id ending in '-previous' would "
            f"name a key variable the gateway reads as another agent's "
            f"outgoing key. Pick another name."
        )
    return agent_id
