"""Re-encrypt the gateway's own secrets under its current store key (K7; D33, K7-08).

    python -m gateway.rewrap [--dry-run] [--rotate-keypair] [--discard-unopenable]

The gateway's half of K6's ``python -m app.scripts.rewrap_secrets``, with
K6's rules for the rows this process owns and no other (D33): the
``gateway``-scope rows — the provider keys pasted in the admin UI and the
sealing keypair. ``LIBRERUN_GATEWAY_SECRETS_KEY`` is a comma-separated list
whose first entry seals and whose every entry opens; rotating it is
"prepend a new key, recreate the gateway, run this, drop the old key,
recreate again" (``docs/platform/Install.md``, "The gateway's store key").
In compose: ``docker compose run --rm gateway python -m gateway.rewrap``.

* **Its own rows only.** Every other scope is the backend's, sealed to a
  key this process neither holds nor may try.
* **``key_id`` and ``fingerprint`` are recomputed**, since both change with
  the key; ``updated_at`` is kept. Each row is updated only if it still
  holds the ciphertext this run read, in one transaction, then one ``INCR
  secrets:version``, so every running gateway reloads within two seconds.
* **A blob the refresher has not adopted yet** (``key_id = 'sealed'``) is
  not a Fernet row and is left for the refresher.
* **``--rotate-keypair``** swaps the sealing keypair: a new private half in
  the row and the new public half in ``gateway_status``, one transaction.
  It re-seals nothing — a stored provider key is a Fernet row, not a blob —
  and a blob sealed to the old key that has not been adopted yet is left to
  be ``rejected`` (unsealable), to be pasted again (K7-08).
* **``--discard-unopenable``** deletes, by name, every row no configured key
  opens — the keypair's included, which the gateway then makes afresh at
  its next start, and with it every blob that could only open under it.
  That is the way out of the outage a lost or rotated-away key causes: the
  provider keys that were stored are re-entered in the admin page, or
  served from ``gateway.env`` meanwhile.

Nothing it prints or logs carries a key, a value, a blob or a ciphertext:
a row is named by its name.

Exit codes:
    0  every row the gateway owns is sealed under the current key (or was
       discarded, with --discard-unopenable)
    2  no usable key: LIBRERUN_GATEWAY_SECRETS_KEY is blank, malformed, or
       also the backend's
    3  rows no configured key opens, named on stderr; every other row was
       rewrapped (or, with --dry-run, would have been). The gateway refuses
       to start while its keypair is among them.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import text

from app import secrets_keyring as keyring
from gateway import provider_store, sealing


async def _rows(session) -> list:
    return (
        await session.execute(
            text(
                "SELECT id, name, ciphertext, key_id FROM secrets "
                "WHERE scope = 'gateway' AND tenant_id IS NULL AND agent_id IS NULL "
                "ORDER BY name, id"
            )
        )
    ).all()


def _opens(keys: list[bytes], row) -> str | None:
    """The value, when a configured key opens the row; else ``None``."""
    if row.key_id not in keyring.key_ids(keys):
        return None
    try:
        return keyring.unseal(keys, row.ciphertext)
    except (keyring.InvalidToken, UnicodeDecodeError):
        return None


async def rewrap(
    *,
    dry_run: bool = False,
    rotate_keypair: bool = False,
    discard_unopenable: bool = False,
    session_factory=None,
    out=None,
    err=None,
) -> int:
    """The script, callable: ``session_factory`` is an async context manager
    factory yielding a session (the gateway's, by default)."""
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        keys = provider_store.configured_keys()
    except keyring.SecretsStoreKeyInvalid as exc:
        print(f"rewrap: {exc}", file=err)
        return 2
    if not keys:
        print(
            f"rewrap: {provider_store.KEY_VARIABLE} is blank, so there is no key to rewrap "
            f"to. Prepend the new key to the list, recreate the gateway, then run this again.",
            file=err,
        )
        return 2
    if session_factory is None:
        from gateway import db

        session_factory = db.sessionmaker()

    current_id = keyring.key_id(keys[0])
    rewrapped: list[str] = []
    unopenable: list[str] = []
    discarded: list[str] = []
    moved_on: list[str] = []
    waiting: list[str] = []
    current = 0
    rotated = False
    async with session_factory() as session:
        try:
            await provider_store._refuse_a_shared_key(session, keys)
        except provider_store.BootRefused as exc:
            print(f"rewrap: {exc}", file=err)
            return 2
        rows = await _rows(session)
        keypair_opens = True
        for row in rows:
            if row.key_id == provider_store.SEALED:
                waiting.append(row.name)
                continue
            value = _opens(keys, row)
            if value is None:
                unopenable.append(row.name)
                if row.name == provider_store.KEYPAIR_ROW:
                    keypair_opens = False
                continue
            if row.key_id == current_id:
                current += 1
                continue
            sealed = keyring.rotate(keys, row.ciphertext)
            if dry_run:
                rewrapped.append(row.name)
                continue
            result = await session.execute(
                text(
                    "UPDATE secrets SET ciphertext = :new, key_id = :key_id, "
                    "fingerprint = :fingerprint WHERE id = :id AND ciphertext = :old"
                ),
                {
                    "new": sealed.ciphertext,
                    "key_id": sealed.key_id,
                    "fingerprint": sealed.fingerprint,
                    "id": row.id,
                    "old": row.ciphertext,
                },
            )
            if result.rowcount == 1:
                rewrapped.append(row.name)
            else:
                # Replaced or cleared since the SELECT: nothing to move.
                moved_on.append(row.name)

        if discard_unopenable and unopenable:
            names = list(unopenable)
            if not keypair_opens:
                # Every blob waiting for adoption was sealed to the keypair
                # being discarded, and can never open.
                names += waiting
                waiting = []
            if not dry_run:
                await session.execute(
                    text(
                        "DELETE FROM secrets WHERE scope = 'gateway' AND tenant_id IS NULL "
                        "AND agent_id IS NULL AND name = ANY(:names)"
                    ),
                    {"names": names},
                )
            discarded = names
            unopenable = []

        if rotate_keypair:
            if not keypair_opens and not discarded:
                print(
                    "rewrap: the sealing keypair does not open under the configured key, so "
                    "there is nothing to rotate; --discard-unopenable removes it and the "
                    "gateway makes a new one at its next start.",
                    file=err,
                )
                return 3
            private = sealing.generate()
            stored = keyring.seal(keys, sealing.private_pem(private))
            if not dry_run:
                await session.execute(
                    text(
                        "INSERT INTO secrets (scope, name, ciphertext, key_id, fingerprint) "
                        "VALUES ('gateway', :name, :ciphertext, :key_id, :fingerprint) "
                        "ON CONFLICT ON CONSTRAINT uq_secrets_owner_name DO UPDATE SET "
                        "ciphertext = EXCLUDED.ciphertext, key_id = EXCLUDED.key_id, "
                        "fingerprint = EXCLUDED.fingerprint, updated_at = NOW()"
                    ),
                    {
                        "name": provider_store.KEYPAIR_ROW,
                        "ciphertext": stored.ciphertext,
                        "key_id": stored.key_id,
                        "fingerprint": stored.fingerprint,
                    },
                )
                # The published half moves in the same transaction, so the
                # admin page never seals to a key the row no longer holds.
                await session.execute(
                    text(
                        "UPDATE gateway_status SET public_key_pem = :pem, updated_at = NOW() "
                        "WHERE id = 1"
                    ),
                    {"pem": sealing.public_pem(private)},
                )
            rotated = True

        wrote = not dry_run and (rewrapped or discarded or rotated)
        if wrote:
            await session.commit()

    if wrote:
        bumped = await provider_store._bump_version()
        if bumped is None:
            print(
                f"rewrap: written, but {keyring.VERSION_KEY} could not be bumped; every "
                f"gateway reloads within {int(provider_store.FULL_RELOAD_SECONDS)} seconds anyway.",
                file=err,
            )

    verb = "would rewrap" if dry_run else "rewrapped"
    print(
        f"rewrap: {len(rewrapped)} gateway row(s) {verb} under key {current_id}, "
        f"{current} already under it, {len(unopenable)} no configured key opens"
        + (f", {len(waiting)} waiting for the refresher" if waiting else "")
        + (f", {len(moved_on)} changed while this ran" if moved_on else ""),
        file=out,
    )
    for name in rewrapped:
        print(f"  {verb}: {name}", file=out)
    for name in moved_on:
        print(f"  changed meanwhile, left as it is: {name}", file=out)
    for name in discarded:
        print(f"  {'would discard' if dry_run else 'discarded'}: {name}", file=out)
    if rotated:
        print(
            f"  {'would rotate' if dry_run else 'rotated'}: {provider_store.KEYPAIR_ROW}; "
            f"blobs sealed to the old key and not yet adopted will be rejected",
            file=out,
        )
    if unopenable:
        print(
            "rewrap: no configured key opens these rows. A provider row serves its "
            "gateway.env value until it is replaced or cleared in Admin -> Settings; the "
            "keypair's refuses the gateway's start. --discard-unopenable removes them:",
            file=err,
        )
        for name in unopenable:
            print(f"  unopenable: {name}", file=err)
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gateway.rewrap",
        description="Re-encrypt the gateway's own secrets under its current store key.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="name what would change and what no key opens; write nothing",
    )
    parser.add_argument(
        "--rotate-keypair",
        action="store_true",
        help="replace the sealing keypair; blobs sealed to the old key and not yet "
        "adopted are then rejected",
    )
    parser.add_argument(
        "--discard-unopenable",
        action="store_true",
        help="delete, by name, the gateway's rows no configured key opens",
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        _run(
            dry_run=args.dry_run,
            rotate_keypair=args.rotate_keypair,
            discard_unopenable=args.discard_unopenable,
        )
    )


async def _run(**options) -> int:
    from gateway import db

    try:
        return await rewrap(**options)
    finally:
        await db.dispose()


if __name__ == "__main__":
    sys.exit(main())
