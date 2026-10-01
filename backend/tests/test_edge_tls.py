"""Certificates the admin sees and manages (K blueprint T2; L42, L43, D44
refined).

``edge-control`` — ``app.edge_control``, the backend's code in a container
of its own — holds the edge's admin socket and the files a platform admin
loads; the backend reads its public status and never a key. These hold:

* nothing unchecked reaches Caddy: a certificate that is not a CA, a key
  that does not match, a certificate that does not name the site, each is
  refused by name before ``/load`` (Caddy 2.11.4 panics on a root it cannot
  load);
* a loaded CA takes an issuer id of its own, and every CA the edge may name
  sits in one ``pki`` block — Caddy keeps the last it parses;
* a refused reload puts the previous files back, still naming files that
  exist, and they are still served;
* a change is taken from the edge's address alone, after the backend's
  authorize route — which reads no body — let it through, and a tenant's
  admin is refused every route;
* no response and no log line carries a key, a malformed body's 422 among
  them;
* the backend says ``root_changed`` until the new root is acknowledged.

Caddy's admin API is a fake on a Unix socket that, like Caddy, refuses a
configuration naming a file that is not there; the leaf comes from a local
TLS server. Every key and certificate is made at run time and none is
committed (A3's ``tree-review``). The asymmetric code here is the test's:
``backend/app`` has none (K7's ``test_no_rsa_code_in_the_backend`` reads
``app/edge_control`` too).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import socketserver
import ssl
import stat
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.database import get_db
from app.edge_control import __main__ as edge_server
from app.edge_control.edge import CHANGE_ROUTES, Edge, EdgeUnavailable, Refused, parse_pki, pki_text
from app.middleware import get_current_user
from app.models import User
from app.routers import admin as admin_router
from app.services import app_settings_service, edge_tls
from tests.test_secret_setting_api import platform_admin  # noqa: F401  (fixture)
from tests.test_secrets_service import db  # noqa: F401  (fixture)

SITE = "librerun.test"
NOW = dt.datetime.now(dt.timezone.utc)
DER = serialization.Encoding.DER


# ---------------------------------------------------------------------------
# Certificates and keys, made at run time
# ---------------------------------------------------------------------------


def _key(curve: ec.EllipticCurve | None = None) -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(curve or ec.SECP256R1())


def _key_pem(key, password: bytes | None = None) -> str:
    encryption = serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption()
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption).decode()


def _pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _sha(cert: x509.Certificate) -> str:
    return hashlib.sha256(cert.public_bytes(DER)).hexdigest()


def _usage(cert_sign: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
        key_agreement=False, key_cert_sign=cert_sign, crl_sign=cert_sign, encipher_only=False, decipher_only=False,
    )


def _ca(name: str = "LibreRun test CA", *, ca: bool = True, basic: bool = True, key_cert_sign: bool = True,
        path_length: int | None = None, days: int = 365, start: int = -1, key=None):
    key = key or _key()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW + dt.timedelta(days=start))
        .not_valid_after(NOW + dt.timedelta(days=days))
        .add_extension(_usage(key_cert_sign), critical=True)
    )
    if basic:
        builder = builder.add_extension(x509.BasicConstraints(ca=ca, path_length=path_length), critical=True)
    return builder.sign(key, hashes.SHA256()), key


def _leaf(names: list[str], issuer: x509.Certificate, issuer_key, *, days: int = 90, start: int = -1):
    key = _key()
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(issuer.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW + dt.timedelta(days=start))
        .not_valid_after(NOW + dt.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name) for name in names]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(issuer_key, hashes.SHA256())
    )
    return cert, key


def _canary(key_pem: str) -> list[str]:
    """The lines of a key's PEM body: a response, a log line or a file that
    carries any of them carries the key."""
    return [line for line in key_pem.splitlines() if line and not line.startswith("-----")]


# ---------------------------------------------------------------------------
# Caddy's admin API, on a Unix socket, and the edge's TLS port
# ---------------------------------------------------------------------------


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class FakeCaddy:
    """GET /config/, GET /pki/ca/<id> and POST /load, as edge-control uses
    them. ``/load`` reads the control files as Caddy would and, like
    Caddy, refuses a configuration that names a file that is not there;
    what it accepts is what it serves. ``panic`` answers the next /load
    with an empty reply and then nothing, as Caddy does on a root it
    cannot load."""

    def __init__(self, control: Path, socket_path: Path, local_root: x509.Certificate) -> None:
        self.control, self.socket_path = control, socket_path
        self.cas = {"local": _pem(local_root)}
        self.loads: list[dict] = []
        self.bodies: list[bytes] = []
        self.panic = False
        self.serving = self.snapshot()
        self.server: _UnixServer | None = None

    def snapshot(self) -> dict:
        def read(name: str) -> str | None:
            path = self.control / name
            return path.read_text() if path.exists() else None

        return {"pki": read("pki.global.caddy"), "tls": read("tls.caddy"), "env": read("env.caddy")}

    @staticmethod
    def selection(snap: dict) -> str:
        chosen = (snap["tls"] or "").strip()
        return (snap["env"] or "").strip() if chosen.startswith("import ") else chosen

    def load(self) -> tuple[int, dict]:
        snap = self.snapshot()
        self.loads.append(snap)
        entries = parse_pki(snap["pki"])
        files = re.fullmatch(r"tls (\S+) (\S+)", self.selection(snap))
        wanted = [path for pair in entries.values() for path in pair] + (list(files.groups()) if files else [])
        for path in wanted:
            if not Path(path).exists():
                return 400, {"error": f"loading new config: loading certificates: open {path}: no such file or directory"}
        named = re.search(r"\bca ((?:env|loaded)-[0-9a-f]{12})\b", self.selection(snap))
        if named and named.group(1) not in entries:
            return 400, {"error": f"no certificate authority configured with id: {named.group(1)}"}
        for issuer, (cert, _) in entries.items():
            self.cas[issuer] = Path(cert).read_text()
        self.serving = snap
        return 200, {}

    def config(self) -> dict:
        selection = self.selection(self.serving)
        tls: dict = {}
        named = re.search(r"\bca ((?:env|loaded)-[0-9a-f]{12})\b", selection)
        files = re.fullmatch(r"tls (\S+) (\S+)", selection)
        acme = re.fullmatch(r"tls (\S+@\S+)", selection)
        if files:
            tls = {"certificates": {"load_files": [{"certificate": files.group(1), "key": files.group(2)}]}}
        elif acme:
            tls = {"automation": {"policies": [{"subjects": [SITE], "issuers": [{"module": "acme", "email": acme.group(1)}]}]}}
        else:
            issuer = {"module": "internal", **({"ca": named.group(1)} if named else {})}
            tls = {"automation": {"policies": [{"subjects": [SITE], "issuers": [issuer]}]}}
        return {"apps": {"http": {"servers": {"srv0": {"routes": [{"match": [{"host": [SITE]}]}]}}}, "tls": tls}}

    def start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def address_string(self) -> str:
                return "unix"

            def log_message(self, *args) -> None:
                pass

            def _answer(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/config/":
                    return self._answer(200, fake.config())
                ca = self.path.removeprefix("/pki/ca/")
                if ca in fake.cas:
                    return self._answer(200, {"id": ca, "name": f"{ca} CA", "root_certificate": fake.cas[ca]})
                return self._answer(404, {"error": "unknown"})

            def do_POST(self) -> None:  # noqa: N802
                fake.bodies.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                if self.path != "/load":
                    return self._answer(404, {"error": "unknown"})
                if fake.panic:
                    self.close_connection = True
                    threading.Thread(target=fake.stop, daemon=True).start()
                    return
                self._answer(*fake.load())

        self.server = _UnixServer(str(self.socket_path), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
            self.socket_path.unlink(missing_ok=True)


class LeafServer:
    """The edge's TLS port: serves one chain, and records each SNI."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.snis: list[str | None] = []
        self.context: ssl.SSLContext | None = None
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def serve(self, cert: x509.Certificate, key) -> None:
        (self.directory / "leaf.crt").write_text(_pem(cert))
        (self.directory / "leaf.key").write_text(_key_pem(key))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.directory / "leaf.crt", self.directory / "leaf.key")
        context.sni_callback = lambda _sock, name, _context: self.snis.append(name)
        self.context = context

    def _loop(self) -> None:
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                with self.context.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(1)
            except (OSError, ssl.SSLError):
                pass

    def stop(self) -> None:
        self.running = False
        self.sock.close()


