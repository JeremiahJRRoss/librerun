from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.run import RunDetail


class SecretSettingState(BaseModel):
    """What the API shows of a secret setting (K6, L31): where its value
    comes from and a fingerprint — never the value."""

    set: bool = Field(
        ...,
        description="True when the encrypted secrets store holds a row for this setting "
        "(source runtime or unreadable); updated_at and updated_by describe that row",
    )
    source: Literal["runtime", "env", "unset", "unreadable"] = Field(
        ...,
        description="runtime: the store's row, which a configured key opens; env: no row, "
        "the environment variable's value applies; unset: neither; unreadable: the store "
        "holds a row no configured key opens (a lost or rotated key) and the environment "
        "applies meanwhile — Replace re-encrypts it under the current key, or Clear it",
    )
    fingerprint: str | None = Field(
        None,
        description="A keyed digest of the value under the store key, 12 hex characters; "
        "null unless source is runtime",
    )
    updated_at: datetime | None = Field(None, description="When the row was last set")
    updated_by: UUID | None = Field(None, description="Who last set the row")


class SettingResponse(BaseModel):
    """Effective value of a runtime-tunable application setting."""

    key: str = Field(..., description="Unique setting identifier")
    value: Any = Field(
        ..., description="Effective value (DB override or default); always null for a secret"
    )
    default_value: Any = Field(
        ..., description="Value used when no DB override exists; always null for a secret"
    )
    value_type: Literal["string_list", "int", "bool", "string", "secret"] = Field(
        ..., description="Expected value shape; drives UI editor selection"
    )
    description: str = Field(..., description="Human-readable explanation")
    is_default: bool = Field(..., description="True when no DB override is set")
    updated_at: datetime | None = Field(None, description="Timestamp of the last override write")
    updated_by: UUID | None = Field(None, description="User who last wrote the override")
    secret: SecretSettingState | None = Field(
        None, description="A secret setting's state (write-only: never its value); null for other types"
    )


class SettingUpdate(BaseModel):
    """Payload for updating a single app setting."""

    value: Any = Field(..., description="New value; coerced to the setting's declared type")


class UserEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    display_name: str | None
    role: str
    auth_provider: str
    is_active: bool
    last_sign_in: datetime | None


class UserInvite(BaseModel):
    email: str
    role: Literal["admin", "customer"] = "customer"


class UserUpdate(BaseModel):
    role: Literal["admin", "customer"] | None = None
    is_active: bool | None = None


class AuthConfig(BaseModel):
    google_enabled: bool = True
    google_allowed_domains: list[str] = []
    google_allowed_emails: list[str] = []
    microsoft_enabled: bool = True
    microsoft_allowed_tenants: list[str] = []
    microsoft_allowed_emails: list[str] = []
    credentials_enabled: bool = False
    # No session_timeout_hours. Nothing ever read it, and session lifetime is
    # now the live platform setting `session_timeout_minutes`, applied by
    # auth_service.session_lifetime(). Removed before 1.0 rather than wired:
    # per-tenant session policy is not one of the three promises (L14), and
    # the seeded default of 24 could not express "inherit the platform value".


class AuditLogEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    action_type: str
    user_email: str | None
    detail: dict | None
    ip_address: str | None = None
    created_at: datetime

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row.id,
            action_type=row.action_type,
            user_email=row.user_email,
            detail=row.detail,
            ip_address=str(row.ip_address) if row.ip_address is not None else None,
            created_at=row.created_at,
        )


class FeedbackAggregateSection(BaseModel):
    section_type: str
    positive: int
    negative: int
    positive_rate: float


class FeedbackAggregate(BaseModel):
    total: int
    positive: int
    negative: int
    positive_rate: float
    per_section: list[FeedbackAggregateSection]
    recent_negatives: list[dict]


class AdminRunDetail(RunDetail):
    """Admin-only run detail. Adds observability fields that are deliberately
    kept off the customer ``RunDetail`` response."""

    trace_id: str | None = None
    phase2_span_id: str | None = None
    # The operator-facing reason the run ended ``error`` (blueprint S7):
    # the agent's own failure text, the exception, the phase a restart cut
    # short — redacted before it was stored. Admin-only because the Run
    # Contract makes an agent's ``failed.error`` and log lines
    # operator-facing; the customer detail carries the chassis's own
    # ``error_code`` / ``error_message`` instead.
    error_detail: str | None = None


