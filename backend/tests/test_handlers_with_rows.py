"""Three route handlers the backend suite never executed.

Found at S5-C by mutating every route handler's comprehension and then
confirming it with coverage over ``app/routers`` under CI's own
invocation. Both methods agreed, and coverage said something stronger
than the mutation did — it was not the element expressions that went
unevaluated, it was the WHOLE HANDLER BODIES:

    runs.py:116-123   list_runs           MISS
    admin.py:317-318  list_app_settings   MISS
    files.py:61-66    redact_preview      MISS

Two of them had no automated coverage of any kind. ``POST
/files/redact-preview`` has no other test in ``backend/`` and the smoke
never calls it, while the intake page calls it live
(``frontend/src/app/runs/new/page.tsx``) and it is the surface carrying
the platform's PII promise. ``GET /admin/settings`` is the same.
``GET /runs`` was reached only by ``scripts/librerun_smoke.py``.

None was a defect: every helper these bodies call is imported at module
scope, so the code was correct. What was missing is the thing that makes
a correct body STAY correct, and this batch has already shipped the
counter-example — ``_step`` was deleted from ``get_progress`` while
reverting, every run with progress 500'd, and the suite passed 1232
because its only test of that endpoint supplied an EMPTY hash. A
comprehension over an empty collection never evaluates its element
expression, so a test for the absence of something certifies the path it
never enters (§12 207(v), (y)).

Hence the shape of every case here: each handler is driven with a
NON-EMPTY result, and paired with its empty case where one is
meaningful. Recorded as gap H12.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from sqlalchemy.sql.elements import BindParameter, TextClause
from sqlalchemy.sql.expression import ColumnClause
from sqlalchemy.sql.schema import Column
from sqlalchemy.sql.selectable import _OffsetLimitParam

from app.database import get_db
from app.middleware import get_current_user
from app.middleware import require_admin
from app.routers import admin as admin_router
from app.routers import files as files_router
from app.routers import runs as runs_router

# Stage 2 of the redactor is pure regex and always runs; the email in the
# sample below is caught by Presidio when it is installed and by the
# second-pass regex when it is not. Asserting on IP and URL keeps this
# case independent of which stages a given environment has.
_LOG_WITH_PII = (
    "2026-09-18 09:14:51 ERROR auth-api login failed for alice.smith@example.com\n"
    "2026-09-18 09:14:52 WARN  retry from 192.168.14.22 "
    "callback https://vendor.example.com/cb\n"
)
_LOG_WITHOUT_PII = (
    "service started\n"
    "cache warm complete\n"
    "shard rebalance finished\n"
)


def _user(role: str = "admin"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="operator@example.com",
        role=role,
        tenant_id=uuid.uuid4(),
        is_platform_admin=True,
    )


def _client(router, *, db=None, user=None, extra_overrides=None) -> TestClient:
    user = user or _user()
    app = FastAPI()
    app.include_router(router.router)

    async def _db_dep():
        yield db

    async def _user_dep(request: Request):
        request.state.tenant_id = user.tenant_id
        request.state.current_user = user
        request.state.session_id = uuid.uuid4()
        return user

    app.dependency_overrides[get_db] = _db_dep
    app.dependency_overrides[get_current_user] = _user_dep
    for dep, override in (extra_overrides or {}).items():
        app.dependency_overrides[dep] = override
    return TestClient(app)


# --------------------------------------------------------------------------
# POST /files/redact-preview — no test anywhere in the tree before this
# --------------------------------------------------------------------------


def test_redact_preview_returns_an_entry_for_every_redaction():
    """The PII surface, driven with a file that actually contains PII.

    This is the case that never ran. `redactions_applied` is built by a
    comprehension over whatever `redact()` found, so a clean upload
    leaves the element expression unevaluated — which is how an endpoint
    on the platform's PII promise reached this point with no test at all.
    """
    client = _client(files_router)

    r = client.post(
        "/files/redact-preview",
        files={"file": ("auth.log", _LOG_WITH_PII, "text/plain")},
        data={"file_type": "log"},
    )

    assert r.status_code == 200, r.text
    body = r.json()

    entries = body["redactions_applied"]
    assert entries, "the sample contains PII; an empty list means nothing was redacted"

    # Every field the element expression sets, on every entry.
    for e in entries:
        assert e["original_placeholder"], f"entry with no placeholder: {e}"
        assert e["pii_type"], f"entry with no pii_type: {e}"
        assert 0 < e["confidence"] <= 1, f"confidence out of range: {e}"

    kinds = {e["pii_type"] for e in entries}
    assert {"IP", "URL"} <= kinds, (
        f"the regex stages should have caught the IP and the URL; got {sorted(kinds)}"
    )

    # The promise itself, not just the bookkeeping about it.
    assert "192.168.14.22" not in body["redacted_content"]
    assert "https://vendor.example.com/cb" not in body["redacted_content"]
    assert body["original_size_bytes"] == len(_LOG_WITH_PII.encode())


def test_redact_preview_on_a_clean_file_returns_no_entries():
    """The paired empty case.

    On its own this proves very little — it is exactly the shape that let
    `get_progress` ship a 500. It is here because an absence test and a
    presence test are only worth having together: this one says a clean
    file is not mangled, the one above says a dirty file is read.
    """
    client = _client(files_router)

    r = client.post(
        "/files/redact-preview",
        files={"file": ("clean.log", _LOG_WITHOUT_PII, "text/plain")},
        data={"file_type": "log"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["redactions_applied"] == []
    assert body["redacted_content"] == _LOG_WITHOUT_PII
    assert body["original_size_bytes"] == len(_LOG_WITHOUT_PII.encode())


def test_redact_preview_reads_a_file_that_is_not_valid_utf8():
    """The property the dead `except` was trying to provide.

    `redact_preview` used to wrap its decode in `try/except` and fall
    back to latin-1. That branch was **unreachable for a decoding
    failure**, which is what it was written to catch:
    `decode("utf-8", errors="replace")` turns every malformed byte into
    U+FFFD and cannot raise a `UnicodeDecodeError`, so no input could
    reach the fallback that way and no test could cover it — a permanent hole in the coverage report,
    and the same shape as the LangGraph example's `getattr` degradation
    path that could never degrade.

    The `except` is gone. What it was FOR — an upload that is not valid
    UTF-8 must still be read rather than 500 — is a real requirement, so
    it is pinned here instead of left to a branch nothing enters.
    """
    client = _client(files_router)
    raw = (
        b"\xff\xfe2026-09-18 ERROR auth-api login from 10.0.0.9\n"
        b"truncated \xc3 sequence and a lone \xed\xa0\x80 surrogate\n"
    )

    r = client.post(
        "/files/redact-preview",
        files={"file": ("mixed.log", raw, "application/octet-stream")},
        data={"file_type": "log"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["original_size_bytes"] == len(raw)
    assert "\ufffd" in body["redacted_content"], (
        "malformed bytes should survive as the replacement character"
    )
    assert {e["pii_type"] for e in body["redactions_applied"]} >= {"IP"}


# Every way a byte string can fail to be UTF-8, named. The case above
# pins the property for ONE hand-picked payload, which is sampling —
# and the deletion it guards rests on a claim about ARBITRARY bytes, so
# a sampled guard is the wrong shape for it. These are the classes a
# UTF-8 decoder can reject, not a selection of them: a decoder's
# accept/reject decision at any position depends on at most four bytes,
# and each entry below is one of the ways that decision goes wrong.
MALFORMED_UTF8 = {
    "lone start byte": b"\xff\xfe",
    "overlong NUL": b"\xc0\x80",
    "overlong slash": b"\xe0\x80\xaf",
    "lone surrogate": b"\xed\xa0\x80",
    "surrogate pair": b"\xed\xa0\xbd\xed\xb8\x80",
    "above U+10FFFF": b"\xf4\x90\x80\x80",
    "five-byte form": b"\xf8\x88\x80\x80\x80",
    "six-byte form": b"\xfc\x84\x80\x80\x80\x80",
    "truncated sequence": b"\xf0\x9f\x98",
    "lone continuations": b"\x80\xbf" * 32,
    "every byte value": bytes(range(256)),
}


@pytest.mark.parametrize("label", sorted(MALFORMED_UTF8))
def test_redact_preview_survives_every_class_of_malformed_utf8(label):
    """The deleted `except` covered a claim about ALL bytes, so does this.

    `errors="replace"` cannot raise a decoding error — CPython
    resolves the name
    "replace" to an internal fast path before any error-handler lookup,
    and 16,843,008 exhaustive inputs raised nothing (§12 208). But the
    route's promise is not "the decode does not raise"; it is "an upload
    that is not valid UTF-8 is still read rather than 500'd", and that
    promise is about every byte string a client can send.

    So the guard enumerates the CLASSES a decoder rejects rather than a
    payload someone thought of, and each one names itself on failure.
    """
    raw = MALFORMED_UTF8[label] + b"\ncontact 10.0.0.9 for the auth-api log\n"
    client = _client(files_router)

    r = client.post(
        "/files/redact-preview",
        files={"file": ("bytes.log", raw, "application/octet-stream")},
        data={"file_type": "log"},
    )

    assert r.status_code == 200, f"{label}: {r.text}"
    body = r.json()
    assert body["original_size_bytes"] == len(raw), label

    # Every malformed SEQUENCE must SURVIVE as U+FFFD. Not "some
    # replacement character is present" — the exact count Python's own
    # total decode produces. This comment deliberately does NOT say what
    # that count is: CPython replaces per maximal invalid subpart
    # (Unicode TR#36), and three attempts to paraphrase that — "per
    # byte", then "per sequence" — were each wrong. The assertion has
    # been right throughout because it DERIVES the count instead of
    # asserting a remembered rule, which is the whole argument for
    # deriving oracles. The reference is
    # computed here with the correct codec, so it is an oracle and not a
    # tautology: a handler that switched to `errors="ignore"` or dropped
    # high bytes would still return 200 with the readable tail intact and
    # pass every other assertion in this case. Both of those are
    # injections in the negative harness, and omitting THIS assertion is
    # what let them through on the first attempt.
    expected = raw.decode("utf-8", errors="replace").count("\ufffd")
    assert body["redacted_content"].count("\ufffd") == expected, (
        f"{label}: expected {expected} replacement characters, got "
        f"{body['redacted_content'].count(chr(0xFFFD))} — malformed bytes "
        "were discarded rather than represented"
    )

    # …and the readable tail must still be redacted: a handler that gave
    # up on the malformed prefix and returned the input untouched would
    # satisfy a status-code-only assertion.
    assert {e["pii_type"] for e in body["redactions_applied"]} >= {"IP"}, label
    assert "10.0.0.9" not in body["redacted_content"], label


class _UploadDB:
    """The second decode site's db: one run to find, one row to persist."""

    def __init__(self, run):
        self._run = run
        self.added = []

    async def get(self, model, pk):
        return self._run

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