@pytest.fixture
def rig(tmp_path):
    # A Unix socket's path must stay short, which pytest's tmp_path is not.
    socket_dir = Path(tempfile.mkdtemp(prefix="ec", dir="/tmp"))
    control = tmp_path / "control"
    control.mkdir()
    (control / "env.caddy").write_text("import tls_environment\n")
    (control / "tls.caddy").write_text(f"import {control}/env.caddy\n")
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("# the Caddyfile edge-control posts to /load\n")
    local_root, local_key = _ca("Caddy Local Authority - 2026 ECC Root")
    leaf, leaf_key = _leaf([SITE], local_root, local_key, days=1)
    fake = FakeCaddy(control, socket_dir / "admin.sock", local_root)
    fake.start()
    tls_dir = tmp_path / "leaf"
    tls_dir.mkdir()
    port = LeafServer(tls_dir)
    port.serve(leaf, leaf_key)
    edge = Edge(control, caddyfile, "127.0.0.1", socket_path=socket_dir / "admin.sock", edge_port=port.port, timeout=5)
    yield SimpleNamespace(edge=edge, fake=fake, port=port, control=control, local_root=local_root, leaf=leaf,
                          tmp=tmp_path)
    fake.stop()
    port.stop()
    shutil.rmtree(socket_dir, ignore_errors=True)


