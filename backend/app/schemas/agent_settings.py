"""The body of ``PUT /agents/{id}/config/settings`` (K5a).

A list of ``{key, value}``, one entry per setting the page saves. The
server holds each value to the type the agent's manifest declares, so the
model here takes any JSON value and leaves the judging to the setting's
own ``coerce``: a type check here would be a second copy of the rules,
and the one that would drift.
"""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentSettingUpdate(BaseModel):
    """One setting to save. ``value: null`` returns it to the agent's
    default, which is also what a value equal to the default does."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    # Required, and null is a value: the clear.
    value: Any