@pytest.mark.parametrize("label", sorted(MALFORMED_UTF8))
def test_upload_run_file_survives_every_class_of_malformed_utf8(
    label, tmp_path, monkeypatch
):
    """The SECOND decode site — the one the sweep missed.

    The `except Exception` fallback was deleted from BOTH handlers, and
    the negative harness even asserts the anchor is unique because the
    dead branch appeared twice. Then eleven cases were written covering
    exactly one of them, and `POST /runs/{id}/files` had no test at all
    beyond an OpenAPI path assertion — so switching THIS decode to
    `errors="strict"` or `"ignore"` would corrupt every uploaded run
    file while all eleven new guards stayed green (Codex P2, round 7).

    I removed the code in two places, and guarded one. Which is the
    round-4 lesson — sweep for the claim's shape, not the cited site —
    failed in the commit whose message says I had applied it.

    The assertion is on what is PERSISTED, not on the response: the
    response carries a path, and a path is not evidence about the bytes
    written to it.
    """
    raw = MALFORMED_UTF8[label] + b"\ncontact 10.0.0.9 for the auth-api log\n"
    user = _user(role="customer")
    run = SimpleNamespace(
        id=uuid.uuid4(), tenant_id=user.tenant_id, user_id=user.id, deleted_at=None
    )
    db = _UploadDB(run)

    # `monkeypatch`, not a hand-rolled try/finally: it is what the rest
    # of this suite uses for settings, and it restores at fixture
    # teardown rather than only on the paths I remembered to wrap. A
    # leaked FILE_STORAGE_PATH points later tests at a deleted tmp dir.
    from app.config import settings

    monkeypatch.setattr(settings, "FILE_STORAGE_PATH", str(tmp_path))

    client = _client(files_router, db=db, user=user)
    r = client.post(
        f"/runs/{run.id}/files",
        files={"file": ("bytes.log", raw, "application/octet-stream")},
        data={"vendor_side": "a", "file_type": "log"},
    )

    assert r.status_code == 201, f"{label}: {r.text}"
    stored = Path(r.json()["storage_path"])
    assert stored.exists(), f"{label}: nothing was written to {stored}"
    written = stored.read_text()

    expected = raw.decode("utf-8", errors="replace").count("\ufffd")
    assert written.count("\ufffd") == expected, (
        f"{label}: the persisted file holds {written.count(chr(0xFFFD))} "
        f"replacement characters, expected {expected} — malformed bytes were "
        "discarded on the way to disk rather than represented"
    )
    assert "10.0.0.9" not in written, (
        f"{label}: an IP reached disk unredacted, which is the platform's "
        "PII promise and not merely a decoding detail"
    )
    assert db.added and db.added[0].pii_redaction_applied is True, label


