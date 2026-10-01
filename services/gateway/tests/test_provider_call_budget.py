"""The provider call never outlives the invocation (Codex round 12, P1).

Three timeouts were already in play — the phase deadline, the step
timeout, and the backend's transport ceiling — and none of them can stop
this process. The transport ceiling closes a *client socket*; it does not
cancel this FastAPI task. The runner's `asyncio.timeout` is in another
process entirely. So a call admitted with four seconds of invocation left
and a 180-second step timeout, or a step declaring no timeout at all,
would run to the step's limit or until the provider stopped — spending
after the phase had ended.

The gateway is the last place that can bound it, and the bound is the run
token's remaining lifetime, which its Redis TTL already carries.

This is decision 62 one layer further out: there the MCP handler was a
separate task the runner could not cancel; here the gateway is a separate
PROCESS, and the same reasoning applies with more force.
"""
from __future__ import annotations

import pytest

from gateway.egress import bounded_timeout


def test_the_step_timeout_wins_while_the_invocation_has_room():
    """The admin's policy is the normal case and must not be eroded."""
    assert bounded_timeout(60, 300.0) == 60


def test_the_invocation_wins_when_it_is_shorter():
    """The case the finding is about: a generous step timeout on a call
    that arrives at the end of a phase."""
    assert bounded_timeout(180, 4.0) == 4.0


def test_a_step_with_no_timeout_is_still_bounded():
    """`timeout_seconds` is optional, and an unset one used to mean the
    provider decided when to stop."""
    assert bounded_timeout(None, 12.0) == 12.0


def test_an_exhausted_invocation_is_not_given_a_floor():
    """This replaces a test that asserted the opposite, and that test was
    pinning a defect.

    It read: "zero or negative is a ValueError at the client, which would
    replace a clean refusal with a traceback about arguments" — true, and
    the wrong remedy. `max(1.0, ...)` granted a spent invocation another
    whole second, in which a fast call could be launched, billed and
    COMPLETE after the deadline the bound exists to enforce (Codex P1).

    The answer to "zero is not a valid timeout" is not to invent a
    timeout. It is not to make the call — see `_within` below."""
    assert bounded_timeout(180, 0.0) == 0.0
    assert bounded_timeout(180, -5.0) == 0.0


def test_a_small_budget_is_passed_through_honestly():
    """Small is not the same as spent. A call with a tenth of a second
    left should fail in a tenth of a second, not be rounded up into a
    second of spending."""
    assert bounded_timeout(180, 0.1) == 0.1


@pytest.mark.asyncio
async def test_an_exhausted_budget_refuses_before_the_provider_is_called():
    """The refusal has to happen BEFORE the operation, not as a timeout
    around it: a fast provider would otherwise answer inside the grace
    and the spend would already have happened."""
    from gateway import egress, errors

    for spent in (0.0, -30.0):
        with pytest.raises(errors.GatewayError) as caught:
            async with egress._within(spent):
                pytest.fail("the provider operation was entered on a spent budget")
        assert caught.value.code == "invocation_deadline"
        assert caught.value.status_code == 504


def test_an_unknown_budget_leaves_the_step_timeout_alone():
    """No TTL is "unknown", not "unlimited" — but inventing a bound from
    nothing would cut off calls the admin allowed. The step's value
    stands, which is the behaviour before this fix."""
    assert bounded_timeout(60, None) == 60
    assert bounded_timeout(None, None) is None


@pytest.mark.parametrize("path", ["chat", "embeddings"])
def test_both_egress_paths_apply_the_bound(path):
    """Round 6's lesson: a value applied on one path and not the other
    agrees by accident until it does not."""
    import inspect

    from gateway import egress

    source = inspect.getsource(egress.embed if path == "embeddings" else egress._call_kwargs)
    assert "bounded_timeout(" in source, (
        f"the {path} path sets the provider timeout without the bound"
    )


def test_the_endpoints_pass_the_principals_budget():
    """The plumbing: a bound nothing supplies is a bound that does not
    exist. Every call into egress must carry the authenticated
    principal's remaining budget."""
    import inspect
    import re

    from gateway import main

    source = inspect.getsource(main)
    calls = re.findall(r"egress\.(complete|open_stream|embed)\(", source)
    assert len(calls) >= 3, f"expected all three egress calls, found {calls}"
    assert source.count("seconds_left=principal.seconds_left") >= 3, (
        "an egress call does not pass the principal's remaining budget"
    )


