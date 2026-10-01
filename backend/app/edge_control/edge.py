"""What the edge serves and needs, and the changes a platform admin makes to
it (K blueprint T2; decisions L42, L43, D44 refined).

Run by the ``edge-control`` service alone, over Caddy's admin API on the
Unix socket in the control volume. The backend never imports this package:
a key a platform admin uploads arrives in a request body to ``edge-control``
and nowhere else, so nothing in the backend's process — where an in-process
agent runs — ever holds one (L42).

The control volume (``/control``) holds:

- ``admin.sock``: Caddy's admin API, never on TCP;
- ``pki.global.caddy``: every CA the edge may name, in one ``pki`` block —
  Caddy keeps the last ``pki`` option it parses — the environment's
  ``env-<12 hex>`` entry (config/edge-start.sh's) and one loaded here,
  ``loaded-<12 hex>``;
- ``env.caddy``: the environment's selection, written at every start;
- ``tls.caddy``: the selection in effect, ``import /control/env.caddy`` or
  the admin's choice;
- ``choice``: absent, or what was chosen here, by whom and when;
- the files a choice names: ``ca-<12 hex>.crt`` and ``.key`` (a loaded CA),
  ``site-<12 hex>.crt`` and ``.key`` (a certificate and key); each key 0600.

Every change writes its new files beside the old ones, reloads, and only
once Caddy has accepted the reload deletes the files the previous choice
named: a key is deleted, never moved, and never before the choice that
replaces it is serving. A refused reload — or an empty reply — puts the
previous control files back, which still name files that exist.

Nothing unchecked reaches ``/load``: Caddy 2.11.4 panics on a CA root it
cannot load. A certificate is read with ``cryptography.x509`` and a pair is
matched by the standard library's ``ssl`` (``load_cert_chain``), so no
asymmetric primitive enters ``backend/app`` (K7's
``test_no_rsa_code_in_the_backend``). Nothing this module returns or logs
carries a key: the status is public material alone.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import ssl
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ExtensionOID, PublicKeyAlgorithmOID

# A body larger than this is refused unread (413).
BODY_LIMIT = 64 * 1024

# When the panel says a certificate or a CA is ending.
FILES_ENDING = timedelta(days=30)
CA_ENDING = timedelta(days=90)

# What Caddy (Go's crypto/x509) loads and serves: a key it cannot parse
# makes it panic rather than refuse, so these are checked here first.
SUPPORTED_CURVES = frozenset({"secp256r1", "secp384r1", "secp521r1"})
MIN_RSA_BITS = 2048
KEY_BLOCKS = frozenset({"PRIVATE KEY", "EC PRIVATE KEY", "RSA PRIVATE KEY"})

# The four changes, by method and path, which the edge sends here after
# forward_auth. The backend's authorize route keeps the same table
# (``app.services.edge_tls.CHANGE_ROUTES``) and a test holds the two equal.
CHANGE_ROUTES = {
    ("PUT", "/api/v1/admin/tls/ca"): "load_ca",
    ("PUT", "/api/v1/admin/tls/files"): "use_files",
    ("PUT", "/api/v1/admin/tls/acme"): "use_acme",
    ("DELETE", "/api/v1/admin/tls/choice"): "use_environment",
}

PKI = "pki.global.caddy"
TLS = "tls.caddy"
ENV = "env.caddy"
CHOICE = "choice"

# The header the three writers share (config/edge-start.sh writes the same
# three lines), so a file one of them wrote reads the same to the other.
PKI_HEADER = (
    "# Every CA the edge may name, in one pki block (config/Caddyfile says why).\n"
    "# The env- entry is config/edge-start.sh's, from LIBRERUN_TLS_CA; a loaded-\n"
    "# entry is edge-control's, from Application Settings.\n"
)

# One entry of the pki block, exactly as both writers lay it out.
_ENTRY = re.compile(
    r"^\tca ((?:env|loaded)-[0-9a-f]{12}) \{\n"
    r"\t\troot \{\n"
    r"\t\t\tcert (\S+)\n"
    r"\t\t\tkey (\S+)\n"
    r"\t\t\}\n"
    r"\t\}$",
    re.M,
)
_PEM = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----\r?\n.*?\r?\n-----END \1-----", re.S)
_ISSUER = re.compile(r"^\s*ca (loaded-[0-9a-f]{12})\s*$", re.M)
_FILES = re.compile(r"^tls (\S+) (\S+)$")
# An ACME account e-mail, strictly: it is written into the Caddyfile the
# edge parses, so nothing that could end a token or a block gets through.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+")


class Refused(Exception):
    """A change this service will not make: 422, naming the check that
    failed. The detail says why in words and never carries what was sent."""

    def __init__(self, check: str, detail: str) -> None:
        super().__init__(detail)
        self.check = check
        self.detail = detail


class EdgeUnavailable(Exception):
    """The admin socket answered nothing — or an empty reply, which is what
    Caddy gives when a configuration makes it panic. 503; the status then
    says ``edge_restart``."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Certificates and keys: checked before anything reaches Caddy
