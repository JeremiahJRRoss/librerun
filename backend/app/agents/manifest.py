"""Agent manifest v1 — ``agent.yaml`` schema and loader (blueprint B7, L5/L11).

Every agent directory ships an ``agent.yaml`` describing the agent to the
chassis: identity, runtime, phase list, output mode, feedback sections,
scenarios directory, and capability slugs. The registry reads it at
discovery time; the runner, approval gate, and UI read the parsed manifest
instead of hardcoding any agent's topology.

Multi-framework fields (L11) are present from day one so nothing calcifies
around Python or the demo agent's two-phase shape:

- ``runtime`` selects the execution mode: ``python-package`` imports the
  agent in-process; ``container`` (blueprint B12a) speaks Run Contract v1
  (HTTP+SSE — see ``docs/authoring/Run_Contract_v1.md``) to a running container
  addressed by the manifest's ``container.url``. Container agents must
  also declare ``input_schema`` — there is no code to serve it.
- ``phases`` is a list, not a pair. The demo agent declares ``analyze`` +
  ``investigate {approval: true}``; a single-phase agent declares one
  entry; ungated consecutive phases auto-advance in one background run.
- ``capabilities`` is the agent's GRANT since blueprint B13: the runner
  builds a per-run façade from this list, and reaching for a capability
  the manifest doesn't name raises ``CapabilityNotGranted`` (in-process
  and over the run-scoped MCP server alike). Names outside
  ``app.capabilities.KNOWN_CAPABILITIES`` stay legal and simply grant
  nothing — descriptive slugs like the demo agent's ``web-search`` predate the
  façade — but they are logged so a typo of a real name is visible.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = structlog.get_logger(__name__)

MANIFEST_FILENAME = "agent.yaml"

_ID_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
_PHASE_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"


class ManifestError(ValueError):
    """Raised when ``agent.yaml`` is missing, unparseable, or invalid.

    Discovery catches this per-directory: one bad manifest logs
    ``agent_manifest_invalid`` and skips that agent, never the others.
    """


class PhaseStepSpec(BaseModel):
    """One entry of ``phases[].steps`` (blueprint S7, §10): a progress
    row's id and the label the run page shows for it.

    ``id`` is whatever the agent puts in ``StepProgress.step_id`` — free
    text the agent chooses, so it is bounded, not patterned: the demo
    agent's rows are its step names, a LangGraph adapter's are
    ``phase:node``. A row
    whose id is declared nowhere renders under its raw id, so the list
    is a courtesy to the reader, never a filter on what the agent may
    report. ``label`` is the human sentence. Declaring a step here says
    nothing about models: the model column comes from the gateway's
    record keyed by LLM step id, and only an id equal to one is ever
    shown a model (gap E5).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=120)


class PhaseSpec(BaseModel):
    """One entry of ``phases``.

    ``approval: true`` means a human must approve the *previous* phase's
    output before this phase runs — the chassis parks the run in
    ``awaiting_approval`` and the approve endpoint resumes it. The first
    phase can't be gated (submission itself is the go signal).

    ``deadline_seconds`` (blueprint S4, §10) is the wall-clock budget of
    one invocation of this phase, under the platform ceiling
    ``LIBRERUN_MAX_PHASE_SECONDS`` (default 3600): the runner fails the
    phase — and the run — when it is exceeded, a container receives it
    as ``deadline_seconds`` in the ``POST /v1/runs`` body and its run
    token expires with it. Unset means the ceiling itself; a value above
    the ceiling is clamped to it (logged at phase start).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_PHASE_NAME_PATTERN, max_length=64)
    approval: bool = False
    deadline_seconds: int | None = Field(default=None, ge=1, strict=True)
    # Blueprint S7 (§10): progress labels for the run page, optional.
    steps: list[PhaseStepSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_step_ids(self) -> "PhaseSpec":
        ids = [s.id for s in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate step ids in phase {self.name!r}: {ids}")
        return self

    def step_label(self, step_id: str) -> str | None:
        for s in self.steps:
            if s.id == step_id:
                return s.label
        return None


class ContainerSpec(BaseModel):
    """How the chassis reaches a ``runtime: container`` agent (blueprint
    B12a, Run Contract v1).

    ``url`` is where the four contract endpoints live — a compose service
    name or any preconfigured URL. ``${VAR}`` references expand from the
    chassis environment at discovery time. v1 addresses running
    containers; the chassis never launches or schedules them.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)