def test_the_principal_takes_its_budget_from_the_token_ttl():
    """And the budget itself is derived, not invented."""
    import inspect

    from gateway import auth

    source = inspect.getsource(auth.read_run_token)
    assert ".ttl(" in source, "the remaining budget is not read from Redis"
    assert "TOKEN_GRACE_SECONDS" in source, (
        "the grace the runner added to the TTL is not subtracted back off"
    )


# --------------------------------------------------------------------------
# The budget must be a DEADLINE, not a snapshot (Codex round 13, P1)
# --------------------------------------------------------------------------


def test_the_principal_carries_a_deadline_not_a_remaining_figure():
    """A relative figure read at authentication is already stale by the
    time the provider is called: body parsing, step resolution and the
    synchronous Presidio walk all run in between, and that walk measures
    ~2s on a 300-message request. An absolute point does not decay."""
    import time

    from gateway.auth import Principal
    from gateway.snapshots import Snapshot

    principal = Principal(
        agent_id="probe-v1",
        snapshot=Snapshot.__new__(Snapshot),
        deadline_at=time.monotonic() + 10.0,
    )

    first = principal.seconds_left()
    time.sleep(0.05)
    second = principal.seconds_left()

    assert first is not None and second is not None
    assert second < first, "the budget did not decay — it is a snapshot again"
    assert not hasattr(principal, "seconds_left_value")


def test_no_deadline_means_no_bound_rather_than_a_zero_one():
    from gateway.auth import Principal
    from gateway.snapshots import Snapshot

    principal = Principal(agent_id="p", snapshot=Snapshot.__new__(Snapshot))
    assert principal.seconds_left() is None


def test_an_expired_deadline_never_goes_negative():
    import time

    from gateway.auth import Principal
    from gateway.snapshots import Snapshot

    principal = Principal(
        agent_id="p",
        snapshot=Snapshot.__new__(Snapshot),
        deadline_at=time.monotonic() - 30.0,
    )
    assert principal.seconds_left() == 0.0


def test_the_endpoints_read_the_budget_at_the_call_not_at_authentication():
    """`seconds_left` is a method for this reason. Passing the bound
    method's *value* from the top of the handler would reintroduce the
    snapshot with none of the symptoms visible."""
    import inspect

    from gateway import main

    source = inspect.getsource(main)
    assert "seconds_left=principal.seconds_left()" in source, (
        "an endpoint passes the budget without reading it at the call"
    )
    assert "seconds_left=principal.seconds_left," not in source, (
        "an endpoint passes the METHOD where a value is wanted, or a "
        "snapshot where the method is wanted"
    )


def test_the_stream_iteration_re_reads_the_budget():
    """Setting the call up costs time as well, and a stream is read
    after that — so the iteration gets a callable, not the number that
    was true when the request was made."""
    import inspect

    from gateway import egress, main

    assert "remaining=principal.seconds_left" in inspect.getsource(main), (
        "the streaming route does not hand the iteration a way to re-read"
    )
    stream = inspect.getsource(egress.open_stream)
    assert "_within(remaining() if remaining else None)" in stream, (
        "the stream iteration is not inside a gateway-owned wall-clock scope"
    )


def test_the_gateway_owns_a_scope_around_every_provider_operation():
    """`bounded_timeout` hands LiteLLM a number and LiteLLM applies it to
    the REQUEST. Nothing in that covers iterating a stream, or anything
    the library does around the call. This is the scope that does."""
    import inspect

    from gateway import egress

    for name in ("complete", "embed", "open_stream"):
        source = inspect.getsource(getattr(egress, name))
        assert "_within(" in source, f"{name} runs a provider call outside the scope"


@pytest.mark.asyncio
async def test_the_scope_refuses_rather_than_letting_the_call_run_on():
    """And the refusal names the gateway as the cause, rather than
    surfacing as an unexplained cancellation."""
    import asyncio

    from gateway import egress, errors

    with pytest.raises(errors.GatewayError) as caught:
        async with egress._within(1.0):
            await asyncio.sleep(5)

    assert caught.value.code == "invocation_deadline"
    assert caught.value.status_code == 504