# ---------------------------------------------------------------------------


def _block_kinds(text: str) -> list[str]:
    return [match.group(1) for match in _PEM.finditer(text)]


def read_certificates(pem: str, what: str) -> list[x509.Certificate]:
    """One or more PEM ``CERTIFICATE`` blocks, and nothing else."""
    kinds = _block_kinds(pem)
    if not kinds or any(kind != "CERTIFICATE" for kind in kinds):
        raise Refused("certificate", f"The {what} is not one or more PEM CERTIFICATE blocks.")
    try:
        return x509.load_pem_x509_certificates(pem.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise Refused("certificate", f"The {what} cannot be read as an X.509 certificate.") from None


def key_block(pem: str) -> str:
    """The one unencrypted PEM private key in ``pem``, as Caddy loads it."""
    blocks = list(_PEM.finditer(pem))
    if len(blocks) != 1 or blocks[0].group(1) not in KEY_BLOCKS or "ENCRYPTED" in blocks[0].group(0):
        raise Refused(
            "key",
            "The key is not one unencrypted PEM private key "
            "(PRIVATE KEY, EC PRIVATE KEY or RSA PRIVATE KEY).",
        )
    return blocks[0].group(0).replace("\r\n", "\n") + "\n"


def check_dates(cert: x509.Certificate, what: str, now: datetime) -> None:
    if cert.not_valid_after_utc <= now:
        raise Refused("expired", f"The {what} ended on {cert.not_valid_after_utc.date().isoformat()}.")
    if cert.not_valid_before_utc > now:
        raise Refused("not_yet_valid", f"The {what} starts on {cert.not_valid_before_utc.date().isoformat()}.")


def check_key_type(cert: x509.Certificate, what: str) -> None:
    """ECDSA on P-256, P-384 or P-521, RSA of 2048 bits or more, or
    Ed25519 — read from the public key algorithm's OID."""
    oid = cert.public_key_algorithm_oid
    if oid == PublicKeyAlgorithmOID.EC_PUBLIC_KEY:
        curve = cert.public_key().curve.name
        if curve not in SUPPORTED_CURVES:
            raise Refused("key_type", f"The {what}'s key is on {curve}; the edge takes P-256, P-384 or P-521.")
    elif oid == PublicKeyAlgorithmOID.RSAES_PKCS1_v1_5:
        bits = cert.public_key().key_size
        if bits < MIN_RSA_BITS:
            raise Refused("key_type", f"The {what}'s RSA key is {bits} bits; the edge takes {MIN_RSA_BITS} or more.")
    elif oid != PublicKeyAlgorithmOID.ED25519:
        raise Refused("key_type", f"The {what}'s key type is not one the edge takes (ECDSA, RSA or Ed25519).")


def check_ca(cert: x509.Certificate, now: datetime) -> None:
    """A CA the edge can issue from: basicConstraints CA:TRUE, keyCertSign,
    in date, and a key type Caddy loads."""
    try:
        constraints = cert.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS).value
    except x509.ExtensionNotFound:
        constraints = None
    if constraints is None or not constraints.ca:
        raise Refused("basicConstraints", "The certificate is not a CA: its basicConstraints do not say CA:TRUE.")
    if constraints.path_length == 0:
        raise Refused(
            "basicConstraints",
            "The CA's basicConstraints set pathLenConstraint 0, and the edge issues "
            "through an intermediate it makes under the root.",
        )
    try:
        usage = cert.extensions.get_extension_for_oid(ExtensionOID.KEY_USAGE).value
    except x509.ExtensionNotFound:
        usage = None
    if usage is None or not usage.key_cert_sign:
        raise Refused("keyCertSign", "The CA's keyUsage does not include keyCertSign.")
    check_dates(cert, "CA certificate", now)
    check_key_type(cert, "CA certificate")


