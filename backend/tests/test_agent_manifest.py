"""Agent manifest v1 (blueprint B7): schema validation, discovery
behaviour, and the scenarios surface.

Three layers under test:

1. ``AgentManifest`` validation — the contract every ``agent.yaml`` must
   satisfy, including the guardrails (first phase can't be gated, paths
   stay inside the agent dir, runtime restricted to the two L11 values).
2. ``discover_agents`` — manifest required, one bad agent never blocks the
   others, container runtime parses but is skipped until B12a.
3. The real shipped files — VITA's manifest, the template's manifest, and
   the demo scenario, which must be a valid ``POST /runs`` body because
   the CI smoke run (B14) submits it verbatim.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.agents import registry
from app.agents.manifest import (
    AgentManifest,
    ManifestError,
    default_manifest,
    load_manifest,
)
from app.agents.protocol import AgentProtocol
from app.routers.agents import list_agents_endpoint, list_scenarios_endpoint
from app.services.intake import validate_user_inputs

REPO_BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_registry():
    registry._clear_registry_for_tests()
    yield
    registry._clear_registry_for_tests()


def _minimal(**overrides) -> dict:
    base = {
        "id": "toy-v1",
        "name": "Toy",
        "runtime": "python-package",
        "phases": [{"name": "analyze"}],
        "output": {"mode": "html_report"},
    }
    base.update(overrides)
    return base


# A minimal body that satisfies the stub agent's input schema below —
# scenarios must be verbatim submittable, and the endpoint enforces it
# against the agent's own schema (Codex P2 on PR #25; per-agent since B8).
_VALID_BODY = {
    "vendor_a": {"name": "Vendor A"},
    "vendor_b": {"name": "Vendor B"},
    "use_case": "A use case description that comfortably clears the fifty-character floor.",
    "problem_statement": "Something is broken between the two vendors.",
}


# --------------------------- schema validation -------------------------------


def test_minimal_manifest_defaults():
    m = AgentManifest.model_validate(_minimal())
    assert m.manifest_version == 1
    assert m.phase_names() == ["analyze"]
    assert m.phases[0].approval is False
    assert [s.id for s in m.feedback_sections] == ["overall"]
    assert m.scenarios == "scenarios"
    assert m.capabilities == []
    assert m.ui.intake.steps == []  # no steps = single-page form (B8)
    assert m.input_schema is None
    # Blueprint S4a: no LLM steps, and outbound redaction ON unless the
    # manifest opts out. The default is the safe one by construction.
    assert m.llm.steps == []
    assert m.llm.redact_outbound is True


# --------------------------- llm.steps (blueprint S4a) -----------------------


def test_llm_steps_parse_with_defaults_and_accessors():
    m = AgentManifest.model_validate(
        _minimal(
            capabilities=["llm"],
            llm={
                "steps": [
                    {
                        "id": "analyze",
                        "label": "Analysis",
                        "provider": "openai",
                        "model": "gpt-4o",
                        "temperature": 0.0,
                        "max_tokens": 2000,
                        "timeout_seconds": 60,
                    },
                    {"id": "draft"},
                ]
            },
        )
    )
    assert m.llm_step_ids() == ["analyze", "draft"]
    step = m.llm_step("analyze")
    assert (step.provider, step.model, step.temperature) == ("openai", "gpt-4o", 0.0)
    assert (step.max_tokens, step.timeout_seconds) == (2000, 60)
    # Every field but the id is optional — an agent may leave the whole
    # model choice to the admin — and the label falls back to the id.
    bare = m.llm_step("draft")
    assert bare.label == "draft"
    assert (bare.provider, bare.model, bare.max_tokens) == (None, None, None)
    assert m.llm_step("nope") is None


def test_llm_steps_without_the_grant_are_refused():
    """The gateway answers 403 llm_not_granted to every call from an agent
    that does not grant ``llm``, so steps declared without it are
    uncallable — a mistake worth failing at load rather than at the first
    run."""
    with pytest.raises(Exception) as exc:
        AgentManifest.model_validate(_minimal(llm={"steps": [{"id": "analyze"}]}))
    assert "llm_not_granted" in str(exc.value)
    # …and the same manifest with the grant is fine.
    AgentManifest.model_validate(
        _minimal(capabilities=["llm"], llm={"steps": [{"id": "analyze"}]})
    )


def test_a_platform_reserved_step_id_is_refused():
    """``kb_embed`` is routed by platform configuration, not by any
    agent's defaults; a declaration would silently shadow it."""
    with pytest.raises(Exception) as exc:
        AgentManifest.model_validate(
            _minimal(capabilities=["llm"], llm={"steps": [{"id": "kb_embed"}]})
        )
    assert "reserved" in str(exc.value)