@pytest.mark.asyncio
async def test_no_budget_means_the_scope_does_not_interfere():
    from gateway import egress

    async with egress._within(None):
        pass  # no timeout, no refusal


@pytest.mark.asyncio
async def test_the_scope_survives_the_error_translator_wrapped_inside_it():
    """The composition, not just the pieces.

    `_within` sits OUTSIDE `_translating` on every real call path, and
    the scope stops a call by CANCELLING the task. That works only
    because `CancelledError` derives from `BaseException` rather than
    `Exception`, so the translator's `except Exception` does not swallow
    it on the way out.

    That is a load-bearing detail of the standard library, invisible at
    both call sites, and a later "be thorough" widening of the translator
    to `except BaseException` would make the scope silently inert. The
    isolated `_within` test would still pass. This one would not."""
    import asyncio

    from gateway import egress, errors
    from gateway.steps import ResolvedStep

    step = ResolvedStep(step_id="think", provider="openai", model="gpt-4o")

    with pytest.raises(errors.GatewayError) as caught:
        async with egress._within(1.0):
            with egress._translating(step):
                await asyncio.sleep(5)

    assert caught.value.code == "invocation_deadline", (
        "the translator swallowed the cancellation — the scope cannot stop a call"
    )


def test_the_translator_catches_exception_not_baseexception():
    """Stated directly as well, because the test above proves it only
    for a timeout and the reason is general: a translator that catches
    `BaseException` would also swallow the runner's cancellation and the
    process's own shutdown."""
    import inspect

    from gateway import egress

    source = inspect.getsource(egress._translating)
    assert "except Exception" in source
    assert "except BaseException" not in source


# --------------------------------------------------------------------------
# Keyless mode observes the deadline too (Codex round 19, P2)
# --------------------------------------------------------------------------
#
# §12 decision 68 exempted the stub from the deadline scope, reasoning
# that it neither spends nor overruns. True, and not the whole reason
# the scope exists: a spent invocation was REFUSED against a real
# provider and ANSWERED by the stub, so a deadline-sensitive flow passed
# the demo and CI and failed the moment credentials were enabled.
#
# That is decision 72 — "an exemption is a claim about one thing the
# branch skips, not about the branch" — applied to decision 68 by the
# reviewer, one round after I wrote it.


def _stub_step():
    from gateway.steps import ResolvedStep

    return ResolvedStep(step_id="think", provider="stub", model="stub")


def _chat_body():
    return {"model": "librerun/think", "messages": [{"role": "user", "content": "hi"}]}


@pytest.mark.asyncio
async def test_a_spent_invocation_is_refused_in_keyless_mode_too():
    from gateway import egress, errors

    with pytest.raises(errors.GatewayError) as caught:
        await egress.complete(_chat_body(), _stub_step(), scenario=None, seconds_left=0.0)
    assert caught.value.code == "invocation_deadline"
    assert caught.value.status_code == 504


@pytest.mark.asyncio
async def test_the_keyless_embeddings_path_refuses_a_spent_invocation():
    from gateway import egress, errors

    with pytest.raises(errors.GatewayError) as caught:
        await egress.embed({"input": "hi"}, _stub_step(), seconds_left=0.0)
    assert caught.value.code == "invocation_deadline"


@pytest.mark.asyncio
async def test_the_keyless_stream_refuses_a_spent_invocation():
    from gateway import egress, errors

    with pytest.raises(errors.GatewayError) as caught:
        iterator = await egress.open_stream(
            _chat_body(), _stub_step(), scenario=None,
            seconds_left=0.0, remaining=lambda: 0.0,
        )
        async for _chunk in iterator:
            pass
    assert caught.value.code == "invocation_deadline"


@pytest.mark.asyncio
async def test_the_keyless_stream_re_reads_the_budget_at_iteration():
    """Admitted with budget, spent by the time it is read.

    Written after a negative test caught this test file rather than the
    code: the first version passed `seconds_left=0.0`, which the SETUP
    scope refuses, so removing the iteration scope entirely changed
    nothing. The budget decays between admission and the first pull —
    that is the case the iteration scope exists for, and it has to be
    the case the test drives."""
    from gateway import egress, errors

    # Setup reads `seconds_left`; the iteration reads `remaining()`.
    # Five seconds at admission, none left when the response pulls.
    iterator = await egress.open_stream(
        _chat_body(), _stub_step(), scenario=None,
        seconds_left=5.0, remaining=lambda: 0.0,
    )
    with pytest.raises(errors.GatewayError) as caught:
        async for _chunk in iterator:
            pass
    assert caught.value.code == "invocation_deadline"


