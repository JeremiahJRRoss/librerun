from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class GoogleLoginRequest(BaseModel):
    credential: str


class MicrosoftLoginRequest(BaseModel):
    code: str
    redirect_uri: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserProfile(BaseModel):
    id: UUID
    email: str
    role: str
    tenant_id: UUID
    display_name: str | None = None
    is_platform_admin: bool = Field(
        ...,
        description=(
            "Whether this user administers the deployment: an admin of the "
            "tenant whose slug is PLATFORM_TENANT_SLUG. A display hint for "
            "the UI; every route keeps its own gate."
        ),
    )
