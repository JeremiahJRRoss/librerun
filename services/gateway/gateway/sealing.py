"""Sealing a provider key to the gateway (K7; L33, D15, D34).

A platform admin pastes a provider key into the admin UI; the browser
seals it to this process's public key and the backend stores the blob it
cannot open (``app.services.provider_keys_service``). This module is the
other end, and the only RSA code in LibreRun: the backend imports none of
it, and a test holds it to that (``test_no_rsa_code_in_the_backend``).

The primitive (D15): RSA-OAEP with SHA-256 for both the digest and MGF1,
3072 bits — what WebCrypto and ``cryptography`` both implement natively,
so neither side adds a dependency. A 3072-bit key's OAEP block under
SHA-256 carries at most 384 - 2·32 - 2 = 318 bytes, which is the limit on
a key pasted into the UI on both sides (K7-16): longer credentials — a
Vertex service-account JSON is about 2.3 KB — stay with ``gateway.env``.

The label binds a blob to the provider it was sealed for (K7-04):
``librerun-provider-key:v1:<name>``. A blob sealed for ``openai`` does
not open as ``anthropic``, so a row copied from one name to another is
rejected rather than served as the wrong provider's key.

The fingerprint an operator compares (D34) is ``SHA256:`` and the hex
SHA-256 of the public key's SubjectPublicKeyInfo DER. The gateway logs
it at boot, the admin page shows the one it seals to, doctor prints it,
and ``openssl pkey -pubin -outform DER | sha256sum`` gives the same
digits; no table stores it.

Nothing here logs, and no exception carries a key, a blob or a value.
"""
from __future__ import annotations

import hashlib

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

KEY_BITS = 3072
BLOB_BYTES = KEY_BITS // 8
# One OAEP block: the modulus less two SHA-256 digests and two bytes.
MAX_KEY_BYTES = BLOB_BYTES - 2 * hashlib.sha256().digest_size - 2
LABEL_PREFIX = "librerun-provider-key:v1:"

__all__ = [
    "BLOB_BYTES",
    "KEY_BITS",
    "LABEL_PREFIX",
    "MAX_KEY_BYTES",
    "Unsealable",
    "fingerprint",
    "generate",
    "label",
    "load_private",
    "open_blob",
    "private_pem",
    "public_pem",
    "seal",
]


class Unsealable(ValueError):
    """A blob that does not open to a provider key under this keypair and
    this provider's label. Says why in words, and carries none of the blob."""


def label(name: str) -> bytes:
    """The OAEP label for provider ``name``: what binds a blob to it."""
    return f"{LABEL_PREFIX}{name}".encode("ascii")


def _oaep(name: str) -> padding.OAEP:
    return padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA256()),
        algorithm=hashes.SHA256(),
        label=label(name),
    )


def generate() -> rsa.RSAPrivateKey:
    """A fresh sealing keypair: 3072 bits, the usual public exponent."""
    return rsa.generate_private_key(public_exponent=65537, key_size=KEY_BITS)


def private_pem(key: rsa.RSAPrivateKey) -> str:
    """PKCS#8 PEM, unencrypted: the row that holds it is a Fernet token
    under the gateway's store key, which is the encryption."""
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def load_private(pem: str) -> rsa.RSAPrivateKey:
    """The keypair a row holds. Refuses anything but a 3072-bit RSA key —
    a row that decrypts to something else was not written by this module."""
    try:
        key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    except (ValueError, TypeError, UnicodeEncodeError):
        raise Unsealable("the stored sealing key is not a PEM private key") from None
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != KEY_BITS:
        raise Unsealable(f"the stored sealing key is not a {KEY_BITS}-bit RSA key")
    return key


def public_pem(key: rsa.RSAPrivateKey) -> str:
    """The half the browser seals to, as SubjectPublicKeyInfo PEM — what
    WebCrypto imports as ``spki`` once the armour is stripped."""
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def fingerprint(public_key_pem: str) -> str:
    """``SHA256:<hex>`` over the SubjectPublicKeyInfo DER (D34)."""
    public = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
    der = public.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return "SHA256:" + hashlib.sha256(der).hexdigest()


def _is_key_text(data: bytes) -> bool:
    # Visible ASCII, no whitespace: a provider key is one token, and the
    # browser trims what is pasted before it seals.
    return 1 <= len(data) <= MAX_KEY_BYTES and all(0x21 <= b <= 0x7E for b in data)


def seal(public_key_pem: str, name: str, value: str) -> bytes:
    """What the browser's ``seal()`` does (``frontend/src/lib/sealing.ts``),
    for the tests and the rewrap script's checks. The backend never calls
    it: it has no RSA code at all."""
    data = value.encode("ascii")
    if not _is_key_text(data):
        raise Unsealable(
            f"a provider key is 1 to {MAX_KEY_BYTES} bytes of visible ASCII; "
            f"a longer credential stays in gateway.env"
        )
    public = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
    return public.encrypt(data, _oaep(name))


def open_blob(key: rsa.RSAPrivateKey, name: str, blob: bytes) -> str:
    """The provider key in ``blob``, sealed for ``name`` to this keypair.

    Raises ``Unsealable`` when the blob is not one OAEP block, was sealed
    to another key or for another provider, or does not hold a provider
    key's text: a blob that opens to 400 bytes of anything was not sealed
    by the admin page, whatever else it is.
    """
    blob = bytes(blob)
    if len(blob) != BLOB_BYTES:
        raise Unsealable(f"a sealed provider key is {BLOB_BYTES} bytes; this one is {len(blob)}")
    try:
        data = key.decrypt(blob, _oaep(name))
    except ValueError:
        raise Unsealable(
            "the blob does not open under this sealing key for this provider "
            "(sealed to another key, or for another provider)"
        ) from None
    if not _is_key_text(data):
        raise Unsealable(
            f"the blob opened, but not to a provider key (1 to {MAX_KEY_BYTES} "
            f"bytes of visible ASCII)"
        )
    return data.decode("ascii")
