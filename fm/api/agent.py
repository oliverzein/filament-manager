# fm/api/agent.py
"""Agent pull API router — token-authed endpoints for the agent job queue.

The agent polls ``GET /api/agent/jobs`` (Bearer ``agent_token``) to claim the
oldest queued agent-facing job (``apply_preset`` / ``report_presets``), does
the work on the printer, then posts the outcome to
``POST /api/agent/results``.  ``report_presets`` results are stored on the
job's ``result`` column (via :func:`fm.service.jobs.complete`) — exactly where
:func:`fm.service.sync_handlers.handle_temp_sync` reads them (it queries the
most recent ``done`` ``report_presets`` job and decodes ``job.result``).
"""
from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import Config
from ..db import SessionLocal
from ..models import Job
from ..service import jobs

router = APIRouter(prefix="/api/agent", tags=["agent"])

# Agent-facing kinds: no server-side handler, left queued by run_pending.
_AGENT_KINDS = {"apply_preset", "report_presets"}


# ---------------------------------------------------------------------------
# Session dependency (same pattern as catalog/sync routers)
# ---------------------------------------------------------------------------

def get_db():
    """Yield a SessionLocal session (auto-commits on clean exit)."""
    with SessionLocal() as s:
        yield s


DbSession = Annotated[Session, Depends(get_db)]


# ---------------------------------------------------------------------------
# Token auth
# ---------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False)


def _require_token(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Validate the Bearer agent token; raise 401 otherwise.

    ``Config.agent_token`` is required since Task 1; if the printer config is
    unavailable there is no token to validate against, so the request cannot
    be authenticated (401).
    """
    config: Config | None = getattr(request.app.state, "config", None)
    expected = config.agent_token if config is not None else ""
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="agent token not configured",
        )
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )
    if creds.credentials != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid agent token",
        )


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _agent_job_response(job: Job) -> dict:
    """Serialize a job for the agent, including its decoded payload."""
    payload: object = None
    if job.payload:
        try:
            payload = json.loads(job.payload)
        except (json.JSONDecodeError, ValueError):
            payload = job.payload
    result: object = None
    if job.result:
        try:
            result = json.loads(job.result)
        except (json.JSONDecodeError, ValueError):
            result = job.result
    return {
        "job_id": job.id,
        "kind": job.kind,
        "status": job.status,
        "payload": payload,
        "result": result,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/jobs", dependencies=[Depends(_require_token)])
def agent_jobs(db: DbSession) -> dict:
    """Claim the oldest queued agent-facing job (queued -> running).

    Returns ``{"jobs": [...]}`` with at most one claimed job; empty when no
    agent-facing job is queued.  The agent is expected to post the outcome to
    ``POST /api/agent/results`` once the work is done.
    """
    job = jobs.claim_next(db, kinds=_AGENT_KINDS)
    if job is None:
        return {"jobs": []}
    return {"jobs": [_agent_job_response(job)]}


class AgentResult(BaseModel):
    job_id: int
    status: str  # "done" | "error"
    result: dict


@router.post("/results", dependencies=[Depends(_require_token)])
def agent_results(body: AgentResult, db: DbSession) -> dict:
    """Record the outcome of a claimed (running) agent job.

    ``status == "done"`` marks the job done and stores ``result`` on
    ``job.result`` (where ``handle_temp_sync`` reads ``report_presets``
    results).  ``status == "error"`` marks it errored.  Either way an
    ``ActivityLog`` row is appended.
    """
    if body.status not in ("done", "error"):
        raise HTTPException(
            status_code=422,
            detail="status must be 'done' or 'error'",
        )
    job = db.get(Job, body.job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="job not found"
        )
    if job.status != "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"job is not running (status={job.status})",
        )

    if body.status == "done":
        jobs.complete(job, body.result)
        jobs.log_activity(
            db, job.kind, "ok", {"job_id": job.id, "result": body.result}
        )
    else:
        jobs.fail(job, body.result)
        jobs.log_activity(
            db, job.kind, "error", {"job_id": job.id, "error": body.result}
        )
    db.flush()
    return _agent_job_response(job)