# --------------------------------------------------------------------------
# GET /runs — reached only by the end-to-end smoke before this
# --------------------------------------------------------------------------


def _run_row(user, *, number: str, title: str):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid.uuid4(),
        run_number=number,
        title=title,
        vendor_a_name="Vendor A",
        vendor_b_name="Vendor B",
        severity="high",
        status="complete",
        created_at=now,
        updated_at=now,
        agent_id="toy-v1",
        user_id=user.id,
        tenant_id=user.tenant_id,
        deleted_at=None,
        user_inputs={"title": title},
    )


class _ListDB:
    """A db whose result set is NOT empty — the whole point of the case.

    It also keeps the statements it was handed. Executing a filter line
    is not the same as applying the filter, and for the non-admin branch
    the difference is access control, so the cases below assert on the
    compiled SQL rather than on the line having run.
    """

    def __init__(self, rows):
        self._rows = rows
        self.statements = []

    def _tree(self, node):
        """The WHERE clause as a nested structure, boolean operators kept.

        Three versions of this came before, each weaker than it read:

        * `"user_id" in str(stmt)` — true on every listing, because
          `user_id` is a SELECTED COLUMN;
        * `"user_id" in str(stmt.whereclause)` — satisfied equally by
          `Run.user_id != user.id` and by a comparison against another
          id, which are the regressions the case exists to catch;
        * a flat BAG of `(column, op, value)` — which discards whether
          the parent node is `and_` or `or_`, so it still passes if
          `_search_clause` flips its `or_` to `and_` (an ordinary search
          would then need the title AND the run number to match), or if
          the ownership equality is OR-ed with an always-true predicate,
          which removes the scoping entirely (Codex P2, round 2).

        Each fix narrowed the previous one instead of changing the KIND
        of evidence, which is why there were three. The structure is what
        the query means, so the structure is what gets read.
        """
        clauses = getattr(node, "clauses", None)
        op = getattr(node, "operator", None)
        if clauses is not None:
            return (getattr(op, "__name__", str(op)), [self._tree(c) for c in clauses])
        left = getattr(node, "left", None)
        right = getattr(node, "right", None)
        if left is not None and op is not None:
            col = getattr(left, "name", None) or str(left)
            value = getattr(right, "value", right)
            if type(value).__name__ == "Null":
                value = None  # so an expected subtree can be written literally
            return (col, getattr(op, "__name__", str(op)), value)
        return ("<opaque>", str(node), None)

    @staticmethod
    def _is_bool_node(n) -> bool:
        return isinstance(n, tuple) and len(n) == 2 and isinstance(n[1], list)

    def _roots(self):
        """One predicate tree per statement — INCLUDING the count query.

        `list_runs` runs `select(count()).select_from(stmt.subquery())`,
        which has no top-level `whereclause` at all: its filters live
        inside the subquery's element. An earlier version of this reader
        therefore skipped the count entirely, so dropping the ownership
        or search filter from the count alone left every guard green
        while production served an unscoped `total` (Codex P2, round 3).
        """
        for clause in self._raw_roots():
            yield self._tree(clause)

    def _raw_roots(self):
        """EVERY predicate in every statement, as SQLAlchemy objects.

        The negative controls read these directly rather than going
        through `_tree`, for the reason in
        `columns_mentioned_per_statement`. This method has now been the
        finding twice, and the two are the same mistake at different
        depths:

        * round 3 — it read only a statement's top-level `whereclause`,
          so the count query, whose filters live in a subquery, was
          never inspected at all;
        * round 8 — it read only the WHERE clause, so scoping moved into
          a JOIN's `onclause` was invisible. `stmt.join(User,
          and_(Run.user_id == User.id, User.id == me))` restricts the
          result while WHERE keeps its ordinary tenant/deleted
          predicates, and the control passed.

        Round 3's fix carried its own second bug, unreported and found
        while fixing round 8: it `continue`d after yielding the
        top-level clause, so the FROM tree was examined **only** when
        there was no WHERE at all. A statement with both kept its join
        hidden either way.

        SQL lets a predicate live in a closed set of places — WHERE,
        HAVING, a JOIN's ON, and, recursively, inside any nested
        selectable — so all four are collected, recursively, with no
        early exit. The enumeration is closed by the grammar rather
        than by what I happened to think of, and
        `unaccounted_binds_per_statement` is the check on THAT claim.
        """
        for predicates in self._predicates_by_statement():
            yield from predicates

    def _predicates_by_statement(self) -> list[list]:
        """The predicates of each statement, grouped BY STATEMENT.

        "The page and the total" is the distinction three assertions in
        this module rest on, and round 8 quietly broke it while fixing
        something else. `_raw_roots` yields one entry per PREDICATE, and
        after round 8 a single statement can contribute several — a
        WHERE and a JOIN's ON. The `*_per_statement` helpers kept
        flattening that, so `len(...) == 2`, whose message reads "the
        page and the total were both inspected", was counting
        PREDICATES. It agreed only because `list_runs` happens to have
        exactly one predicate per query, which is the same accident the
        rest of this module exists to remove.

        Found by re-reading my own diff rather than by a reviewer, and
        only because the round-8 fix changed what a "root" means without
        changing anything that said "statement".
        """
        return [list(self._predicates_of(stmt, set())) for stmt in self.statements]

    def _predicates_of(self, stmt, seen):
        if id(stmt) in seen:
            return
        seen.add(id(stmt))
        where = getattr(stmt, "whereclause", None)
        if where is not None:
            yield where
        for criterion in getattr(stmt, "_having_criteria", ()) or ():
            yield criterion
        for frm in stmt.get_final_froms():
            yield from self._predicates_in_from(frm, seen)

    def _predicates_in_from(self, frm, seen):
        """A FROM entry is a tree: joins nest, and each side may itself
        be a subquery carrying a whole SELECT of its own."""
        if id(frm) in seen:
            return
        seen.add(id(frm))
        onclause = getattr(frm, "onclause", None)
        if onclause is not None:
            yield onclause
        for side in ("left", "right"):
            nested = getattr(frm, side, None)
            if nested is not None:
                yield from self._predicates_in_from(nested, seen)
        element = getattr(frm, "element", None)
        if element is not None and hasattr(element, "get_final_froms"):
            yield from self._predicates_of(element, seen)

    @staticmethod
    def _bind_objects(node) -> dict:
        found: dict = {}
        seen: set = set()

        def walk(n):
            if id(n) in seen:
                return
            seen.add(id(n))
            if isinstance(n, BindParameter):
                found[id(n)] = n
            for child in n.get_children():
                walk(child)

        walk(node)
        return found

    def unaccounted_binds_per_statement(self) -> list[list]:
        """Bound values the statement carries that NO collected
        predicate does — the check on the READER rather than on the
        query.

        Every round so far fixed the shape that had just been found and
        left the next one exactly as blind: `or_` in round 5, nested
        SELECTs in round 6, JOIN `onclause` in round 8. Enumerating
        harder is not a strategy, it is the same move at higher cost.

        This is the version that does not depend on my enumeration being
        complete. A predicate hiding anywhere `_raw_roots` does not look
        still has to bind its value into the compiled statement, so if a
        bind appears in the statement and in none of the collected
        predicates, the reader has missed something — whatever shape it
        turned out to be. Verified against the round-8 defect: with the
        OLD collector, the join's bound user id shows up here in both
        statements, so this check alone would have caught that finding
        without knowing what a JOIN is.

        `LIMIT`/`OFFSET` are bound too and are not predicates. They are
        exempted by their SQLAlchemy type, `_OffsetLimitParam`, rather
        than by matching values — a derived exemption, not a list of the
        numbers this suite happens to use.
        """
        out = []
        for stmt in self.statements:
            everything = self._bind_objects(stmt)
            pagination = {
                k for k, v in everything.items() if isinstance(v, _OffsetLimitParam)
            }
            carried: set = set()
            for predicate in self._predicates_of(stmt, set()):
                carried |= set(self._bind_objects(predicate))
            out.append(
                [everything[k] for k in set(everything) - carried - pagination]
            )
        return out

    def required_per_statement(self) -> list[list]:
        """`required()`, but kept separate per statement.

        The page and the total are two queries, and a filter present in
        one and missing from the other is exactly the regression a merged
        view hides.
        """
        out = []
        for predicates in self._predicates_by_statement():
            leaves: list = []

            def walk(n):
                if self._is_bool_node(n):
                    op, kids = n
                    if op == "and_":
                        for k in kids:
                            walk(k)
                    return
                leaves.append(n)

            for predicate in predicates:
                walk(self._tree(predicate))
            out.append(leaves)
        return out

    def required(self) -> list:
        """Leaves the query actually ENFORCES.

        A predicate constrains the result only if it is reachable from
        the root through `and_`. Anything under an `or_` is optional by
        construction, so it is deliberately NOT collected here — that is
        the whole distinction the flat version threw away.
        """
        out: list = []

        def walk(n):
            if self._is_bool_node(n):
                op, kids = n
                if op == "and_":
                    for k in kids:
                        walk(k)
                return  # an or_ node's children are not required
            out.append(n)

        for root in self._roots():
            walk(root)
        return out

    def columns_mentioned_per_statement(self) -> list[tuple[set, set, bool]]:
        """What each statement's predicate REFERENCES — asked of
        SQLAlchemy, not derived from `_tree`.

        The mirror image of `required()`, and the distinction matters
        because the two kinds of assertion want opposite things:

        * a POSITIVE claim ("the query is scoped to this user") is about
          what the query ENFORCES, so a leaf under an `or_` must not
          count — it constrains nothing. That is `required()`, and it
          reads the tree, because the exact structure is the claim.
        * a NEGATIVE claim ("an admin listing mentions no user at all")
          is about what the query CONTAINS, so nothing may be skipped —
          not an `or_` branch, not a nested SELECT, not an expression
          wrapped in a function call.

        Round 5 fixed the `or_` half of that by walking the tree more
        completely. Round 6 is why that was still the wrong mechanism:
        **`_tree` is a reader I wrote, and it was blind in four separate
        ways at once.** Measured, on the shipped walker:

            Run.id.in_(select(Run.id).where(Run.user_id == uid))
                -> ("id", "in_op", <ScalarSelect>)   # subquery is a VALUE
            exists(select(Run.id).where(Run.user_id == uid))
                -> ("<opaque>", "EXISTS (...)", None)
            func.coalesce(Run.user_id, Run.tenant_id) == uid
                -> ("coalesce", "eq", uid)           # a FICTIONAL column name
            text("runs.user_id = '...'")
                -> ("<opaque>", "...", None)

        Every one of those satisfies "this predicate mentions no
        user_id" about a query scoped by user_id, and `_ListDB` never
        executes the SQL, so nothing downstream disagrees. The third is
        the worst: `getattr(left, "name")` on a function returns the
        FUNCTION's name, so the reader does not report a blank, it
        reports a plausible-looking lie.

        Chasing those four shapes would leave the fifth. So the negative
        controls stop reading my tree and ask SQLAlchemy what the
        expression references, which is total by construction: three of
        the four shapes above are then seen with no special case for any
        of them.

        The fourth, `text()`, has no column objects to find — it is
        opaque SQL. That is reported as opacity rather than as absence,
        because a reader that cannot see into a predicate has not
        established that anything is missing from it, and a control that
        says "absent" when it means "unreadable" is the gate that
        reports success by not looking.

        Returns one `(columns, bound_values, opaque)` per statement.
        """
        out = []
        for predicates in self._predicates_by_statement():
            cols: set = set()
            values: set = set()
            opaque = False
            seen: set = set()

            def walk(node):
                nonlocal opaque
                if id(node) in seen:
                    return
                seen.add(id(node))
                if isinstance(node, TextClause):
                    opaque = True
                elif isinstance(node, Column):
                    # A real mapped column: `.name` IS the column name.
                    if getattr(node, "name", None):
                        cols.add(node.name)
                elif isinstance(node, ColumnClause):
                    # A bare `ColumnClause` is `literal_column(...)`, whose
                    # "name" is arbitrary SQL text, not an identifier:
                    # `literal_column("runs.user_id")` reports
                    # "runs.user_id", which an exact intersection with
                    # {"user_id"} misses, and
                    # `literal_column("coalesce(runs.user_id, 1)")` is
                    # equally legal. So it is OPAQUE, for the same reason
                    # `TextClause` is — normalising the qualified case
                    # would fix one spelling and stay blind to the rest,
                    # which is round 6's mistake with new punctuation
                    # (Codex P2, round 9).
                    opaque = True
                if isinstance(node, BindParameter):
                    try:
                        values.add(node.value)
                    except TypeError:          # an unhashable bound value
                        values.add(repr(node.value))
                for child in node.get_children():
                    walk(child)

            for predicate in predicates:
                walk(predicate)
            out.append((cols, values, opaque))
        return out

    def alternatives_per_statement(self) -> list[list]:
        """`alternatives()`, kept separate per statement.

        The merged version repeated, one assertion over, the very defect
        just fixed for `required()`: dropping the search predicate from
        the count subquery while keeping it on the page query still left
        ONE matching group in the merged list, so the assertion passed
        and `total` could count rows the results do not show. Asserting
        `len(required_per_statement()) == 2` did not help — it proves two
        roots exist, not that both carry the predicate (Codex P2, round
        4).
        """
        out = []
        for predicates in self._predicates_by_statement():
            groups: list = []

            def walk(n):
                if not self._is_bool_node(n):
                    return
                op, kids = n
                if op == "and_":
                    for k in kids:
                        walk(k)
                elif op == "or_":
                    groups.append(n)

            for predicate in predicates:
                walk(self._tree(predicate))
            out.append(groups)
        return out

    def alternatives(self) -> list:
        """Each REQUIRED `or_` node, WITH its nested structure intact.

        The previous version flattened everything below the outer `or_`
        into a bag of leaves, which loses the nested `and_` guarding the
        legacy fallback. Flipping that inner `and_` to an `or_` would
        make every NULL-title run match every search, and the flattened
        version could not tell the difference (Codex P2, round 3).

        So the subtree is returned whole and compared whole. That also
        removes the other half of the same finding: selecting a group
        because SOME leaf mentions the term, then reading column names
        off EVERY leaf regardless of its operator or value, let the title
        comparison become `IS NULL` while the run-number leaf carried the
        term and nothing noticed.
        """
        groups: list = []

        def walk(n):
            if not self._is_bool_node(n):
                return
            op, kids = n
            if op == "and_":
                for k in kids:
                    walk(k)
            elif op == "or_":
                groups.append(n)

        for root in self._roots():
            walk(root)
        return groups

    def required_columns(self) -> set:
        return {leaf[0] for leaf in self.required() if isinstance(leaf, tuple)}

    async def execute(self, stmt):
        self.statements.append(stmt)
        rows = self._rows
        class _R:
            def scalar_one(self_inner):
                return len(rows)

            def scalars(self_inner):
                return self_inner

            def all(self_inner):
                return rows

        return _R()