@pytest.mark.asyncio
async def test_keyless_mode_still_answers_while_there_is_budget():
    """The refusal must not cost keyless mode its job."""
    from gateway import egress

    answer = await egress.complete(
        _chat_body(), _stub_step(), scenario=None, seconds_left=30.0
    )
    assert answer["choices"]


def test_every_stub_branch_is_inside_the_deadline_scope():
    """The structural half, and the same shape as the allowlist guard in
    `test_egress_params.py`: the defect was that a branch RETURNS before
    reaching something, so the guard is about ordering rather than about
    the three paths that happen to exist today."""
    import ast
    import inspect

    from gateway import egress

    tree = ast.parse(inspect.getsource(egress))
    checked = 0
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        stub_tests = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "is_stub"
        ]
        if not stub_tests:
            continue
        for test in stub_tests:
            branch = next(
                (
                    node for node in ast.walk(fn)
                    if isinstance(node, ast.If) and node.test is test
                ),
                None,
            )
            assert branch is not None, f"{fn.name}: is_stub outside an if"
            scoped = [
                n for n in ast.walk(branch)
                if isinstance(n, ast.Call)
                and getattr(n.func, "id", None) in ("_within", "_bounded_sync")
            ]
            assert scoped, (
                f"{fn.name} answers from the stub without going through "
                f"_bounded_sync, so keyless mode accepts an invocation a "
                f"credentialled call refuses"
            )
            checked += 1
    assert checked >= 3, f"expected the three egress paths, found {checked}"


def test_the_stub_helper_both_scopes_and_measures():
    """`_bounded_sync` is the one place the stub's deadline lives, and it
    needs BOTH halves.

    `_within` alone was round 19's fix and half of one: `asyncio.timeout`
    delivers cancellation at an `await`, and the stub is synchronous end
    to end, so a 0.05s scope around 0.4s of work returned after 0.4s. The
    scope still earns its place — it is what refuses a budget already
    spent, before any work — and the elapsed measurement is what catches
    a budget that runs out during (Codex round 20, P2)."""
    import ast
    import inspect

    from gateway import egress

    body = ast.parse(inspect.getsource(egress._bounded_sync).strip())
    called = {
        n.func.id
        for n in ast.walk(body)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    attrs = {
        n.func.attr
        for n in ast.walk(body)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "_within" in called, "_bounded_sync no longer refuses a spent budget"
    assert "monotonic" in attrs, (
        "_bounded_sync no longer measures elapsed time, so a budget that "
        "runs out DURING synchronous stub work is not caught — asyncio "
        "cannot interrupt code that never awaits"
    )


@pytest.mark.asyncio
async def test_a_budget_that_expires_during_stub_work_is_refused(monkeypatch):
    """The half `_within` cannot do, driven rather than asserted about."""
    import time

    from gateway import egress, errors, stub_provider

    real = stub_provider.complete

    def slow(*args, **kwargs):
        time.sleep(0.3)
        return real(*args, **kwargs)

    monkeypatch.setattr(stub_provider, "complete", slow)

    with pytest.raises(errors.GatewayError) as caught:
        await egress.complete(
            _chat_body(), _stub_step(), scenario=None, seconds_left=0.05
        )
    assert caught.value.code == "invocation_deadline"


@pytest.mark.asyncio
async def test_slow_stub_work_inside_the_budget_still_answers(monkeypatch):
    """The measurement must refuse an overrun, not any slow call."""
    import time

    from gateway import egress, stub_provider

    real = stub_provider.complete

    def slow(*args, **kwargs):
        time.sleep(0.1)
        return real(*args, **kwargs)

    monkeypatch.setattr(stub_provider, "complete", slow)

    answer = await egress.complete(
        _chat_body(), _stub_step(), scenario=None, seconds_left=30.0
    )
    assert answer["choices"]