@contextlib.contextmanager
def _edge_control(edge: Edge, edge_address: str):
    """edge-control's own HTTP server, on loopback, taking changes from
    ``edge_address`` alone."""
    server = edge_server.serve(edge, edge_address, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _request(base: str, method: str, path: str, payload=None, *, raw: bytes | None = None,
             headers: dict | None = None, chunked: bool = False) -> tuple[int, object, bytes]:
    parts = urlsplit(base)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=30)
    body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
    try:
        if chunked:
            connection.request(method, path, body=iter([body]), headers={"Content-Type": "application/json"},
                               encode_chunked=True)
        else:
            connection.request(method, path, body=body,
                               headers={"Content-Type": "application/json", **(headers or {})})
    except (BrokenPipeError, ConnectionResetError):
        # edge-control refuses a body it will not read, answers and closes.
        # A client still sending then meets the reset, and reads the answer
        # the server wrote first; none written, and getresponse() fails.
        pass
    response = connection.getresponse()
    data = response.read()
    connection.close()
    try:
        return response.status, json.loads(data), data
    except ValueError:
        return response.status, data.decode("utf-8", "replace"), data


def _client(db, user: User, app: FastAPI | None = None) -> AsyncClient:  # noqa: F811
    app = app or FastAPI()
    app.include_router(admin_router.router)

    async def _db_dep():
        yield db

    async def _user_dep():
        return user

    app.dependency_overrides[get_db] = _db_dep
    app.dependency_overrides[get_current_user] = _user_dep
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


@pytest_asyncio.fixture
async def tenant_admin(db, monkeypatch) -> User:  # noqa: F811
    """An admin of an ordinary tenant: the platform is another tenant."""
    import app.config as cfg

    tenant_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'A tenant', :s)"),
        {"id": tenant_id, "s": f"tenant-{tenant_id.hex[:12]}"},
    )
    user_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, auth_provider, role)"
            " VALUES (:id, :t, :e, 'credentials', 'admin')"
        ),
        {"id": user_id, "t": tenant_id, "e": f"{user_id.hex[:10]}@example.com"},
    )
    monkeypatch.setattr(cfg.settings, "PLATFORM_TENANT_SLUG", f"platform-{uuid.uuid4().hex[:12]}")
    return await db.get(User, user_id)


async def _tls_rows(db, tenant_id) -> list[dict]:  # noqa: F811
    rows = (
        await db.execute(
            text("SELECT detail FROM activity_audit_log WHERE tenant_id = :t AND action_type = 'config_change'"),
            {"t": tenant_id},
        )
    ).scalars().all()
    return [row for row in rows if row.get("surface") == "tls"]


def _ca_files(control: Path, cert: x509.Certificate) -> list[Path]:
    digest = _sha(cert)[:12]
    return [control / f"ca-{digest}.crt", control / f"ca-{digest}.key"]


# ---------------------------------------------------------------------------
# What edge-control refuses, by name, before anything reaches Caddy
# ---------------------------------------------------------------------------


def test_a_certificate_that_is_not_a_ca_is_refused_by_name(rig):
    leaf, key = _ca("Not a CA", ca=False)
    with pytest.raises(Refused) as refused:
        rig.edge.load_ca(_pem(leaf), _key_pem(key))
    assert refused.value.check == "basicConstraints" and "basicConstraints" in refused.value.detail

    # The other ways a certificate is not a CA the edge can issue from.
    expired = _ca(days=-1, start=-30)
    cases = [
        (_ca(basic=False), "basicConstraints"),
        (_ca(path_length=0), "basicConstraints"),
        (_ca(key_cert_sign=False), "keyCertSign"),
        (expired, "expired"),
        (_ca(key=_key(ec.SECP256K1())), "key_type"),
    ]
    for (cert, cert_key), check in cases:
        with pytest.raises(Refused) as refused:
            rig.edge.load_ca(_pem(cert), _key_pem(cert_key))
        assert refused.value.check == check, (check, refused.value.detail)
    # A root and an intermediate together is not one CA.
    root, root_key = _ca()
    with pytest.raises(Refused) as refused:
        rig.edge.load_ca(_pem(root) + _pem(_ca("Other")[0]), _key_pem(root_key))
    assert refused.value.check == "certificate"

    # Nothing reached the control volume, and nothing reached Caddy.
    assert not [path for path in rig.control.iterdir() if path.name.startswith(("ca-", ".upload-"))]
    assert rig.fake.loads == []

    # Through edge-control's own server: 422, naming basicConstraints.
    with _edge_control(rig.edge, "127.0.0.1") as base:
        status, body, _ = _request(base, "PUT", "/api/v1/admin/tls/ca", {"certificate": _pem(leaf), "key": _key_pem(key)})
    assert status == 422, body
    assert body["check"] == "basicConstraints" and "basicConstraints" in body["detail"], body


