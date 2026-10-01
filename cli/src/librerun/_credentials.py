"""Who the CLI signs in as, and where it will send that (K4b, D30; #134).

``doctor`` and ``run`` sign in. The email comes from ``--email``, else
``LIBRERUN_EMAIL``; the password from ``--password-stdin`` — one line
from a pipe, or typed with no echo when stdin is a terminal — else
``LIBRERUN_PASSWORD``. Both variables are read from the process
environment and never from ``.env``: the file holds the deployment's
bootstrap credentials, and an operator's own password does not belong in
it. ``doctor`` takes no ``--password`` at all, because a command line is
visible to every user of the machine in the process list; ``run`` keeps
its ``--password`` for one release and says so on stderr.

Nothing is sent until the answer is known to be LibreRun's:

* ``GET {base}/api/v1/meta`` must answer 200 with ``name`` ``LibreRun``,
  so another program on the port is never handed a password;
* for ``doctor`` with no ``--base-url``, the base is derived from ``.env``
  as always, and the engine is asked first whether this checkout's
  ``backend`` container is running there (``compose ps``: this project,
  this working directory, publishing on the address and port the base
  names) — another checkout's stack on the same port answers the meta
  check as LibreRun too (#134) — and the credential then goes to the very
  address the engine vouched for;
* the sign-in, ``/api/v1/auth/me`` and the meta check itself follow no
  redirect, because ``urllib`` forwards ``Authorization`` to any host a
  3xx names.

Behind the HTTPS edge (T1) the base is ``https://<host>:<port>``: named
with ``--base-url``, or ``LIBRERUN_URL`` in the process environment, never
``.env``. An ``https`` base is verified against ``--cacert``, else
``LIBRERUN_CA_FILE``, else the system's trust. For ``doctor`` with no
``--base-url``, the engine is asked about this checkout's ``edge`` instead
of its ``backend`` — on the same three conditions — because the https port
is the edge's; the credential then goes to the address the engine vouched
for, under the URL's host name, which the certificate names. A certificate
does not stand in for the question: two clones with one directory name
share the edge's data volume, so each other's local CA (#134).
"""
from __future__ import annotations

import getpass
import ipaddress
import json
import os
import re
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

from . import _compose, _http
from ._common import CliError

EMAIL_VARIABLE = "LIBRERUN_EMAIL"
PASSWORD_VARIABLE = "LIBRERUN_PASSWORD"
# T1: where LibreRun answers (the edge's https origin serves the UI and
# /api/v1 alike), and the CA file an https base is verified against. Both
# are the operator's shell's, like the two above, and never .env lines.
URL_VARIABLE = "LIBRERUN_URL"
CA_FILE_VARIABLE = "LIBRERUN_CA_FILE"
PRODUCT_NAME = "LibreRun"

HOW_TO_SIGN_IN = (
    f"pass --email <address> --password-stdin (one line on stdin, or typed "
    f"with no echo at a terminal), or export {EMAIL_VARIABLE} and "
    f"{PASSWORD_VARIABLE} in this shell"
)


class NothingSent(CliError):
    """The CLI declined to send a credential, and says why."""


def read_password_stdin(stream=None) -> str:
    """One line from a pipe, or ``getpass`` with no echo at a terminal."""
    stream = sys.stdin if stream is None else stream
    if stream.isatty():
        password = getpass.getpass("Password: ")
    else:
        password = stream.readline().rstrip("\r\n")
    if not password:
        raise CliError(
            "--password-stdin read an empty password: send it as one line "
            "(printf '%s\\n' \"$PASSWORD\" | librerun ...) or type it at the prompt"
        )
    return password