def test_duplicate_step_ids_are_refused():
    with pytest.raises(Exception) as exc:
        AgentManifest.model_validate(
            _minimal(
                capabilities=["llm"],
                llm={"steps": [{"id": "analyze"}, {"id": "analyze"}]},
            )
        )
    assert "duplicate llm step ids" in str(exc.value)


@pytest.mark.parametrize(
    "step",
    [
        {"id": "Analyze"},  # upper case is off the id charset
        {"id": "analyze", "unknown_field": 1},  # extra = forbid
        {"id": "analyze", "temperature": -1},  # below the floor
        {"id": "analyze", "max_tokens": 0},  # below the floor
        {"id": "analyze", "timeout_seconds": 0},  # below the floor
        {"label": "no id"},  # the one required field
    ],
)
def test_malformed_llm_steps_are_refused(step):
    with pytest.raises(Exception):
        AgentManifest.model_validate(
            _minimal(capabilities=["llm"], llm={"steps": [step]})
        )


def test_redact_outbound_opt_out_is_explicit_and_recorded():
    m = AgentManifest.model_validate(
        _minimal(capabilities=["llm"], llm={"redact_outbound": False})
    )
    assert m.llm.redact_outbound is False
    with pytest.raises(Exception):
        AgentManifest.model_validate(_minimal(llm={"redaction": False}))


def test_intake_steps_parse_and_legacy_string_coerces():
    m = AgentManifest.model_validate(
        _minimal(
            ui={
                "intake": {
                    "steps": [
                        {"title": "Basics", "fields": ["description"]},
                        {"title": "Info only", "description": "note", "fields": []},
                    ]
                }
            }
        )
    )
    assert [s.title for s in m.ui.intake.steps] == ["Basics", "Info only"]
    # Pre-B8 manifests said ui.intake: generic — still parses as "no steps".
    legacy = AgentManifest.model_validate(_minimal(ui={"intake": "generic"}))
    assert legacy.ui.intake.steps == []


def test_intake_bespoke_removed_in_b8():
    with pytest.raises(Exception, match="bespoke"):
        AgentManifest.model_validate(_minimal(ui={"intake": "bespoke"}))


def test_intake_duplicate_field_across_steps_rejected():
    with pytest.raises(Exception, match="more than one intake step"):
        AgentManifest.model_validate(
            _minimal(
                ui={
                    "intake": {
                        "steps": [
                            {"title": "One", "fields": ["x"]},
                            {"title": "Two", "fields": ["x"]},
                        ]
                    }
                }
            )
        )


def test_plain_string_feedback_sections_coerced():
    m = AgentManifest.model_validate(
        _minimal(feedback_sections=["overall", "details"])
    )
    assert [(s.id, s.label) for s in m.feedback_sections] == [
        ("overall", "overall"),
        ("details", "details"),
    ]


