"""Agent keys: the env normalisation, boot reconciliation, and lookup (D10)."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from gateway import keys
from gateway.config import AGENT_KEY_PREFIX, KEY_VALUE_PREFIX


# ----------------------------- the name mapping -----------------------------


@pytest.mark.parametrize(
    "agent_id,variable",
    [
        ("triage-v1", "LIBRERUN_AGENT_KEY_TRIAGE_V1"),
        ("langgraph-triage", "LIBRERUN_AGENT_KEY_LANGGRAPH_TRIAGE"),
        ("echo-v1", "LIBRERUN_AGENT_KEY_ECHO_V1"),
        ("a", "LIBRERUN_AGENT_KEY_A"),
        ("agent9", "LIBRERUN_AGENT_KEY_AGENT9"),
    ],
)
def test_the_normalisation_is_a_bijection(agent_id, variable):
    """Compose variable names admit no hyphens, so the id is normalised —
    and on the manifest charset only ``-`` changes, so the gateway can
    invert it to recover the id exactly."""
    assert keys.env_name_for(agent_id) == variable
    assert keys.agent_id_for(variable) == agent_id


def test_a_variable_that_names_no_valid_agent_is_refused_by_name():
    with pytest.raises(keys.KeyVariableError) as exc:
        keys.agent_id_for(AGENT_KEY_PREFIX + "_LEADING")
    assert AGENT_KEY_PREFIX + "_LEADING" in str(exc.value)
    with pytest.raises(keys.KeyVariableError):
        keys.agent_id_for("OPENAI_API_KEY")


def test_the_rotation_suffix_is_read_as_a_role_not_an_agent():
    parsed = keys.parse_key_variables(
        {
            "LIBRERUN_AGENT_KEY_ECHO_V1": "lr_agent_new",
            "LIBRERUN_AGENT_KEY_ECHO_V1_PREVIOUS": "lr_agent_old",
            "UNRELATED": "x",
        }
    )
    assert {(p.agent_id, p.role, p.value) for p in parsed} == {
        ("echo-v1", "current", "lr_agent_new"),
        ("echo-v1", "previous", "lr_agent_old"),
    }


def test_an_id_that_collides_with_the_rotation_suffix_is_refused():
    """``LIBRERUN_AGENT_KEY_FOO_PREVIOUS`` cannot be both agent
    ``foo-previous``'s key and agent ``foo``'s outgoing one. The reader
    picks the rotation, so both the lone rotation variable and the id
    that can never be provisioned are refused by name."""
    with pytest.raises(keys.KeyVariableError) as exc:
        keys.parse_key_variables({"LIBRERUN_AGENT_KEY_FOO_PREVIOUS": "lr_agent_x"})
    assert "ambiguous" in str(exc.value)
    with pytest.raises(keys.KeyVariableError) as exc:
        keys.env_name_for("foo-previous")
    assert "foo-previous" in str(exc.value)
    # …and with the current key present the pair reads cleanly.
    parsed = keys.parse_key_variables(
        {
            "LIBRERUN_AGENT_KEY_FOO": "lr_agent_new",
            "LIBRERUN_AGENT_KEY_FOO_PREVIOUS": "lr_agent_x",
        }
    )
    assert {p.role for p in parsed} == {"current", "previous"}


def test_two_agents_sharing_a_value_are_refused():
    with pytest.raises(keys.KeyVariableError) as exc:
        keys.parse_key_variables(
            {
                "LIBRERUN_AGENT_KEY_ONE": "lr_agent_same",
                "LIBRERUN_AGENT_KEY_TWO": "lr_agent_same",
            }
        )
    assert "exactly one agent" in str(exc.value)


def test_a_minted_key_is_recognisable_and_random():
    a, b = keys.mint_key(), keys.mint_key()
    assert a.startswith(KEY_VALUE_PREFIX) and b.startswith(KEY_VALUE_PREFIX)
    assert a != b
    assert len(a) > len(KEY_VALUE_PREFIX) + 32
    assert keys.prefix_of(a) == a[len(KEY_VALUE_PREFIX) :][:8]
    # The value itself never becomes the stored hash.
    assert keys.hash_key(a) != a and len(keys.hash_key(a)) == 64


# --------------------------- boot reconciliation ----------------------------


async def _rows(session, agent_id: str) -> list[dict]:
    result = await session.execute(
        text(
            "SELECT key_hash, key_prefix, source, role, previous_since, "
            "previous_until, last_used_at FROM agent_keys "
            "WHERE agent_id = :a ORDER BY role"
        ),
        {"a": agent_id},
    )
    return [dict(r._mapping) for r in result.all()]


@pytest.mark.asyncio
async def test_boot_registers_the_key_the_environment_carries(session, agent_id):
    value = keys.mint_key()
    env = {keys.env_name_for(agent_id): value}

    report = await keys.reconcile_env_keys(session, env)

    assert report.registered == [agent_id]
    rows = await _rows(session, agent_id)
    assert len(rows) == 1
    assert rows[0]["role"] == "current"
    assert rows[0]["source"] == "env"
    assert rows[0]["key_hash"] == keys.hash_key(value)
    assert rows[0]["key_prefix"] == keys.prefix_of(value)
    resolved = await keys.resolve(session, value)
    assert resolved is not None and resolved.agent_id == agent_id


@pytest.mark.asyncio
async def test_a_second_boot_with_the_same_environment_changes_nothing(
    session, agent_id
):
    value = keys.mint_key()
    env = {keys.env_name_for(agent_id): value}
    await keys.reconcile_env_keys(session, env)
    before = await _rows(session, agent_id)

    report = await keys.reconcile_env_keys(session, env)

    assert report.rotated == []
    assert await _rows(session, agent_id) == before


@pytest.mark.asyncio
async def test_replacing_the_value_refuses_the_old_one(session, agent_id):
    """The Accept case: replace a LIBRERUN_AGENT_KEY_* value, recreate the
    gateway, and the old value stops working."""
    old, new = keys.mint_key(), keys.mint_key()
    await keys.reconcile_env_keys(session, {keys.env_name_for(agent_id): old})

    await keys.reconcile_env_keys(session, {keys.env_name_for(agent_id): new})

    assert await keys.resolve(session, old) is None
    assert (await keys.resolve(session, new)).agent_id == agent_id


@pytest.mark.asyncio
async def test_the_previous_variable_keeps_the_old_value_working(session, agent_id):
    """An env key is rotated in env: move the old value to the _PREVIOUS
    line and both work until the line is removed."""
    old, new = keys.mint_key(), keys.mint_key()
    await keys.reconcile_env_keys(session, {keys.env_name_for(agent_id): old})

    await keys.reconcile_env_keys(
        session,
        {
            keys.env_name_for(agent_id): new,
            keys.env_name_for(agent_id) + "_PREVIOUS": old,
        },
    )

    assert (await keys.resolve(session, new)).role == "current"
    assert (await keys.resolve(session, old)).role == "previous"
    rows = {r["role"]: r for r in await _rows(session, agent_id)}
    # An env-backed previous key has no expiry window: removing the line
    # is what ends it, not a clock.
    assert rows["previous"]["previous_until"] is None
    assert rows["previous"]["previous_since"] is not None

    await keys.reconcile_env_keys(session, {keys.env_name_for(agent_id): new})
    assert await keys.resolve(session, old) is None


@pytest.mark.asyncio
async def test_removing_an_agents_line_refuses_its_key(session, agent_id):
    value = keys.mint_key()
    await keys.reconcile_env_keys(session, {keys.env_name_for(agent_id): value})

    await keys.reconcile_env_keys(session, {})

    assert await keys.resolve(session, value) is None
    assert await _rows(session, agent_id) == []


@pytest.mark.asyncio
async def test_taking_over_an_admin_key_is_atomic_and_leaves_it_working(
    session, agent_id
):
    """An env line arriving for an agent whose current key was issued in
    the admin UI must not break the container still holding that key: it
    is demoted to `previous` with the standard window in the same
    transaction that installs the env key."""
    admin_value = keys.mint_key()
    await session.execute(
        text(
            "INSERT INTO agent_keys (agent_id, key_hash, key_prefix, source, role) "
            "VALUES (:a, :h, :p, 'admin', 'current')"
        ),
        {
            "a": agent_id,
            "h": keys.hash_key(admin_value),
            "p": keys.prefix_of(admin_value),
        },
    )
    env_value = keys.mint_key()

    report = await keys.reconcile_env_keys(
        session, {keys.env_name_for(agent_id): env_value}
    )

    assert report.taken_over == [agent_id]
    assert (await keys.resolve(session, env_value)).role == "current"
    demoted = await keys.resolve(session, admin_value)
    assert demoted is not None and demoted.role == "previous"
    rows = {r["role"]: r for r in await _rows(session, agent_id)}
    assert rows["previous"]["source"] == "admin"
    assert rows["previous"]["previous_until"] is not None


@pytest.mark.asyncio
async def test_a_takeover_that_declares_its_own_predecessor_gets_it(
    session, agent_id
):
    """Both lines at once, over an admin-issued key — the shape of a
    first reboot after an operator moves an agent into the environment.

    The current-key takeover clears the `previous` slot before demoting
    the admin key into it, so installing the declared predecessor first
    deleted it one step later: the operator's own `_PREVIOUS` value was
    rejected and the admin key they were replacing stayed usable in its
    place (Codex P2). Declaring a predecessor has to mean getting it.
    """
    admin_value, env_value, declared_previous = (
        keys.mint_key(),
        keys.mint_key(),
        keys.mint_key(),
    )
    await session.execute(
        text(
            "INSERT INTO agent_keys (agent_id, key_hash, key_prefix, source, role) "
            "VALUES (:a, :h, :p, 'admin', 'current')"
        ),
        {
            "a": agent_id,
            "h": keys.hash_key(admin_value),
            "p": keys.prefix_of(admin_value),
        },
    )

    await keys.reconcile_env_keys(
        session,
        {
            keys.env_name_for(agent_id): env_value,
            keys.env_name_for(agent_id) + "_PREVIOUS": declared_previous,
        },
    )

    assert (await keys.resolve(session, env_value)).role == "current"
    declared = await keys.resolve(session, declared_previous)
    assert declared is not None and declared.role == "previous"
    # The admin key it replaced is gone: the environment named what the
    # previous key should be, and a demotion is not a nomination.
    assert await keys.resolve(session, admin_value) is None
    assert len(await _rows(session, agent_id)) == 2


@pytest.mark.asyncio
async def test_a_takeover_with_no_declared_predecessor_still_demotes(
    session, agent_id
):
    """The other side of the same reorder, which must not change: with
    only the current line, the admin key still gets the grace window."""
    admin_value, env_value = keys.mint_key(), keys.mint_key()
    await session.execute(
        text(
            "INSERT INTO agent_keys (agent_id, key_hash, key_prefix, source, role) "
            "VALUES (:a, :h, :p, 'admin', 'current')"
        ),
        {
            "a": agent_id,
            "h": keys.hash_key(admin_value),
            "p": keys.prefix_of(admin_value),
        },
    )

    await keys.reconcile_env_keys(session, {keys.env_name_for(agent_id): env_value})

    demoted = await keys.resolve(session, admin_value)
    assert demoted is not None and demoted.role == "previous"


@pytest.mark.asyncio
async def test_an_expired_previous_key_is_refused(session, agent_id):
    value = keys.mint_key()
    await session.execute(
        text(
            "INSERT INTO agent_keys (agent_id, key_hash, key_prefix, source, role, "
            " previous_since, previous_until) "
            "VALUES (:a, :h, :p, 'admin', 'previous', "
            " NOW() - INTERVAL '2 days', NOW() - INTERVAL '1 day')"
        ),
        {"a": agent_id, "h": keys.hash_key(value), "p": keys.prefix_of(value)},
    )
    assert await keys.resolve(session, value) is None

    await session.execute(
        text(
            "UPDATE agent_keys SET previous_until = NOW() + INTERVAL '1 day' "
            "WHERE key_hash = :h"
        ),
        {"h": keys.hash_key(value)},
    )
    assert (await keys.resolve(session, value)).role == "previous"


@pytest.mark.asyncio
async def test_an_admin_key_the_environment_never_names_is_left_alone(
    session, agent_id
):
    """The environment speaks for env keys only — an admin-issued key for
    another agent must survive every boot."""
    other = f"{agent_id}-other"
    admin_value = keys.mint_key()
    await session.execute(
        text(
            "INSERT INTO agent_keys (agent_id, key_hash, key_prefix, source, role) "
            "VALUES (:a, :h, :p, 'admin', 'current')"
        ),
        {"a": other, "h": keys.hash_key(admin_value), "p": keys.prefix_of(admin_value)},
    )

    await keys.reconcile_env_keys(
        session, {keys.env_name_for(agent_id): keys.mint_key()}
    )

    assert (await keys.resolve(session, admin_value)).agent_id == other


@pytest.mark.asyncio
async def test_a_value_already_issued_to_another_agent_stops_the_boot(
    session, agent_id
):
    """key_hash is unique across the whole table so a presented key maps
    to exactly one agent. The boot says which variable, not which
    constraint."""
    other = f"{agent_id}-other"
    shared = keys.mint_key()
    await session.execute(
        text(
            "INSERT INTO agent_keys (agent_id, key_hash, key_prefix, source, role) "
            "VALUES (:a, :h, :p, 'admin', 'current')"
        ),
        {"a": other, "h": keys.hash_key(shared), "p": keys.prefix_of(shared)},
    )

    with pytest.raises(keys.KeyVariableError) as exc:
        await keys.reconcile_env_keys(session, {keys.env_name_for(agent_id): shared})
    assert keys.env_name_for(agent_id) in str(exc.value)
    assert other in str(exc.value)


@pytest.mark.asyncio
async def test_using_a_key_records_when(session, agent_id):
    value = keys.mint_key()
    await keys.reconcile_env_keys(session, {keys.env_name_for(agent_id): value})
    assert (await _rows(session, agent_id))[0]["last_used_at"] is None

    resolved = await keys.resolve(session, value)
    await keys.touch(session, resolved.key_hash)

    assert (await _rows(session, agent_id))[0]["last_used_at"] is not None