class NetworkSpec(BaseModel):
    """Where a container agent may reach (blueprint S4, §10).

    Agent containers join only the ``agents`` network, declared
    ``internal: true`` in ``compose.yaml``: no path to Vector but the
    chassis relay, and none to the Internet at all — the gateway is the
    only way to a model. ``egress: true`` declares that this agent's
    compose fragment also joins the non-internal ``egress`` network — an
    opt-out from "no path off-box but through the chassis" that the
    admin page shows; the fragment must match (the CLI template carries
    the line commented).
    """

    model_config = ConfigDict(extra="forbid")

    egress: bool = False


# The one step id the platform owns: knowledge search's query embedding
# (blueprint S4a). It is routed by platform configuration, not by any
# agent's defaults, so an agent may not declare a step by that name — a
# declaration would silently shadow the platform's own routing.
RESERVED_STEP_IDS = ("kb_embed",)

_STEP_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"


class LlmStepSpec(BaseModel):
    """One entry of ``llm.steps`` — an LLM call the agent makes, with the
    model defaults it ships (blueprint S4a, L25, §10).

    The agent names the step on every call (``X-LibreRun-Step`` or a
    ``librerun/<id>`` model); the gateway resolves provider, model,
    temperature, ``max_tokens`` and timeout from the tenant's admin
    configuration for that step, falling back to what is declared here.
    So a model change is data, edited in the UI, needing no redeploy —
    and an id that is *not* declared here is refused (``400
    unknown_step``), because an invented one would slip past every
    choice the admin made for the declared ones.

    Every field but ``id`` is optional: an agent may declare a step and
    leave the whole model choice to the admin. ``label`` is what the
    admin page shows; it defaults to the id.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_STEP_ID_PATTERN, max_length=100)
    label: str = ""
    description: str = ""
    provider: str | None = Field(default=None, max_length=50)
    model: str | None = Field(default=None, max_length=200)
    temperature: float | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, ge=1, strict=True)
    timeout_seconds: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def _label_defaults_to_id(self) -> "LlmStepSpec":
        if not self.label:
            self.label = self.id
        return self

    @field_validator("id")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v in RESERVED_STEP_IDS:
            raise ValueError(
                f"step id {v!r} is reserved by the platform — it is routed "
                f"by platform configuration, not by an agent's defaults"
            )
        return v


class LlmSpec(BaseModel):
    """The agent's LLM surface: the steps it calls, and whether the
    gateway redacts what it sends (blueprint S4a, §10).

    ``redact_outbound`` defaults to **true**: every string the model will
    read is redacted, every identifier is checked and a request whose
    identifier would change is refused, and content the gateway cannot
    read at all — media parts, token-id embedding inputs — is refused
    rather than forwarded under a switch that promises redaction. An
    agent that needs multimodal input sets it to ``false``, an explicit
    opt-out the manifest records and the admin page shows. It governs
    what the *model* sees; it never governs what telemetry keeps, which
    goes through the walker either way.
    """

    model_config = ConfigDict(extra="forbid")

    steps: list[LlmStepSpec] = Field(default_factory=list)
    redact_outbound: bool = True

    @model_validator(mode="after")
    def _unique_step_ids(self) -> "LlmSpec":
        ids = [s.id for s in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate llm step ids: {ids}")
        return self

    def step(self, step_id: str) -> LlmStepSpec | None:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None


# A setting's key (K5a, L32): what a run reads the value by, what the
# ``agent_settings`` row stores it under and what the admin page names.
# Public because K8a names an agent's tool secrets in the same charset.
SETTING_KEY_PATTERN = r"^[a-z][a-z0-9_]*$"

_JSON_KIND = {
    bool: "a boolean",
    int: "an integer",
    float: "a number",
    str: "a string",
    list: "a list",
    dict: "an object",
    type(None): "null",
}


def _kind(value: Any) -> str:
    """What JSON ``value`` is, never what it says: a refusal names the
    type it met, so a value an admin typed is not echoed into an error."""
    return _JSON_KIND.get(type(value), type(value).__name__)


class AgentSettingSpec(BaseModel):
    """One entry of ``settings[]`` — a value the agent reads at run time and
    a tenant's admin edits on the agent page's Settings tab (K5a, L32).

    The manifest declares the setting and its default; the value a run
    reads is this tenant's. It lives in the ``agent_settings`` table, and a
    row there exists only while it DIFFERS from the default (D17): a value
    set back to the default is not stored, so the agent's next release can
    move a default and reach every tenant that never chose one. A run reads
    the values through ``caps.config.settings()`` in-process or the MCP
    ``config_get`` tool, and neither needs a grant: a setting is the agent's
    own configuration, not a platform capability.

    ``type`` decides what a value may be, strictly (``coerce``): a boolean
    is never an ``int`` and an integer never a ``bool``, a ``float`` takes
    an integer and keeps a float, an ``enum`` is one of its ``options`` and
    a ``string_list`` is a list of strings. ``label`` is what the admin page
    shows; it defaults to the key.
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=SETTING_KEY_PATTERN, max_length=64)
    label: str = Field(default="", max_length=120)
    type: Literal["string", "int", "float", "bool", "enum", "string_list"]
    # The value every tenant reads until its admin chooses another. It is
    # held to the setting's own type, so a manifest cannot declare a
    # default its own Settings tab would refuse.
    default: Any
    # The choices of an enum setting, and of no other type.
    options: list[str] | None = None
    description: str = ""

    @model_validator(mode="after")
    def _options_and_default(self) -> "AgentSettingSpec":
        """An ``enum`` declares at least one option, each once, and no
        other type declares any; the ``default`` must pass ``coerce``, so
        every tenant starts from a value the setting accepts (a ``float``'s
        is kept as a float)."""
        if self.type == "enum":
            if not self.options:
                raise ValueError(
                    f"setting {self.key!r} is an enum and declares no options"
                )
            if len(set(self.options)) != len(self.options):
                raise ValueError(
                    f"setting {self.key!r} declares an option twice: {self.options}"
                )
        elif self.options is not None:
            raise ValueError(
                f"setting {self.key!r}: options are for an enum setting, "
                f"not a {self.type} one"
            )
        try:
            self.default = self.coerce(self.default)
        except ValueError as exc:
            raise ValueError(f"setting {self.key!r}: its default {exc}") from None
        if not self.label:
            self.label = self.key
        return self

    def coerce(self, value: Any) -> Any:
        """``value`` as this setting holds it, or ``ValueError`` saying why not.

        Strict per type, because JSON's booleans are Python's integers:
        ``True == 1``, so a comparison or an ``isinstance(value, int)``
        that did not look first would store ``1`` in a boolean setting
        and call ``true`` an integer (K5-07). A ``float`` setting takes an
        integer — a number input sends ``0`` for ``0.0`` — and returns a
        float, so ``0`` and a declared ``0.0`` are the same value. Nothing
        is parsed out of a string: ``"5"`` is not an integer.
        """
        kind = self.type
        if kind == "bool":
            if isinstance(value, bool):
                return value
            raise ValueError(f"must be true or false, not {_kind(value)}")
        if kind == "int":
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            raise ValueError(f"must be an integer, not {_kind(value)}")
        if kind == "float":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                try:
                    number = float(value)
                except OverflowError:
                    number = math.inf
                # JSON has no NaN or infinity, and PostgreSQL's JSONB
                # refuses them, so neither may reach a row.
                if math.isfinite(number):
                    return number
                raise ValueError("must be a finite number")
            raise ValueError(f"must be a number, not {_kind(value)}")
        if kind == "string":
            if isinstance(value, str):
                return value
            raise ValueError(f"must be a string, not {_kind(value)}")
        if kind == "enum":
            if isinstance(value, str) and value in (self.options or ()):
                return value
            raise ValueError(f"must be one of {self.options}")
        if not isinstance(value, list):
            raise ValueError(f"must be a list of strings, not {_kind(value)}")
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise ValueError(
                    f"must be a list of strings; item {index} is {_kind(item)}"
                )
        return list(value)


