"""Gate S's own gate: `scripts/secret_sweep.py` (K blueprint §3.2).

The sweep is what says "no secret reached any sink". A sweep that read
nothing would say exactly the same thing, which is why the script has
three devices against that — every sink must yield bytes, a planted
positive control must be found, and the report is re-verified by a
second run — and why those devices are tested HERE, in a suite with no
containers, rather than only in the smoke job that needs a booted stack.

The smoke job's twelve negative doubles inject a real leak into a real
sink, or hand the verifier a report it must refuse. These tests are the other half: they check the detector and the
floors directly, including the cases a CI run would never reach on
purpose.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "secret_sweep.py"


def _load():
    spec = importlib.util.spec_from_file_location("librerun_secret_sweep", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sweep_mod = _load()
CANARY = "CANARY-APP_SECRET_KEY-0123456789abcdef"


def _report(**overrides) -> dict:
    report = {
        "sinks": [
            {"name": "api-responses", "bytes": 4096, "note": "12 responses"},
            {"name": "file-tree:./data", "bytes": 2048, "note": "9 files"},
            {"name": "sql:app_settings", "bytes": 256, "note": "psql (rc=0)"},
        ],
        "canaries": ["APP_SECRET_KEY"],
        "findings": [],
        "marker_found_in": ["api-responses", "sql:app_settings"],
    }
    report.update(overrides)
    return report


# --------------------------------------------------------------------------
# The detector
# --------------------------------------------------------------------------


def test_a_canary_in_a_sink_is_found_and_named():
    sinks = [
        sweep_mod.Sink("container-logs:backend", f"some log line {CANARY} here"),
        sweep_mod.Sink("file-tree:./data", "nothing to see"),
    ]

    report = sweep_mod.sweep(sinks, {"APP_SECRET_KEY": CANARY}, "")

    assert report["findings"] == [
        {"canary": "APP_SECRET_KEY", "sink": "container-logs:backend"}
    ]


def test_the_report_never_carries_the_value():
    """The report is uploaded as a CI artifact. A finding that quoted the
    secret would put it somewhere more durable than the log it leaked
    into (L31)."""
    sinks = [sweep_mod.Sink("container-logs:backend", f"leak: {CANARY}")]

    report = sweep_mod.sweep(sinks, {"APP_SECRET_KEY": CANARY}, "")

    assert CANARY not in json.dumps(report)


def test_a_clean_sweep_finds_nothing():
    sinks = [sweep_mod.Sink("container-logs:backend", "boot ok\nlistening\n")]

    report = sweep_mod.sweep(sinks, {"APP_SECRET_KEY": CANARY}, "")

    assert report["findings"] == []


def test_the_marker_is_recorded_per_sink():
    marker = "http://localhost:16686/librerun-sweep-marker-1"
    sinks = [
        sweep_mod.Sink("api-responses", f"...{marker}..."),
        sweep_mod.Sink("sql:app_settings", f"trace_viewer_base_url|{marker}"),
        sweep_mod.Sink("container-logs:redis", "PONG"),
    ]

    report = sweep_mod.sweep(sinks, {"APP_SECRET_KEY": CANARY}, marker)

    assert report["marker_found_in"] == ["api-responses", "sql:app_settings"]


# --------------------------------------------------------------------------
# …and the floors, which are what stop "found nothing" meaning "looked
# at nothing"
# --------------------------------------------------------------------------


def test_a_good_report_verifies():
    sweep_mod.verify(_report(), expect_sinks=3, min_bytes=1, marker_sinks=2)


def test_an_empty_sink_is_refused():
    report = _report(
        sinks=[
            {"name": "container-logs:backend", "bytes": 0, "note": "rc=1"},
            {"name": "file-tree:./data", "bytes": 2048, "note": "9 files"},
            {"name": "sql:app_settings", "bytes": 256, "note": "psql"},
        ]
    )

    with pytest.raises(sweep_mod.SweepFailure) as raised:
        sweep_mod.verify(report, expect_sinks=3, min_bytes=1, marker_sinks=2)

    assert "container-logs:backend" in str(raised.value)


def test_too_few_sinks_is_refused():
    with pytest.raises(sweep_mod.SweepFailure) as raised:
        sweep_mod.verify(_report(), expect_sinks=15, min_bytes=1, marker_sinks=2)

    assert "15" in str(raised.value)


def test_a_missing_positive_control_is_refused():
    """The device that matters most: if the sweep cannot find a value it
    planted itself, "no canaries found" is not evidence of anything."""
    with pytest.raises(sweep_mod.SweepFailure) as raised:
        sweep_mod.verify(
            _report(marker_found_in=[]), expect_sinks=3, min_bytes=1, marker_sinks=2
        )

    assert "positive control" in str(raised.value)


def test_findings_fail_verification_and_are_named():
    report = _report(
        findings=[{"canary": "APP_SECRET_KEY", "sink": "container-logs:backend"}]
    )

    with pytest.raises(sweep_mod.SweepFailure) as raised:
        sweep_mod.verify(report, expect_sinks=3, min_bytes=1, marker_sinks=2)

    message = str(raised.value)
    assert "APP_SECRET_KEY" in message and "container-logs:backend" in message


# --------------------------------------------------------------------------
# Canary hygiene and the file-tree sink
# --------------------------------------------------------------------------


def test_a_short_canary_is_refused(tmp_path):
    path = tmp_path / "canaries.json"
    path.write_text(json.dumps({"APP_SECRET_KEY": "short"}))

    with pytest.raises(sweep_mod.SweepFailure):
        sweep_mod.load_canaries(str(path))


def test_an_empty_canary_file_is_refused(tmp_path):
    path = tmp_path / "canaries.json"
    path.write_text("{}")

    with pytest.raises(sweep_mod.SweepFailure):
        sweep_mod.load_canaries(str(path))


def test_the_file_tree_sink_reads_every_file(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "backend.jsonl").write_text(f'{{"msg": "{CANARY}"}}\n')
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "report.html").write_text("<p>clean</p>")

    sink = sweep_mod.sink_file_tree(str(tmp_path), skip=set())

    assert CANARY in sink.text
    assert "clean" in sink.text
    assert sink.bytes_read > 0


def test_the_file_tree_sink_skips_the_sweeps_own_files(tmp_path):
    """The canary list lives on the same disk. Reading it back and
    calling it a leak would be a gate that can only fail, which is no
    more useful than one that can only pass."""
    canaries = tmp_path / "canaries.json"
    canaries.write_text(json.dumps({"APP_SECRET_KEY": CANARY}))

    sink = sweep_mod.sink_file_tree(str(tmp_path), skip={str(canaries.resolve())})

    assert CANARY not in sink.text


# --------------------------------------------------------------------------
# The command line, as the workflow runs it
# --------------------------------------------------------------------------


def _run(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_verify_report_accepts_a_good_report(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_report()))

    done = _run("--verify-report", str(path), "--expect-sinks", "3", "--marker-sinks", "2")

    assert done.returncode == 0, done.stderr


def test_verify_report_refuses_a_report_that_read_nothing(tmp_path):
    """The negative probe for the step the smoke job runs after the
    sweep: a report with no sinks in it must not pass."""
    path = tmp_path / "report.json"
    path.write_text(
        json.dumps({"sinks": [], "findings": [], "marker_found_in": [], "canaries": []})
    )

    done = _run("--verify-report", str(path), "--expect-sinks", "15", "--marker-sinks", "3")

    assert done.returncode == 1
    assert "sink" in done.stderr


def test_the_script_refuses_to_sweep_with_no_canaries():
    done = _run("--report", "/dev/null")

    assert done.returncode == 1
    assert "--canaries" in done.stderr


def test_a_sweep_with_only_a_file_tree_finds_a_planted_canary(tmp_path):
    """End to end through the command line, no containers: plant, sweep,
    and require a non-zero exit."""
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    (data / "logs" / "backend.jsonl").write_text(f"leaked {CANARY}\n")
    canaries = tmp_path / "canaries.json"
    canaries.write_text(json.dumps({"APP_SECRET_KEY": CANARY}))

    done = _run(
        "--canaries", str(canaries),
        "--report", str(tmp_path / "report.json"),
        "--data-dir", str(data),
        "--expect-sinks", "1",
    )

    assert done.returncode == 1
    assert "APP_SECRET_KEY" in done.stderr
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["findings"][0]["canary"] == "APP_SECRET_KEY"
    assert CANARY not in json.dumps(report)


def test_the_same_sweep_passes_when_the_tree_is_clean(tmp_path):
    """The positive control for the test above — without it, a sweep that
    always failed would look just as good."""
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    (data / "logs" / "backend.jsonl").write_text("nothing here\n")
    canaries = tmp_path / "canaries.json"
    canaries.write_text(json.dumps({"APP_SECRET_KEY": CANARY}))

    done = _run(
        "--canaries", str(canaries),
        "--report", str(tmp_path / "report.json"),
        "--data-dir", str(data),
        "--expect-sinks", "1",
    )

    assert done.returncode == 0, done.stderr + done.stdout


# --------------------------------------------------------------------------
# K6: a secret set through the settings API, and the secrets table
# --------------------------------------------------------------------------

FINGERPRINT = "0123456789ab"


def _planted(found_in) -> dict:
    return {
        "setting": "auth.azure_client_secret",
        "slot": "K6_SETTING_SECRET",
        "fingerprint": FINGERPRINT,
        "found_in": found_in,
    }


def test_unfound_fingerprint_refused():
    """The planted secret's fingerprint is the proof the sweep read the
    row: a report where no sql: sink shows it is refused, whatever else
    shows it — the API echoes it too, and that proves nothing about the
    table."""
    for found_in in ([], ["api-responses"], ["api-responses", "file-tree:./data"]):
        with pytest.raises(sweep_mod.SweepFailure) as raised:
            sweep_mod.verify(
                _report(planted=[_planted(found_in)]),
                expect_sinks=3, min_bytes=1, marker_sinks=2, expect_planted=1,
            )
        assert "auth.azure_client_secret" in str(raised.value) and "sql:" in str(raised.value)

    sweep_mod.verify(
        _report(planted=[_planted(["api-responses", "sql:secrets"])]),
        expect_sinks=3, min_bytes=1, marker_sinks=2, expect_planted=1,
    )

    # A report that planted nothing where one was asked for is refused too.
    with pytest.raises(sweep_mod.SweepFailure, match="planted through the settings API"):
        sweep_mod.verify(_report(), expect_sinks=3, min_bytes=1, marker_sinks=2, expect_planted=1)


def test_the_sweep_records_where_the_fingerprint_was_seen():
    sinks = [
        sweep_mod.Sink("api-responses", f'{{"fingerprint": "{FINGERPRINT}"}}'),
        sweep_mod.Sink("sql:secrets", f"platform|auth.azure_client_secret|k|{FINGERPRINT}|gAAAAA"),
        sweep_mod.Sink("sql:users", "nothing"),
    ]
    planted = [{"setting": "auth.azure_client_secret", "slot": "K6_SETTING_SECRET", "fingerprint": FINGERPRINT}]

    report = sweep_mod.sweep(sinks, {"K6_SETTING_SECRET": CANARY}, "", planted)

    assert report["planted"][0]["found_in"] == ["api-responses", "sql:secrets"]
    assert report["findings"] == []
    assert CANARY not in json.dumps(report)


def test_a_secret_setting_is_planted_and_its_answer_is_checked(monkeypatch):
    """PUT the canary; require 200, "value": null and a fingerprint — a
    response carrying the value, or none, or a refusal, stops the sweep."""
    answers = {
        "ok": (200, json.dumps({"value": None, "secret": {"fingerprint": FINGERPRINT}})),
        "echoed": (200, json.dumps({"value": CANARY, "secret": {"fingerprint": FINGERPRINT}})),
        "no fingerprint": (200, json.dumps({"value": None, "secret": {"fingerprint": None}})),
        "unconfigured": (503, json.dumps({"code": "secrets_store_unconfigured"})),
    }
    sent, recorded = [], []

    for label, answer in answers.items():
        monkeypatch.setattr(
            sweep_mod, "_http",
            lambda method, url, token=None, body=None, answer=answer: sent.append((method, url, body)) or answer,
        )
        record = lambda *entry: recorded.append(entry)  # noqa: E731
        if label == "ok":
            entry = sweep_mod.plant_secret_setting(
                "http://x", "tok", "auth.azure_client_secret", "K6_SETTING_SECRET", CANARY, record
            )
            assert entry == {"setting": "auth.azure_client_secret", "slot": "K6_SETTING_SECRET", "fingerprint": FINGERPRINT}
            assert sent[-1] == ("PUT", "http://x/api/v1/admin/settings/auth.azure_client_secret", {"value": CANARY})
        else:
            with pytest.raises(sweep_mod.SweepFailure):
                sweep_mod.plant_secret_setting(
                    "http://x", "tok", "auth.azure_client_secret", "K6_SETTING_SECRET", CANARY, record
                )


def test_a_secret_setting_needs_its_slot_and_the_api(tmp_path):
    canaries = tmp_path / "canaries.json"
    canaries.write_text(json.dumps({"APP_SECRET_KEY": CANARY}))
    done = _run(
        "--canaries", str(canaries), "--report", str(tmp_path / "r.json"),
        "--secret-setting", "auth.azure_client_secret:K6_SETTING_SECRET",
        "--data-dir", str(tmp_path), "--expect-sinks", "1",
    )
    assert done.returncode == 1 and "--api-url" in done.stderr


def test_sweep_is_stdlib_only():
    """It runs on a bare CI runner beside the stack: every import must be
    the standard library's, K6's additions included."""
    import ast

    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    imported.discard("__future__")
    assert imported, "the scan found no import at all — it is not reading the script"
    assert imported <= set(sys.stdlib_module_names), sorted(imported - set(sys.stdlib_module_names))


# --------------------------------------------------------------------------
# K7: a provider key sealed to the gateway, and gateway_status
# --------------------------------------------------------------------------

SEALED_FINGERPRINT = "f00dfacecafe"


def _sealed(found_in) -> dict:
    return {"provider": "anthropic", "file": "sealed.b64", "fingerprint": SEALED_FINGERPRINT,
            "found_in": found_in}


def test_sealed_fingerprint_must_be_found():
    """The gateway fingerprints a sealed key once it adopts it, and writes
    the fingerprint into gateway_status: a report where sql:gateway_status
    does not show it is refused, whatever else shows it — the API echoes
    it, and sql:secrets holds a fingerprint of its own."""
    for found_in in ([], ["api-responses"], ["api-responses", "sql:secrets"]):
        with pytest.raises(sweep_mod.SweepFailure) as raised:
            sweep_mod.verify(
                _report(sealed=[_sealed(found_in)]),
                expect_sinks=3, min_bytes=1, marker_sinks=2, expect_sealed=1,
            )
        assert "anthropic" in str(raised.value) and "sql:gateway_status" in str(raised.value)

    sweep_mod.verify(
        _report(sealed=[_sealed(["api-responses", "sql:gateway_status"])]),
        expect_sinks=3, min_bytes=1, marker_sinks=2, expect_sealed=1,
    )
    assert sweep_mod._require_fingerprint(
        _report(sealed=[_sealed(["sql:gateway_status"])]), "sql:gateway_status", SEALED_FINGERPRINT
    ) is None

    # A report that sealed nothing where one was asked for is refused too.
    with pytest.raises(sweep_mod.SweepFailure, match="sealed and adopted"):
        sweep_mod.verify(_report(), expect_sinks=3, min_bytes=1, marker_sinks=2, expect_sealed=1)


def test_the_sweep_records_where_the_sealed_fingerprint_was_seen():
    sinks = [
        sweep_mod.Sink("api-responses", f'{{"fingerprint": "{SEALED_FINGERPRINT}"}}'),
        sweep_mod.Sink("sql:gateway_status", f'1.0.0|f|[{{"fingerprint": "{SEALED_FINGERPRINT}"}}]|PEM'),
        sweep_mod.Sink("sql:users", "nothing"),
    ]
    sealed = [{"provider": "anthropic", "file": "sealed.b64", "fingerprint": SEALED_FINGERPRINT}]

    report = sweep_mod.sweep(sinks, {"K7_SEALED_PROVIDER_KEY": CANARY}, "", None, sealed)

    assert report["sealed"][0]["found_in"] == ["api-responses", "sql:gateway_status"]
    assert report["findings"] == []


# --------------------------------------------------------------------------
# K8a: a tool secret set for a tenant through the agents API
# --------------------------------------------------------------------------

TOOL_FINGERPRINT = "7001c0ffee42"


def _tool(found_in) -> dict:
    return {"agent": "vita-v1", "name": "tavily_api_key", "slot": "K8_TOOL_SECRET",
            "fingerprint": TOOL_FINGERPRINT, "found_in": found_in}


def test_a_tool_secret_is_planted_and_its_answer_is_checked(monkeypatch):
    """PUT the canary as this tenant's value; require 200, no value field
    and the tenant row's fingerprint — anything else stops the sweep."""
    answers = {
        "ok": (200, json.dumps({"name": "tavily_api_key", "tenant": {"fingerprint": TOOL_FINGERPRINT}})),
        "echoed": (200, json.dumps({"value": CANARY, "tenant": {"fingerprint": TOOL_FINGERPRINT}})),
        "no fingerprint": (200, json.dumps({"tenant": {"fingerprint": None}})),
        "undeclared": (404, json.dumps({"code": "secret_not_declared"})),
    }
    sent = []
    for label, answer in answers.items():
        monkeypatch.setattr(
            sweep_mod, "_http",
            lambda method, url, token=None, body=None, answer=answer: sent.append((method, url, body)) or answer,
        )
        record = lambda *entry: None  # noqa: E731
        if label == "ok":
            entry = sweep_mod.plant_tool_secret(
                "http://x", "tok", "vita-v1", "tavily_api_key", "K8_TOOL_SECRET", CANARY, record
            )
            assert entry == {"agent": "vita-v1", "name": "tavily_api_key", "slot": "K8_TOOL_SECRET",
                             "fingerprint": TOOL_FINGERPRINT}
            assert sent[-1] == (
                "PUT", "http://x/api/v1/agents/vita-v1/secrets/tenant/tavily_api_key", {"value": CANARY}
            )
        else:
            with pytest.raises(sweep_mod.SweepFailure):
                sweep_mod.plant_tool_secret(
                    "http://x", "tok", "vita-v1", "tavily_api_key", "K8_TOOL_SECRET", CANARY, record
                )