def from_args(args, environ=None) -> tuple[str | None, str | None]:
    """``(email, password)`` from the command line, else the environment.

    Either may be None; the caller decides what a missing one means.
    ``--password-stdin`` reads stdin here, once, before anything is sent.
    """
    environ = os.environ if environ is None else environ
    email = (getattr(args, "email", None) or environ.get(EMAIL_VARIABLE) or "").strip() or None
    if getattr(args, "password_stdin", False):
        password = read_password_stdin()
    else:
        password = getattr(args, "password", None) or environ.get(PASSWORD_VARIABLE) or None
    return email, password


def ca_file(args, environ=None) -> str | None:
    """``--cacert``, else ``LIBRERUN_CA_FILE`` from the process environment;
    refused by its path, before anything is sent, when it is missing."""
    environ = os.environ if environ is None else environ
    path = (getattr(args, "cacert", None) or environ.get(CA_FILE_VARIABLE) or "").strip() or None
    _http.tls_context(path)
    return path


def tls_hint(base: str, error) -> str:
    """What to do when an https base's certificate did not verify."""
    if urlsplit(base).scheme == "https" and "CERTIFICATE" in str(error).upper():
        return (
            f" — the certificate did not verify: pass the CA that issued it with --cacert "
            f"(or {CA_FILE_VARIABLE}); for the edge's local CA that is the root.crt copied out of "
            f"librerun-edge (docs/platform/Install.md, \"HTTPS at the edge\")"
        )
    return ""


# ---------------------------------------------------------------------------
# is the answer LibreRun's?
# ---------------------------------------------------------------------------


def check_meta(base: str, *, cafile: str | None = None, address: str | None = None) -> dict:
    """``GET /api/v1/meta``, which must answer as LibreRun, unredirected."""
    status, meta = _http.get(
        f"{base}/api/v1/meta", timeout=10, follow_redirects=False, cafile=cafile, address=address
    )
    if status == 200 and isinstance(meta, dict) and meta.get("name") == PRODUCT_NAME:
        return meta
    if status == 0:
        error = (meta or {}).get("error")
        why = f"nothing answered at {base} ({error}){tls_hint(base, error)}"
    elif 300 <= status < 400:
        why = (
            f"{base}/api/v1/meta redirected ({status} to {(meta or {}).get('location')}); "
            f"a credential never follows a redirect — name the final address with --base-url"
        )
    elif status == 200:
        name = meta.get("name") if isinstance(meta, dict) else None
        why = f"{base}/api/v1/meta answered as {name!r}, not as {PRODUCT_NAME}"
    else:
        why = f"{base}/api/v1/meta answered {status}, not as {PRODUCT_NAME}"
    raise NothingSent(f"{why}; nothing was sent")


# ---------------------------------------------------------------------------
# is it this checkout's? (#134: `compose ps` before a port is trusted)
# ---------------------------------------------------------------------------


def _ps_records(stdout: str) -> list[dict]:
    """The container records of ``compose ps --format json``, in each shape
    the engines print: Docker Compose's JSON lines (2.21+) or one array
    (earlier), podman-compose's array across many lines. ``compose.sh``
    prints a ``Using:`` line of its own first, and an empty project prints
    nothing at all. A value is decoded wherever a line opens one, and a
    line inside a value already decoded is not a second one."""
    decoder = json.JSONDecoder()
    records: list[dict] = []
    consumed = 0
    for match in re.finditer(r"(?m)^[ \t]*([\[{])", stdout):
        start = match.start(1)
        if start < consumed:
            continue
        try:
            value, consumed = decoder.raw_decode(stdout, start)
        except ValueError:
            continue
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict):
                records.append(item)
    return records


def _labels(record: dict) -> dict:
    """Docker writes the labels as one ``k=v,k=v`` string, Podman as an
    object. A value may itself hold a comma (a list of config files), so
    a piece with no ``=`` continues the previous value."""
    raw = record.get("Labels")
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    labels: dict[str, str] = {}
    last = None
    for piece in str(raw or "").split(","):
        if "=" in piece:
            last, value = piece.split("=", 1)
            labels[last] = value
        elif last is not None:
            labels[last] += "," + piece
    return labels


