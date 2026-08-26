# fm/api/sync.py
"""Sync orchestration API router — push, preset, tempsync, drift status,
job resolve.

Importing this module registers the sync handlers into ``jobs.HANDLERS``
(via ``sync_handlers.register_handlers`` called at import time).
"""
from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Config
from ..db import SessionLocal
from ..models import Job, Profile, SyncTarget
from ..service import jobs
from ..service.sync_handlers import refresh_preset_drift, register_handlers  # noqa: F401 — side-effect import

router = APIRouter(prefix="/api", tags=["sync"])

# Ensure handlers are registered (idempotent — dict assignment).
register_handlers()


# ---------------------------------------------------------------------------
# Session dependency
# ---------------------------------------------------------------------------

def get_db():
    """Yield a SessionLocal session (auto-commits on clean exit)."""
    with SessionLocal() as s:
        yield s


DbSession = Annotated[Session, Depends(get_db)]


# ---------------------------------------------------------------------------
# Config from app state
# ---------------------------------------------------------------------------

def _get_config(request: Request) -> Config:
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="printer not configured (set FM_PRINTER_IP etc.)",
        )
    return config


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class PushRequest(BaseModel):
    profile_ids: list[int] | None = None
    force_reboot: bool = False
    no_reboot: bool = False


class TempSyncRequest(BaseModel):
    profile_ids: list[int] | None = None


class ResolveChoice(BaseModel):
    choice: str  # "force_reboot" | "wait" | "no_reboot"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job_response(job: Job) -> dict:
    result = None
    if job.result:
        try:
            result = json.loads(job.result)
        except (json.JSONDecodeError, ValueError):
            result = job.result
    return {
        "job_id": job.id,
        "kind": job.kind,
        "status": job.status,
        "result": result,
    }


# ---------------------------------------------------------------------------
# Sync endpoints
# ---------------------------------------------------------------------------

@router.post("/sync/push")
def sync_push(body: PushRequest, request: Request, db: DbSession) -> dict:
    """Enqueue a push job and drain the queue."""
    config = _get_config(request)
    payload = {
        "profile_ids": body.profile_ids,
        "force_reboot": body.force_reboot,
        "no_reboot": body.no_reboot,
    }
    job = jobs.enqueue(db, "push", payload)
    db.flush()
    jobs.run_pending(db, config)
    db.flush()
    return _job_response(job)


@router.post("/sync/preset/{profile_id}")
def sync_preset(profile_id: int, request: Request, db: DbSession) -> dict:
    """Enqueue a generate_preset job for ``profile_id`` and drain the queue."""
    config = _get_config(request)
    job = jobs.enqueue(db, "generate_preset", {"profile_id": profile_id})
    db.flush()
    jobs.run_pending(db, config)
    db.flush()
    return _job_response(job)


@router.post("/sync/tempsync")
def sync_tempsync(body: TempSyncRequest, request: Request, db: DbSession) -> dict:
    """Enqueue a temp_sync job and drain the queue."""
    config = _get_config(request)
    payload = {"profile_ids": body.profile_ids}
    job = jobs.enqueue(db, "temp_sync", payload)
    db.flush()
    jobs.run_pending(db, config)
    db.flush()
    return _job_response(job)


@router.get("/status/drift")
def drift_status(db: DbSession) -> list[dict]:
    """Per-profile drift matrix: printer_db + orca_preset SyncTarget status."""
    refresh_preset_drift(db)
    profiles = list(db.scalars(select(Profile).order_by(Profile.id)))
    result = []
    for p in profiles:
        entry: dict = {
            "profile_id": p.id,
            "name": p.name,
            "brand": p.brand,
            "printer_db_id": p.printer_db_id,
        }
        for kind in ("printer_db", "orca_preset"):
            target = db.scalars(
                select(SyncTarget).where(
                    SyncTarget.profile_id == p.id,
                    SyncTarget.kind == kind,
                )
            ).first()
            entry[kind] = {
                "status": target.status if target else "pending",
                "expected_hash": target.expected_hash if target else None,
            }
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Job resolve + query
# ---------------------------------------------------------------------------

@router.post("/jobs/{job_id}/resolve")
def resolve_job(
    job_id: int, body: ResolveChoice, request: Request, db: DbSession
) -> dict:
    """Resolve an awaiting_input job: update payload, re-queue, drain."""
    config = _get_config(request)
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "awaiting_input":
        raise HTTPException(
            status_code=409, detail="job is not awaiting input"
        )

    payload = json.loads(job.payload) if job.payload else {}
    if body.choice == "force_reboot":
        payload["force_reboot"] = True
    elif body.choice == "no_reboot":
        payload["no_reboot"] = True
    elif body.choice == "wait":
        pass  # retry as-is
    else:
        raise HTTPException(
            status_code=422,
            detail="invalid choice; expected force_reboot|wait|no_reboot",
        )

    job.payload = json.dumps(payload)
    job.status = "queued"
    job.result = None
    db.flush()
    jobs.run_pending(db, config)
    db.flush()
    return _job_response(job)


@router.get("/jobs")
def list_jobs(db: DbSession, since: int = 0) -> list[dict]:
    """List jobs with id > ``since`` (for UI progress polling)."""
    rows = list(db.scalars(
        select(Job).where(Job.id > since).order_by(Job.id)
    ))
    return [_job_response(j) for j in rows]


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: DbSession) -> dict:
    """Return a single job's status + result (used by tests + UI polling)."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_response(job)