def test_a_tool_secrets_fingerprint_must_be_in_sql_secrets():
    """The row the PUT wrote must have been read: its fingerprint in
    sql:secrets, whatever else shows it."""
    for found_in in ([], ["api-responses"], ["api-responses", "sql:gateway_status"]):
        with pytest.raises(sweep_mod.SweepFailure) as raised:
            sweep_mod.verify(
                _report(tool_secrets=[_tool(found_in)]),
                expect_sinks=3, min_bytes=1, marker_sinks=2, expect_tool_secrets=1,
            )
        assert "tavily_api_key" in str(raised.value) and "sql:secrets" in str(raised.value)
    sweep_mod.verify(
        _report(tool_secrets=[_tool(["api-responses", "sql:secrets"])]),
        expect_sinks=3, min_bytes=1, marker_sinks=2, expect_tool_secrets=1,
    )
    with pytest.raises(sweep_mod.SweepFailure, match="tool secret"):
        sweep_mod.verify(_report(), expect_sinks=3, min_bytes=1, marker_sinks=2, expect_tool_secrets=1)

    sinks = [sweep_mod.Sink("sql:secrets", f"tenant|tavily_api_key|k|{TOOL_FINGERPRINT}|gAAAAA")]
    report = sweep_mod.sweep(sinks, {"K8_TOOL_SECRET": CANARY}, "", None, None,
                             [{"agent": "vita-v1", "name": "tavily_api_key", "slot": "K8_TOOL_SECRET",
                               "fingerprint": TOOL_FINGERPRINT}])
    assert report["tool_secrets"][0]["found_in"] == ["sql:secrets"]