def test_a_key_that_does_not_match_is_refused(rig):
    ca, ca_key = _ca()
    other = _key()
    with pytest.raises(Refused) as refused:
        rig.edge.load_ca(_pem(ca), _key_pem(other))
    assert refused.value.check == "key_mismatch"
    # Both files deleted, under their names and their temporary ones.
    assert not [path for path in rig.control.iterdir() if path.name.startswith(("ca-", ".upload-"))]
    assert rig.fake.loads == []

    # Not a key, an encrypted key, a certificate in the key's place.
    for bad in ("not a key at all", _key_pem(ca_key, password=b"a passphrase"), _pem(ca), _key_pem(ca_key) * 2):
        with pytest.raises(Refused) as refused:
            rig.edge.load_ca(_pem(ca), bad)
        assert refused.value.check == "key", bad[:40]

    # The files the admin brings are matched the same way.
    leaf, _ = _leaf([SITE], ca, ca_key)
    with pytest.raises(Refused) as refused:
        rig.edge.use_files(_pem(leaf), _key_pem(other))
    assert refused.value.check == "key_mismatch"
    assert not [path for path in rig.control.iterdir() if path.name.startswith(("site-", ".upload-"))]
    assert rig.fake.loads == []


def test_files_must_name_the_site(rig):
    ca, ca_key = _ca()
    for names in (["other.example"], ["*.other.test"], [f"www.{SITE}"]):
        leaf, key = _leaf(names, ca, ca_key)
        with pytest.raises(Refused) as refused:
            rig.edge.use_files(_pem(leaf), _key_pem(key))
        assert refused.value.check == "names" and SITE in refused.value.detail, refused.value.detail
    old, old_key = _leaf([SITE], ca, ca_key, days=-1, start=-30)
    with pytest.raises(Refused) as refused:
        rig.edge.use_files(_pem(old), _key_pem(old_key))
    assert refused.value.check == "expired"
    assert rig.fake.loads == []

    # The site's own name, then a wildcard for its leftmost label, each as
    # a chain: served, the key 0600, and the first one's files gone once the
    # second served.
    for names in ([SITE], ["*.test"]):
        leaf, key = _leaf(names, ca, ca_key)
        status = rig.edge.use_files(_pem(leaf) + _pem(ca), _key_pem(key))
        digest = _sha(leaf)[:12]
        crt, key_path = rig.control / f"site-{digest}.crt", rig.control / f"site-{digest}.key"
        assert (rig.control / "tls.caddy").read_text() == f"tls {crt} {key_path}\n"
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
        assert crt.read_text() == _pem(leaf) + _pem(ca)
        assert status["issuer"] == {"kind": "files"} and status["source"] == "ui"
    assert len(list(rig.control.glob("site-*.key"))) == 1


def test_status_reads_the_socket_and_the_served_leaf(rig):
    status = rig.edge.status()
    assert status["site"] == [SITE]
    assert status["issuer"] == {"kind": "internal", "ca": "local"}
    assert status["root"]["ca"] == "local" and status["root"]["sha256"] == _sha(rig.local_root)
    assert status["root"]["pem"] == _pem(rig.local_root)
    assert status["leaf"]["sha256"] == _sha(rig.leaf) and status["leaf"]["names"] == [SITE]
    assert status["leaf"]["issuer"] == rig.local_root.subject.rfc4514_string()
    assert status["source"] == "environment" and status["choice"] is None
    assert status["environment"] == {"variable": "LIBRERUN_TLS", "ca": None}
    assert status["needs"] == ["trust_root"]
    # The handshake named the site: with no SNI the edge serves nothing.
    assert rig.port.snis == [SITE]

    # A loaded CA ending within 90 days, and the admin's own files ending
    # within 30, each say so; ACME says what it needs.
    soon, soon_key = _ca("Ending soon", days=60)
    status = rig.edge.load_ca(_pem(soon), _key_pem(soon_key))
    assert status["needs"] == ["trust_root", "ca_ending"], status["needs"]
    leaf, leaf_key = _leaf([SITE], soon, soon_key, days=20)
    rig.port.serve(leaf, leaf_key)
    status = rig.edge.use_files(_pem(leaf), _key_pem(leaf_key))
    assert status["needs"] == ["files_ending"] and status["root"] is None, status
    status = rig.edge.use_acme("admin@example.com")
    assert status["issuer"] == {"kind": "acme", "email": "admin@example.com"}
    assert status["needs"] == ["acme_requirements"]

    # The socket answering nothing while the edge serves: edge_restart,
    # and the leaf is still read, under the last name the socket gave.
    rig.fake.stop()
    status = rig.edge.status()
    assert status["needs"] == ["edge_restart"] and status["issuer"] is None
    assert status["leaf"]["sha256"] == _sha(leaf)


