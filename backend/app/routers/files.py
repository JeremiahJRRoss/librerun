import os
from pathlib import Path
from uuid import UUID, uuid4

import aiofiles
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.middleware import get_current_user
from app.models import Run, RunFile, User
from app.schemas.files import PiiRedactionPreview, RedactionEntry
from app.services import pii_service
from app.services.pii_service import redact

router = APIRouter(tags=["files"])

# File-upload limits previously lived in the shell's config.json under
# the ``files`` key. Phase 6 inlined them here as the only consumer left.
_MAX_LOG_SIZE_MB = 10
_ALLOWED_LOG_EXTENSIONS = {".log", ".txt", ".jsonl", ".out", ".err"}
_MAX_CONFIG_SIZE_MB = 5
_ALLOWED_CONFIG_EXTENSIONS = {".json", ".yaml", ".yml", ".toml", ".conf", ".ini"}


def _validate_upload(file_type: str, upload: UploadFile, size_bytes: int) -> None:
    if file_type not in ("log", "config"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "file_type must be 'log' or 'config'")
    if file_type == "log":
        max_mb = _MAX_LOG_SIZE_MB
        allowed = _ALLOWED_LOG_EXTENSIONS
    else:
        max_mb = _MAX_CONFIG_SIZE_MB
        allowed = _ALLOWED_CONFIG_EXTENSIONS
    if size_bytes > max_mb * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"File exceeds {max_mb} MB")
    ext = os.path.splitext(upload.filename or "")[1].lower()
    if ext not in allowed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Extension {ext} not allowed")


@router.post("/files/redact-preview", response_model=PiiRedactionPreview)
async def redact_preview(
    file: UploadFile = File(...),
    file_type: str = Form("log"),
    user: User = Depends(get_current_user),
):
    """Redact an uploaded file and return the result for form prefill.

    Generalized in blueprint B8: any ``x-pii``-marked schema field can
    offer this preview — ``file_type`` selects the extension/size policy
    (``log`` or ``config``), and the old demo-agent-shaped ``vendor_side``
    parameter is gone (it never influenced the output).

    Answers ``503 pii_detector_unavailable`` when the detector is not
    ready (blueprint S4c): a preview whose whole promise is "this is
    what we would store" must not be able to show text that a broken
    stage 3 left whole.
    """
    pii_service.require_ready(stage="redact_preview")
    raw = await file.read()
    _validate_upload(file_type, file, len(raw))

    # There is deliberately no `try/except` around this decode, and a
    # `-> latin-1` fallback should not be added back.
    #
    # `errors="replace"` cannot raise a DECODING error: CPython resolves
    # the name "replace" to an internal fast path before any
    # error-handler lookup, so malformed input becomes U+FFFD and no
    # handler runs — not even one rebound through
    # `codecs.register_error`. Exhaustive over every 1-, 2- and 3-byte
    # input and every 4-byte boundary class.
    #
    # How MANY replacement characters a given malformed input yields is
    # deliberately not stated here. It is per maximal invalid subpart
    # (Unicode TR#36), which is neither per byte nor per sequence, and
    # this comment got it wrong three times trying to say so. Nothing
    # here rests on it, and the test that does derives the count from
    # Python rather than from a sentence.
    #
    # It CAN raise `MemoryError`, which `except Exception` would have
    # caught, and for malformed-heavy input latin-1 can allocate less
    # (U+FFFD forces a two-byte representation while latin-1 output
    # stays one-byte) — so the fallback was not useless there. How much
    # less depends entirely on the input and is sometimes nothing at
    # all; §12 208 has the measurements. It is still not worth having:
    # `_validate_upload` bounds this input at 10 MB before this line,
    # and the fallback's success mode is silent mojibake in content this
    # platform promises to redact — worse than the 500 it avoids.
    #
    # The rationale above was narrowed twice in review; deviations entry
    # 208 of the development record has the measurements and what each
    # earlier version got wrong.
    #
    # Keeping it would also have kept a permanent hole in the coverage
    # report: no ordinary input can enter that branch, so no test could
    # ever cover it — the same shape as the degradation path that could
    # never degrade in §12 207. Both copies in this file went together.
    text = raw.decode("utf-8", errors="replace")

    redacted, redactions = redact(text)
    return PiiRedactionPreview(
        redacted_content=redacted,
        redactions_applied=[
            RedactionEntry(
                original_placeholder=r.placeholder, pii_type=r.pii_type, confidence=r.confidence
            )
            for r in redactions
        ],
        original_size_bytes=len(raw),
    )


