from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, Field


class FeedbackSubmit(BaseModel):
    run_id: UUID = Field(
        validation_alias=AliasChoices("run_id", "case_id"),
        description=(
            "The run the feedback is about. `case_id` is accepted as the "
            "deprecated pre-1.0 spelling for one release (blueprint S1 / "
            "decision L18) and stops being accepted at v1.1."
        ),
    )
    # Validated in the router against the run agent's manifest
    # ``feedback_sections`` (blueprint B9) — the chassis declares no
    # section vocabulary of its own.
    section_type: str = Field(min_length=1, max_length=30)
    citation_id: int | None = None
    rating: Literal["positive", "negative"]
    comment: str | None = None