def _assert_query_never_mentions(db, why, *, columns=frozenset(), values=frozenset()):
    """The ONLY sanctioned way to assert something is ABSENT from a query.

    All three negative controls in this module route through here,
    because all three have now been wrong in the same way twice — round
    5 for `or_`, round 6 for nested SELECTs and function calls — and
    each time I fixed the one Codex cited and had to be told the
    siblings shared it. A shared refusal is the only version a FOURTH
    control cannot forget to copy.

    Two things it refuses to do:

    * **Conclude from an unreadable predicate.** Raw SQL text has no
      column objects to find, so "no `user_id` was found" there means
      "I could not look", and reporting the two the same way is exactly
      the gate that reports success by not looking.
    * **Pass on nothing at all.** A control that inspects zero
      statements is satisfied by construction — the same defect as a
      comprehension over an empty collection certifying the element
      expression it never evaluates, which is what put this whole module
      in the tree (§12 207(v)).
    """
    # The reader is checked before its report is believed: a bound value
    # the statement carries and no collected predicate does means a
    # predicate is hiding somewhere `_raw_roots` does not look, and the
    # absence claim below would be about the part it happened to read.
    for i, stray in enumerate(db.unaccounted_binds_per_statement()):
        assert not stray, (
            f"statement {i} binds {[b.value for b in stray]}, which appears in "
            "NO predicate this reader collected — so a predicate is hiding "
            "somewhere it does not look (a JOIN `ON`, a HAVING, a nested "
            f"SELECT), and it cannot establish that {why}."
        )

    per_statement = db.columns_mentioned_per_statement()
    assert per_statement, (
        f"no predicate was captured at all, so nothing was checked: {why}. "
        "A negative control over zero statements passes by construction."
    )
    labels = ("total", "page")
    for i, (cols, vals, opaque) in enumerate(per_statement):
        which = labels[i] if i < len(labels) else f"statement {i}"
        assert not opaque, (
            f"the {which} query carries raw SQL this reader cannot see into, "
            f"so it cannot establish that {why}. Refusing to report absence "
            "from a predicate it could not read."
        )
        hit = set(columns) & cols
        assert not hit, f"the {which} query references {sorted(hit)} — {why}"
        bound = set(values) & vals
        assert not bound, f"the {which} query binds {sorted(bound)} — {why}"