class OutputSpec(BaseModel):
    """How the final phase's result is rendered.

    ``html_report``: the agent returns rendered HTML (the demo agent's mode); the
    report endpoints serve/PDF it. ``structured``: the agent returns JSON
    only; the UI renders it generically (fully wired in blueprint B9).
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["html_report", "structured"]


class FeedbackSection(BaseModel):
    """A feedback target the results view offers thumbs on.

    Declared here so the frontend can stop hardcoding section lists
    (consumed in blueprint B9). Plain strings in YAML are coerced to
    ``{id: s, label: s}``. The id length cap matches the
    ``run_feedback.section_type`` storage column (VARCHAR(30)) so every
    advertised section is guaranteed submittable.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_PHASE_NAME_PATTERN, max_length=30)
    label: str


class IntakeStep(BaseModel):
    """One step of the generic intake wizard (blueprint B8).

    ``fields`` lists top-level input-schema property names shown on this
    step. An empty list makes an informational step (``description`` only
    — the demo agent's "Configs" placeholder). Schema properties not claimed by any
    step are collected into an automatic "Details" step, and the chassis
    always appends the final Review step itself — agents never declare it.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=80)
    description: str = ""
    fields: list[str] = Field(default_factory=list)


class IntakeSpec(BaseModel):
    """Schema-driven intake layout. No steps = single-page form."""

    model_config = ConfigDict(extra="forbid")

    steps: list[IntakeStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_duplicate_fields(self) -> "IntakeSpec":
        seen: set[str] = set()
        for step in self.steps:
            for f in step.fields:
                if f in seen:
                    raise ValueError(f"field {f!r} appears in more than one intake step")
                seen.add(f)
        return self


# A dotted path into a JSON object: ``problem_statement``,
# ``refined_problem.refined_problem_statement``. Segments are identifiers;
# list indexes are deliberately not supported (a title or summary is a
# named field, never "the third thing").
_DOTTED_PATH = r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$"


class ListSpec(BaseModel):
    """Run-list hints (blueprint S2). ``title_path`` names the input the
    dashboard shows as the run's title — a dotted path into
    ``user_inputs``, resolved at intake into the run's ``title``. Omitted,
    the first string property of the input schema that is not marked
    ``x-pii`` is the title (logs make poor titles)."""

    model_config = ConfigDict(extra="forbid")

    title_path: str | None = Field(default=None, pattern=_DOTTED_PATH, max_length=200)


class ApprovalSpec(BaseModel):
    """Approval-gate hints (blueprint S2). ``summary_path`` names the
    string inside a parked phase output that the approval view shows and
    offers for editing — a dotted path into that output. Omitted, the
    first non-empty string found depth-first in the output is the
    summary."""

    model_config = ConfigDict(extra="forbid")

    summary_path: str | None = Field(default=None, pattern=_DOTTED_PATH, max_length=200)


class UISpec(BaseModel):
    """Frontend hints. ``intake`` describes the generic schema-driven
    intake: an object with ``steps`` renders the stepped wizard, an empty
    one a single-page form. The legacy string ``"generic"`` still parses
    (as "no steps"); ``"bespoke"`` was removed in blueprint B8 when the
    demo agent moved onto the generic wizard. ``list`` and ``approval``
    carry the dotted paths the run list and the approval view resolve
    (blueprint S2)."""

    model_config = ConfigDict(extra="forbid")

    intake: IntakeSpec = Field(default_factory=IntakeSpec)
    list: ListSpec = Field(default_factory=ListSpec)
    approval: ApprovalSpec = Field(default_factory=ApprovalSpec)

    @field_validator("intake", mode="before")
    @classmethod
    def _coerce_legacy_strings(cls, v):
        if v == "generic":
            return {}
        if v == "bespoke":
            raise ValueError(
                "ui.intake 'bespoke' was removed in blueprint B8 — declare "
                "ui.intake.steps for a stepped wizard, or omit ui.intake "
                "for a single-page form"
            )
        return v


class AgentManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_version: Literal[1] = 1
    id: str = Field(pattern=_ID_PATTERN, max_length=50)
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    # Blueprint S7 (§10): the framework the agent is built on, free text
    # for the badge on its card (``langgraph``, ``llamaindex``,
    # ``vercel-ai-sdk``). Empty means the card shows the runtime instead.
    # It is a label and nothing else: the chassis dispatches on
    # ``runtime``, never on this (L7 — no framework-specific UI).
    framework: str = Field(default="", max_length=40)
    runtime: Literal["python-package", "container"]
    # Required when runtime is ``container`` (validated below): where the
    # chassis reaches the agent's Run Contract v1 endpoints.
    container: ContainerSpec | None = None
    phases: list[PhaseSpec] = Field(min_length=1)
    # Path (relative to the agent directory) of a JSON Schema file for the
    # intake form. Optional in v1 — agents may still serve the schema from
    # code via ``AgentProtocol.input_schema()`` (the demo agent did until B8).
    input_schema: str | None = None
    output: OutputSpec
    feedback_sections: list[FeedbackSection] = Field(
        default_factory=lambda: [FeedbackSection(id="overall", label="Overall")]
    )
    # Directory (relative to the agent directory) holding demo scenario
    # JSON files. The directory not existing simply means "no scenarios".
    scenarios: str = "scenarios"
    capabilities: list[str] = Field(default_factory=list)
    # K8a (D20, D35): the third-party tool secrets the agent may ask for by
    # name — a search key, a vector-store key — in the settings keys'
    # charset. Each tenant's admin sets this tenant's value and a platform
    # admin every tenant's default; a run reads its tenant's through
    # caps.secrets.get or the MCP secret_get tool. No grant is needed: a
    # tool secret is the agent's own data, not a platform capability.
    secrets: list[str] = Field(default_factory=list)
    ui: UISpec = Field(default_factory=UISpec)
    # Blueprint S4: whether the container also joins the ``egress`` network.
    network: NetworkSpec = Field(default_factory=NetworkSpec)
    # Blueprint S4a: the agent's LLM steps with their model defaults, and
    # whether the gateway redacts what it sends on the agent's behalf.
    llm: LlmSpec = Field(default_factory=LlmSpec)
    # K5a (L32): the settings the agent reads at run time, with their
    # defaults. Each tenant's admin edits the effective values on the
    # agent page's Settings tab, and a declaration here turns the
    # deprecated config_meta() path off for this agent.
    settings: list[AgentSettingSpec] = Field(default_factory=list)

    @field_validator("feedback_sections", mode="before")
    @classmethod
    def _coerce_plain_strings(cls, v):
        if isinstance(v, list):
            return [{"id": s, "label": s} if isinstance(s, str) else s for s in v]
        return v

    @field_validator("capabilities")
    @classmethod
    def _capability_slugs(cls, v: list[str]) -> list[str]:
        import re

        for slug in v:
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", slug):
                raise ValueError(f"capability {slug!r} is not a lowercase slug")
            if slug == "secrets":
                # K8a: tool secrets are declared in secrets[], and reading
                # them needs no grant — a grant named for them would read
                # as one that did.
                raise ValueError(
                    "'secrets' is not a capability: declare the agent's tool "
                    "secrets in secrets[], which needs no grant"
                )
        # Deprecated spellings (``case_store`` → ``run_store``, blueprint
        # S1) are normalized at load so the rest of the chassis sees one
        # vocabulary; the alias goes at v1.1.
        from app.capabilities import normalize_grants

        return normalize_grants(v)

    @field_validator("secrets")
    @classmethod
    def _tool_secret_names(cls, v: list[str]) -> list[str]:
        """Each ``secrets[]`` name is a settings key (``[a-z][a-z0-9_]*``, at
        most 64 characters), declared once, and not reserved: upper-cased —
        as the in-process fallback reads the environment — it is not a
        setting of the backend's own or that setting's ``_FILE`` spelling,
        not a model provider's key (L23), and not in the platform's
        ``LIBRERUN_`` or ``OTEL_`` namespace (D35)."""
        import re

        from app.services.tool_secrets_service import reserved_reason

        for name in v:
            if not isinstance(name, str) or not re.fullmatch(SETTING_KEY_PATTERN, name):
                raise ValueError(
                    f"secret {name!r} is not a lowercase key ([a-z][a-z0-9_]*)"
                )
            if len(name) > 64:
                raise ValueError(f"secret {name!r} is longer than 64 characters")
            reason = reserved_reason(name)
            if reason is not None:
                raise ValueError(f"secret {name!r} is reserved: {reason}")
        repeated = sorted({name for name in v if v.count(name) > 1})
        if repeated:
            raise ValueError(f"duplicate secrets: {repeated}")
        return v

    @field_validator("scenarios", "input_schema")
    @classmethod
    def _relative_paths_only(cls, v: str | None) -> str | None:
        """Manifest paths are served by the chassis — keep them inside the
        agent directory (no absolute paths, no ``..`` escapes)."""
        if v is None:
            return v
        p = Path(v)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"path {v!r} must be relative and inside the agent dir")
        return v

    @model_validator(mode="after")
    def _check_phases(self) -> "AgentManifest":
        names = [p.name for p in self.phases]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate phase names: {names}")
        if self.phases[0].approval:
            raise ValueError(
                "phases[0] cannot require approval — submission itself is "
                "the approval for the first phase"
            )
        return self

    @model_validator(mode="after")
    def _check_llm_grant(self) -> "AgentManifest":
        """Declared steps that the gateway would refuse are a mistake, not
        a configuration.

        The gateway answers ``403 llm_not_granted`` to any model call from
        an agent whose manifest does not grant ``llm``, whatever
        credential it presents — so steps declared without the grant are
        uncallable, and the author finds out at the first run instead of
        here. The manifest stays the one declaration of an agent's
        platform access (L5, L11): if it calls a model, it says so.
        """
        if self.llm.steps and "llm" not in self.capabilities:
            raise ValueError(
                "llm.steps are declared but the manifest does not grant "
                "'llm' — add llm to capabilities, or the gateway will "
                "refuse every call these steps name (403 llm_not_granted)"
            )
        return self

    @model_validator(mode="after")
    def _unique_setting_keys(self) -> "AgentManifest":
        """Each ``settings[]`` key is declared once: a run reads a value by
        its key, and two declarations of one key would be two defaults and
        two types for one value."""
        keys = [s.key for s in self.settings]
        repeated = sorted({k for k in keys if keys.count(k) > 1})
        if repeated:
            raise ValueError(f"duplicate settings keys: {repeated}")
        return self

    @model_validator(mode="after")
    def _check_container_binding(self) -> "AgentManifest":
        """Run Contract v1 (blueprint B12a): a container agent must say
        where it lives and what its intake looks like — there is no code
        to ask. A ``container`` section on a python-package agent is a
        contradiction worth failing loudly."""
        if self.runtime == "container":
            if self.container is None:
                raise ValueError(
                    "runtime 'container' requires a container: section "
                    "with the agent's url (Run Contract v1, blueprint B12a)"
                )
            if not self.input_schema:
                raise ValueError(
                    "runtime 'container' requires input_schema: a JSON "
                    "Schema file shipped next to the manifest — container "
                    "agents cannot serve their intake schema from code"
                )
        elif self.container is not None:
            raise ValueError(
                "container: section is only valid with runtime 'container'"
            )
        return self

    # -- convenience accessors used by the runner and routers ---------------

    def phase_names(self) -> list[str]:
        return [p.name for p in self.phases]

    def phase_index(self, name: str) -> int | None:
        for i, p in enumerate(self.phases):
            if p.name == name:
                return i
        return None

    def next_phase_after(self, name: str) -> PhaseSpec | None:
        idx = self.phase_index(name)
        if idx is None or idx + 1 >= len(self.phases):
            return None
        return self.phases[idx + 1]

    def is_final(self, name: str) -> bool:
        return self.phase_index(name) == len(self.phases) - 1

    def llm_step(self, step_id: str) -> LlmStepSpec | None:
        return self.llm.step(step_id)

    def llm_step_ids(self) -> list[str]:
        return [s.id for s in self.llm.steps]

    def setting(self, key: str) -> AgentSettingSpec | None:
        for s in self.settings:
            if s.key == key:
                return s
        return None

    def step_labels(self) -> dict[str, str]:
        """``{step_id: label}`` over every phase's ``steps`` (S7) — what
        the run page reads through the agent listing."""
        labels: dict[str, str] = {}
        for phase in self.phases:
            for step in phase.steps:
                labels.setdefault(step.id, step.label)
        return labels


def load_manifest(agent_dir: Path) -> AgentManifest:
    """Parse + validate ``<agent_dir>/agent.yaml``.

    Raises ``ManifestError`` with a message specific enough to act on —
    the log line it lands in is an agent author's primary debugging tool.
    """
    path = agent_dir / MANIFEST_FILENAME
    if not path.exists():
        raise ManifestError(f"{path} does not exist")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError(
            f"{path} must contain a YAML mapping, got {type(raw).__name__}"
        )
    try:
        return AgentManifest.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError; keep the import light
        raise ManifestError(f"{path} failed validation: {exc}") from exc


def default_manifest(agent) -> AgentManifest:
    """Back-compat manifest for agents registered directly via ``register()``
    without an ``agent.yaml`` (test stubs, embedded fixtures).

    Mirrors the ``AgentProtocol`` base-class shape — the analyze/investigate
    pair with a gate before investigate — which is what every pre-manifest
    agent implemented. Filesystem discovery never uses this: on-disk agents
    must ship a real manifest from B7 on.
    """
    return AgentManifest(
        id=agent.agent_id,
        name=getattr(agent, "display_name", agent.agent_id),
        description=getattr(agent, "description", ""),
        runtime="python-package",
        phases=[
            PhaseSpec(name="analyze"),
            PhaseSpec(name="investigate", approval=True),
        ],
        output=OutputSpec(mode="html_report"),
    )
