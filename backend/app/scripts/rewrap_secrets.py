"""Re-encrypt the backend's secrets under the current store key (K6-07; D14, D33).

    python -m app.scripts.rewrap_secrets [--dry-run]

``LIBRERUN_BACKEND_SECRETS_KEY`` is a comma-separated list whose first
entry seals and whose every entry opens. Rotating it is: prepend a new key,
recreate the backend, run this, drop the old key, recreate again
(``docs/platform/Install.md``, "The secrets store key"). This is the third
step: every row still sealed under an older key is opened by whichever
configured key sealed it and re-encrypted under the current one by
``MultiFernet.rotate``.

What it touches, and what it leaves:

* **Its own rows only.** Every scope but ``gateway``: those rows are
  sealed to the gateway's key (K7), which this process neither holds nor
  may try.
* **``key_id`` and ``fingerprint`` are recomputed**, since both are keyed
  digests and change with the key; the admin page's fingerprint changes
  with them, and the value does not.
* **``updated_at`` is kept.** Nobody set a new value, and the page's
  "updated" line says when someone did.
* **One transaction, then one** ``INCR secrets:version``. Each row is
  updated only if it still holds the ciphertext this run read, so a value
  set while the script ran is never overwritten with an older one.
* **Every row is opened, the current key's too.** A row whose ciphertext
  no configured key opens is named whatever its ``key_id`` says, since a
  row damaged in place keeps the id of the key that sealed it.

Nothing it prints or logs carries a value, a ciphertext or a key: a row is
named by its scope, its owner and its name.

Exit codes:
    0  every row the backend owns is sealed under the current key
    2  no usable key: LIBRERUN_BACKEND_SECRETS_KEY is blank or malformed
    3  rows no configured key opens, named on stderr; every other row was
       rewrapped (or, with --dry-run, would have been). Replace or clear
       each in Admin -> Settings — the environment serves meanwhile.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select, update

from app import secrets_keyring as keyring
from app.models.secret import Secret
from app.services import secrets_service


def _label(row) -> str:
    owner = [row.scope]
    if row.tenant_id is not None:
        owner.append(f"tenant {row.tenant_id}")
    if row.agent_id is not None:
        owner.append(f"agent {row.agent_id}")
    return f"{' · '.join(owner)}: {row.name}"


async def rewrap(*, dry_run: bool, session_factory=None, out=None, err=None) -> int:
    """The script, callable: ``session_factory`` is an async context
    manager factory yielding a session (the app's, by default)."""
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        keys = secrets_service.configured_keys()
    except keyring.SecretsStoreKeyInvalid as exc:
        print(f"rewrap: {exc}", file=err)
        return 2
    if not keys:
        print(
            f"rewrap: {secrets_service.KEY_VARIABLE} is blank, so there is no key to "
            f"rewrap to. Prepend the new key to the list, recreate the backend, "
            f"then run this again.",
            file=err,
        )
        return 2
    if session_factory is None:
        from app.database import async_session as session_factory

    current_id = keyring.key_id(keys[0])
    ids = keyring.key_ids(keys)
    rewrapped: list[str] = []
    unreadable: list[str] = []
    moved_on: list[str] = []
    current = 0
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(
                    Secret.id,
                    Secret.scope,
                    Secret.tenant_id,
                    Secret.agent_id,
                    Secret.name,
                    Secret.key_id,
                    Secret.ciphertext,
                )
                .where(Secret.scope != "gateway")
                .order_by(Secret.scope, Secret.name, Secret.id)
            )
        ).all()
        for row in rows:
            if row.key_id == current_id:
                # A key id says which key sealed the row, not that the row
                # still opens: one damaged in place keeps its id. So it is
                # opened here too, the value dropped at once, and a row
                # that does not open is named with the rest rather than
                # counted as current (Codex, K6 round 1).
                try:
                    keyring.unseal(keys, row.ciphertext)
                except keyring.InvalidToken:
                    unreadable.append(_label(row))
                else:
                    current += 1
                continue
            if row.key_id not in ids:
                unreadable.append(_label(row))
                continue
            try:
                sealed = keyring.rotate(keys, row.ciphertext)
            except keyring.InvalidToken:
                unreadable.append(_label(row))
                continue
            if dry_run:
                rewrapped.append(_label(row))
                continue
            result = await db.execute(
                update(Secret)
                .where(Secret.id == row.id, Secret.ciphertext == row.ciphertext)
                .values(
                    ciphertext=sealed.ciphertext,
                    key_id=sealed.key_id,
                    fingerprint=sealed.fingerprint,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount == 1:
                rewrapped.append(_label(row))
            else:
                # Replaced or cleared since the SELECT: a write seals under
                # the current key, so there is nothing left to move.
                moved_on.append(_label(row))
        if rewrapped and not dry_run:
            await db.commit()
    if rewrapped and not dry_run:
        from app.redis import get_redis

        try:
            redis = await get_redis()
            await redis.incr(keyring.VERSION_KEY)
        except Exception as exc:  # noqa: BLE001 — caches expire in 30 s
            print(
                f"rewrap: the rows are rewrapped, but {keyring.VERSION_KEY} could not "
                f"be bumped ({type(exc).__name__}); a backend's cached values expire "
                f"within {int(secrets_service.CACHE_SECONDS)} seconds anyway.",
                file=err,
            )

    verb = "would rewrap" if dry_run else "rewrapped"
    print(
        f"rewrap: {len(rewrapped)} row(s) {verb} under key {current_id}, "
        f"{current} already under it, {len(unreadable)} no configured key opens"
        + (f", {len(moved_on)} changed while this ran" if moved_on else ""),
        file=out,
    )
    for label in rewrapped:
        print(f"  {verb}: {label}", file=out)
    for label in moved_on:
        print(f"  changed meanwhile, left as it is: {label}", file=out)
    if unreadable:
        print(
            "rewrap: no configured key opens these rows — each serves its environment "
            "fallback until it is replaced or cleared in Admin -> Settings:",
            file=err,
        )
        for label in unreadable:
            print(f"  unreadable: {label}", file=err)
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.rewrap_secrets",
        description="Re-encrypt the backend's secrets under the current store key.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="name what would be rewrapped and what no key opens; write nothing",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(dry_run=args.dry_run))


async def _run(*, dry_run: bool) -> int:
    """One process, one loop: close the pool and the Redis client on it,
    so nothing is left for interpreter shutdown to find."""
    from app.database import engine
    from app.redis import close_redis

    try:
        return await rewrap(dry_run=dry_run)
    finally:
        await engine.dispose()
        await close_redis()


if __name__ == "__main__":
    sys.exit(main())