# ---- Model providers (K7; L33, D16) ----


class ProviderEntry(BaseModel):
    """One provider-key name as the gateway reports it — never a key."""

    name: Literal["openai", "anthropic", "google"] = Field(
        ..., description="One per provider-key variable of the gateway (K7-02)"
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="The provider spellings a step may name that this key serves; empty "
        "until the gateway has reported",
    )
    source: Literal["runtime", "env", "unset"] | None = Field(
        None,
        description="runtime: a key pasted here, which the gateway holds and serves; env: "
        "the gateway's environment serves this provider; unset: neither; null until the gateway "
        "reports",
    )
    fingerprint: str | None = Field(
        None,
        description="A keyed digest of a stored key under the gateway's store key, 12 hex "
        "characters — the gateway's to compute; null unless source is runtime",
    )
    set_by: UUID | None = Field(None, description="Who last set the stored row")
    set_at: datetime | None = Field(None, description="When the stored row was last set")
    row: Literal["runtime", "pending", "rejected"] | None = Field(
        None,
        description="The stored row's state: runtime (in effect), pending (the gateway has "
        "not processed it yet), rejected (it will not open; the environment serves "
        "meanwhile); null with no row",
    )
    reason: Literal["unsealable", "unopenable"] | None = Field(
        None,
        description="Why a row was rejected: unsealable (the blob does not open under the "
        "gateway's sealing key for this provider), unopenable (a stored row no configured "
        "store key opens)",
    )


class ProvidersStatus(BaseModel):
    """``GET /admin/providers``: what the gateway holds, from the row it writes."""

    reported: bool = Field(..., description="Whether the gateway has written its status row")
    stub: bool | None = Field(None, description="Keyless mode, as the gateway reported it")
    gateway_version: str | None = None
    updated_at: datetime | None = Field(None, description="When the gateway last wrote its row")
    public_key_pem: str | None = Field(
        None,
        description="The public key a provider key is sealed to in the browser (RSA-OAEP, "
        "SHA-256, 3072 bits); null while the gateway's store key is blank. Its fingerprint, "
        "SHA256: over the SPKI DER, is computed by each reader and compared with the "
        "gateway's boot log (D34)",
    )
    providers: list[ProviderEntry]


class ProviderKeyUpdate(BaseModel):
    """A provider key, sealed in the browser to the gateway's public key.
    Never the key itself: the backend cannot open this, and refuses a body
    that is not one 3072-bit OAEP block."""

    sealed: str = Field(
        ...,
        description="base64 of the RSA-OAEP-SHA256 ciphertext, label "
        "librerun-provider-key:v1:<name> — 384 bytes decoded",
    )


class ProviderKeyAccepted(BaseModel):
    """``202``: stored for the gateway to adopt; only it can fingerprint the key."""

    name: str
    row: Literal["pending"] = "pending"


# ---- Certificates at the edge (T2; L42, L43, D44) ----


TlsNeed = Literal[
    "edge_off", "edge_restart", "trust_root", "root_changed", "files_ending", "ca_ending", "acme_requirements"
]


class TlsCertificate(BaseModel):
    """One certificate's public facts — never a key."""

    subject: str | None = None
    issuer: str | None = None
    names: list[str] = Field(default_factory=list, description="Its subject alternative names")
    not_before: datetime | None = None
    not_after: datetime | None = None
    sha256: str = Field(..., description="SHA-256 of the certificate's DER, hex")


class TlsRoot(TlsCertificate):
    """The root the edge issues from, when its issuer is its own CA."""

    ca: str = Field(
        ...,
        description="The CA's issuer id: local (the one the edge generated), env-<12 hex> "
        "(LIBRERUN_TLS_CA's) or loaded-<12 hex> (loaded on this page)",
    )
    name: str | None = Field(None, description="The CA's name, as the edge reports it")


class TlsIssuer(BaseModel):
    kind: Literal["internal", "acme", "files"]
    ca: str | None = Field(None, description="internal: the CA's issuer id")
    email: str | None = Field(None, description="acme: the account e-mail")


class TlsChoice(BaseModel):
    """What a platform admin chose on this page, which wins over the
    environment until "Use the environment's setting"."""

    kind: Literal["ca", "files", "acme"]
    by: UUID | None = Field(None, description="Who chose, by user id")
    by_email: str | None = Field(None, description="Who chose, in this tenant")
    at: datetime | None = Field(None, description="When")