def test_a_run_slot_that_never_fetched_is_refused(monkeypatch):
    """K8b: a run that fetches its tool secret over MCP is a slot, and a
    stamp the run itself moved — the planted row's ``last_used_at``, later
    than before the run, stamped when ``secret_get`` answers — is its
    proof. No stamp, a stamp an earlier fetch left (a replaced value keeps
    its row's stamp: Codex on #186), a slot that recorded nothing before
    the run, a run that did not complete, or fewer slots than the floor is
    refused; the slot never carries the run's output; and both stamps are
    read from the planted tenant row itself."""
    good = {"agent": "echo-v1", "name": "echo_token", "run_id": "r-1", "status": "complete",
            "last_used_before": None, "last_used_at": "2026-09-30T05:00:00Z"}
    floors = dict(expect_sinks=3, min_bytes=1, marker_sinks=2, expect_tool_secret_runs=1)
    sweep_mod.verify(_report(tool_secret_runs=[good]), **floors)
    moved = {**good, "last_used_before": "2026-09-30T04:00:00+00:00"}
    sweep_mod.verify(_report(tool_secret_runs=[moved]), **floors)
    stale = "2026-09-29T23:00:00Z"
    unrecorded = {k: v for k, v in good.items() if k != "last_used_before"}
    for broken in (
        {**good, "last_used_at": None},
        {**good, "status": "error"},
        {**good, "last_used_before": stale, "last_used_at": stale},
        {**good, "last_used_before": "2026-09-30T06:00:00Z"},
        unrecorded,
    ):
        with pytest.raises(sweep_mod.SweepFailure, match="fetch over MCP never happened"):
            sweep_mod.verify(_report(tool_secret_runs=[broken]), **floors)
    with pytest.raises(sweep_mod.SweepFailure, match="run.s. fetched a tool secret"):
        sweep_mod.verify(_report(), **floors)

    # The run as the sweep drives it: the TENANT row of the planted name
    # read before the run is submitted, the run polled to complete, and the
    # row read again — never the default's stamp, and never the run's
    # output into the slot.
    def secrets(tenant_stamp, default_stamp="2026-09-30T05:30:00Z"):
        return (200, json.dumps({"secrets": [{"name": "echo_token",
                                              "tenant": {"set": True, "last_used_at": tenant_stamp},
                                              "agent": {"set": True, "last_used_at": default_stamp}}]}))

    answers = iter([
        secrets(None),
        (202, json.dumps({"run_id": "r-1", "run_number": "RUN-1"})),
        (200, json.dumps({"status": "complete", "structured_output": {"secret_set": True}})),
        secrets("2026-09-30T05:00:00Z"),
    ])
    sent = []
    monkeypatch.setattr(
        sweep_mod, "_http",
        lambda method, url, token=None, body=None: sent.append((method, url, body)) or next(answers),
    )
    planted = {"agent": "echo-v1", "name": "echo_token", "slot": "K8B_TOOL_SECRET",
               "fingerprint": TOOL_FINGERPRINT}
    slot = sweep_mod.run_with_tool_secret(
        "http://x", "tok", "echo-v1", {"message": "gate s", "fetch_secret": True},
        planted, lambda *entry: None,
    )
    assert slot == good
    assert [(m, u) for m, u, _ in sent] == [
        ("GET", "http://x/api/v1/agents/echo-v1/secrets"),
        ("POST", "http://x/api/v1/runs?agent_id=echo-v1"),
        ("GET", "http://x/api/v1/runs/r-1"),
        ("GET", "http://x/api/v1/agents/echo-v1/secrets"),
    ]
    assert sent[1][2] == {"message": "gate s", "fetch_secret": True}
    report = sweep_mod.sweep([], {"K8B_TOOL_SECRET": CANARY}, "", None, None, None, [slot])
    assert report["tool_secret_runs"] == [good]
    assert "secret_set" not in json.dumps(report)

    # A stamp an earlier fetch left, which the run did not move, is
    # recorded as it is and refused.
    answers = iter([
        secrets(stale),
        (202, json.dumps({"run_id": "r-2", "run_number": "RUN-2"})),
        (200, json.dumps({"status": "complete"})),
        secrets(stale),
    ])
    slot = sweep_mod.run_with_tool_secret(
        "http://x", "tok", "echo-v1", {"message": "gate s"}, planted, lambda *entry: None,
    )
    assert (slot["last_used_before"], slot["last_used_at"]) == (stale, stale)
    with pytest.raises(sweep_mod.SweepFailure, match="fetch over MCP never happened"):
        sweep_mod.verify(_report(tool_secret_runs=[slot]), **floors)