def test_feedback_section_id_capped_at_storage_length():
    """FeedbackSection.id must fit run_feedback.section_type VARCHAR(30)
    so every advertised section is submittable (Codex P2 on PR #27)."""
    ok = AgentManifest.model_validate(
        _minimal(feedback_sections=[{"id": "s" * 30, "label": "x"}])
    )
    assert ok.feedback_sections[0].id == "s" * 30
    with pytest.raises(Exception):
        AgentManifest.model_validate(
            _minimal(feedback_sections=[{"id": "s" * 31, "label": "x"}])
        )


def test_container_runtime_requires_url_and_schema():
    """B12a: a container agent must say where it lives (container.url)
    and what its intake looks like (input_schema) — there is no code to
    ask. Fully-specified container manifests validate."""
    with pytest.raises(Exception, match="container: section"):
        AgentManifest.model_validate(_minimal(runtime="container"))
    with pytest.raises(Exception, match="input_schema"):
        AgentManifest.model_validate(
            _minimal(runtime="container", container={"url": "http://a:1"})
        )
    m = AgentManifest.model_validate(
        _minimal(
            runtime="container",
            container={"url": "http://agent:8090"},
            input_schema="input_schema.json",
        )
    )
    assert m.runtime == "container"
    assert m.container.url == "http://agent:8090"


def test_container_section_is_invalid_on_python_package():
    with pytest.raises(Exception, match="only valid with runtime"):
        AgentManifest.model_validate(_minimal(container={"url": "http://a:1"}))


@pytest.mark.parametrize(
    "patch",
    [
        {"runtime": "docker"},
        {"id": "Bad_Id"},
        {"phases": []},
        {"phases": [{"name": "a"}, {"name": "a"}]},
        {"phases": [{"name": "first", "approval": True}]},
        {"output": {"mode": "pdf"}},
        {"scenarios": "/etc"},
        {"scenarios": "../outside"},
        {"input_schema": "../../secrets.json"},
        {"capabilities": ["Not A Slug"]},
        {"unknown_field": 1},
    ],
)
def test_invalid_manifests_rejected(patch):
    with pytest.raises(Exception):
        AgentManifest.model_validate(_minimal(**patch))


def test_phase_helpers():
    m = AgentManifest.model_validate(
        _minimal(
            phases=[
                {"name": "a"},
                {"name": "b", "approval": True},
                {"name": "c"},
            ]
        )
    )
    assert m.phase_index("b") == 1
    assert m.phase_index("zzz") is None
    assert m.next_phase_after("a").name == "b"
    assert m.next_phase_after("c") is None
    assert m.is_final("c") is True
    assert m.is_final("a") is False


def test_default_manifest_mirrors_protocol_shape():
    class _A(AgentProtocol):
        agent_id = "legacy-v1"
        display_name = "Legacy"
        description = "d"

    m = default_manifest(_A())
    assert m.id == "legacy-v1"
    assert m.phase_names() == ["analyze", "investigate"]
    assert m.phases[1].approval is True


# --------------------------- loader ------------------------------------------


def _write_agent(tmp_path: Path, name: str, *, manifest: str | None, agent_id: str = "toy-v1") -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "__init__.py").write_text("")
    (d / "agent.py").write_text(
        textwrap.dedent(
            f"""
            from app.agents.protocol import AgentProtocol

            class ToyAgent(AgentProtocol):
                agent_id = "{agent_id}"
                display_name = "Toy"
                description = "d"

                def input_schema(self):
                    return {{"type": "object", "properties": {{}}}}
            """
        )
    )
    if manifest is not None:
        (d / "agent.yaml").write_text(manifest)
    return d


_GOOD_YAML = textwrap.dedent(
    """
    manifest_version: 1
    id: toy-v1
    name: Toy
    runtime: python-package
    phases:
      - name: analyze
    output:
      mode: html_report
    """
)


def test_load_manifest_missing_file(tmp_path):
    with pytest.raises(ManifestError, match="does not exist"):
        load_manifest(tmp_path)


def test_load_manifest_bad_yaml(tmp_path):
    (tmp_path / "agent.yaml").write_text("phases: [unclosed")
    with pytest.raises(ManifestError, match="not valid YAML"):
        load_manifest(tmp_path)


