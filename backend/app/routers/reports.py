from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.middleware import get_current_user
from app.models import AsyncTask, Run, User
from app.services.report_service import generate_report, render_embedded

router = APIRouter(tags=["reports"])


@router.get("/runs/{run_id}/report/embedded")
async def get_embedded_report(
    run_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the rendered report body as an HTML string for the React page.

    Tenant-scoped + owner-checked: only the case owner (or tenant admin)
    can fetch it. The returned HTML is the same template as the PDF/HTML
    download, with ``embedded=True`` (no ``<html>``/``<head>`` wrapper and
    a CSS prefix the agent's template scopes its own styles under).
    """
    tenant_id = request.state.tenant_id
    run = await db.get(Run, run_id)
    if run is None or run.tenant_id != tenant_id or run.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if user.role != "admin" and run.user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    if run.status != "complete":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Report unavailable while status={run.status}",
        )
    html = await render_embedded(run_id)
    return {"html": html}


class ReportCreate(BaseModel):
    format: str = "html"  # "html" | "pdf"


@router.post("/runs/{run_id}/report", status_code=status.HTTP_202_ACCEPTED)
async def create_report(
    run_id: UUID,
    payload: ReportCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = request.state.tenant_id
    run = await db.get(Run, run_id)
    if run is None or run.tenant_id != tenant_id or run.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if user.role != "admin" and run.user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    if payload.format not in ("html", "pdf"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "format must be html or pdf")

    task = AsyncTask(
        tenant_id=tenant_id,
        user_id=user.id,
        task_type="report_generate",
        status="pending",
    )
    db.add(task)
    await db.flush()
    task_id = task.id
    await db.commit()

    background_tasks.add_task(generate_report, task_id, run_id, payload.format)
    return {"task_id": str(task_id)}


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(AsyncTask, task_id)
    if task is None or task.tenant_id != request.state.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return {"status": task.status, "result_url": task.result_url, "error": task.error_message}


@router.get("/tasks/{task_id}/download")
async def download_task(
    task_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(AsyncTask, task_id)
    if task is None or task.tenant_id != request.state.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if task.status != "complete":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Task status: {task.status}")

    reports_dir = Path(settings.FILE_STORAGE_PATH) / "reports"
    for ext in ("pdf", "html"):
        p = reports_dir / f"{task_id}.{ext}"
        if p.exists():
            media = "application/pdf" if ext == "pdf" else "text/html"
            return FileResponse(str(p), media_type=media, filename=f"report_{task_id}.{ext}")
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Report file missing")