def test_a_loaded_ca_takes_an_issuer_id_of_its_own(rig):
    chooser = str(uuid.uuid4())
    first, first_key = _ca("First CA")
    status = rig.edge.load_ca(_pem(first), _key_pem(first_key), chooser)
    first_id = "loaded-" + _sha(first)[:12]
    # Measured on the pinned image: a reload under the same issuer id keeps
    # serving the leaf the previous root issued, so never `local`.
    assert f"\t\tca {first_id}\n" in (rig.control / "tls.caddy").read_text()
    assert status["issuer"] == {"kind": "internal", "ca": first_id}
    assert status["root"]["sha256"] == _sha(first) and status["root"]["ca"] == first_id
    assert status["source"] == "ui" and status["choice"]["kind"] == "ca" and status["choice"]["by"] == chooser
    crt, key = _ca_files(rig.control, first)
    assert parse_pki((rig.control / "pki.global.caddy").read_text()) == {first_id: (str(crt), str(key))}
    assert stat.S_IMODE(key.stat().st_mode) == 0o600 and key.read_text() == _key_pem(first_key)

    second, second_key = _ca("Second CA")
    status = rig.edge.load_ca(_pem(second), _key_pem(second_key))
    second_id = "loaded-" + _sha(second)[:12]
    assert second_id != first_id and status["issuer"] == {"kind": "internal", "ca": second_id}
    assert not any(path.exists() for path in (crt, key)), "the first CA's files outlived the change"
    assert _key_pem(second_key) not in json.dumps(status)


def test_every_ca_sits_in_one_pki_block(rig):
    # The environment's CA, as config/edge-start.sh writes it at the start.
    certs = rig.tmp / "certs"
    certs.mkdir()
    env_ca, env_key = _ca("Environment CA")
    (certs / "ca.crt").write_text(_pem(env_ca))
    (certs / "ca.key").write_text(_key_pem(env_key))
    env_id = "env-" + hashlib.sha256((certs / "ca.crt").read_bytes()).hexdigest()[:12]
    env_entry = {env_id: (str(certs / "ca.crt"), str(certs / "ca.key"))}
    (rig.control / "pki.global.caddy").write_text(pki_text(env_entry))
    (rig.control / "env.caddy").write_text(f"tls {{\n\tissuer internal {{\n\t\tca {env_id}\n\t}}\n}}\n")
    assert rig.edge.status()["environment"] == {"variable": "LIBRERUN_TLS_CA", "ca": env_id}

    first, first_key = _ca("First CA")
    rig.edge.load_ca(_pem(first), _key_pem(first_key))
    first_entry = {"loaded-" + _sha(first)[:12]: tuple(map(str, _ca_files(rig.control, first)))}
    second, second_key = _ca("Second CA")
    rig.edge.load_ca(_pem(second), _key_pem(second_key))
    second_entry = {"loaded-" + _sha(second)[:12]: tuple(map(str, _ca_files(rig.control, second)))}
    rig.edge.use_environment()

    # What each reload read: one pki block, with the environment's CA, the
    # one still serving and the new one; and what stayed after each.
    during = [parse_pki(snap["pki"]) for snap in rig.fake.loads]
    assert during == [
        {**env_entry, **first_entry},
        {**env_entry, **first_entry, **second_entry},
        {**env_entry, **second_entry},
    ], during
    assert all(snap["pki"].count("pki {") == 1 for snap in rig.fake.loads)
    assert parse_pki((rig.control / "pki.global.caddy").read_text()) == env_entry
    assert not list(rig.control.glob("ca-*")), "a loaded CA outlived the change that replaced it"
    assert (rig.control / "tls.caddy").read_text() == f"import {rig.control}/env.caddy\n"
    status = rig.edge.status()
    assert status["source"] == "environment" and status["issuer"] == {"kind": "internal", "ca": env_id}


