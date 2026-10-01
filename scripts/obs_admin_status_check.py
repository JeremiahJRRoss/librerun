#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Admin -> Observability names the overlay that is really loaded (S7a).

The Accept item, end to end rather than from the unit tests alone:
``backend/tests/test_obs_vendor_status.py`` proves the mapping from
``LIBRERUN_OBS_VENDOR`` to what the report says, and this proves the
running deployment agrees — the backend really received the selector,
the endpoint really serves it, and the page an operator opens names the
vendor whose sinks the contract test is about to decode.

It also checks the two things the report must NOT claim: the sink list
says it is the overlay's declaration rather than a read-back from
Vector, and the router line is reachability from the backend. A page
that quietly became a delivery receipt would be worse than no page.

    obs_admin_status_check.py <vendor>

Reads INITIAL_ADMIN_EMAIL / INITIAL_ADMIN_PASSWORD from the environment
and the base URL from LIBRERUN_BASE_URL (default http://localhost:8000).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def call(base, method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}/api/v1{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main() -> int:
    vendor = sys.argv[1]
    base = os.environ.get("LIBRERUN_BASE_URL", "http://localhost:8000").rstrip("/")

    status, body = call(
        base,
        "POST",
        "/auth/login",
        body={
            "email": os.environ["INITIAL_ADMIN_EMAIL"],
            "password": os.environ["INITIAL_ADMIN_PASSWORD"],
        },
    )
    assert status == 200, ("login", status, body)

    status, report = call(base, "GET", "/admin/otel-status", token=body["access_token"])
    assert status == 200, ("otel-status", status, report)

    overlay = report.get("overlay") or {}
    assert overlay.get("vendor") == vendor, (
        f"the admin status names {overlay.get('vendor')!r} while the stack is "
        f"running the {vendor} overlay"
    )
    assert overlay.get("active") is True, overlay
    assert overlay.get("supported") is True, overlay
    assert f"config/vector-{vendor}.yaml" in overlay.get("configs", []), overlay
    assert f"config/otel-bridge-{vendor}.yaml" in overlay.get("configs", []), overlay
    legs = {s["leg"] for s in overlay.get("sinks", [])}
    assert legs == {"logs", "traces"}, (
        f"the status shows legs {sorted(legs)} — an overlay that lost its "
        f"trace sink would read as 'active' over logs alone"
    )
    assert "not read back" in overlay.get("sinks_are", ""), (
        "the report no longer says the sink list is the overlay's "
        "declaration — it would read as a delivery receipt from the vendor"
    )

    router = report.get("vector") or {}
    assert router.get("reachable") is True, router
    assert router.get("checked", "").startswith("tcp connect"), router

    print(
        f"admin status: {overlay.get('label', vendor)} active, both legs, "
        f"router reachable ({router.get('endpoint')})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