def test_list_runs_renders_a_row_for_every_run():
    """`_to_summary` runs here, and nowhere else in the suite.

    The existing router fixture returns `all() -> []`, so the row builder
    was never evaluated by any unit test; only `scripts/librerun_smoke.py`
    reached it, by listing runs against a live stack.
    """
    user = _user()
    rows = [
        _run_row(user, number="RUN-1001", title="SSO handshake fails"),
        _run_row(user, number="RUN-1002", title="Catalogue search is slow"),
    ]
    client = _client(runs_router, db=_ListDB(rows), user=user)

    r = client.get("/runs")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert [row["run_number"] for row in body["runs"]] == ["RUN-1001", "RUN-1002"]
    for row, source in zip(body["runs"], rows):
        assert row["id"] == str(source.id)
        assert row["status"] == "complete"
        assert row["severity"] == "high"
        assert row["agent_id"] == "toy-v1"
        assert row["title"]


def test_list_runs_applies_the_search_clause():
    """Codex: this asserted nothing the `if search` block affects.

    `_ListDB.execute` returns its preloaded row whatever statement it is
    handed, so the response was identical whether the block built a
    predicate or was deleted outright — the case raised the coverage
    number and the test count without constraining anything. Covering a
    line is not testing it, which is the whole finding this PR exists to
    fix, committed inside the PR that fixes it.

    The predicate is asserted instead: search matches the title, the run
    number, and the stored inputs of a pre-S2 row whose title is NULL,
    each against the supplied term.
    """
    user = _user()
    db = _ListDB([_run_row(user, number="RUN-1001", title="SSO handshake fails")])
    client = _client(runs_router, db=db, user=user)

    r = client.get("/runs", params={"search": "SSO"})

    assert r.status_code == 200, r.text
    # A search is a DISJUNCTION the query requires: matching any one of
    # title / run number / stored inputs is enough. Asserting it as an
    # or_ group is what makes the case fail if `_search_clause` flips to
    # `and_`, which would quietly require every column to match at once.
    # `Run.title` maps to the legacy DB column `problem_statement` —
    # learned by reading the tree; asserting on "title" would have failed
    # for a reason unrelated to whether search works.
    expected = (
        "or_",
        [
            ("problem_statement", "ilike_op", "%SSO%"),
            ("run_number", "ilike_op", "%SSO%"),
            (
                "and_",
                [
                    ("problem_statement", "is_", None),
                    ("CAST(runs.user_inputs AS TEXT)", "ilike_op", "%SSO%"),
                ],
            ),
        ],
    )
    per_statement = db.alternatives_per_statement()
    assert len(per_statement) == 2, (
        "a listing runs two queries — the page and the total — and the "
        f"search must reach both; saw {len(per_statement)} predicate roots"
    )
    for which, groups in zip(("total", "page"), per_statement):
        assert expected in groups, (
            f"the {which} query must carry the search predicate. It is "
            "compared WHOLE rather than by features pulled out of it, "
            "because every feature-based version so far was satisfied by "
            "a regression: a flattened bag missed `or_` becoming `and_`, "
            "and matching 'some leaf carries the term' missed the title "
            "comparison becoming `IS NULL`. And it is required of BOTH "
            "queries, because a `total` counting rows the results do not "
            "show is the same leak one assertion further on.\n"
            f"expected: {expected}\ngot: {groups}"
        )

    # …and without a term, no search predicate at all — otherwise the
    # assertion above could pass on a clause the handler always adds.
    plain = _ListDB([_run_row(user, number="RUN-1001", title="SSO handshake fails")])
    _client(runs_router, db=plain, user=user).get("/runs")
    _assert_query_never_mentions(
        plain,
        "an unfiltered listing must carry the search term NOWHERE. "
        "Asserting only that no `or_` group exists would miss a term "
        "applied as a bare conjunct",
        values={"%SSO%"},
    )