def test_a_failed_reload_restores_the_previous_files(rig):
    ca, ca_key = _ca()
    rig.edge.load_ca(_pem(ca), _key_pem(ca_key))
    ca_id = "loaded-" + _sha(ca)[:12]
    files = _ca_files(rig.control, ca)
    selection = (rig.control / "tls.caddy").read_text()

    # From the loaded CA back to an environment naming files that do not
    # exist: Caddy refuses, and the error is the response's.
    (rig.control / "env.caddy").write_text(f"tls {rig.tmp}/gone.crt {rig.tmp}/gone.key\n")
    with pytest.raises(Refused) as refused:
        rig.edge.use_environment()
    assert refused.value.check == "reload" and "gone.crt" in refused.value.detail
    # The CA's files are still there, still named, and still served: the
    # edge was asked to load the previous files again, and took them.
    assert all(path.exists() for path in files), "the previous choice's files went before the reload was accepted"
    assert (rig.control / "tls.caddy").read_text() == selection and (rig.control / "choice").exists()
    assert ca_id in parse_pki((rig.control / "pki.global.caddy").read_text())
    assert rig.fake.loads[-1]["tls"] == selection and rig.fake.serving == rig.fake.loads[-1]
    assert rig.edge.status()["issuer"] == {"kind": "internal", "ca": ca_id}

    # An empty reply — what Caddy gives when it panics on a root it cannot
    # load — is a refusal too: the files go back, the new ones go, and the
    # status says edge_restart.
    (rig.control / "env.caddy").write_text("import tls_environment\n")
    other, other_key = _ca("Other CA")
    rig.fake.panic = True
    with pytest.raises(EdgeUnavailable):
        rig.edge.load_ca(_pem(other), _key_pem(other_key))
    assert all(path.exists() for path in files)
    assert not any(path.exists() for path in _ca_files(rig.control, other))
    assert (rig.control / "tls.caddy").read_text() == selection
    assert "edge_restart" in rig.edge.status()["needs"]


# ---------------------------------------------------------------------------
# Who may ask, and from where
# ---------------------------------------------------------------------------


def test_edge_control_takes_a_change_from_the_edge_alone(rig):
    chooser = str(uuid.uuid4())
    with _edge_control(rig.edge, "127.0.0.2") as base:  # the edge is elsewhere
        status, body, _ = _request(base, "PUT", "/api/v1/admin/tls/acme", {"email": "admin@example.com"})
        assert status == 403 and body["check"] == "origin", body
        assert rig.fake.loads == [] and not (rig.control / "choice").exists()
        # The status is public material, for the backend on the edge network.
        status, body, _ = _request(base, "GET", "/status")
        assert status == 200 and body["site"] == [SITE]

    with _edge_control(rig.edge, "127.0.0.1") as base:  # the edge is the caller
        status, body, _ = _request(base, "PUT", "/api/v1/admin/tls/acme", {"email": "admin@example.com"},
                                   headers={"X-Librerun-Chosen-By": chooser})
        assert status == 200, body
        assert body["issuer"] == {"kind": "acme", "email": "admin@example.com"} and body["choice"]["by"] == chooser
        # The path as the edge matched it: any case, escaped or not.
        status, body, _ = _request(base, "DELETE", "/API/V1/ADMIN/TLS/CHOIC%45")
        assert status == 200 and body["source"] == "environment", body
        # A method or a path that is not one of the four is not a change.
        assert _request(base, "GET", "/api/v1/admin/tls/ca")[0] == 404
        assert _request(base, "PUT", "/api/v1/admin/tls/ca/")[0] == 404
        assert _request(base, "POST", "/api/v1/admin/tls/ca")[0] == 405
        # A body over 64 KiB is refused unread, and one without a length.
        status, body, _ = _request(base, "PUT", "/api/v1/admin/tls/ca", raw=b"x" * (64 * 1024 + 1))
        assert status == 413 and body["check"] == "body_limit"
        assert _request(base, "PUT", "/api/v1/admin/tls/ca", {"certificate": "c", "key": "k"}, chunked=True)[0] == 411
        # An e-mail that would end a Caddyfile token is no e-mail.
        for email in ("admin@example.com\n}", "admin@example.com {$HOME}", "a b@example.com", 7):
            status, body, _ = _request(base, "PUT", "/api/v1/admin/tls/acme", {"email": email})
            assert status == 422 and body["check"] == "email", (email, body)


@pytest.mark.asyncio
async def test_a_tenant_admin_is_refused_every_tls_route(db, tenant_admin, monkeypatch):  # noqa: F811
    async def _no_edge():
        raise AssertionError("a refused caller made the backend ask edge-control")

    monkeypatch.setattr(edge_tls, "read_status", _no_edge)
    forwarded = {"X-Forwarded-Method": "PUT", "X-Forwarded-Uri": "/api/v1/admin/tls/ca"}
    async with _client(db, tenant_admin) as client:
        for method, path in (
            ("GET", "/admin/tls"),
            ("GET", "/admin/tls/root.pem"),
            ("POST", "/admin/tls/acknowledge"),
            ("GET", "/admin/tls/authorize"),
        ):
            response = await client.request(method, path, headers=forwarded)
            assert response.status_code == 403, (method, path, response.status_code)
            assert "X-Librerun-Chosen-By" not in response.headers
    assert await _tls_rows(db, tenant_admin.tenant_id) == []
    assert (await db.execute(text("SELECT count(*) FROM app_settings WHERE key = 'tls.last_root'"))).scalar() == 0


