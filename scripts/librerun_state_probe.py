#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Probe the demo agent's admin configuration through the API (blueprint S2, K5b).

Stdlib-only, like ``librerun_smoke.py`` — the smoke workflow drives it to
prove that an admin's configuration is data in the database, and so
survives ``up -d --force-recreate`` (gap H5):

* ``edit-setting KEY VALUE`` then ``assert-setting KEY VALUE`` — a setting
  saved through ``PUT /agents/{id}/config/settings`` is this tenant's
  value (``overridden``) and reads back after the recreate. ``VALUE`` is
  parsed as JSON — ``7``, ``true``, ``["a"]`` — and kept as a string when
  it does not parse, so ``basic`` needs no quoting.
* ``edit-step --temperature T`` then ``assert-step`` — an admin edit made
  through ``PUT /agents/{id}/config/steps`` is still there after the
  recreate.

Usage:
    librerun_state_probe.py --base-url URL --admin-email E --admin-password P
        [--agent vita-v1] {edit-setting KEY VALUE | assert-setting KEY VALUE |
        edit-step [--temperature T] [--record FILE] | assert-step [--record FILE]}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from librerun_smoke import SmokeFailure, login, wait_for_health  # noqa: E402


def _request(method: str, url: str, token: str, body=None) -> tuple[int, dict | list | None]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"detail": raw.decode(errors="replace")}


def _config(base: str, token: str, agent: str) -> dict:
    status, body = _request("GET", f"{base}/api/v1/agents/{agent}/config", token)
    if status != 200 or not isinstance(body, dict):
        raise SmokeFailure(f"GET config failed ({status}): {body}")
    return body


def _setting_value(raw: str):
    """The value as JSON when it parses — ``7`` is an integer, ``true`` a
    boolean — else the string itself, so an enum value needs no quotes."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--admin-email", required=True)
    p.add_argument("--admin-password", required=True)
    p.add_argument("--agent", default="vita-v1")
    sub = p.add_subparsers(dest="cmd", required=True)
    s0 = sub.add_parser("edit-setting")
    s0.add_argument("key")
    s0.add_argument("value")
    s1 = sub.add_parser("assert-setting")
    s1.add_argument("key")
    s1.add_argument("value")
    s2 = sub.add_parser("edit-step")
    s2.add_argument("--temperature", type=float, default=0.33)
    s2.add_argument("--record", default="state-probe.json")
    s3 = sub.add_parser("assert-step")
    s3.add_argument("--record", default="state-probe.json")
    args = p.parse_args()

    base = args.base_url.rstrip("/")
    wait_for_health(base)
    token = login(base, args.admin_email, args.admin_password)

    if args.cmd == "edit-setting":
        value = _setting_value(args.value)
        status, body = _request(
            "PUT",
            f"{base}/api/v1/agents/{args.agent}/config/settings",
            token,
            [{"key": args.key, "value": value}],
        )
        if status != 204:
            raise SmokeFailure(f"PUT settings failed ({status}): {body}")
        print(f"edited setting {args.key}={value!r}")
        return 0

    if args.cmd == "assert-setting":
        cfg = _config(base, token, args.agent)
        # K5a: `settings` is a list of {key, label, type, value, default,
        # overridden}, this tenant's effective values.
        entries = [s for s in cfg.get("settings") or [] if isinstance(s, dict)]
        match = [s for s in entries if s.get("key") == args.key]
        if not match:
            raise SmokeFailure(
                f"setting {args.key!r} is not served for {args.agent}: "
                f"{sorted(str(s.get('key')) for s in entries)}"
            )
        expected = _setting_value(args.value)
        got = match[0].get("value")
        # K5b: the value must be this tenant's row, not a default that
        # happens to match, and the deprecated per-agent path is gone.
        if got != expected or match[0].get("overridden") is not True:
            raise SmokeFailure(
                f"setting {args.key}={got!r} (overridden={match[0].get('overridden')!r}), "
                f"expected {expected!r} as this tenant's value — the tenant's row in "
                f"agent_settings is missing or was not read back from the database"
            )
        if cfg.get("meta", {}).get("deprecated"):
            raise SmokeFailure(
                f"{args.agent} is still served through the deprecated settings path"
            )
        print(f"setting ok: {args.key}={got!r}, this tenant's value")
        return 0

    if args.cmd == "edit-step":
        cfg = _config(base, token, args.agent)
        steps = cfg.get("steps") or []
        if not steps:
            raise SmokeFailure("agent exposes no steps to edit")
        step_id = steps[0]["step_id"]
        status, body = _request(
            "PUT",
            f"{base}/api/v1/agents/{args.agent}/config/steps",
            token,
            [{"step_id": step_id, "temperature": args.temperature}],
        )
        if status != 204:
            raise SmokeFailure(f"PUT steps failed ({status}): {body}")
        with open(args.record, "w") as f:
            json.dump({"step_id": step_id, "temperature": args.temperature}, f)
        print(f"edited step {step_id!r}: temperature={args.temperature}")
        return 0

    if args.cmd == "assert-step":
        with open(args.record) as f:
            rec = json.load(f)
        cfg = _config(base, token, args.agent)
        steps = {s["step_id"]: s for s in cfg.get("steps") or []}
        got = steps.get(rec["step_id"], {}).get("temperature")
        if got != rec["temperature"]:
            raise SmokeFailure(
                f"step {rec['step_id']!r} temperature={got!r} after recreate, expected "
                f"{rec['temperature']!r} — the admin edit did not survive"
            )
        print(f"edit survived: step {rec['step_id']!r} temperature={got}")
        return 0
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SmokeFailure as e:
        print(f"\nSTATE PROBE FAILED: {e}", file=sys.stderr)
        sys.exit(1)