def _publishers(record: dict) -> list[tuple[str, int]] | None:
    """``(host address, host port)`` for each port the container publishes
    — an empty address for every address — or None when the record does
    not say (then neither is a condition)."""
    found: list[tuple[str, int]] = []
    said = False
    publishers = record.get("Publishers")  # Docker Compose
    if isinstance(publishers, list):
        said = True
        for publisher in publishers:
            if isinstance(publisher, dict):
                found.append((str(publisher.get("URL") or ""), _port(publisher.get("PublishedPort"))))
    bindings = record.get("Ports")  # Podman (Docker's is a display string)
    if isinstance(bindings, list):
        said = True
        for binding in bindings:
            if isinstance(binding, dict):
                found.append((str(binding.get("host_ip") or ""), _port(binding.get("host_port"))))
    return [(host, port) for host, port in found if port] if said else None


def _addresses(host: str) -> list[str]:
    """The addresses a request to ``host`` may reach, in resolution order."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        if address not in addresses:
            addresses.append(address)
    return addresses


def _is_local(ip) -> bool:
    """An address of this machine: loopback, or one a socket can bind."""
    if ip.is_loopback:
        return True
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.bind((str(ip), 0))
        return True
    except OSError:
        return False


def _serves(published: str, address: str) -> bool:
    """Whether a port published on ``published`` answers at ``address``:
    the same address, or a wildcard and one of this machine's addresses —
    ``0.0.0.0`` for IPv4, ``::`` for IPv6, Podman's empty one for both."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    host = published.strip("[]")
    if host in ("", "*"):
        return _is_local(ip)
    try:
        bound = ipaddress.ip_address(host)
    except ValueError:
        return False
    if bound.is_unspecified:
        return bound.version == ip.version and _is_local(ip)
    return bound == ip