class TlsEnvironment(BaseModel):
    """What the environment selects: LIBRERUN_TLS, or LIBRERUN_TLS_CA's CA."""

    variable: Literal["LIBRERUN_TLS", "LIBRERUN_TLS_CA"]
    ca: str | None = None


class TlsStatus(BaseModel):
    """``GET /admin/tls``: what the edge serves and what it needs next (L43).
    Public material alone: no key is ever in it (L42)."""

    edge: Literal["on", "off"] = Field(
        ..., description="off: edge-control did not answer, so the tls profile is off and this is plain HTTP"
    )
    site: list[str] = Field(default_factory=list, description="The names the edge answers for")
    issuer: TlsIssuer | None = None
    root: TlsRoot | None = None
    leaf: TlsCertificate | None = Field(None, description="The certificate the edge serves, read by a handshake")
    source: Literal["environment", "ui"] | None = Field(
        None, description="environment: no choice is recorded here; ui: a choice made on this page"
    )
    choice: TlsChoice | None = None
    environment: TlsEnvironment | None = None
    why: str | None = Field(None, description="Why the source in effect is the one in effect")
    needs: list[TlsNeed] = Field(default_factory=list, description="What the admin must do next, each with its action")
    acknowledged_root: str | None = Field(
        None, description="The SHA-256 of the root last acknowledged as trusted (tls.last_root)"
    )


# ---- The deployment view and the agent keys (K9; D16, D29, D43) ----


class DeploymentSetting(BaseModel):
    """One allowlisted name, as the backend reads it: never a secret."""

    name: str
    env_class: Literal[1, 2] = Field(
        ..., description="Its class in .env.example: 1 bootstrap, 2 posture"
    )
    value: str | int | float | bool | None = Field(
        None, description="The value the backend runs with; a URL without its userinfo, query or fragment"
    )
    source: Literal["env", "default"] = Field(
        ...,
        description="env: set in the process environment or .env, compose's defaults included; "
        "default: the settings model's own",
    )
    hint: str = Field(..., description="Where to change it")


class DeploymentHeader(BaseModel):
    """An OTLP header variable, by presence alone: it can carry a credential."""

    name: str
    set: bool


class DeploymentProvider(BaseModel):
    name: str
    source: Literal["runtime", "env", "unset"] | None = None
    fingerprint: str | None = None


class DeploymentGateway(BaseModel):
    reachable: bool = Field(..., description="Whether the gateway's /healthz answered")
    reported: bool = Field(..., description="Whether the gateway has written its status row")
    version: str | None = None
    updated_at: datetime | None = Field(None, description="When the gateway last wrote its row")
    providers: list[DeploymentProvider] = Field(default_factory=list)


class DeploymentTransport(BaseModel):
    """The request's own scheme and host (D43), never a variable's."""

    scheme: str
    host: str | None = None


class DeploymentView(BaseModel):
    """``GET /admin/deployment``: the posture read-only, each value with its
    source (K9-04). An allowlist of names: never a secret, the environment
    or a URL's userinfo or query."""

    version: str
    license: str
    source_url: str | None = Field(None, description="Where the source of this version is, stripped as a URL")
    demo: bool
    stub: bool | None = Field(None, description="Keyless mode, as the gateway's /healthz says; null when it is down")
    gateway: DeploymentGateway
    settings: list[DeploymentSetting]
    otlp_headers: list[DeploymentHeader]
    transport: DeploymentTransport


class AgentKeyRow(BaseModel):
    """One installed agent key: never its value (D10)."""

    agent_id: str
    key_prefix: str = Field(..., description="The eight characters after lr_agent_")
    source: Literal["env", "admin"] = Field(
        ..., description="env: installed from the gateway's environment, rotated in .env; admin: issued here"
    )
    role: Literal["current", "previous"]
    issued_at: datetime
    issued_by: UUID | None = None
    previous_since: datetime | None = None
    previous_until: datetime | None = None
    last_used_at: datetime | None = None
    rotatable: bool = Field(..., description="Whether this page can rotate or revoke it: admin keys alone")
    registered: bool = Field(..., description="Whether an agent of this id is registered in this backend")


class IssuedAgentKey(BaseModel):
    """``201``: the value, in this response and nowhere else, ever again."""

    agent_id: str
    key: str
    key_prefix: str


class RotatedAgentKey(IssuedAgentKey):
    """The new current key; the previous one works for ``grace_hours``."""

    grace_hours: int
