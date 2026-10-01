"""What the HTTPS edge serves and needs, for Application Settings (K blueprint
T2; decisions L42, L43, D44 refined).

The backend reads the edge through ``edge-control``'s ``GET /status`` —
public material alone — and never through Caddy's admin socket, which it
cannot reach: the control volume is the edge's and ``edge-control``'s, and
the backend mounts neither of the edge's volumes. It adds what only it
knows:

- ``edge_off``: ``edge-control`` did not answer, so the ``tls`` profile is
  off and this deployment is plain HTTP;
- ``root_changed``: the root the edge issues from is not the one a platform
  admin last acknowledged — the ``app_settings`` row ``tls.last_root``,
  which no registry entry exposes and :func:`acknowledge_root` alone writes.
  With no row there is only ``trust_root``, so a restore — the database
  back, the edge's volumes empty (L42) — says so until the admin loads the
  CA again or acknowledges the new root.

The four certificate changes are not the backend's: the edge sends them to
``edge-control`` after asking :func:`authorized_change` here, with the
request's headers and never its body, so a key never enters this process.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting, User

# edge-control on the `edge` network (compose.yaml), which the backend
# joins. Never through a proxy: the name is the compose network's.
EDGE_CONTROL_URL = "http://edge-control:8081"
TIMEOUT = httpx.Timeout(10.0, connect=2.0)

LAST_ROOT = "tls.last_root"
SURFACE = "tls"

# The four changes the edge sends to edge-control after forward_auth, by
# method and path — the same table as ``app.edge_control.edge.CHANGE_ROUTES``
# (a test holds the two equal; the backend never imports that package).
CHANGE_ROUTES = {
    ("PUT", "/api/v1/admin/tls/ca"): "load_ca",
    ("PUT", "/api/v1/admin/tls/files"): "use_files",
    ("PUT", "/api/v1/admin/tls/acme"): "use_acme",
    ("DELETE", "/api/v1/admin/tls/choice"): "use_environment",
}

# The status's needs, each with the action the panel offers for it.
NEEDS = (
    "edge_off",
    "edge_restart",
    "trust_root",
    "root_changed",
    "files_ending",
    "ca_ending",
    "acme_requirements",
)


async def read_status() -> dict[str, Any] | None:
    """``edge-control``'s ``GET /status``, or None when it does not answer."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as client:
            response = await client.get(f"{EDGE_CONTROL_URL}/status")
        if response.status_code != 200:
            return None
        answer = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return answer if isinstance(answer, dict) else None


async def last_root(db: AsyncSession) -> str | None:
    row = await db.get(AppSetting, LAST_ROOT)
    value = row.value if row is not None else None
    return value if isinstance(value, str) else None


def edge_off() -> dict[str, Any]:
    return {
        "edge": "off",
        "site": [],
        "issuer": None,
        "root": None,
        "leaf": None,
        "source": None,
        "choice": None,
        "environment": None,
        "why": (
            "The HTTPS edge is not running: this deployment serves plain HTTP. "
            "docs/platform/Install.md, “HTTPS at the edge”, says how to turn it on."
        ),
        "needs": ["edge_off"],
        "acknowledged_root": None,
    }


async def status(db: AsyncSession, viewer: User, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the edge serves and needs, with ``edge_off`` and ``root_changed``
    added and the chooser named. Public material alone: ``edge-control``
    returns no key, and the root's PEM stays out of this answer
    (``root.pem`` serves it)."""
    raw = raw if raw is not None else await read_status()
    if raw is None:
        return edge_off()
    acknowledged = await last_root(db)
    needs = [need for need in raw.get("needs") or [] if need in NEEDS]
    root = raw.get("root")
    if isinstance(root, dict):
        root = {key: value for key, value in root.items() if key != "pem"}
        if acknowledged and acknowledged == root.get("sha256"):
            needs = [need for need in needs if need != "trust_root"]
        elif acknowledged:
            needs.append("root_changed")
    else:
        root = None
    choice = raw.get("choice")
    if isinstance(choice, dict):
        choice = {**choice, "by_email": await _email(db, viewer, choice.get("by"))}
    else:
        choice = None
    return {
        "edge": "on",
        "site": raw.get("site") or [],
        "issuer": raw.get("issuer"),
        "root": root,
        "leaf": raw.get("leaf"),
        "source": raw.get("source"),
        "choice": choice,
        "environment": raw.get("environment"),
        "why": raw.get("why"),
        "needs": needs,
        "acknowledged_root": acknowledged,
    }


async def _email(db: AsyncSession, viewer: User, by: Any) -> str | None:
    """Who chose, by the id ``edge-control`` recorded — looked up in the
    viewer's own tenant, as every query is."""
    try:
        user_id = UUID(str(by))
    except (ValueError, TypeError):
        return None
    return (
        await db.execute(select(User.email).where(User.id == user_id, User.tenant_id == viewer.tenant_id))
    ).scalar_one_or_none()


async def root_pem() -> str | None:
    """The root the edge issues from, as PEM — public — or None when it
    issues from no CA of its own (ACME, the admin's files) or is off."""
    raw = await read_status()
    root = (raw or {}).get("root")
    pem = root.get("pem") if isinstance(root, dict) else None
    return pem if isinstance(pem, str) and pem.startswith("-----BEGIN CERTIFICATE-----") else None


async def acknowledge_root(db: AsyncSession, user: User) -> str | None:
    """Record the root ``/status`` names as the one browsers were told to
    trust. The one writer of ``tls.last_root``, by the model: the settings
    service refuses a key with no registry entry, and this one has none."""
    raw = await read_status()
    root = (raw or {}).get("root")
    sha = root.get("sha256") if isinstance(root, dict) else None
    if not isinstance(sha, str) or len(sha) != 64:
        return None
    row = await db.get(AppSetting, LAST_ROOT)
    if row is None:
        db.add(AppSetting(key=LAST_ROOT, value=sha, updated_by=user.id))
    else:
        row.value = sha
        row.updated_by = user.id
    await db.flush()
    return sha


def authorized_change(method: str | None, uri: str | None) -> tuple[str, str] | None:
    """The change the edge is asking about — its ``X-Forwarded-Method`` and
    ``X-Forwarded-Uri`` — as ``(route, name)``, e.g. ``("PUT
    /api/v1/admin/tls/ca", "load_ca")``, or None for anything that is not
    one of the four. A path is read as the edge and edge-control read it:
    unquoted and lower-cased."""
    if not method or not uri:
        return None
    key = (method.upper(), unquote(urlsplit(uri).path).lower())
    name = CHANGE_ROUTES.get(key)
    return (f"{key[0]} {key[1]}", name) if name else None