def check_pair(cert_path: Path, key_path: Path) -> None:
    """The standard library matches the pair (``KEY_VALUES_MISMATCH``), so
    no asymmetric code is written here."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(str(cert_path), str(key_path), password=lambda: b"")
    except ssl.SSLError as exc:
        if exc.reason == "KEY_VALUES_MISMATCH" or "KEY_VALUES_MISMATCH" in str(exc):
            raise Refused("key_mismatch", "The key does not match the certificate.") from None
        raise Refused("key", "The key cannot be read with the certificate.") from None


def names_of(cert: x509.Certificate) -> list[str]:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return []
    return [*san.get_values_for_type(x509.DNSName), *(str(ip) for ip in san.get_values_for_type(x509.IPAddress))]


def covers(names: list[str], site: str) -> bool:
    """Whether a certificate naming ``names`` serves ``site``: the name
    itself, or a wildcard for its leftmost label."""
    wanted = site.lower().rstrip(".")
    for name in names:
        name = name.lower().rstrip(".")
        if name == wanted:
            return True
        if name.startswith("*.") and "." in wanted and wanted.split(".", 1)[1] == name[2:]:
            return True
    return False


def describe(cert: x509.Certificate, der: bytes | None = None) -> dict[str, Any]:
    """The public facts of one certificate."""
    der = der if der is not None else cert.public_bytes(Encoding.DER)
    return {
        "subject": cert.subject.rfc4514_string() or None,
        "issuer": cert.issuer.rfc4514_string() or None,
        "names": names_of(cert),
        "not_before": _iso(cert.not_valid_before_utc),
        "not_after": _iso(cert.not_valid_after_utc),
        "sha256": hashlib.sha256(der).hexdigest(),
    }


# ---------------------------------------------------------------------------
# The control files
# ---------------------------------------------------------------------------


def parse_pki(text: str | None) -> dict[str, tuple[str, str]]:
    """``{issuer id: (cert path, key path)}``, in file order."""
    if not text:
        return {}
    return {match.group(1): (match.group(2), match.group(3)) for match in _ENTRY.finditer(text)}


def pki_text(entries: dict[str, tuple[str, str]]) -> str | None:
    """The whole file for ``entries``, the environment's first; None when
    there is nothing to name, so the Caddyfile's glob matches nothing."""
    if not entries:
        return None
    ordered = sorted(entries.items(), key=lambda item: (not item[0].startswith("env-"), item[0]))
    lines = ["pki {"]
    for issuer, (cert, key) in ordered:
        lines += [
            f"\tca {issuer} {{",
            "\t\troot {",
            f"\t\t\tcert {cert}",
            f"\t\t\tkey {key}",
            "\t\t}",
            "\t}",
        ]
    lines.append("}")
    return PKI_HEADER + "\n".join(lines) + "\n"


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = str(path)

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def _caddy_error(body: bytes) -> str:
    """The adapter's error from a refused reload, as Caddy words it — a
    configuration names paths, never key material, and anything shaped
    like PEM is cut all the same."""
    try:
        message = str(json.loads(body).get("error") or "")
    except (ValueError, AttributeError):
        message = body.decode("utf-8", "replace")
    message = _PEM.sub("[PEM]", message).split("-----BEGIN", 1)[0].strip()
    return message[:600] or "no reason given"


