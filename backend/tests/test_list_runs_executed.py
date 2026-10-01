"""`GET /runs` against a REAL database, where the query actually runs.

Ten rounds of review on this PR went into `test_handlers_with_rows.py`,
which drives the same handler with a fake session and then asserts on a
READING of the SQLAlchemy statement — because that fake never executes
it. Six separate findings were that the reading was wrong: it discarded
`or_`, recorded a subquery as a value, reported a function's name as a
column, never looked in a JOIN's `ON`, and treated `literal_column`'s
qualified name as an identifier. Each was fixed and each was real.

**None of that class could happen here.** `backend-suite` runs with
PostgreSQL 16 and Redis 7 as services and `DATABASE_URL` set, so the
handler's query can simply be executed and the assertion made on the
ROWS IT RETURNS. No defect in a reader can make these pass, because
there is no reader.

**But that is not immunity, and the next two rounds proved it.** Written
at round 11, this docstring first said the module "cannot be fooled".
Round 12 found that every row the fixture seeded shared one
`tenant_id`, so the platform's one mandatory predicate was unobservable
— executing the query buys nothing if the data cannot tell the cases
apart. Round 13 found the tenant loop asserting rows and not `total`,
while this very docstring claimed both were covered.

So the honest statement: running the query removes one family of blind
spots and exposes another. The reader's failures move into the
**fixture** (can the data distinguish the case?) and into **what the
assertions cover** (rows, counts, or only the ones someone remembered).
Leaving the original sentence standing after those two rounds would
have been the same half-revert this file's neighbour was corrected for.

The structural module still earns its place — it names *which*
predicate broke and runs without a database — but it is the fast check,
and this is the ground truth. I should have written this file first.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import get_db
from app.middleware import get_current_user
from app.routers import runs as runs_router

REQUIRE_DB = os.environ.get("LIBRERUN_REQUIRE_DB", "").strip().lower() in {
    "1",
    "true",
    "yes",
}


@pytest_asyncio.fixture
async def db():
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config import settings

    engine = create_async_engine(settings.DATABASE_URL.get_secret_value())
    try:
        async with engine.connect() as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        reason = f"no database at DATABASE_URL ({type(exc).__name__}: {exc})"
        if REQUIRE_DB:
            pytest.fail("LIBRERUN_REQUIRE_DB is set, so this may not skip: " + reason)
        pytest.skip(reason)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(bind=connection)
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def world(db):
    """One tenant, two customers, and a run belonging to each.

    Two users is the whole point: a listing scoped to nobody and a
    listing scoped correctly are the same rows until somebody else's
    run exists to be leaked.
    """
    tenant_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, :n, :s)"),
        {"id": tenant_id, "n": "Probe", "s": f"probe-{tenant_id.hex[:12]}"},
    )

    async def user(role: str) -> uuid.UUID:
        uid = uuid.uuid4()
        await db.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, auth_provider, role)"
                " VALUES (:id, :t, :e, 'google', :r)"
            ),
            {"id": uid, "t": tenant_id, "e": f"{uid.hex[:10]}@example.com", "r": role},
        )
        return uid

    async def run(owner: uuid.UUID, number: str, title: str, **cols) -> uuid.UUID:
        """Seed one run. Extra columns go through `cols`.

        `status` is taken from `cols` like any other column rather than
        hardcoded beside them: hardcoding it meant `run(..., status=...)`
        — the obvious next thing to want, and what the status assertion
        below hints at — emitted `status` twice and produced invalid SQL
        that no current caller triggers. A helper that breaks on its own
        obvious use is a trap for whoever extends this file.
        """
        rid = uuid.uuid4()
        cols = {"status": "submitted", **cols}
        names = "".join(f", {k}" for k in cols)
        binds = "".join(f", :{k}" for k in cols)
        await db.execute(
            text(
                "INSERT INTO runs (id, tenant_id, user_id, run_number,"
                f" problem_statement{names}) VALUES (:id, :t, :u, :n,"
                f" :ps{binds})"
            ),
            {"id": rid, "t": tenant_id, "u": owner, "n": number,
             "ps": title, **cols},
        )
        return rid

    mine = await user("customer")
    theirs = await user("customer")
    admin = await user("admin")
    await run(mine, "RUN-1001", "SSO handshake fails", severity="high")
    await run(theirs, "RUN-1002", "Billing webhook retries", severity="low")
    # A COMPLETE run, owned by `mine`. Two things at once: it exercises
    # the `status=` path of the helper above (which used to emit the
    # column twice), and it pairs the "filtering for complete returns
    # nothing" assertion with one for the presence — a test for an
    # absence alone passes on a filter that returns nothing ever.
    await run(mine, "RUN-1003", "Cert rotation done", severity="low",
              status="complete")

    # A SECOND tenant, with its own admin and its own run. Without it
    # every row in this fixture shares one `tenant_id`, so deleting
    # `Run.tenant_id == tenant_id` from the handler leaves every
    # assertion above green — the admin still sees exactly these rows and
    # ownership still hides the other customer's (Codex P2). Tenant
    # scoping is the platform's one mandatory rule and it was the one
    # predicate this module could not observe.
    #
    # Note for anyone re-running this locally: a dev database with
    # leftover rows makes the deletion *look* caught, because those rows
    # leak in. That is not a guard — it is a pass/fail decided by data
    # the test did not create, and CI's database is fresh.
    other_tenant = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, :n, :s)"),
        {"id": other_tenant, "n": "Other", "s": f"other-{other_tenant.hex[:12]}"},
    )
    outsider = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, auth_provider, role)"
            " VALUES (:id, :t, :e, 'google', 'admin')"
        ),
        {"id": outsider, "t": other_tenant, "e": f"{outsider.hex[:10]}@other.example"},
    )
    await db.execute(
        text(
            "INSERT INTO runs (id, tenant_id, user_id, run_number, status,"
            " problem_statement, severity) VALUES (:id, :t, :u, 'RUN-2001',"
            " 'submitted', :ps, 'high')"
        ),
        {"id": uuid.uuid4(), "t": other_tenant, "u": outsider,
         "ps": "SSO handshake fails for somebody else"},
    )

    await db.flush()
    return {
        "tenant_id": tenant_id,
        "mine": mine,
        "theirs": theirs,
        "admin": admin,
        "other_tenant": other_tenant,
        "outsider": outsider,
    }


def _client(db, *, user_id, tenant_id, role) -> AsyncClient:
    """One event loop for both halves.

    `TestClient` runs the app in its own thread and loop, while the
    session here is bound to an asyncpg connection created in the
    pytest-asyncio loop — and asyncpg refuses to be used from two loops
    ("another operation is in progress"). `ASGITransport` keeps the
    request in this loop, which is the pattern
    `test_h6_transaction_boundary.py` already uses for the same reason.
    """
    app = FastAPI()
    app.include_router(runs_router.router)
    from types import SimpleNamespace

    principal = SimpleNamespace(
        id=user_id, email="probe@example.com", role=role,
        tenant_id=tenant_id, is_platform_admin=(role == "admin"),
    )

    async def _db_dep():
        yield db

    async def _user_dep(request: Request):
        request.state.tenant_id = tenant_id
        request.state.current_user = principal
        request.state.session_id = uuid.uuid4()
        return principal

    app.dependency_overrides[get_db] = _db_dep
    app.dependency_overrides[get_current_user] = _user_dep
    return AsyncClient(transport=ASGITransport(app=app),
                       base_url="http://testserver")


def _numbers(body) -> set:
    return {r["run_number"] for r in body["runs"]}


@pytest.mark.asyncio
async def test_a_customer_listing_does_not_contain_another_customers_run(world, db):
    """The access-control property, EXECUTED.

    The structural module asserts that the statement requires
    `user_id == me`. This asserts that RUN-1002, which belongs to
    somebody else, is not in the response — which is the thing that
    would actually harm a tenant, and which no misreading of a predicate
    can make pass.
    """
    client = _client(db, user_id=world["mine"], tenant_id=world["tenant_id"],
                     role="customer")

    r = await client.get("/runs")

    assert r.status_code == 200, r.text
    body = r.json()
    assert _numbers(body) == {"RUN-1001", "RUN-1003"}, (
        "a customer's listing must contain both their own runs and nothing "
        f"else; got {_numbers(body)}"
    )
    assert body["total"] == 2, (
        "`total` is the number the user is TOLD, and a count that disagrees "
        "with the rows is the same leak one field over — rounds 3 and 4 of "
        f"this PR were about exactly that. Got total={body['total']} with "
        f"{len(body['runs'])} rows"
    )
    assert body["total"] == len(body["runs"]), "the count must match the page"


@pytest.mark.asyncio
async def test_an_admin_listing_does_contain_both_customers_runs(world, db):
    """The paired control, EXECUTED.

    Without this the case above passes on a handler that returns nothing
    at all — and "returns nothing" is indistinguishable from "correctly
    scoped" when only one user's runs exist.
    """
    client = _client(db, user_id=world["admin"], tenant_id=world["tenant_id"],
                     role="admin")

    r = await client.get("/runs")

    assert r.status_code == 200, r.text
    assert _numbers(r.json()) == {"RUN-1001", "RUN-1002", "RUN-1003"}, (
        "an admin must see every customer's runs; if this is scoped, the "
        "customer case above proves nothing"
    )


@pytest.mark.asyncio
async def test_search_returns_only_matching_runs_and_a_total_that_agrees(world, db):
    """Search, EXECUTED — including the count query."""
    client = _client(db, user_id=world["admin"], tenant_id=world["tenant_id"],
                     role="admin")

    r = await client.get("/runs", params={"search": "SSO"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert _numbers(body) == {"RUN-1001"}, _numbers(body)
    assert body["total"] == 1, (
        f"the count query must carry the search too; total={body['total']}"
    )

    wide = await client.get("/runs", params={"search": "RUN-100"})
    assert _numbers(wide.json()) == {"RUN-1001", "RUN-1002", "RUN-1003"}, (
        "search must match the run number as well as the title"
    )


@pytest.mark.asyncio
async def test_the_severity_filter_returns_only_matching_runs(world, db):
    """A filter, EXECUTED, with its paired unfiltered case."""
    client = _client(db, user_id=world["admin"], tenant_id=world["tenant_id"],
                     role="admin")

    high = await client.get("/runs", params={"severity": "high"})
    assert high.status_code == 200, high.text
    assert _numbers(high.json()) == {"RUN-1001"}, _numbers(high.json())
    assert high.json()["total"] == 1

    low = await client.get("/runs", params={"severity": "low"})
    assert _numbers(low.json()) == {"RUN-1002", "RUN-1003"}, _numbers(low.json())

    unfiltered = await client.get("/runs")
    assert _numbers(unfiltered.json()) == {"RUN-1001", "RUN-1002", "RUN-1003"}, (
        "without the filter both runs must come back, or the assertion "
        "above is satisfied by a handler that returns one row regardless"
    )

    # `status` too, and not because it is a different code path — it is
    # the same shape. The harness said so: with only the severity case
    # here, `status-filter-dropped` was caught by the structural module
    # alone, which means this module did not constrain it.
    submitted = await client.get("/runs", params={"status": "submitted"})
    assert _numbers(submitted.json()) == {"RUN-1001", "RUN-1002"}, (
        _numbers(submitted.json())
    )
    complete = await client.get("/runs", params={"status": "complete"})
    assert _numbers(complete.json()) == {"RUN-1003"}, (
        "filtering for complete must return exactly the complete run. "
        "Asserting only that it returns NOTHING would pass on a filter "
        "that returns nothing ever, which is why this fixture now has a "
        f"complete run to find; got {_numbers(complete.json())}"
    )
    assert complete.json()["total"] == 1


@pytest.mark.asyncio
async def test_no_listing_crosses_the_tenant_boundary(world, db):
    """Tenant scoping, EXECUTED — the one rule CLAUDE.md calls mandatory.

    RUN-2001 belongs to another tenant and is deliberately built to slip
    past every OTHER filter in this module: its title contains "SSO", its
    severity is high, its status is submitted. So if the tenant predicate
    goes, it appears in the plain listing, in the search results and in
    the severity results — and in `total` for each.

    The admin case is the one that matters. A customer's listing is also
    scoped by `user_id`, which hides another tenant's rows for a reason
    that has nothing to do with tenancy; the admin has no such accident
    protecting it.
    """
    admin = _client(db, user_id=world["admin"], tenant_id=world["tenant_id"],
                    role="admin")

    plain = await admin.get("/runs")
    assert plain.status_code == 200, plain.text
    assert "RUN-2001" not in _numbers(plain.json()), (
        "an admin must not see another tenant's runs; tenant scoping is "
        f"mandatory (CLAUDE.md). Got {_numbers(plain.json())}"
    )
    assert plain.json()["total"] == 3, (
        "`total` must not count another tenant's rows either — a count "
        f"that crosses the boundary leaks it just as surely. Got "
        f"{plain.json()['total']}"
    )

    # …and it must not arrive through a filter, either — in the rows OR
    # in the count. RUN-2001 matches every one of these on its own
    # merits, and each `expected_total` is this tenant's own answer.
    #
    # Checking only the rows here was a real hole (Codex P2): drop the
    # tenant predicate from the count query ONLY when a status filter is
    # applied, and the page stays correctly scoped while `total` climbs
    # from 2 to 3. Nothing saw it, while this file's own docstring and
    # the CHANGELOG both claimed `total` was protected for every filter.
    # The prose outran the assertions.
    for label, params, expected_total in (
        ("search", {"search": "SSO"}, 1),
        ("severity", {"severity": "high"}, 1),
        ("status", {"status": "submitted"}, 2),
    ):
        r = await admin.get("/runs", params=params)
        assert "RUN-2001" not in _numbers(r.json()), (
            f"the {label} filter must not reach across tenants; got "
            f"{_numbers(r.json())}"
        )
        assert r.json()["total"] == expected_total, (
            f"the {label} filter's COUNT must not reach across tenants "
            "either. A `total` that counts the outsider leaks it just as "
            "surely as listing it — and a count query can lose the tenant "
            f"predicate while the page keeps it. Got {r.json()['total']}, "
            f"expected {expected_total}"
        )

    # The customer side too, for completeness — though ownership scoping
    # would hide it anyway, which is exactly why the admin case above is
    # the real assertion.
    customer = _client(db, user_id=world["mine"], tenant_id=world["tenant_id"],
                       role="customer")
    mine = await customer.get("/runs")
    assert "RUN-2001" not in _numbers(mine.json())