def test_load_manifest_non_mapping(tmp_path):
    (tmp_path / "agent.yaml").write_text("- just\n- a list\n")
    with pytest.raises(ManifestError, match="YAML mapping"):
        load_manifest(tmp_path)


def test_load_manifest_validation_error(tmp_path):
    (tmp_path / "agent.yaml").write_text(_GOOD_YAML.replace("toy-v1", "Bad Id"))
    with pytest.raises(ManifestError, match="failed validation"):
        load_manifest(tmp_path)


# --------------------------- discovery ---------------------------------------


def test_discovery_registers_manifest_and_dir(tmp_path):
    d = _write_agent(tmp_path, "toy", manifest=_GOOD_YAML)
    count = registry.discover_agents(tmp_path)
    assert count == 1
    assert registry.get_agent("toy-v1") is not None
    assert registry.get_manifest("toy-v1").phase_names() == ["analyze"]
    assert registry.get_agent_dir("toy-v1") == d


def test_discovery_skips_missing_manifest(tmp_path, caplog):
    _write_agent(tmp_path, "toy", manifest=None)
    count = registry.discover_agents(tmp_path)
    assert count == 0
    assert registry.get_agent("toy-v1") is None
    assert any("agent_manifest_missing" in r.getMessage() for r in caplog.records)


def test_discovery_skips_invalid_manifest(tmp_path, caplog):
    _write_agent(tmp_path, "toy", manifest="runtime: docker\n")
    count = registry.discover_agents(tmp_path)
    assert count == 0
    assert any("agent_manifest_invalid" in r.getMessage() for r in caplog.records)


def test_discovery_skips_underspecified_container_manifest(tmp_path, caplog):
    """A bare ``runtime: container`` swap is no longer a supported-later
    skip — B12a made the binding fields mandatory, so it is an invalid
    manifest (missing container.url / input_schema) and is skipped as
    such. The full container registration path is covered in
    ``test_container_runner.py``."""
    _write_agent(
        tmp_path,
        "toy",
        manifest=_GOOD_YAML.replace("python-package", "container"),
    )
    count = registry.discover_agents(tmp_path)
    assert count == 0
    assert any("agent_manifest_invalid" in r.getMessage() for r in caplog.records)


def test_discovery_accepts_structured_output_since_b9(tmp_path):
    """B9 wired the generic structured-results path, so the B7-era
    registration gate is gone — structured-mode agents register."""
    _write_agent(
        tmp_path,
        "toy",
        manifest=_GOOD_YAML.replace("mode: html_report", "mode: structured"),
    )
    count = registry.discover_agents(tmp_path)
    assert count == 1
    assert registry.get_manifest("toy-v1").output.mode == "structured"


def test_discovery_skips_id_mismatch(tmp_path, caplog):
    _write_agent(tmp_path, "toy", manifest=_GOOD_YAML, agent_id="different-id")
    count = registry.discover_agents(tmp_path)
    assert count == 0
    assert any("agent_manifest_id_mismatch" in r.getMessage() for r in caplog.records)


def test_one_bad_agent_never_blocks_the_good_one(tmp_path):
    _write_agent(tmp_path, "bad", manifest="runtime: docker\n")
    _write_agent(tmp_path, "toy", manifest=_GOOD_YAML)
    count = registry.discover_agents(tmp_path)
    assert count == 1
    assert registry.get_agent("toy-v1") is not None


# --------------------------- scenarios surface -------------------------------


class _StubAgent(AgentProtocol):
    agent_id = "toy-v1"
    display_name = "Toy"
    description = "d"

    # Mirrors the constraints scenarios must satisfy: since B8 the
    # scenario loader validates against the agent's own schema.
    def input_schema(self):
        return {
            "type": "object",
            "required": ["vendor_a", "vendor_b", "use_case", "problem_statement"],
            "properties": {
                "vendor_a": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                },
                "vendor_b": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                },
                "use_case": {"type": "string", "minLength": 50},
                "problem_statement": {"type": "string", "minLength": 20},
                "severity": {
                    "type": "string",
                    "enum": ["critical", "high", "medium", "low"],
                },
            },
        }