class Edge:
    """The edge's control directory and admin socket.

    ``control`` is the directory as this process sees it, which in the
    deployment is also the path the edge reads (both mount the volume at
    ``/control``); the paths written into the files are its own.
    """

    def __init__(
        self,
        control: Path,
        caddyfile: Path,
        edge_address: str,
        *,
        socket_path: Path | None = None,
        edge_port: int = 8443,
        clock: Callable[[], datetime] = utc_now,
        timeout: float = 10.0,
        load_timeout: float = 30.0,
    ) -> None:
        self.control = Path(control)
        self.caddyfile = Path(caddyfile)
        self.edge_address = edge_address
        self.socket_path = Path(socket_path) if socket_path else self.control / "admin.sock"
        self.edge_port = edge_port
        self.clock = clock
        self.timeout = timeout
        self.load_timeout = load_timeout
        self._lock = threading.Lock()
        self._last_site: list[str] = []

    # ---- the admin socket ---------------------------------------------------

    def caddy(self, method: str, path: str, body: bytes | None = None,
              content_type: str | None = None, timeout: float | None = None) -> tuple[int, bytes]:
        connection = _UnixConnection(self.socket_path, timeout or self.timeout)
        try:
            headers = {"Content-Type": content_type} if content_type else {}
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        except (OSError, http.client.HTTPException):
            raise EdgeUnavailable("The edge's admin socket answered nothing.") from None
        finally:
            connection.close()

    def config(self) -> dict[str, Any]:
        status, body = self.caddy("GET", "/config/")
        if status != 200:
            raise EdgeUnavailable(f"The edge's admin socket answered {status} to /config/.")
        try:
            return json.loads(body) or {}
        except ValueError:
            raise EdgeUnavailable("The edge's admin socket answered /config/ with no JSON.") from None

    def reload(self) -> None:
        """POST the Caddyfile to ``/load``: the edge adapts it with its own
        environment, placeholders expanded, and swaps it in without a
        restart, or refuses it with the adapter's error."""
        status, body = self.caddy(
            "POST", "/load", self.caddyfile.read_bytes(), "text/caddyfile", timeout=self.load_timeout
        )
        if status != 200:
            raise Refused("reload", f"The edge refused the new configuration: {_caddy_error(body)}")

    # ---- reading what is in effect ------------------------------------------

    @staticmethod
    def site_names(config: dict[str, Any]) -> list[str]:
        names: list[str] = []
        servers = ((config.get("apps") or {}).get("http") or {}).get("servers") or {}
        for server in servers.values():
            for route in server.get("routes") or []:
                for matcher in route.get("match") or []:
                    for host in matcher.get("host") or []:
                        if host not in names:
                            names.append(host)
        return names

    @staticmethod
    def issuer(config: dict[str, Any], site: list[str]) -> dict[str, Any] | None:
        tls = ((config.get("apps") or {}).get("tls")) or {}
        if (tls.get("certificates") or {}).get("load_files"):
            return {"kind": "files"}
        for policy in (tls.get("automation") or {}).get("policies") or []:
            subjects = policy.get("subjects") or []
            if subjects and site and not any(name in subjects for name in site):
                continue
            issuers = policy.get("issuers") or []
            if not issuers:
                continue
            first = issuers[0]
            if first.get("module") == "internal":
                return {"kind": "internal", "ca": first.get("ca") or "local"}
            if first.get("module") == "acme":
                return {"kind": "acme", "email": first.get("email")}
        return None

    def root(self, ca: str) -> dict[str, Any] | None:
        status, body = self.caddy("GET", f"/pki/ca/{ca}")
        if status != 200:
            return None
        try:
            answer = json.loads(body)
            pem = answer["root_certificate"]
            cert = x509.load_pem_x509_certificate(pem.encode("ascii"))
        except (ValueError, KeyError, TypeError, UnicodeEncodeError):
            return None
        return {"ca": ca, "name": answer.get("name"), **describe(cert), "pem": pem}

    def leaf(self, sni: str) -> dict[str, Any] | None:
        """The certificate the edge serves, by a handshake to its own address
        with the site's first name as SNI — measured: with no SNI, or
        another name, the edge answers a TLS alert and no certificate."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            with socket.create_connection((self.edge_address, self.edge_port), timeout=self.timeout) as raw:
                with context.wrap_socket(raw, server_hostname=sni) as tls:
                    der = tls.getpeercert(binary_form=True)
        except (OSError, ssl.SSLError, ValueError):
            return None
        if not der:
            return None
        return describe(x509.load_der_x509_certificate(der), der)

    def read(self, name: str) -> str | None:
        try:
            return (self.control / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def choice(self) -> dict[str, Any] | None:
        text = self.read(CHOICE)
        if text is None:
            return None
        try:
            recorded = json.loads(text)
        except ValueError:
            recorded = {}
        return recorded if isinstance(recorded, dict) else {}

    def environment(self) -> dict[str, Any]:
        """What the environment selects, from ``env.caddy`` as the start
        wrote it: LIBRERUN_TLS_CA's CA, or LIBRERUN_TLS."""
        text = self.read(ENV) or ""
        match = re.search(r"\bca (env-[0-9a-f]{12})\b", text)
        if match:
            return {"variable": "LIBRERUN_TLS_CA", "ca": match.group(1)}
        return {"variable": "LIBRERUN_TLS", "ca": None}

    def status(self) -> dict[str, Any]:
        """What the edge serves and what it needs: public material alone."""
        now = self.clock()
        needs: list[str] = []
        site: list[str] = []
        issuer: dict[str, Any] | None = None
        root: dict[str, Any] | None = None
        try:
            config = self.config()
            site = self.site_names(config) or self._last_site
            self._last_site = site
            issuer = self.issuer(config, site)
            if issuer and issuer["kind"] == "internal":
                root = self.root(issuer["ca"])
        except EdgeUnavailable:
            site = self._last_site
            needs.append("edge_restart")
        leaf = self.leaf(site[0]) if site else None
        choice = self.choice()
        environment = self.environment()
        if choice is not None:
            source = "ui"
            why = (
                "Chosen on Application Settings. It stays in effect, across restarts, "
                "until “Use the environment's setting”."
            )
        else:
            source = "environment"
            if environment["variable"] == "LIBRERUN_TLS_CA":
                why = ("No choice is recorded on Application Settings, so the environment's applies: "
                       "LIBRERUN_TLS_CA names the CA the edge issues from.")
            else:
                why = ("No choice is recorded on Application Settings, so the environment's applies: "
                       "LIBRERUN_TLS.")
        if issuer and issuer["kind"] == "internal":
            needs.append("trust_root")
            loaded = str(issuer.get("ca", "")).startswith(("loaded-", "env-"))
            if loaded and root and _parse(root["not_after"]) - now < CA_ENDING:
                needs.append("ca_ending")
        if issuer and issuer["kind"] == "files" and leaf and _parse(leaf["not_after"]) - now < FILES_ENDING:
            needs.append("files_ending")
        if issuer and issuer["kind"] == "acme":
            needs.append("acme_requirements")
        return {
            "site": site,
            "issuer": issuer,
            "root": root,
            "leaf": leaf,
            "source": source,
            "choice": choice,
            "environment": environment,
            "why": why,
            "needs": needs,
        }

    # ---- the changes ----------------------------------------------------------

    def load_ca(self, cert_pem: str, key_pem: str, by: str | None = None) -> dict[str, Any]:
        """Load a CA the edge issues from: at install, or after a restore to
        bring back the root browsers already trust (L42)."""
        with self._lock:
            return self._load_ca(cert_pem, key_pem, by)

    def _load_ca(self, cert_pem: str, key_pem: str, by: str | None) -> dict[str, Any]:
        now = self.clock()
        certs = read_certificates(cert_pem, "CA certificate")
        if len(certs) != 1:
            raise Refused("certificate", "A CA is one certificate, its root; an intermediate without its root is not taken.")
        check_ca(certs[0], now)
        key = key_block(key_pem)
        der = certs[0].public_bytes(Encoding.DER)
        digest = hashlib.sha256(der).hexdigest()[:12]
        issuer = f"loaded-{digest}"
        cert_path, key_path = self.control / f"ca-{digest}.crt", self.control / f"ca-{digest}.key"
        self._place(certs[0].public_bytes(Encoding.PEM).decode("ascii"), key, cert_path, key_path)
        # A new issuer id is the point: measured, a reload under the same id
        # keeps serving the leaf the previous root issued, and a new id
        # issues the leaf again at once.
        selection = f"tls {{\n\tissuer internal {{\n\t\tca {issuer}\n\t}}\n}}\n"
        return self._change(
            selection, self._record("ca", by, now), (issuer, str(cert_path), str(key_path)), [cert_path, key_path]
        )

    def use_files(self, cert_pem: str, key_pem: str, by: str | None = None) -> dict[str, Any]:
        """Serve the admin's own certificate chain and key."""
        with self._lock:
            return self._use_files(cert_pem, key_pem, by)

    def _use_files(self, cert_pem: str, key_pem: str, by: str | None) -> dict[str, Any]:
        now = self.clock()
        certs = read_certificates(cert_pem, "certificate")
        check_dates(certs[0], "certificate", now)
        check_key_type(certs[0], "certificate")
        site = self.site_names(self.config()) or self._last_site
        names = names_of(certs[0])
        missing = [name for name in site if not covers(names, name)]
        if missing or not site:
            raise Refused(
                "names",
                f"The certificate names {', '.join(names) or 'nothing'}, not the site's "
                f"{', '.join(missing or site) or 'name'}.",
            )
        key = key_block(key_pem)
        digest = hashlib.sha256(certs[0].public_bytes(Encoding.DER)).hexdigest()[:12]
        cert_path, key_path = self.control / f"site-{digest}.crt", self.control / f"site-{digest}.key"
        chain = "".join(cert.public_bytes(Encoding.PEM).decode("ascii") for cert in certs)
        self._place(chain, key, cert_path, key_path)
        return self._change(f"tls {cert_path} {key_path}\n", self._record("files", by, now), None, [cert_path, key_path])

    def use_acme(self, email: Any, by: str | None = None) -> dict[str, Any]:
        """ACME with the admin's account e-mail."""
        if not isinstance(email, str) or len(email) > 254 or not _EMAIL.fullmatch(email):
            raise Refused("email", "That is not an e-mail address the edge can register with ACME.")
        with self._lock:
            return self._change(f"tls {email}\n", self._record("acme", by, self.clock()), None, [])

    def use_environment(self, by: str | None = None) -> dict[str, Any]:
        """Back to the environment's setting: the choice goes, and the edge
        serves what LIBRERUN_TLS and LIBRERUN_TLS_CA say — the CA the
        environment names is read by the edge alone."""
        selection = f"import {self.control / ENV}\n"
        with self._lock:
            if self.read(CHOICE) is None and self.read(TLS) == selection:
                return self.status()
            return self._change(selection, None, None, [])

    # ---- how a change is made -------------------------------------------------

    @staticmethod
    def _record(kind: str, by: str | None, now: datetime) -> dict[str, Any]:
        try:
            by = str(uuid.UUID(by)) if by else None
        except (ValueError, TypeError, AttributeError):
            by = None
        return {"kind": kind, "by": by, "at": _iso(now)}

    def _write(self, name: str, text: str | None) -> None:
        path = self.control / name
        if text is None:
            path.unlink(missing_ok=True)
            return
        temporary = self.control / f".{name}.{secrets.token_hex(6)}"
        temporary.write_text(text, encoding="utf-8")
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)

    def _place(self, cert_text: str, key_text: str, cert_path: Path, key_path: Path) -> None:
        """Write the pair under temporary names — the key 0600 from its
        first byte — match it, and only then give it its names. A pair that
        does not match is deleted, and never touches the names a serving
        choice may hold."""
        token = secrets.token_hex(6)
        cert_temporary = self.control / f".upload-{token}.crt"
        key_temporary = self.control / f".upload-{token}.key"
        try:
            cert_temporary.write_text(cert_text, encoding="ascii")
            descriptor = os.open(key_temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(key_text)
            check_pair(cert_temporary, key_temporary)
            os.chmod(cert_temporary, 0o644)
            os.replace(cert_temporary, cert_path)
            os.replace(key_temporary, key_path)
        finally:
            cert_temporary.unlink(missing_ok=True)
            key_temporary.unlink(missing_ok=True)

    def _named_files(self, selection: str | None, entries: dict[str, tuple[str, str]]) -> set[Path]:
        """The files a selection serves from: a loaded CA's pair, or a
        certificate and key in the control volume."""
        text = (selection or "").strip()
        match = _ISSUER.search(text)
        if match and match.group(1) in entries:
            return {Path(path) for path in entries[match.group(1)]}
        match = _FILES.match(text)
        if match:
            return {Path(match.group(1)), Path(match.group(2))}
        return set()

    def _change(
        self,
        selection: str,
        choice: dict[str, Any] | None,
        new_entry: tuple[str, str, str] | None,
        new_files: list[Path],
    ) -> dict[str, Any]:
        # The caller holds the lock, from its first check to the reload.
        before = {name: self.read(name) for name in (PKI, TLS, CHOICE)}
        entries = parse_pki(before[PKI])
        serving = self._named_files(before[TLS], entries)
        # While the reload runs: the environment's CA, the one still
        # serving and the new one, in one pki block.
        during = {
            issuer: paths
            for issuer, paths in entries.items()
            if issuer.startswith("env-") or {Path(path) for path in paths} == serving
        }
        if new_entry is not None:
            during[new_entry[0]] = (new_entry[1], new_entry[2])
        self._write(PKI, pki_text(during))
        self._write(TLS, selection)
        self._write(CHOICE, json.dumps(choice, sort_keys=True) + "\n" if choice else None)
        try:
            self.reload()
        except (Refused, EdgeUnavailable):
            # The previous control files go back — they name files that
            # still exist — and the edge is asked to load them again.
            for name, text in before.items():
                self._write(name, text)
            for path in new_files:
                if path not in serving:
                    path.unlink(missing_ok=True)
            try:
                self.reload()
            except (Refused, EdgeUnavailable):
                pass  # the status says edge_restart, and a restart reads the files put back
            raise
        # Accepted: the new choice is serving. The pki file keeps the
        # environment's CA and the new one, and only now do the files
        # the previous choice named go — deleted, never moved.
        kept = {
            issuer: paths
            for issuer, paths in during.items()
            if issuer.startswith("env-") or (new_entry is not None and issuer == new_entry[0])
        }
        self._write(PKI, pki_text(kept))
        named = set(new_files) | {Path(path) for paths in kept.values() for path in paths}
        for path in serving - named:
            path.unlink(missing_ok=True)
        self._sweep(named)
        return self.status()

    def _sweep(self, named: set[Path]) -> None:
        """Delete every certificate or key this service wrote that nothing
        names any more — what an interrupted change can leave behind."""
        for path in self.control.iterdir():
            if path in named:
                continue
            if re.fullmatch(r"(?:ca|site)-[0-9a-f]{12}\.(?:crt|key)|\.upload-[0-9a-f]{12}\.(?:crt|key)", path.name):
                path.unlink(missing_ok=True)


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))