@pytest.mark.asyncio
async def test_the_authorize_route_reads_no_body(db, platform_admin):  # noqa: F811
    (route,) = [route for route in admin_router.router.routes if route.path == "/admin/tls/authorize"]
    assert route.body_field is None and route.dependant.body_params == []

    # The ASGI receive the app is handed fails the test if the body is read.
    inner = FastAPI()

    async def guarded(scope, receive, send):
        async def no_body():
            raise AssertionError("the authorize route read the request's body")

        await inner(scope, no_body if scope["type"] == "http" else receive, send)

    async with _client(db, platform_admin, inner) as unguarded:
        client = AsyncClient(transport=ASGITransport(app=guarded), base_url="http://testserver")
        for method, uri, route_name in (
            ("PUT", "/api/v1/admin/tls/ca", ("PUT /api/v1/admin/tls/ca", "load_ca")),
            ("PUT", "/API/V1/ADMIN/TLS/FIL%45S?x=1", ("PUT /api/v1/admin/tls/files", "use_files")),
            ("put", "/api/v1/admin/tls/acme", ("PUT /api/v1/admin/tls/acme", "use_acme")),
            ("DELETE", "/api/v1/admin/tls/choice", ("DELETE /api/v1/admin/tls/choice", "use_environment")),
        ):
            response = await client.request(
                "GET", "/admin/tls/authorize", content=b'{"key": "a body the route never reads"}',
                headers={"X-Forwarded-Method": method, "X-Forwarded-Uri": uri},
            )
            assert response.status_code == 204, (uri, response.text)
            assert response.headers["X-Librerun-Chosen-By"] == str(platform_admin.id)
            rows = await _tls_rows(db, platform_admin.tenant_id)
            assert rows[-1] == {"surface": "tls", "route": route_name[0], "change": route_name[1]}, rows
        audited = len(await _tls_rows(db, platform_admin.tenant_id))
        # Anything else is not a change: 404, the chooser unnamed, nothing audited.
        for headers in (
            {"X-Forwarded-Method": "GET", "X-Forwarded-Uri": "/api/v1/admin/tls/ca"},
            {"X-Forwarded-Method": "PUT", "X-Forwarded-Uri": "/api/v1/admin/tls/ca/"},
            {"X-Forwarded-Method": "PUT", "X-Forwarded-Uri": "/api/v1/admin/tls"},
            {"X-Forwarded-Method": "PUT"},
            {},
        ):
            response = await client.get("/admin/tls/authorize", headers=headers)
            assert response.status_code == 404 and "X-Librerun-Chosen-By" not in response.headers, headers
        assert len(await _tls_rows(db, platform_admin.tenant_id)) == audited
        await client.aclose()
        assert unguarded is not None
    # The backend's table is edge-control's own (it never imports that package).
    assert edge_tls.CHANGE_ROUTES == CHANGE_ROUTES


# ---------------------------------------------------------------------------
# What the backend adds, and what nothing carries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_root_changed_until_acknowledged(db, platform_admin, monkeypatch):  # noqa: F811
    root_sha = "a" * 64
    raw = {
        "site": [SITE],
        "issuer": {"kind": "internal", "ca": "local"},
        "root": {"ca": "local", "name": "Caddy Local Authority", "sha256": root_sha, "pem": "-----BEGIN CERTIFICATE-----\n"},
        "leaf": None,
        "source": "environment",
        "choice": {"kind": "ca", "by": str(platform_admin.id), "at": "2026-09-30T01:00:00Z"},
        "environment": {"variable": "LIBRERUN_TLS", "ca": None},
        "why": "No choice is recorded.",
        "needs": ["trust_root"],
    }

    async def _read():
        return raw

    monkeypatch.setattr(edge_tls, "read_status", _read)
    await db.execute(text("DELETE FROM app_settings WHERE key = 'tls.last_root'"))

    # No row: the root is to be trusted, but nothing changed.
    status = await edge_tls.status(db, platform_admin)
    assert status["needs"] == ["trust_root"] and status["acknowledged_root"] is None
    assert "pem" not in status["root"], "the status leaves the PEM to root.pem"
    assert status["choice"]["by_email"] == platform_admin.email

    # A different root acknowledged before — a restore that started a new one.
    await db.execute(
        text("INSERT INTO app_settings (key, value) VALUES ('tls.last_root', CAST(:v AS JSONB))"),
        {"v": json.dumps("b" * 64)},
    )
    status = await edge_tls.status(db, platform_admin)
    assert status["needs"] == ["trust_root", "root_changed"]

    # The acknowledgement, through the route: recorded, audited, cleared.
    async with _client(db, platform_admin) as client:
        response = await client.post("/admin/tls/acknowledge")
    assert response.status_code == 200, response.text
    assert response.json()["needs"] == [] and response.json()["acknowledged_root"] == root_sha
    assert await edge_tls.last_root(db) == root_sha
    assert (await _tls_rows(db, platform_admin.tenant_id))[-1] == {
        "surface": "tls", "action": "acknowledge_root", "root": root_sha,
    }

    # The row is no registered setting: no listing shows it and the
    # settings service refuses to write it.
    assert "tls.last_root" not in {spec.key for spec in app_settings_service.list_specs()}
    with pytest.raises(KeyError):
        await app_settings_service.set_setting(db, "tls.last_root", "c" * 64, platform_admin)

    # With no answer from edge-control: edge_off, and nothing to change.
    async def _off():
        return None

    monkeypatch.setattr(edge_tls, "read_status", _off)
    async with _client(db, platform_admin) as client:
        status = (await client.get("/admin/tls")).json()
        assert status["edge"] == "off" and status["needs"] == ["edge_off"]
        assert (await client.post("/admin/tls/acknowledge")).status_code == 409
        assert (await client.get("/admin/tls/root.pem")).status_code == 404


