from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal
from uuid import UUID


@dataclass
class StepProgress:
    step_id: str
    status: str  # pending | running | complete | error | skipped
    detail: str | None = None


@dataclass
class AgentInput:
    run_id: UUID
    tenant_id: UUID
    user_inputs: dict
    prior_analysis: dict | None = None
    user_edits: str | None = None
    # The per-run capability façade (blueprint B13) — an
    # ``app.capabilities.Capabilities`` granted per the agent manifest's
    # ``capabilities:`` list. None only in legacy/direct test harnesses.
    capabilities: object | None = None
    # The invocation's wall-clock budget in seconds (blueprint S4): the
    # manifest phase's ``deadline_seconds`` under the platform ceiling,
    # resolved by the runner, which enforces it. The container runtime
    # forwards it in the POST body and bounds the run token with it.
    # None only in legacy/direct test harnesses.
    deadline_seconds: int | None = None
    # The run's owner (blueprint S4): what the chassis stamps as the
    # attribution of every audit row an agent emits — never an argument
    # the agent supplies. None only in legacy/direct test harnesses.
    user_id: UUID | None = None
    # The run's label (``RUN-1042``), for the identity the relay stamps on a
    # container's telemetry. None only in legacy/direct test harnesses.
    run_number: str | None = None

    @property
    def case_id(self) -> UUID:
        """Deprecated read alias of ``run_id`` (blueprint S1, L18): the
        platform noun is ``run``. Kept for one release so agents written
        against the pre-1.0 spelling keep working; removed at v1.1."""
        return self.run_id


@dataclass
class AnalysisResult:
    display: dict
    structured: dict
    status: str = "awaiting_approval"


@dataclass
class InvestigationResult:
    status: str
    report_html: str | None = None
    structured: dict | None = None
    error: str | None = None


@dataclass
class ConfigField:
    """One setting of the deprecated ``config_meta()`` surface.

    **Deprecated since K5a, removed at v1.2 (L32).** Declare the setting in
    the manifest's ``settings[]`` instead (``app.agents.manifest.
    AgentSettingSpec``: ``field_type`` is ``type`` there and
    ``enum_options`` is ``options``), where each tenant gets its own value.
    Until v1.2 the chassis serves these in the new shapes, marked
    ``meta.deprecated``, and logs ``agent_settings_protocol_deprecated``.
    """

    key: str
    label: str
    field_type: Literal["string", "int", "float", "bool", "enum", "string_list"]
    description: str = ""
    default: Any = None
    enum_options: list[str] | None = None


@dataclass
class AgentConfigMeta:
    """What ``config_meta()`` returns. ``settings`` is **deprecated since
    K5a and removed at v1.2 (L32)**: a manifest's ``settings[]`` replaces
    it, and a manifest that declares any turns this path off for its agent.
    """

    supported_providers: list[str] = field(
        default_factory=lambda: ["openai", "anthropic", "google"]
    )
    step_editable_fields: list[str] = field(
        default_factory=lambda: ["temperature", "max_tokens", "timeout_seconds"]
    )
    settings: list[ConfigField] = field(default_factory=list)


OnProgress = Callable[[StepProgress], Awaitable[None]]


class AgentProtocol:
    """Contract every pluggable agent implements.

    Subclasses must set ``agent_id``, ``display_name``, ``description`` and
    override ``input_schema`` / ``analyze`` / ``investigate``. All other
    methods have sensible defaults so agents only opt in to config surface,
    review schemas, and custom feedback sections as needed.
    """

    agent_id: str
    display_name: str
    description: str

    def input_schema(self) -> dict:
        raise NotImplementedError

    async def analyze(self, inp: AgentInput, on_progress: OnProgress) -> AnalysisResult:
        raise NotImplementedError

    async def investigate(
        self, inp: AgentInput, on_progress: OnProgress
    ) -> InvestigationResult:
        raise NotImplementedError

    async def run_phase(
        self, phase_name: str, inp: AgentInput, on_progress: OnProgress
    ) -> AnalysisResult | InvestigationResult:
        """Dispatch a manifest-declared phase to the agent method of the
        same name (blueprint B7).

        The chassis runner only ever calls this — phase names come from the
        agent's ``agent.yaml``, so the chassis never hardcodes any agent's
        topology. Override for exotic dispatch (e.g. one handler serving
        several phases); the default ``getattr`` covers the normal case of
        one async method per declared phase.
        """
        handler = getattr(self, phase_name, None)
        # Not merely "is there an attribute" but "did the AGENT write
        # one". ``analyze`` and ``investigate`` are defined here, so
        # ``getattr`` finds them for an agent that implements neither and
        # the message below — written for exactly this case — was
        # unreachable for the two phase names most agents declare. What
        # an author saw instead was a bare ``NotImplementedError`` with
        # no indication of which phase or why (Codex round 18 found the
        # bundled template in that state; this is why it was quiet).
        bound = getattr(handler, "__func__", handler)
        base = AgentProtocol.__dict__.get(phase_name)
        inherited = bound is getattr(base, "__func__", base)
        if handler is None or not callable(handler) or inherited:
            raise NotImplementedError(
                f"{type(self).__name__} declares phase {phase_name!r} in its "
                f"manifest but defines no matching method"
            )
        return await handler(inp, on_progress)

    def config_meta(self) -> AgentConfigMeta | None:
        """**Deprecated since K5a, removed at v1.2 (L32).** Declare the
        agent's settings in its manifest's ``settings[]``: each tenant then
        edits its own values on the Settings tab and a run reads them
        through ``caps.config.settings()``. Until v1.2 the settings this
        returns are still served, for an agent that declares no
        ``settings[]``, as one value for every tenant."""
        return None

    def get_settings(self) -> dict[str, Any]:
        """**Deprecated since K5a, removed at v1.2 (L32)**, with
        ``config_meta()``: a run reads its tenant's values through
        ``caps.config.settings()`` (in-process) or the MCP ``config_get``
        tool (containers), and the chassis holds them."""
        return {}

    def update_settings(self, updates: dict[str, Any]) -> None:
        """**Deprecated since K5a, removed at v1.2 (L32)**, with
        ``config_meta()``: the chassis stores each tenant's values in
        ``agent_settings`` itself. Until v1.2 a Save on the deprecated
        path makes one call here with every value it saves, ``null``
        replaced by the declared default."""
        pass

    def review_schema(self) -> dict | None:
        return None

    def render_report_document(self, run, structured: dict) -> str | None:
        """Standalone HTML document for report download / PDF export
        (blueprint B9).

        Called by the shell's report service when a user requests an
        export. Return a complete HTML document string, or ``None`` to let
        the chassis fall back: the cached embedded-fragment HTML when the
        agent produced one (``output.mode: html_report``), else a generic
        rendering of the structured result. The demo agent overrides this to run its
        own Jinja template — the chassis never imports agent modules.
        """
        return None

    def feedback_sections(self) -> list[str]:
        return ["overall"]

    def report_template_path(self) -> str | None:
        return None