def test_a_customer_only_sees_their_own_runs():
    """Access control, and the case Codex found twice too weak.

    Two defects in the first version, both mine, both the same shape —
    a test that executes the branch without constraining it:

    * it signed in as `role="member"`, **a role that cannot exist**.
      `schema.sql:53` is `CHECK (role IN ('admin', 'customer'))` and
      `member` appears nowhere in the tree, so a regression scoping only
      `member` users would have passed while every real customer got an
      unscoped listing.
    * it asserted `"user_id" in str(stmt.whereclause)`, which
      `Run.user_id != user.id` and `Run.user_id == someone_else` both
      satisfy — the ownership check and its negation are the same string.

    Now: the real production role, and the predicate read as a tree.
    """
    user = _user(role="customer")
    db = _ListDB([_run_row(user, number="RUN-1001", title="Mine")])
    client = _client(runs_router, db=db, user=user)

    r = client.get("/runs")

    assert r.status_code == 200, r.text
    per_statement = db.required_per_statement()
    assert len(per_statement) == 2, (
        "a listing runs two queries — the page and the total — and both "
        f"must be inspected; saw {len(per_statement)}"
    )
    for which, required in zip(("total", "page"), per_statement):
        # Tenant scoping first, because it is the platform's one
        # mandatory rule (CLAUDE.md) and NEITHER module asserted it
        # until Codex asked for it on the executed one. Both queries
        # must REQUIRE it: a `total` counting another tenant's rows
        # leaks across the boundary as surely as showing them.
        assert ("tenant_id", "eq", user.tenant_id) in required, (
            f"the {which} query must REQUIRE an equality on the caller's "
            f"tenant. Predicates: {required}"
        )
        assert ("user_id", "eq", user.id) in required, (
            f"the {which} query must REQUIRE an equality on the customer's "
            "own id. Present-somewhere is not enough (OR-ed with anything it "
            "scopes nothing), and neither is present-in-one-query: a `total` "
            "counting runs the page will not show is still a leak. "
            f"Predicates: {required}"
        )

    # The control: an admin must NOT carry that restriction, or the
    # assertion above would pass for a reason that has nothing to do with
    # the branch under test.
    admin = _user(role="admin")
    admin_db = _ListDB([_run_row(admin, number="RUN-1002", title="All")])
    _client(runs_router, db=admin_db, user=admin).get("/runs")
    _assert_query_never_mentions(
        admin_db,
        "an admin listing must not be scoped to one user; if it is, the "
        "customer assertion above proves nothing",
        columns={"user_id"},
    )


