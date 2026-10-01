"""The gateway declares what it imports (K7-12).

The image installs this directory's lock, ``requirements.lock.txt``,
hashed, and nothing else (``services/gateway/Dockerfile``; A3), and
``requirements.txt`` stays its provenance, so the ``>=`` floors below still
hold there. Until A3, CI's gateway job installed the backend's list beside
it — so a library the gateway reached only through the backend's
requirements passed every test here and failed in the image.
Since K7 the gateway opens its own rows in the secrets store and the
browser's sealed blobs, which is ``cryptography``'s work.
"""
from __future__ import annotations

import re
from pathlib import Path

REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"


def _declared() -> dict[str, str]:
    names = {}
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)(\[[^\]]*\])?\s*(.*)$", line)
        assert match, f"unreadable requirement line: {raw!r}"
        names[match.group(1).lower().replace("_", "-")] = match.group(3)
    return names


def test_the_gateway_declares_cryptography():
    declared = _declared()
    assert "cryptography" in declared, (
        "services/gateway/requirements.txt must name cryptography: the image installs that "
        "file alone, and the gateway's secrets and sealing need it"
    )
    assert declared["cryptography"].startswith(">="), declared["cryptography"]


def test_the_libraries_the_store_and_the_sealing_use_are_what_the_gateway_imports():
    """The modules K7 adds import ``cryptography`` directly or through the
    chassis's keyring, and nothing the list does not name."""
    source = "\n".join(
        (REQUIREMENTS.parent / "gateway" / name).read_text(encoding="utf-8")
        for name in ("sealing.py", "provider_store.py", "rewrap.py")
    )
    assert "from cryptography" in source
    assert "secrets_keyring" in source