def test_a_run_slot_needs_one_planted_secret_for_its_agent():
    """K8b, Codex on #186: a run slot vouches for the row its agent's run
    read, so the agent must have exactly one planted tool secret — none
    would fetch nothing the sweep set, and two would leave the slot
    guessing which row to read."""
    echo = {"agent": "echo-v1", "name": "echo_token", "slot": "K8B_TOOL_SECRET"}
    other = {"agent": "vita-v1", "name": "search_key", "slot": "K8_TOOL_SECRET"}
    assert sweep_mod._planted_for_run([other, echo], "echo-v1", "echo-v1:{}") is echo
    with pytest.raises(sweep_mod.SweepFailure, match="no --tool-secret was planted for echo-v1"):
        sweep_mod._planted_for_run([other], "echo-v1", "echo-v1:{}")
    second = {"agent": "echo-v1", "name": "echo_other", "slot": "K8B_TOOL_SECRET"}
    with pytest.raises(sweep_mod.SweepFailure, match="2 tool secrets were planted for echo-v1"):
        sweep_mod._planted_for_run([echo, other, second], "echo-v1", "echo-v1:{}")


def test_a_report_with_an_exemption_is_refused():
    """D20 as K8a refines it: ``secret_get``'s result goes to the container
    and no sink records it, so Gate S needs no exemption — and a report
    that claims one, even for a sink that is clean, is refused."""
    with pytest.raises(sweep_mod.SweepFailure, match="exemptions"):
        sweep_mod.verify(
            _report(exemptions=["api-responses"]), expect_sinks=3, min_bytes=1, marker_sinks=2
        )
    sweep_mod.verify(_report(exemptions=[]), expect_sinks=3, min_bytes=1, marker_sinks=2)


