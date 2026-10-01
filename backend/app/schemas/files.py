from pydantic import BaseModel


class RedactionEntry(BaseModel):
    original_placeholder: str
    pii_type: str
    confidence: float


class PiiRedactionPreview(BaseModel):
    redacted_content: str
    redactions_applied: list[RedactionEntry]
    original_size_bytes: int