def _register_with_scenarios(
    tmp_path: Path, files: dict[str, str], manifest_overrides: dict | None = None
) -> None:
    scen = tmp_path / "scenarios"
    scen.mkdir()
    for fname, content in files.items():
        (scen / fname).write_text(content)
    manifest = AgentManifest.model_validate(_minimal(**(manifest_overrides or {})))
    registry.register(_StubAgent(), manifest, agent_dir=tmp_path)


@pytest.mark.asyncio
async def test_scenarios_endpoint_serves_valid_files(tmp_path):
    _register_with_scenarios(
        tmp_path,
        {
            "demo.json": json.dumps(
                {
                    "name": "Demo",
                    "description": "d",
                    "user_inputs": _VALID_BODY,
                }
            )
        },
    )
    rows = await list_scenarios_endpoint("toy-v1", None)
    assert rows == [
        {
            "id": "demo",
            "name": "Demo",
            "description": "d",
            "user_inputs": _VALID_BODY,
        }
    ]


@pytest.mark.asyncio
async def test_scenarios_endpoint_skips_malformed_files(tmp_path, caplog):
    _register_with_scenarios(
        tmp_path,
        {
            "broken.json": "{not json",
            "no-name.json": json.dumps({"user_inputs": _VALID_BODY}),
            "no-inputs.json": json.dumps({"name": "x"}),
            "ok.json": json.dumps({"name": "OK", "user_inputs": _VALID_BODY}),
        },
    )
    rows = await list_scenarios_endpoint("toy-v1", None)
    assert [r["id"] for r in rows] == ["ok"]
    assert (
        sum("agent_scenario_invalid" in r.getMessage() for r in caplog.records) == 3
    )


@pytest.mark.asyncio
async def test_scenarios_endpoint_skips_unsubmittable_bodies(tmp_path, caplog):
    """`user_inputs` shaped like an object but violating the POST /runs
    schema must not be advertised — the UI would load it and then 422 on
    submit (Codex P2 on PR #25)."""
    missing_vendor = {k: v for k, v in _VALID_BODY.items() if k != "vendor_a"}
    short_use_case = dict(_VALID_BODY, use_case="too short")
    bad_severity = dict(_VALID_BODY, severity="urgent")
    _register_with_scenarios(
        tmp_path,
        {
            "a-missing-vendor.json": json.dumps(
                {"name": "m", "user_inputs": missing_vendor}
            ),
            "b-short-use-run.json": json.dumps(
                {"name": "s", "user_inputs": short_use_case}
            ),
            "c-bad-severity.json": json.dumps(
                {"name": "b", "user_inputs": bad_severity}
            ),
            "d-good.json": json.dumps({"name": "g", "user_inputs": _VALID_BODY}),
        },
    )
    rows = await list_scenarios_endpoint("toy-v1", None)
    assert [r["id"] for r in rows] == ["d-good"]
    assert (
        sum(
            "not a submittable POST /runs body" in r.getMessage()
            for r in caplog.records
        )
        == 3
    )


@pytest.mark.asyncio
async def test_scenarios_endpoint_no_dir_returns_empty(tmp_path):
    registry.register(
        _StubAgent(), AgentManifest.model_validate(_minimal()), agent_dir=tmp_path
    )
    assert await list_scenarios_endpoint("toy-v1", None) == []


