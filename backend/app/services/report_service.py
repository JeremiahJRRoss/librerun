"""Render run reports as HTML/PDF.

Two responsibilities, both agent-agnostic since blueprint B9:

1. ``render_embedded`` returns the cached embedded-fragment HTML the
   frontend injects on the case page. It comes straight from
   ``RunSnapshot.report_html`` (written by the agent runner when the
   manifest's final phase completes, from the agent's renderer).
2. ``generate_report`` turns the same agent result into a downloadable
   HTML/PDF document. The agent supplies the document via its
   ``render_report_document`` hook (the demo agent runs its own Jinja template
   behind it); agents without one fall back to the cached embedded
   fragment, and structured-mode agents to a generic rendering of their
   structured result. The chassis imports no agent modules and knows no
   agent's vocabulary.
"""
from __future__ import annotations

import html as _html
import json
from pathlib import Path
from uuid import UUID

import structlog
from sqlalchemy import select

from app.agents.registry import get_agent
from app.config import settings
from app.database import async_session
from app.logging_pii import user_content
from app.models import AsyncTask, Run, RunSnapshot

logger = structlog.get_logger(__name__)


def _generic_document(run, structured: dict | None, report_html: str | None) -> str:
    """Fallback standalone document for agents without a document renderer.

    Prefers the agent's embedded fragment; otherwise renders the
    structured result as definition lists — plain, but faithful, and it
    keeps export working for structured-mode agents out of the box.
    """
    if report_html:
        body = report_html
    elif structured:
        parts = []
        for key, value in structured.items():
            title = _html.escape(str(key).replace("_", " ").title())
            rendered = _html.escape(json.dumps(value, indent=2, default=str))
            parts.append(f"<h2>{title}</h2><pre>{rendered}</pre>")
        body = "\n".join(parts)
    else:
        body = "<p>No result content available.</p>"
    run_number = _html.escape(getattr(run, "run_number", "") or "")
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>Run report {run_number}</title>"
        "<style>body{font-family:sans-serif;margin:2rem;}"
        "pre{white-space:pre-wrap;background:#f6f6f6;padding:.75rem;}</style>"
        f"</head><body><h1>Run report {run_number}</h1>{body}</body></html>"
    )


async def render_embedded(run_id: UUID) -> str:
    """Return the cached embedded-fragment HTML for the case detail page."""
    async with async_session() as db:
        snap = (
            await db.execute(
                select(RunSnapshot).where(RunSnapshot.run_id == run_id)
            )
        ).scalar_one_or_none()
    if snap is None or not snap.report_html:
        raise RuntimeError(f"No rendered report for run {run_id}")
    return snap.report_html


async def generate_report(task_id: UUID, run_id: UUID, fmt: str) -> None:
    """Render a downloadable report through the agent's document hook."""
    async with async_session() as db:
        task = await db.get(AsyncTask, task_id)
        if task is None:
            return
        try:
            task.status = "running"
            await db.commit()

            run = await db.get(Run, run_id)
            snap = (
                await db.execute(
                    select(RunSnapshot).where(RunSnapshot.run_id == run_id)
                )
            ).scalar_one_or_none()
            if run is None or snap is None:
                raise RuntimeError("Run or snapshot not found")
            # ``{}`` is a legitimate completed result (a no-findings run) —
            # only the complete absence of both outputs is unexportable.
            if snap.structured_data is None and not snap.report_html:
                raise RuntimeError("Run has no result to export")

            html = None
            agent = get_agent(run.agent_id) if run.agent_id else None
            if agent is not None and snap.structured_data is not None:
                html = agent.render_report_document(run, snap.structured_data)
            if html is None:
                html = _generic_document(run, snap.structured_data, snap.report_html)

            reports_dir = Path(settings.FILE_STORAGE_PATH) / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            out_path = reports_dir / f"{task_id}.{fmt}"

            if fmt == "html":
                out_path.write_text(html, encoding="utf-8")
            elif fmt == "pdf":
                try:
                    from weasyprint import HTML  # type: ignore

                    HTML(string=html).write_pdf(str(out_path))
                except Exception as e:
                    logger.warning(
                        "weasyprint_failed",
                        task_id=str(task_id),
                        fallback="html",
                        error=user_content(str(e)),
                    )
                    out_path = reports_dir / f"{task_id}.html"
                    out_path.write_text(html, encoding="utf-8")
                    fmt = "html"
            else:
                raise ValueError(f"Unknown format: {fmt}")

            task.result_url = f"/api/v1/tasks/{task_id}/download"
            task.status = "complete"
            await db.commit()
        except Exception as e:
            logger.exception(
                "report_generation_failed",
                task_id=str(task_id),
                error=user_content(str(e)),
            )
            task.status = "error"
            task.error_message = str(e)[:500]
            await db.commit()
