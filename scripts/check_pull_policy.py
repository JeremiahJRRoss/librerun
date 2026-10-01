#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""R12, first half: every service that builds is never pulled (A2, #133).

Reads compose's resolved model — ``compose config --format json``, every
file and profile merged — on stdin, and exits 1 naming each service that
has a ``build`` and no ``pull_policy: build``: with any other policy,
compose asks a registry for the image before it builds one, and a release
that is source only (L37) has no registry to ask. A service with no
``build`` (postgres, redis, the viewer, the edge) is an upstream image and
is passed by. It fails when it saw no service that builds: a model it could
not read, or one with the build lines gone, proves nothing.

    ./compose.sh --profile '*' config --format json \\
        | grep -v '^Using:' | python3 scripts/check_pull_policy.py

(``compose.sh`` names its engine on stdout before compose speaks; the
line is dropped here too, so a caller that forgets is not failed for it.)
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    text = "\n".join(
        line for line in sys.stdin.read().splitlines() if not line.startswith("Using:")
    )
    try:
        model = json.loads(text)
    except ValueError as exc:
        print(f"::error::check_pull_policy: stdin is not compose's JSON model ({exc})")
        return 1
    services = (model or {}).get("services") or {}
    building = {name: spec for name, spec in services.items() if (spec or {}).get("build")}
    if not building:
        print(
            f"::error::check_pull_policy saw no service that builds among {len(services)}: "
            "a model with no build lines proves nothing about pulling."
        )
        return 1
    for name in sorted(building):
        print(f"{name}: builds, pull_policy {building[name].get('pull_policy') or '(unset)'}")
    missing = sorted(name for name, spec in building.items() if spec.get("pull_policy") != "build")
    if missing:
        print(
            f"::error::{', '.join(missing)} build(s) from this checkout but would ask a "
            "registry first: give each `pull_policy: build` beside its `image:` (#133)."
        )
        return 1
    passed = len(services) - len(building)
    print(
        f"clean: {len(building)} service(s) build and are never pulled; "
        f"{passed} upstream image(s) passed by"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