@router.post("/runs/{run_id}/files", status_code=status.HTTP_201_CREATED)
async def upload_run_file(
    run_id: UUID,
    request: Request,
    file: UploadFile = File(...),
    vendor_side: str = Form(...),
    file_type: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Blueprint S4c: the same refusal as the preview. This handler
    # writes the redacted text to disk AND a row that claims
    # ``pii_redaction_applied=True``, so a degraded pass here stores a
    # false claim as well as the text it is false about.
    pii_service.require_ready(stage="file_upload")
    tenant_id = request.state.tenant_id
    run = await db.get(Run, run_id)
    if run is None or run.tenant_id != tenant_id or run.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if user.role != "admin" and run.user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your run")

    raw = await file.read()
    if vendor_side not in ("a", "b"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "vendor_side must be 'a' or 'b'")
    _validate_upload(file_type, file, len(raw))

    # The same fallback, removed with it — see `redact_preview` above
    # for why a decoding error cannot reach it and why the deletion
    # still stands for the memory case. It was here twice, which is why
    # the negative test asserts the anchor is unique.
    text = raw.decode("utf-8", errors="replace")
    redacted, _ = redact(text)

    storage_dir = Path(settings.FILE_STORAGE_PATH) / str(tenant_id) / str(run_id)
    storage_dir.mkdir(parents=True, exist_ok=True)
    file_id = uuid4()
    storage_path = storage_dir / f"{file_id}_{file.filename or 'file'}"
    async with aiofiles.open(storage_path, "w") as f:
        await f.write(redacted)

    cf = RunFile(
        id=file_id,
        run_id=run.id,
        tenant_id=tenant_id,
        vendor_side=vendor_side,
        file_type=file_type,
        original_name=file.filename or "file",
        storage_path=str(storage_path),
        file_size_bytes=len(redacted.encode("utf-8")),
        mime_type=file.content_type,
        pii_redaction_applied=True,
    )
    db.add(cf)
    await db.flush()
    return {"file_id": str(file_id), "storage_path": str(storage_path)}


@router.get("/runs/{run_id}/files")
async def list_run_files(
    run_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = request.state.tenant_id
    run = await db.get(Run, run_id)
    if run is None or run.tenant_id != tenant_id or run.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if user.role != "admin" and run.user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your run")

    files = (
        await db.execute(
            select(RunFile).where(RunFile.run_id == run_id, RunFile.tenant_id == tenant_id)
        )
    ).scalars().all()
    return [
        {
            "id": str(f.id),
            "vendor_side": f.vendor_side,
            "file_type": f.file_type,
            "original_name": f.original_name,
            "file_size_bytes": f.file_size_bytes,
            "uploaded_at": f.uploaded_at.isoformat(),
        }
        for f in files
    ]


@router.get("/runs/{run_id}/files/{file_id}")
async def download_run_file(
    run_id: UUID,
    file_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = request.state.tenant_id
    cf = await db.get(RunFile, file_id)
    if cf is None or cf.tenant_id != tenant_id or cf.run_id != run_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    run = await db.get(Run, run_id)
    if run is None or run.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if user.role != "admin" and run.user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your run")

    return FileResponse(cf.storage_path, filename=cf.original_name)