@pytest.mark.asyncio
async def test_scenarios_endpoint_unknown_agent_404s():
    with pytest.raises(HTTPException) as exc:
        await list_scenarios_endpoint("nope", None)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_agent_list_carries_manifest_fields(tmp_path):
    _register_with_scenarios(
        tmp_path,
        {"demo.json": json.dumps({"name": "Demo", "user_inputs": _VALID_BODY})},
    )
    rows = await list_agents_endpoint(None)
    assert len(rows) == 1
    row = rows[0]
    assert row["agent_id"] == "toy-v1"
    assert row["phases"] == [{"name": "analyze", "approval": False, "steps": []}]
    assert row["framework"] == ""
    assert row["ui"] == {"intake": {"steps": []}}
    assert row["output"] == {"mode": "html_report"}
    assert row["capabilities"] == []
    assert row["has_scenarios"] is True


# --------------------------- shipped files -----------------------------------


def test_vita_manifest_is_valid_with_seven_step_intake():
    m = load_manifest(REPO_BACKEND / "agents" / "vita_v1")
    assert m.id == "vita-v1"
    assert m.name == "VITA Vendor Troubleshooter"
    assert m.runtime == "python-package"
    assert m.phase_names() == ["analyze", "investigate"]
    assert m.phases[0].approval is False
    assert m.phases[1].approval is True
    assert m.output.mode == "html_report"
    # Six declared steps + the chassis Review step = the historical seven.
    assert [s.title for s in m.ui.intake.steps] == [
        "Vendors",
        "Logs",
        "Use Case",
        "Problem",
        "Impact",
        "Configs",
    ]
    assert m.ui.intake.steps[1].fields == ["logs_a", "logs_b"]
    assert m.ui.intake.steps[5].fields == []  # informational Configs step
    assert [s.id for s in m.feedback_sections] == [
        "refined_problem",
        "mitigation",
        "resolution",
        "avoidance",
    ]


# --------------------------- S7: phases[].steps[] and framework --------------


def test_phase_steps_parse_with_labels_and_the_lookup_spans_phases():
    m = AgentManifest.model_validate(
        _minimal(
            phases=[
                {"name": "analyze", "steps": [{"id": "classify", "label": "Classify"}]},
                {
                    "name": "investigate",
                    "approval": True,
                    "steps": [
                        {"id": "investigate:draft", "label": "Draft the plan"},
                        {"id": "classify", "label": "Classify again"},
                    ],
                },
            ]
        )
    )
    assert m.phases[0].step_label("classify") == "Classify"
    assert m.phases[0].step_label("nope") is None
    # First declaration wins across phases: one id, one label on the page.
    assert m.step_labels() == {"classify": "Classify", "investigate:draft": "Draft the plan"}


def test_phase_steps_default_to_none_and_framework_to_empty():
    m = AgentManifest.model_validate(_minimal())
    assert m.phases[0].steps == []
    assert m.framework == ""
    assert m.step_labels() == {}


def test_framework_is_free_text_for_the_badge_and_bounded():
    assert AgentManifest.model_validate(_minimal(framework="langgraph")).framework == "langgraph"
    with pytest.raises(ValueError):
        AgentManifest.model_validate(_minimal(framework="x" * 41))


@pytest.mark.parametrize(
    "steps",
    [
        [{"id": "a", "label": "A"}, {"id": "a", "label": "A again"}],  # duplicate in one phase
        [{"id": "", "label": "A"}],  # empty id
        [{"id": "a", "label": ""}],  # empty label
        [{"id": "a"}],  # label missing
        [{"id": "a", "label": "A", "model": "gpt-4o"}],  # a step is a label, never a model
    ],
)
def test_malformed_phase_steps_are_refused(steps):
    with pytest.raises(ValueError):
        AgentManifest.model_validate(_minimal(phases=[{"name": "analyze", "steps": steps}]))