def test_a_sweep_that_could_not_read_the_deployment_view_is_refused(monkeypatch):
    """K9: the sweep reads the deployment view, the page that shows the
    most of the deployment, and a read that did not answer 200 stops it —
    a refused read would sweep an error body and find nothing in a view it
    never saw. With 200, the view, the trace pipeline's report and the
    agent keys' list are all in the API sink."""
    from types import SimpleNamespace

    args = SimpleNamespace(api_url="http://x", admin_email="a@b", admin_password="pw", max_runs=0,
                           marker=None, marker_setting=None)
    for deployment in (403, 200):
        asked = []

        def _http(method, url, token=None, body=None, deployment=deployment):
            asked.append((method, url))
            if url.endswith("/api/v1/auth/login"):
                return 200, json.dumps({"access_token": "tok"})
            if url.endswith("/api/v1/admin/deployment"):
                return deployment, json.dumps({"settings": []} if deployment == 200 else {"detail": "no"})
            return 200, "[]"

        monkeypatch.setattr(sweep_mod, "_http", _http)
        if deployment == 403:
            with pytest.raises(sweep_mod.SweepFailure, match="could not read the deployment view"):
                sweep_mod.sink_api(args)
        else:
            sink = sweep_mod.sink_api(args)
            for label in ("GET /admin/deployment -> 200", "GET /admin/otel-status -> 200",
                          "GET /admin/agent-keys -> 200"):
                assert label in sink.text, label
        assert ("GET", "http://x/api/v1/admin/deployment") in asked