def test_list_runs_applies_the_status_and_severity_filters():
    """Two ordinary query filters, each previously unexecuted."""
    user = _user()
    db = _ListDB([_run_row(user, number="RUN-1001", title="SSO handshake fails")])
    client = _client(runs_router, db=db, user=user)

    r = client.get("/runs", params={"status": "complete", "severity": "high"})

    assert r.status_code == 200, r.text
    for which, required in zip(("total", "page"), db.required_per_statement()):
        assert ("status", "eq", "complete") in required, (which, required)
        assert ("severity", "eq", "high") in required, (which, required)

    # Without the params, neither predicate is built — so the assertions
    # above cannot be satisfied by a clause the handler always adds.
    plain = _ListDB([_run_row(user, number="RUN-1001", title="x")])
    _client(runs_router, db=plain, user=user).get("/runs")
    _assert_query_never_mentions(
        plain,
        "an unfiltered listing must carry neither filter",
        columns={"status", "severity"},
    )


def test_list_runs_with_no_runs_renders_an_empty_page():
    """The paired empty case — and the one the suite already had."""
    user = _user()
    client = _client(runs_router, db=_ListDB([]), user=user)

    r = client.get("/runs")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["runs"] == []
    assert body["total"] == 0


# --------------------------------------------------------------------------
# GET /settings — no test anywhere in the tree before this
# --------------------------------------------------------------------------