def _port(value) -> int:
    """A published port, or 0 for none (an exposed port Docker lists with
    no host side) and for anything that is not a port number."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _vetted(root: Path, base: str, wanted: str, profiles: tuple[str, ...]) -> tuple[str | None, str]:
    """The address at which the engine says this checkout's ``wanted``
    service publishes ``base``'s port, and why — None when it does not, and
    ``""`` when its record names no published port (an engine that does not
    say), which leaves the port and the address unconditioned.

    ``compose ps`` lists this compose project's containers. Two clones
    with the same directory name are one project name to compose, so the
    ``working_dir`` label must be this checkout, and the container must
    publish the port ``base`` names on an address ``base``'s host reaches —
    an ``.env`` edited since ``up`` can name a port, or a host, that
    another stack now holds.
    """
    try:
        result = _compose.run(
            root, "ps", "--format", "json", profiles=profiles, capture=True, check=False, timeout=60
        )
    except CliError as exc:
        return None, f"`compose ps` could not run ({exc}): which stack answers there is unknown"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return None, (
            f"`compose ps` failed (exit {result.returncode}"
            + (f": {detail[-1]}" if detail else "")
            + "): which stack answers there is unknown"
        )
    parts = urlsplit(base)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    candidates = _addresses(parts.hostname or "")
    here = root.resolve()
    seen: list[str] = []
    for record in _ps_records(result.stdout or ""):
        labels = _labels(record)
        service = record.get("Service") or labels.get("com.docker.compose.service")
        if service != wanted or str(record.get("State", "")).lower() != "running":
            continue
        working_dir = labels.get("com.docker.compose.project.working_dir")
        if working_dir and Path(working_dir).resolve() != here:
            seen.append(f"{'an' if wanted[0] in 'aeiou' else 'a'} {wanted} from {working_dir}")
            continue
        publishers = _publishers(record)
        if publishers is None:
            return "", f"this checkout's {wanted} container is running (the engine names no published port)"
        on_port = [host for host, published in publishers if published == port]
        if not on_port:
            ports = sorted({published for _, published in publishers})
            seen.append(f"this checkout's {wanted} on port(s) {ports or 'none'}, not {port}")
            continue
        served = [address for address in candidates if any(_serves(host, address) for host in on_port)]
        if not served:
            hosts = ", ".join(sorted({host or "*" for host in on_port}))
            seen.append(f"this checkout's {wanted} publishing port {port} on {hosts}, which {parts.hostname} does not reach")
            continue
        address = next((a for a in served if ipaddress.ip_address(a).version == 4), served[0])
        netloc = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
        return address, f"this checkout's {wanted} container publishes {netloc}"
    if seen:
        return None, f"`compose ps` shows {'; '.join(seen)}"
    return None, f"`compose ps` shows no running {wanted} container for this checkout"


def this_checkouts_backend(root: Path, base: str) -> tuple[str | None, str]:
    """The base URL at which the engine says this checkout's backend
    answers, or None, and why (``_vetted``'s three conditions). The URL
    returned names the address the engine vouched for, so the credential
    goes nowhere else. For an http base; an https one asks for the edge."""
    address, why = _vetted(root, base, "backend", ("app",))
    if not address:
        return (base if address == "" else None), why
    parts = urlsplit(base)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    netloc = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
    return f"{parts.scheme}://{netloc}{parts.path}".rstrip("/"), why


def this_checkouts_edge(root: Path, base: str) -> tuple[bool, str | None, str]:
    """For an https base (T1): whether the engine says this checkout's HTTPS
    edge publishes its port, the address to connect to, and why.

    The same three conditions as the backend's — running, this checkout's
    ``working_dir``, publishing the base's port on an address the base's
    host reaches — asked of the ``edge``, with ``tls`` beside ``app`` so an
    engine that lists only the active profiles' services lists it. The
    address is None when the engine names no published port; the caller
    then connects by name. Otherwise the credential goes to that address
    and to no other, under the URL's host name (``_http``'s ``address``)."""
    address, why = _vetted(root, base, "edge", ("app", "tls"))
    if address is None:
        return False, None, why
    return True, (address or None), why


# ---------------------------------------------------------------------------
# the three calls that carry a credential
# ---------------------------------------------------------------------------


def sign_in(
    base: str, email: str, password: str, *, cafile: str | None = None, address: str | None = None
) -> str:
    """The meta check, then the sign-in; returns the bearer token. With
    ``address`` both go to that address, under ``base``'s host name."""
    check_meta(base, cafile=cafile, address=address)
    status, body = _http.post(
        f"{base}/api/v1/auth/login",
        body={"email": email, "password": password},
        follow_redirects=False,
        cafile=cafile,
        address=address,
    )
    if 300 <= status < 400:
        raise CliError(
            f"the sign-in at {base} redirected ({status} to {(body or {}).get('location')}), "
            f"and a credential never follows a redirect"
        )
    if status == 0:
        error = (body or {}).get("error")
        raise CliError(f"the backend at {base} did not answer: {error}{tls_hint(base, error)}. Is the stack up? (`librerun up`)")
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        raise CliError(f"login failed ({status}) for {email}: {_short(body)}")
    return body["access_token"]


def whoami(base: str, token: str, *, cafile: str | None = None, address: str | None = None) -> dict:
    """``GET /api/v1/auth/me`` with the token, unredirected."""
    status, profile = _http.get(
        f"{base}/api/v1/auth/me", token=token, follow_redirects=False, cafile=cafile, address=address
    )
    if 300 <= status < 400:
        raise CliError(
            f"{base}/api/v1/auth/me redirected ({status} to {(profile or {}).get('location')}); "
            f"the token was not sent onward"
        )
    if status != 200 or not isinstance(profile, dict):
        raise CliError(f"/api/v1/auth/me answered {status}: {_short(profile)}")
    return profile


def _short(body) -> str:
    text = str(body)
    return text if len(text) <= 300 else text[:300] + "…"