@pytest.mark.asyncio
async def test_no_response_or_log_line_carries_a_key(rig, db, platform_admin, monkeypatch, capsys, caplog):  # noqa: F811
    caplog.set_level("DEBUG")
    ca, ca_key = _ca("Canary CA")
    leaf, leaf_key = _leaf([SITE], ca, ca_key)
    canaries = _canary(_key_pem(ca_key)) + _canary(_key_pem(leaf_key))
    key = _key_pem(ca_key)
    bodies: list[bytes] = []

    with _edge_control(rig.edge, "127.0.0.1") as base:
        monkeypatch.setattr(edge_tls, "EDGE_CONTROL_URL", base)
        sends = [
            ("PUT", "/api/v1/admin/tls/ca", {"certificate": _pem(_ca("Not a CA", ca=False)[0]), "key": key}, None),
            ("PUT", "/api/v1/admin/tls/ca", {"certificate": _pem(_ca("Another CA")[0]), "key": key}, None),
            ("PUT", "/api/v1/admin/tls/ca", None, b'{"certificate": "x", "key": "' + key.encode()),
            ("PUT", "/api/v1/admin/tls/ca", {"certificate": 5, "key": key}, None),
            ("PUT", "/api/v1/admin/tls/ca", {"certificate": _pem(ca), "key": key, "extra": key}, None),
            ("PUT", "/api/v1/admin/tls/files", {"certificate": _pem(leaf), "key": key}, None),
            ("PUT", "/api/v1/admin/tls/files", {"certificate": _pem(leaf), "key": _key_pem(leaf_key)}, None),
            ("PUT", "/api/v1/admin/tls/acme", {"email": canaries[0]}, None),
            ("DELETE", "/api/v1/admin/tls/choice", None, key.encode()),
            ("GET", "/status", None, None),
        ]
        codes = []
        for method, path, payload, raw in sends:
            status, _, data = _request(base, method, path, payload, raw=raw)
            codes.append(status)
            bodies.append(data)
        assert codes == [422, 422, 422, 422, 200, 422, 200, 422, 200, 200], codes

        async with _client(db, platform_admin) as client:
            rig.edge.load_ca(_pem(ca), key)
            for method, path, content in (
                ("GET", "/admin/tls", None),
                ("GET", "/admin/tls/root.pem", None),
                ("POST", "/admin/tls/acknowledge", key.encode()),
                ("GET", "/admin/tls/authorize", key.encode()),
            ):
                response = await client.request(
                    method, path, content=content,
                    headers={"X-Forwarded-Method": "PUT", "X-Forwarded-Uri": "/api/v1/admin/tls/ca"},
                )
                assert response.status_code in (200, 204), (path, response.text)
                bodies.append(response.content)
    # Not vacuous: the key reached edge-control, which wrote it where the
    # edge reads it, and nowhere else in the control volume.
    assert _key_pem(ca_key) in [path.read_text() for path in rig.control.glob("*.key")]
    written = [path.read_text() for path in rig.control.iterdir() if path.is_file() and path.suffix != ".key"]

    captured = capsys.readouterr()
    logs = captured.out + captured.err + caplog.text
    assert "edge_control_request" in logs, "the capture saw no edge-control log line"
    for canary in canaries:
        assert not any(canary.encode() in body for body in bodies), "a response carries a key"
        assert canary not in logs, "a log line carries a key"
        assert not any(canary in text_ for text_ in written), "a control file other than a key carries one"
