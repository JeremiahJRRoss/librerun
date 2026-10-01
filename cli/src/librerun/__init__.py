"""The LibreRun CLI (blueprint S6).

Standard library only (default D5). Every command operates on a LibreRun
checkout: the repository root is found by walking up from the working
directory to ``compose.yaml`` and ``compose.sh``, or named with
``--root``. Nothing here imports the chassis — the CLI shells out to
``compose.sh`` and talks to a running stack over HTTP, so it installs on
a machine with Docker and nothing else.
"""

__version__ = "1.1.0b1"
