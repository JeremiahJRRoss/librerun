"""``urllib`` with the two shapes every command needs: JSON in, JSON out,
and a status code that is never an exception.

Two more since T1 (K blueprint, edge TLS), for an ``https`` base URL:

* ``cafile`` — the one CA file an ``https`` URL is verified against,
  ``ssl.create_default_context(cafile=…)``, as curl's ``--cacert`` does:
  the edge's local root, which no system trusts by default. A missing or
  unreadable file is refused by its path before anything is sent.
* ``address`` — the address the connection goes to, which the caller has
  already vetted (``doctor`` asks the engine which address this checkout's
  edge publishes on). The request still names the URL's host: its Host
  header, its SNI and the name the certificate is verified against. It is
  never a request to ``https://<address>``, which the certificate — issued
  to a name, not an address — would fail, and it never goes through a
  proxy, which would decide for itself where the connection lands.
"""
from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from ._common import CliError


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand a 3xx back to the caller instead of following it.

    ``urllib`` follows a redirect by copying the request's headers onto a
    new request to whatever host the ``Location`` names, ``Authorization``
    included. A credential must reach the base URL the operator named and
    nothing else, so the calls that carry one follow nothing (K4b, #134).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT = urllib.request.build_opener(_NoRedirect)


class _Pinned(urllib.request.HTTPSHandler):
    """Every https connection opens ``address`` — the URL's port, the vetted
    address — and wraps it with ``server_hostname`` set to the URL's host,
    so the certificate must name that host. ``http.client`` does the wrap;
    only the socket's destination is replaced."""

    def __init__(self, address: str, context: ssl.SSLContext | None):
        super().__init__(context=context)
        self._address = address

    def https_open(self, req):
        address = self._address

        def connection(host, **kwargs):
            conn = http.client.HTTPSConnection(host, **kwargs)
            conn._create_connection = lambda _where, timeout=None, source_address=None: (
                socket.create_connection((address, conn.port), timeout, source_address)
            )
            return conn

        return self.do_open(connection, req, context=self._context)


_CONTEXTS: dict[str, ssl.SSLContext] = {}


def tls_context(cafile: str | None) -> ssl.SSLContext | None:
    """The TLS context for ``cafile``, or None for the system's own trust.

    Raises ``CliError`` naming the path when the file is missing or holds no
    certificate — refused before anything is sent."""
    if not cafile:
        return None
    path = Path(cafile).expanduser()
    key = str(path)
    if key in _CONTEXTS:
        return _CONTEXTS[key]
    if not path.is_file():
        raise CliError(f"the CA file {cafile} does not exist (--cacert, or LIBRERUN_CA_FILE); nothing was sent")
    try:
        context = ssl.create_default_context(cafile=key)
    except (ssl.SSLError, OSError, ValueError) as exc:
        raise CliError(f"the CA file {cafile} holds no usable certificate ({exc}); nothing was sent") from exc
    _CONTEXTS[key] = context
    return context


def request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body=None,
    headers: dict | None = None,
    timeout: float = 30,
    follow_redirects: bool = True,
    cafile: str | None = None,
    address: str | None = None,
) -> tuple[int, object]:
    """``(status, parsed body)``; status 0 when nothing answered at all.

    A non-JSON body comes back as ``{"raw": text}`` so a caller can print
    it; an unreachable host is ``(0, {"error": ...})`` rather than a
    traceback, because "the backend is not up" is an answer the CLI has
    to phrase, not a crash. With ``follow_redirects=False`` a 3xx is the
    answer, ``(status, {"location": ...})``, and nothing is sent onward.
    ``cafile`` and ``address`` are the module docstring's.
    """
    context = tls_context(cafile)
    if address is not None and urlsplit(url).scheme != "https":
        # An http base names its vetted address in the URL itself (K4b).
        raise ValueError(f"a pinned address is for an https URL, not {url}")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    handlers: list = []
    if address is not None:
        handlers += [_Pinned(address, context), urllib.request.ProxyHandler({})]
    elif context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    if handlers:
        if not follow_redirects:
            handlers.append(_NoRedirect)
        opener = urllib.request.build_opener(*handlers).open
    else:
        opener = urllib.request.urlopen if follow_redirects else _NO_REDIRECT.open
    try:
        with opener(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (_parse(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        if not follow_redirects and 300 <= exc.code < 400:
            return exc.code, {"location": exc.headers.get("Location")}
        return exc.code, _parse(raw)
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
        return 0, {"error": str(getattr(exc, "reason", exc))}


def _parse(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw[:2000]}


def get(url: str, **kwargs) -> tuple[int, object]:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs) -> tuple[int, object]:
    return request("POST", url, **kwargs)