def test_shipped_manifests_declare_labels_for_the_rows_they_report():
    """The demo agent's labels used to be a table inside the chassis's
    ProgressList component (agent vocabulary in the platform, the S3
    finding); they live in its manifest now, one per orchestrator step.
    The examples declare theirs the same way, and a framework each."""
    vita = load_manifest(REPO_BACKEND / "agents" / "vita_v1")
    assert set(vita.step_labels()) == {
        "validate_and_classify_inputs",
        "refine_problem_statement",
        "construct_search_queries_vendor_a",
        "construct_search_queries_vendor_b",
        "search_internal_kb",
        "search_public_resources",
        "assess_skills",
        "generate_resolution_plan",
        "generate_followup_questions",
    }
    # Its LLM steps that report progress are labelled under the SAME id
    # — which is what lets the page show the model beside them (E5).
    for step_id in ("refine_problem_statement", "generate_resolution_plan"):
        assert step_id in vita.step_labels() and vita.llm_step(step_id) is not None
    examples = REPO_BACKEND / "agents" / "_examples"
    expected = {
        "echo_container": ("librerun-agent", {"echo"}),
        "langgraph_triage": (
            "langgraph",
            {"analyze:classify", "analyze:summarise", "investigate:gather_context", "investigate:draft"},
        ),
        "llamaindex_summarize": ("llamaindex", {"extract", "summarize"}),
        "vercel_ai_answer_ts": ("vercel-ai-sdk", {"answer"}),
    }
    for directory, (framework, steps) in expected.items():
        m = load_manifest(examples / directory)
        assert m.framework == framework, directory
        assert set(m.step_labels()) == steps, directory
        assert all(label.strip() for label in m.step_labels().values()), directory


@pytest.mark.asyncio
async def test_agent_list_carries_steps_and_framework(tmp_path):
    _register_with_scenarios(
        tmp_path,
        {"demo.json": json.dumps({"name": "Demo", "user_inputs": _VALID_BODY})},
        manifest_overrides={
            "framework": "toyframework",
            "phases": [
                {"name": "analyze", "steps": [{"id": "look", "label": "Look around"}]}
            ],
        },
    )
    rows = await list_agents_endpoint(None)
    assert rows[0]["framework"] == "toyframework"
    assert rows[0]["phases"] == [
        {"name": "analyze", "approval": False, "steps": [{"id": "look", "label": "Look around"}]}
    ]


def test_demo_scenario_is_a_submittable_body():
    """The CI smoke run (B14) submits this file's ``user_inputs``
    verbatim to POST /runs — so it must satisfy VITA's input schema
    (the request contract since B8), minimum lengths included."""
    import agents.vita_v1.agent as vita_module

    path = (
        REPO_BACKEND / "agents" / "vita_v1" / "scenarios" / "demo-vendor-interop.json"
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["name"]
    schema = vita_module.VitaAgent().input_schema()
    assert validate_user_inputs(schema, data["user_inputs"]) == []


def test_ui_list_and_approval_paths_parse_and_default_to_none():
    """Blueprint S2: the run title and the approval summary are dotted
    paths the manifest names; omitted, the chassis falls back to the
    first string input / the first string in the parked output."""
    m = AgentManifest.model_validate(
        _minimal(ui={"list": {"title_path": "incident.title"}, "approval": {"summary_path": "draft.summary"}})
    )
    assert m.ui.list.title_path == "incident.title"
    assert m.ui.approval.summary_path == "draft.summary"
    bare = AgentManifest.model_validate(_minimal())
    assert bare.ui.list.title_path is None
    assert bare.ui.approval.summary_path is None


@pytest.mark.parametrize("bad", ["", ".x", "x.", "a..b", "a[0]", "a b", "a/b"])
def test_ui_paths_reject_anything_but_dotted_identifiers(bad):
    with pytest.raises(Exception):
        AgentManifest.model_validate(_minimal(ui={"list": {"title_path": bad}}))
    with pytest.raises(Exception):
        AgentManifest.model_validate(_minimal(ui={"approval": {"summary_path": bad}}))


def test_ui_specs_forbid_unknown_keys():
    with pytest.raises(Exception):
        AgentManifest.model_validate(_minimal(ui={"list": {"titel_path": "x"}}))