_EDITOR_ID = uuid.uuid4()


def test_list_app_settings_renders_a_row_for_every_setting(monkeypatch):
    """`SettingResponse(...)` runs here, and nowhere else in the suite."""
    now = datetime.now(timezone.utc)
    rows = [
        SimpleNamespace(
            key="LIBRERUN_MAX_PHASE_SECONDS",
            value="3600",
            default_value="3600",
            value_type="int",
            description="Ceiling for a phase deadline",
            is_default=True,
            updated_at=now,
            updated_by=None,
        ),
        SimpleNamespace(
            key="PII_CONFIDENCE_THRESHOLD",
            value="0.7",
            default_value="0.5",
            # `value_type` is a closed Literal on the response model —
            # string_list | int | bool | string — and `updated_by` is the
            # editing user's id, not their email. Both learned by driving
            # the endpoint rather than by reading the ORM row.
            value_type="string",
            description="Minimum confidence before a match is redacted",
            is_default=False,
            updated_at=now,
            updated_by=_EDITOR_ID,
        ),
    ]

    async def _get_all_settings(db):
        return rows

    monkeypatch.setattr(
        admin_router.app_settings_service, "get_all_settings", _get_all_settings
    )

    async def _admin_dep():
        return _user()

    client = _client(
        admin_router,
        db=object(),
        extra_overrides={
            admin_router.require_platform_admin: _admin_dep,
            require_admin: _admin_dep,
        },
    )

    r = client.get("/admin/settings")

    assert r.status_code == 200, r.text
    body = r.json()
    assert [row["key"] for row in body] == [
        "LIBRERUN_MAX_PHASE_SECONDS",
        "PII_CONFIDENCE_THRESHOLD",
    ]
    # is_default is the field the page reads to say "this is a DB override".
    assert [row["is_default"] for row in body] == [True, False]
    assert body[1]["updated_by"] == str(_EDITOR_ID)
    assert body[0]["value"] == "3600"


def test_list_app_settings_with_no_registered_settings_is_empty(monkeypatch):
    """The paired empty case."""

    async def _none(db):
        return []

    monkeypatch.setattr(admin_router.app_settings_service, "get_all_settings", _none)

    async def _admin_dep():
        return _user()

    client = _client(
        admin_router,
        db=object(),
        extra_overrides={
            admin_router.require_platform_admin: _admin_dep,
            require_admin: _admin_dep,
        },
    )

    r = client.get("/admin/settings")

    assert r.status_code == 200, r.text
    assert r.json() == []
