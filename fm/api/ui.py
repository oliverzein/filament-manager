# fm/api/ui.py
"""UI page routes — server-side rendered Jinja2 + HTMX interactivity.

Functional pages only; visual/layout refinement is a separate designer pass
(see AGENTS.md "Designer role for UI").
"""
from __future__ import annotations

import html as _html
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..config import Config
from ..models import ActivityLog, Job, ProfileCache, Spool, SyncTarget
from ..service import jobs
from ..service import sync_handlers
from ..service.cache_sync import sync_profile_cache
from ..printer.dbcodec import find_entry, load_db, save_db, remove_entry
from .sync import _get_config
from ..settings_store import (
    load_settings, save_settings, build_config,
    load_orca_token, save_orca_token,
    load_github_config,
)
from ..orca_cloud import OrcaCloudClient, OrcaCloudError
from ..github_backup import GitHubBackupClient, GitHubBackupError


# status_class is defined in app.py but importing it there creates a circular
# import (app.py imports ui).  The mapping is trivial so we mirror it locally.
def _status_class(value: str) -> str:
    """Map status values to CSS badge class names (mirrors app.status_class)."""
    return {"drift": "drifted", "done": "completed"}.get(value, value)

router = APIRouter(tags=["ui"])


# ---------------------------------------------------------------------------
# Session dependency (same pattern as catalog/sync routers)
# ---------------------------------------------------------------------------

def get_db():
    """Yield a SessionLocal session (auto-commits on clean exit)."""
    with SessionLocal() as s:
        yield s


DbSession = Annotated[Session, Depends(get_db)]


def _templates(request: Request):
    return request.app.state.templates


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_local(dt: datetime | None) -> str | None:
    """Format a UTC datetime as local time string (HH:MM:SS)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")



# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: DbSession) -> HTMLResponse:
    """Spool count by preset (top 10), preset sync status, last 20 activity
    rows, cached preset count, total spool inventory, and printer DB target
    stats."""
    spool_counts = db.execute(
        select(
            ProfileCache.cloud_resource_id,
            ProfileCache.vendor,
            ProfileCache.name,
            func.count(Spool.id).label("spool_count"),
        )
        .join(Spool, Spool.cloud_resource_id == ProfileCache.cloud_resource_id, isouter=True)
        .where(ProfileCache.deleted_from_cloud == False)  # noqa: E712
        .group_by(ProfileCache.cloud_resource_id)
        .order_by(func.count(Spool.id).desc(), ProfileCache.name)
        .limit(10)
    ).all()

    total_spools = db.scalar(select(func.count(Spool.id))) or 0
    cached_presets = db.scalar(
        select(func.count(ProfileCache.cloud_resource_id))
        .where(ProfileCache.deleted_from_cloud == False)  # noqa: E712
    ) or 0
    pdb_in_sync = db.scalar(
        select(func.count(SyncTarget.id))
        .where(SyncTarget.kind == "printer_db",
               SyncTarget.status == "in_sync")
    ) or 0
    pdb_total = db.scalar(
        select(func.count(SyncTarget.id))
        .where(SyncTarget.kind == "printer_db")
    ) or 0

    presets = _preset_status_rows(db)

    activity = list(
        db.scalars(
            select(ActivityLog).order_by(ActivityLog.ts.desc()).limit(20)
        )
    )

    return _templates(request).TemplateResponse(
        request, "dashboard.html",
        {
            "spool_counts": spool_counts, "presets": presets,
            "activity": activity,
            "total_spools": total_spools, "cached_presets": cached_presets,
            "pdb_in_sync": pdb_in_sync, "pdb_total": pdb_total,
        },
    )

# ---------------------------------------------------------------------------
# Spools — list + filters + add form + HTMX remaining patch
# ---------------------------------------------------------------------------
@router.get("/spools", response_class=HTMLResponse)
def spools_page(
    request: Request,
    db: DbSession,
    cloud_resource_id: str | None = None,
    location: str | None = None,
) -> HTMLResponse:
    stmt = select(Spool).order_by(Spool.id.desc())
    if cloud_resource_id:
        stmt = stmt.where(Spool.cloud_resource_id == cloud_resource_id)
    if location:
        stmt = stmt.where(Spool.location == location)
    rows = list(db.scalars(stmt))

    profiles = list(
        db.scalars(
            select(ProfileCache)
            .where(ProfileCache.deleted_from_cloud == False)  # noqa: E712
            .order_by(ProfileCache.vendor, ProfileCache.name)
        )
    )

    return _templates(request).TemplateResponse(
        request, "spools.html",
        {
            "spools": rows,
            "profiles": profiles,
            "filter_cloud_resource_id": cloud_resource_id or "",
            "filter_location": location or "",
        },
    )


@router.post("/spools/form", response_class=HTMLResponse)
def spool_create_form(
    request: Request,
    db: DbSession,
    cloud_resource_id: str = Form(...),
    color_override: str | None = Form(None),
    remaining_pct: int = Form(100),
    location: str = Form(""),
    notes: str = Form(""),
) -> RedirectResponse:
    if db.get(ProfileCache, cloud_resource_id) is None:
        raise HTTPException(status_code=404, detail="profile not found")
    now = _now()
    s = Spool(
        cloud_resource_id=cloud_resource_id,
        color_override=color_override or None,
        remaining_pct=remaining_pct,
        location=location,
        notes=notes,
        created_at=now, updated_at=now,
    )
    db.add(s)
    db.flush()
    return RedirectResponse(url="/spools", status_code=303)


@router.post("/spools/{sid}/remaining", response_class=HTMLResponse)
def spool_update_remaining(
    request: Request, sid: int, db: DbSession, remaining_pct: int = Form(...),
) -> HTMLResponse:
    s = db.get(Spool, sid)
    if s is None:
        raise HTTPException(status_code=404, detail="spool not found")
    s.remaining_pct = remaining_pct
    s.updated_at = _now()
    db.flush()
    return HTMLResponse(
        f'<span id="remaining-{sid}" class="remaining">{remaining_pct}%</span>'
    )

@router.post("/spools/{sid}/edit", response_class=HTMLResponse)
def spool_edit_form(
    request: Request,
    sid: int,
    db: DbSession,
    cloud_resource_id: str = Form(...),
    color_override: str | None = Form(None),
    remaining_pct: int = Form(100),
    location: str = Form(""),
    notes: str = Form(""),
) -> RedirectResponse:
    """Update a spool's editable fields from the inline edit form."""
    s = db.get(Spool, sid)
    if s is None:
        raise HTTPException(status_code=404, detail="spool not found")
    if db.get(ProfileCache, cloud_resource_id) is None:
        raise HTTPException(status_code=404, detail="profile not found")
    s.cloud_resource_id = cloud_resource_id
    s.color_override = color_override or None
    s.remaining_pct = remaining_pct
    s.location = location
    s.notes = notes
    s.updated_at = _now()
    db.flush()
    return RedirectResponse(url="/spools", status_code=303)


@router.post("/spools/{sid}/delete", response_class=HTMLResponse)
def spool_delete_form(sid: int, db: DbSession) -> RedirectResponse:
    """Delete a spool from the catalog."""
    s = db.get(Spool, sid)
    if s is None:
        raise HTTPException(status_code=404, detail="spool not found")
    db.delete(s)
    db.flush()
    return RedirectResponse(url="/spools", status_code=303)

@router.post("/spools/{sid}/duplicate", response_class=HTMLResponse)
def spool_duplicate_form(sid: int, db: DbSession) -> RedirectResponse:
    """Duplicate a spool with remaining_pct reset to 100."""
    s = db.get(Spool, sid)
    if s is None:
        raise HTTPException(status_code=404, detail="spool not found")
    now = _now()
    dup = Spool(
        cloud_resource_id=s.cloud_resource_id,
        color_override=s.color_override,
        remaining_pct=100,
        location=s.location,
        notes=s.notes,
        created_at=now,
        updated_at=now,
    )
    db.add(dup)
    db.flush()
    return RedirectResponse(url="/spools", status_code=303)


# ---------------------------------------------------------------------------
# Sync page — preset & status table, action buttons, busy dialog, job progress
# ---------------------------------------------------------------------------

def _preset_status_rows(db: Session) -> list[dict]:
    """Build Preset & Status Table rows from ProfileCache + SyncTarget.

    Each row carries: name, vendor, type, cloud_resource_id, printer_db_id,
    status (in_sync|pending|orphan), deleted_from_cloud, is_inherited.
    """
    presets = list(db.scalars(
        select(ProfileCache).order_by(ProfileCache.vendor, ProfileCache.name)
    ))
    rows: list[dict] = []
    for p in presets:
        target = db.scalars(
            select(SyncTarget).where(
                SyncTarget.cloud_id == p.cloud_resource_id,
                SyncTarget.kind == "printer_db",
            )
        ).first()
        is_stock = (p.vendor or "").strip().lower() in sync_handlers.STOCK_RFID_BRANDS
        if p.deleted_from_cloud:
            status = "orphan"
        elif is_stock:
            status = "stock"
        elif target is not None and target.status == "in_sync":
            status = "in_sync"
        else:
            status = "pending"
        rows.append({
            "name": p.name,
            "vendor": p.vendor,
            "type": p.type,
            "cloud_resource_id": p.cloud_resource_id,
            "printer_db_id": target.printer_db_id if target else None,
            "status": status,
            "is_stock": is_stock,
            "deleted_from_cloud": p.deleted_from_cloud,
            "is_inherited": p.is_inherited,
        })
    return rows


def _last_cache_synced(db: Session) -> str | None:
    """Return the most recent ProfileCache.last_seen_at as a local time string."""
    latest = db.scalar(select(func.max(ProfileCache.last_seen_at)))
    return _fmt_local(latest) if latest else None

@router.get("/sync", response_class=HTMLResponse)
def sync_page(request: Request, db: DbSession) -> HTMLResponse:
    """Preset & Status Table from ProfileCache, HTMX action buttons, busy
    dialog for awaiting_input jobs, and a job progress area."""
    presets = _preset_status_rows(db)
    awaiting_jobs = list(db.scalars(
        select(Job).where(Job.status == "awaiting_input").order_by(Job.id)
    ))
    recent_jobs = list(db.scalars(
        select(Job).order_by(Job.id.desc()).limit(20)
    ))
    return _templates(request).TemplateResponse(
        request, "sync_hub.html",
        {
            "presets": presets,
            "awaiting_jobs": awaiting_jobs,
            "recent_jobs": recent_jobs,
            "github_configured": load_github_config() is not None,
            "last_synced": _last_cache_synced(db),
        },
    )


@router.get("/sync/progress", response_class=HTMLResponse)
def sync_progress_fragment(db: DbSession, since: int = 0) -> HTMLResponse:
    """HTMX fragment: job table for the progress area (polls /api/jobs?since).

    Returns an HTML table of jobs with id > ``since`` so the sync page can
    poll via ``hx-get="/sync/progress?since=N"``.
    """
    recent = list(db.scalars(
        select(Job).where(Job.id > since).order_by(Job.id.desc()).limit(20)
    ))
    if not recent:
        return HTMLResponse("<p>No new jobs.</p>")
    import html as _html
    rows = "".join(
        f'<tr class="status-{j.status}"><td>{j.id}</td><td>{j.kind}</td>'
        f'<td>{j.status}</td>'
        + (f'<td class="job-error">{_html.escape(json.loads(j.result).get("error", ""))}</td>'
           if j.status == "error" and j.result else "<td></td>")
        + f'</tr>'
        for j in recent
    )
    return HTMLResponse(
        f'<table><thead><tr><th>ID</th><th>Kind</th><th>Status</th><th>Detail</th></tr>'
        f'</thead><tbody>{rows}</tbody></table>'
    )


@router.post("/sync/jobs/{job_id}/resolve", response_class=HTMLResponse)
def sync_resolve_job(
    request: Request, job_id: int, db: DbSession,
    choice: str = Form(...),
) -> HTMLResponse:
    """HTMX form endpoint: resolve an awaiting_input job and return an
    updated busy-dialog fragment.

    Accepts ``choice`` as a form field (HTMX's default urlencoded encoding).
    Performs the same resolve logic as ``POST /api/jobs/{id}/resolve`` but
    returns HTML instead of JSON.
    """
    config = _get_config(request)
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "awaiting_input":
        raise HTTPException(
            status_code=409, detail="job is not awaiting input"
        )

    payload = json.loads(job.payload) if job.payload else {}
    if choice == "force_reboot":
        payload["force_reboot"] = True
    elif choice == "no_reboot":
        payload["no_reboot"] = True
    elif choice == "wait":
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

    # Re-render the busy-dialog fragment with remaining awaiting_input jobs.
    awaiting = list(db.scalars(
        select(Job).where(Job.status == "awaiting_input").order_by(Job.id)
    ))
    if not awaiting:
        return HTMLResponse("")
    buttons = "".join(
        f'<div class="awaiting-job" id="awaiting-{j.id}">'
        f'<p>Job #{j.id} — {j.kind}</p>'
        f'<button hx-post="/sync/jobs/{j.id}/resolve" '
        f'hx-vals=\'{{"choice": "force_reboot"}}\' '
        f'hx-target="#busy-dialog" hx-swap="outerHTML">'
        f'Force reboot</button>'
        f'<button hx-post="/sync/jobs/{j.id}/resolve" '
        f'hx-vals=\'{{"choice": "wait"}}\' '
        f'hx-target="#busy-dialog" hx-swap="outerHTML">'
        f'Wait</button>'
        f'<button hx-post="/sync/jobs/{j.id}/resolve" '
        f'hx-vals=\'{{"choice": "no_reboot"}}\' '
        f'hx-target="#busy-dialog" hx-swap="outerHTML">'
        f'No reboot</button>'
        f'</div>'
        for j in awaiting
    )
    return HTMLResponse(
        f'<section id="busy-dialog" class="busy-dialog">'
        f'<h2>Awaiting input</h2>'
        f'<p>The printer is busy. Choose how to proceed:</p>'
        f'{buttons}'
        f'</section>'
    )

# ---------------------------------------------------------------------------
# Action modal — overlay with live progress, busy choices, hub refresh
# Generalized from the push-modal pattern to serve push/backup.
# ---------------------------------------------------------------------------

# Maps job.kind → modal kind for fragment rendering.
_JOB_KIND_TO_MODAL: dict[str, str] = {
    "push": "push",
    "github_backup": "backup",
}

# Per-kind text for running / done / error states.
_ACTION_MODAL_RUNNING_TEXT: dict[str, str] = {
    "push": "Pushing to printer…",
    "backup": "Backing up to GitHub…",
}
_ACTION_MODAL_ERROR_TITLE: dict[str, str] = {
    "push": "Push failed",
    "backup": "Backup failed",
}


def _parse_job_result(result_json: str | None) -> dict | None:
    if not result_json:
        return None
    try:
        return json.loads(result_json)
    except (json.JSONDecodeError, ValueError):
        return None


def _hub_refresh_div() -> str:
    """Hidden div that triggers a hub-fragment refresh on load."""
    return (
        '<div hx-get="/sync/hub-fragment" hx-target="#sync-hub-content" '
        'hx-trigger="load" hx-swap="innerHTML"></div>'
    )


def _action_modal_fragment(kind: str, job_id: int, status: str,
                           result: dict | None,
                           verify_status: str | None = None,
                           verify_result: dict | None = None) -> str:
    """Render the action-modal body HTML for a given job state.

    ``kind`` is one of "push", "backup" and controls
    the operation-specific text (headings, spinner labels, success/error
    messages).

    ``verify_status`` / ``verify_result`` carry the auto-enqueued verify
    job's state so the modal can show "Verifying…" while verify runs
    after the push job is done (push kind only).
    """
    poll_url = f"/sync/{kind}-modal/{job_id}" if job_id else ""
    if status in ("queued", "running"):
        running_text = _ACTION_MODAL_RUNNING_TEXT.get(kind, "Working…")
        return (
            f'<div class="push-modal-state push-modal-running">'
            f'<div class="spinner spinner-lg"></div>'
            f'<p>{running_text}</p>'
            f'<p class="push-modal-sub">Job #{job_id} · {status}</p>'
            f'<button class="button button-ghost" onclick="closeActionModal()">Dismiss</button>'
            f'</div>'
            f'<div hx-get="{poll_url}" hx-trigger="every 1.5s" '
            f'hx-target="#action-modal-body" hx-swap="innerHTML"></div>'
        )

    if status == "awaiting_input" and kind == "push":
        busy = (result or {}).get("busy") or {}
        filename = busy.get("filename", "")
        progress = busy.get("progress", 0)
        busy_line = (
            f'<p class="push-modal-busy-detail">Printing: '
            f'{_html.escape(str(filename))} ({progress}%)</p>'
            if filename else ""
        )
        return (
            f'<div class="push-modal-state push-modal-awaiting">'
            f'<h3>Printer is busy</h3>'
            f'{busy_line}'
            f'<p>The printer is currently printing. Choose how to proceed:</p>'
            f'<div class="button-group">'
            f'<button class="button button-danger" '
            f'hx-post="/sync/jobs/{job_id}/resolve-modal" '
            f'hx-vals=\'{{"choice": "force_reboot"}}\' '
            f'hx-target="#action-modal-body" hx-swap="innerHTML">'
            f'Force reboot</button>'
            f'<button class="button button-secondary" '
            f'hx-post="/sync/jobs/{job_id}/resolve-modal" '
            f'hx-vals=\'{{"choice": "wait"}}\' '
            f'hx-target="#action-modal-body" hx-swap="innerHTML">'
            f'Wait</button>'
            f'<button class="button button-ghost" '
            f'hx-post="/sync/jobs/{job_id}/resolve-modal" '
            f'hx-vals=\'{{"choice": "no_reboot"}}\' '
            f'hx-target="#action-modal-body" hx-swap="innerHTML">'
            f'No reboot</button>'
            f'</div>'
            f'<p class="push-modal-sub">Job #{job_id} · awaiting input</p>'
            f'</div>'
        )

    if status == "done":
        # Push-specific: verify job polling
        if kind == "push" and verify_status in ("queued", "running"):
            return (
                f'<div class="push-modal-state push-modal-running">'
                f'<div class="spinner spinner-lg"></div>'
                f'<p>Verifying…</p>'
                f'<p class="push-modal-sub">Push done — checking printer DB</p>'
                f'<button class="button button-ghost" onclick="closeActionModal()">Dismiss</button>'
                f'<div hx-get="/sync/push-modal/{job_id}" hx-trigger="every 1.5s" '
                f'hx-target="#action-modal-body" hx-swap="innerHTML"></div>'
            )

        if kind == "push":
            pushed = (result or {}).get("pushed", 0)
            skipped = (result or {}).get("skipped", 0)
            sub = "Verification complete." if verify_status == "done" else "Push complete."
            return (
                f'<div class="push-modal-state push-modal-done">'
                f'<div class="push-modal-icon push-modal-icon-ok">✓</div>'
                f'<h3>Push complete</h3>'
                f'<p>{pushed} profile(s) pushed, {skipped} skipped.</p>'
                f'<p class="push-modal-sub">{sub}</p>'
                f'<button class="button button-primary" onclick="closeActionModal()">Close</button>'
                f'</div>'
                + _hub_refresh_div()
            )


        if kind == "backup":
            preset_count = (result or {}).get("preset_count", 0)
            return (
                f'<div class="push-modal-state push-modal-done">'
                f'<div class="push-modal-icon push-modal-icon-ok">✓</div>'
                f'<h3>Backup complete</h3>'
                f'<p>Backup complete: {preset_count} presets pushed to GitHub.</p>'
                f'<button class="button button-primary" onclick="closeActionModal()">Close</button>'
                f'</div>'
                + _hub_refresh_div()
            )

    if status == "error":
        err = (result or {}).get("error", "Unknown error")
        err_title = _ACTION_MODAL_ERROR_TITLE.get(kind, "Operation failed")
        return (
            f'<div class="push-modal-state push-modal-error">'
            f'<div class="push-modal-icon push-modal-icon-err">✗</div>'
            f'<h3>{err_title}</h3>'
            f'<p class="push-modal-error-msg">{_html.escape(str(err))}</p>'
            f'<button class="button button-secondary" onclick="closeActionModal()">Close</button>'
            f'</div>'
            + _hub_refresh_div()
        )

    return (
        f'<div class="push-modal-state push-modal-error">'
        f'<div class="push-modal-icon push-modal-icon-err">?</div>'
        f'<h3>Unexpected state</h3>'
        f'<p class="push-modal-error-msg">Unknown status: {_html.escape(str(status))}</p>'
        f'<button class="button button-secondary" onclick="closeActionModal()">Close</button>'
        f'</div>'
    )


def _push_modal_fragment(job_id: int, status: str, result: dict | None,
                         verify_status: str | None = None,
                         verify_result: dict | None = None) -> str:
    """Backward-compat wrapper — delegates to ``_action_modal_fragment``."""
    return _action_modal_fragment(
        "push", job_id, status, result, verify_status, verify_result,
    )


def _lookup_verify_job(db: Session, push_job_id: int) -> Job | None:
    """Find the latest verify job enqueued after ``push_job_id``."""
    return db.scalars(
        select(Job).where(
            Job.kind == "verify", Job.id > push_job_id,
        ).order_by(Job.id.desc())
    ).first()


def _action_modal_poll_impl(job_id: int, db: Session) -> HTMLResponse:
    """Shared poll logic: look up job, render fragment for its current state."""
    job = db.get(Job, job_id)
    if job is None:
        return HTMLResponse(
            '<div class="push-modal-state push-modal-error">'
            '<div class="push-modal-icon push-modal-icon-err">✗</div>'
            '<h3>Job not found</h3>'
            '<button class="button button-secondary" onclick="closeActionModal()">Close</button>'
            '</div>'
        )
    kind = _JOB_KIND_TO_MODAL.get(job.kind, job.kind)
    verify_job = _lookup_verify_job(db, job.id) if kind == "push" and job.status == "done" else None
    return HTMLResponse(_action_modal_fragment(
        kind, job.id, job.status, _parse_job_result(job.result),
        verify_job.status if verify_job else None,
        _parse_job_result(verify_job.result) if verify_job else None,
    ))


def _enqueue_and_modal(
    request: Request, db: Session, kind: str, job_kind: str, payload: dict,
) -> HTMLResponse:
    """Shared enqueue + background drain + modal fragment logic."""
    config = getattr(request.app.state, "config", None)
    if config is None:
        return HTMLResponse(_action_modal_fragment(
            kind, 0, "error",
            {"error": "Configuration error — set up the printer connection in Settings"},
        ))
    job = jobs.enqueue(db, job_kind, payload)
    db.flush()
    db.commit()
    jobs.drain_background(config)
    db.expire_all()
    job = db.get(Job, job.id)
    verify_job = _lookup_verify_job(db, job.id) if kind == "push" and job.status == "done" else None
    return HTMLResponse(_action_modal_fragment(
        kind, job.id, job.status, _parse_job_result(job.result),
        verify_job.status if verify_job else None,
        _parse_job_result(verify_job.result) if verify_job else None,
    ))


@router.post("/sync/push-modal", response_class=HTMLResponse)
def sync_push_modal(
    request: Request, db: DbSession,
    profile_ids: str | None = Form(None),
    cloud_resource_ids: str | None = Form(None),
) -> HTMLResponse:
    """Enqueue a push job, start background drain, return modal fragment.

    HTMX form endpoint: the push buttons POST here with optional
    ``cloud_resource_ids`` (comma-separated Orca Cloud UUIDs) or
    ``profile_ids`` (comma-separated catalog IDs, legacy fallback). The
    job is committed immediately so the background drain thread can see
    it. Returns the modal body fragment (spinner + polling).
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        return HTMLResponse(
            '<div class="push-modal-state push-modal-error">'
            '<div class="push-modal-icon push-modal-icon-err">✗</div>'
            '<h3>Printer not configured</h3>'
            '<p>Set up the printer connection in Settings first.</p>'
            '<button class="button button-secondary" onclick="closeActionModal()">Close</button>'
            '</div>'
        )
    ids = None
    if profile_ids:
        ids = [int(x.strip()) for x in profile_ids.split(",") if x.strip()]
    cloud_ids = None
    if cloud_resource_ids:
        cloud_ids = [x.strip() for x in cloud_resource_ids.split(",") if x.strip()]
    payload = {
        "profile_ids": ids,
        "cloud_resource_ids": cloud_ids,
        "force_reboot": False,
        "no_reboot": False,
    }
    return _enqueue_and_modal(request, db, "push", "push", payload)


@router.get("/sync/push-modal/{job_id}", response_class=HTMLResponse)
def sync_push_modal_poll(job_id: int, db: DbSession) -> HTMLResponse:
    """Poll endpoint: return modal fragment for the job's current state."""
    return _action_modal_poll_impl(job_id, db)




@router.post("/sync/backup-modal", response_class=HTMLResponse)
def sync_backup_modal(
    request: Request, db: DbSession,
) -> HTMLResponse:
    """Enqueue a github_backup job, start background drain, return modal fragment."""
    return _enqueue_and_modal(request, db, "backup", "github_backup", {})


@router.get("/sync/backup-modal/{job_id}", response_class=HTMLResponse)
def sync_backup_modal_poll(job_id: int, db: DbSession) -> HTMLResponse:
    """Poll endpoint for backup-modal: return modal fragment for the job's state."""
    return _action_modal_poll_impl(job_id, db)


@router.post("/sync/jobs/{job_id}/resolve-modal", response_class=HTMLResponse)
def sync_resolve_modal(
    request: Request, job_id: int, db: DbSession,
    choice: str = Form(...),
) -> HTMLResponse:
    """Resolve an awaiting_input job from the modal, start background drain.

    Same logic as ``POST /sync/jobs/{id}/resolve`` but returns a modal
    body fragment instead of a busy-dialog fragment.
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="printer not configured")
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "awaiting_input":
        raise HTTPException(
            status_code=409, detail="job is not awaiting input"
        )

    payload = json.loads(job.payload) if job.payload else {}
    if choice == "force_reboot":
        payload["force_reboot"] = True
    elif choice == "no_reboot":
        payload["no_reboot"] = True
    elif choice == "wait":
        pass
    else:
        raise HTTPException(
            status_code=422,
            detail="invalid choice; expected force_reboot|wait|no_reboot",
        )

    job.payload = json.dumps(payload)
    job.status = "queued"
    job.result = None
    db.flush()
    db.commit()
    jobs.drain_background(config)
    db.expire_all()
    job = db.get(Job, job_id)
    verify_job = _lookup_verify_job(db, job.id) if job.status == "done" else None
    return HTMLResponse(_action_modal_fragment(
        "push", job.id, job.status, _parse_job_result(job.result),
        verify_job.status if verify_job else None,
        _parse_job_result(verify_job.result) if verify_job else None,
    ))


@router.get("/sync/hub-fragment", response_class=HTMLResponse)
def sync_hub_fragment(request: Request, db: DbSession) -> HTMLResponse:
    """Return the preset & status table + target cards as an HTMX fragment.

    Used by the action-modal completion path to refresh the Sync Hub's
    main content (preset table + right-column summaries) without a
    full page reload.
    """
    presets = _preset_status_rows(db)
    awaiting_jobs = list(db.scalars(
        select(Job).where(Job.status == "awaiting_input").order_by(Job.id)
    ))
    return _templates(request).TemplateResponse(
        request, "_sync_hub_grid.html",
        {
            "presets": presets,
            "awaiting_jobs": awaiting_jobs,
            "github_configured": load_github_config() is not None,
            "last_synced": _last_cache_synced(db),
        },
    )

@router.post("/sync/cache-sync", response_class=HTMLResponse)
def sync_cache_sync(request: Request, db: DbSession) -> HTMLResponse:
    """HTMX endpoint: sync Orca Cloud presets into profile_cache,
    then return the refreshed hub fragment."""
    client = sync_handlers._orca_client_factory()
    res = {"synced": 0, "deleted": 0, "offline": True}
    if client is not None:
        res = sync_profile_cache(db, client)
        db.commit()
    jobs.log_activity(
        db, "cache_sync",
        "ok" if not res.get("offline") else "warn",
        res,
    )
    db.commit()
    return sync_hub_fragment(request, db)

# ---------------------------------------------------------------------------
# Orphan printer DB entry removal — Double-Confirm typed token
# ---------------------------------------------------------------------------

@router.post("/sync/printer-db/{entry_id}/remove", response_class=HTMLResponse)
def sync_printer_db_remove(
    request: Request, entry_id: str, db: DbSession,
    confirm: str = Form(...),
) -> HTMLResponse:
    """Remove an orphan printer DB entry and its SyncTarget.

    Double-Confirm: the typed ``confirm`` token must match ``entry_id``
    exactly (e.g. ``99001``) before any deletion proceeds.  Removes the
    entry from the cached printer DB (if present) and deletes the
    SyncTarget row.  Returns the refreshed ``_sync_hub_grid.html``
    fragment so HTMX can swap it into ``#sync-hub-content``.
    """
    if confirm != entry_id:
        raise HTTPException(
            status_code=422,
            detail=f"Typed token must match entry ID exactly: {entry_id}",
        )
    # Locate the SyncTarget tracking this printer DB entry.
    target = db.scalars(
        select(SyncTarget).where(
            SyncTarget.printer_db_id == entry_id,
            SyncTarget.kind == "printer_db",
        )
    ).first()
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"No SyncTarget for printer DB entry {entry_id}",
        )
    # Remove from the cached printer DB (if a cache is available).
    # Guard with find_entry so a missing cache entry doesn't block
    # SyncTarget cleanup.
    cache_path = (
        sync_handlers._cache_db_path
        if sync_handlers._cache_db_path is not None
        else sync_handlers._resolve_cache_path()
    )
    if cache_path is not None and Path(cache_path).exists():
        printer_db = load_db(cache_path)
        if find_entry(printer_db, entry_id) is not None:
            remove_entry(printer_db, entry_id)
            save_db(cache_path, printer_db)
    # Remove the SyncTarget — always executes regardless of cache state.
    db.delete(target)
    jobs.log_activity(
        db, "printer_db_remove", "ok",
        {"entry_id": entry_id, "sync_target_id": target.id},
    )
    db.commit()
    # Return the refreshed hub fragment for HTMX swap.
    presets = _preset_status_rows(db)
    awaiting_jobs = list(db.scalars(
        select(Job).where(Job.status == "awaiting_input").order_by(Job.id)
    ))
    return _templates(request).TemplateResponse(
        request, "_sync_hub_grid.html",
        {
            "presets": presets,
            "awaiting_jobs": awaiting_jobs,
            "github_configured": load_github_config() is not None,
        },
    )

# ---------------------------------------------------------------------------
# Settings — printer connection + Orca Cloud (persisted to settings.json)
# ---------------------------------------------------------------------------

_SETTINGS_DEFAULTS = {
    "ws_port": 9999,
    "version_override": 9876543210,
    "id_range_start": 99001,
}


def _settings_context(
    config: Config | None, saved: bool = False, errors: list[str] | None = None,
) -> dict:
    """Build the template context for the settings page."""
    values: dict = {}
    if config is not None:
        values = {
            "printer_ip": config.printer_ip,
            "ssh_user": config.ssh_user,
            "ssh_password": config.ssh_password,
            "db_remote_path": config.db_remote_path,
            "ws_port": config.ws_port,
            "version_override": config.version_override,
            "id_range_start": config.id_range_start,
        }
    else:
        stored = load_settings() or {}
        values = {
            "printer_ip": stored.get("printer_ip", ""),
            "ssh_user": stored.get("ssh_user", ""),
            "ssh_password": stored.get("ssh_password", ""),
            "db_remote_path": stored.get("db_remote_path", ""),
            "ws_port": stored.get("ws_port", _SETTINGS_DEFAULTS["ws_port"]),
            "version_override": stored.get(
                "version_override", _SETTINGS_DEFAULTS["version_override"]),
            "id_range_start": stored.get(
                "id_range_start", _SETTINGS_DEFAULTS["id_range_start"]),
        }
    # Orca Cloud connection status
    token_info = load_orca_token()
    orca_connected = token_info is not None
    orca_user_email = ""
    if orca_connected:
        try:
            client = OrcaCloudClient(token_info[0])
            orca_user_email = client.get_user_id()
        except OrcaCloudError:
            orca_connected = False
    return {
        **values,
        "configured": config is not None,
        "saved": saved,
        "errors": errors or [],
        "orca_connected": orca_connected,
        "orca_user_email": orca_user_email,
        "github_token": (load_settings() or {}).get("github_token", ""),
        "github_repo": (load_settings() or {}).get("github_repo", ""),
        "github_branch": (load_settings() or {}).get("github_branch", "main"),
        "github_configured": load_github_config() is not None,
    }


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    """Render the settings form pre-filled from current config or settings.json."""
    config = getattr(request.app.state, "config", None)
    saved = request.query_params.get("saved") == "1"
    return _templates(request).TemplateResponse(
        request, "settings.html", _settings_context(config, saved=saved),
    )


@router.post("/settings", response_class=HTMLResponse, response_model=None)
def settings_save(
    request: Request,
    printer_ip: str = Form(""),
    ssh_user: str = Form(""),
    ssh_password: str = Form(""),
    db_remote_path: str = Form(""),
    ws_port: str = Form(""),
    version_override: str = Form(""),
    id_range_start: str = Form(""),
    github_token: str = Form(""),
    github_repo: str = Form(""),
    github_branch: str = Form("main"),
) -> HTMLResponse | RedirectResponse:
    """Validate, persist settings.json, update app.state.config, redirect."""
    printer_ip = printer_ip.strip()
    ssh_user = ssh_user.strip()
    ssh_password = ssh_password.strip()
    db_remote_path = db_remote_path.strip()

    errors: list[str] = []
    for label, val in [
        ("Printer IP", printer_ip), ("SSH user", ssh_user),
        ("SSH password", ssh_password), ("Remote DB path", db_remote_path),
    ]:
        if not val:
            errors.append(f"{label} is required.")

    def _int(name: str, raw: str, default: int) -> int:
        raw = raw.strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            errors.append(f"{name} must be a whole number.")
            return default

    port = _int("WebSocket port", ws_port, _SETTINGS_DEFAULTS["ws_port"])
    ver = _int("Version override", version_override, _SETTINGS_DEFAULTS["version_override"])
    ids = _int("ID range start", id_range_start, _SETTINGS_DEFAULTS["id_range_start"])

    submitted = {
        "printer_ip": printer_ip, "ssh_user": ssh_user,
        "ssh_password": ssh_password, "db_remote_path": db_remote_path,
        "ws_port": port,
        "version_override": ver, "id_range_start": ids,
    }

    if errors:
        ctx = {**_settings_context(None, saved=False, errors=errors), **submitted}
        ctx["configured"] = False
        return _templates(request).TemplateResponse(
            request, "settings.html", ctx, status_code=422,
        )

    # GitHub backup config (optional — empty token means not configured)
    if github_token.strip():
        submitted["github_token"] = github_token.strip()
        submitted["github_repo"] = github_repo.strip()
        submitted["github_branch"] = github_branch.strip() or "main"

    save_settings(submitted)
    request.app.state.config = build_config(submitted)
    return RedirectResponse(url="/settings?saved=1", status_code=303)


@router.post("/api/settings/orca-connect")
def orca_connect(email: str = Form(""), password: str = Form("")) -> dict:
    """Connect to Orca Cloud via email+password login.

    Calls Supabase password grant to obtain a refresh token, then persists it.
    """
    email = email.strip()
    password = password.strip()
    if not email or not password:
        return {"ok": False, "error": "Email and password are required."}
    try:
        refresh_token, user_id = OrcaCloudClient.login_with_password(email, password)
    except OrcaCloudError as exc:
        return {"ok": False, "error": exc.message}
    save_orca_token(refresh_token, user_id)
    return {"ok": True, "user_id": user_id}


@router.post("/api/settings/orca-disconnect")
def orca_disconnect() -> dict:
    """Disconnect from Orca Cloud by removing stored credentials."""
    stored = load_settings() or {}
    stored.pop("orca_refresh_token", None)
    stored.pop("orca_user_id", None)
    save_settings(stored)
    return {"ok": True}


@router.post("/api/settings/test-connection")
def test_connection(
    printer_ip: str = Form(""),
    ssh_user: str = Form(""),
    ssh_password: str = Form(""),
    db_remote_path: str = Form(""),
) -> dict:
    """SSH to printer and check DB path exists."""
    missing = [f for f in (printer_ip, ssh_user, ssh_password, db_remote_path) if not f.strip()]
    if missing:
        return {"ok": False, "error": "Fill in all printer connection fields first."}
    cmd = [
        "sshpass", "-p", ssh_password,
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        f"{ssh_user}@{printer_ip}",
        f"test -f {db_remote_path} && echo EXISTS",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Connection timed out (15s) to {printer_ip}."}
    except FileNotFoundError:
        return {"ok": False, "error": "sshpass not installed on the server."}
    if result.returncode == 255 and "Permission denied" in result.stderr:
        return {"ok": False, "error": "SSH authentication failed. Check user and password."}
    if result.returncode == 255:
        return {"ok": False, "error": f"Cannot reach {printer_ip}. Check IP or network. {result.stderr.strip()}"}
    if "EXISTS" not in result.stdout:
        return {"ok": False, "error": f"DB path not found: {db_remote_path}"}
    return {"ok": True, "error": None}


@router.post("/api/settings/github-test")
def github_test_connection(
    github_token: str = Form(""),
    github_repo: str = Form(""),
    github_branch: str = Form("main"),
) -> dict:
    """Test GitHub token + repo access."""
    github_token = github_token.strip()
    github_repo = github_repo.strip()
    if not github_token or not github_repo:
        return {"ok": False, "error": "Token and repo are required."}
    try:
        client = GitHubBackupClient(
            github_token, github_repo, github_branch.strip() or "main",
        )
        info = client.test_connection()
    except GitHubBackupError as exc:
        return {"ok": False, "error": exc.message}
    return {"ok": True, "repo": info["repo"], "default_branch": info["default_branch"]}


# ---------------------------------------------------------------------------
# Logs page — unified timeline of background jobs and direct actions
# ---------------------------------------------------------------------------

# Action label mapping (raw kind/action → human-readable label)
_LOG_ACTION_LABELS: dict[str, str] = {
    "push": "Push to printer",
    "verify": "Verify printer DB",
    "github_backup": "GitHub backup",
    "printer_db_remove": "Remove printer DB entry",
    "cache_sync": "Orca Cloud cache sync",
}

# ActivityLog outcome → (display status, status_class, status_label)
_ACT_OUTCOME_MAP: dict[str, tuple[str, str, str]] = {
    "ok": ("done", "done", "done"),
    "warn": ("awaiting_input", "awaiting_input", "awaiting input"),
    "error": ("error", "error", "error"),
}


def _relative_time(dt: datetime | None) -> str:
    """Human-readable relative time: '3m ago', '2h ago', 'just now'."""
    if not dt:
        return ""
    # SQLite may return offset-naive datetimes; assume UTC if so.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = _now() - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _compute_duration(job: Job) -> str:
    """Format a job's duration string for the logs table."""
    if job.status in ("queued", "running", "awaiting_input"):
        return job.status
    if not job.finished_at:
        return "—"
    delta = (job.finished_at - job.created_at).total_seconds()
    if delta < 1:
        return "instant"
    if delta < 60:
        return f"{delta:.1f}s"
    mins = int(delta // 60)
    secs = int(delta % 60)
    return f"{mins}m {secs}s"


def _normalize_job_entry(job: Job) -> dict:
    """Normalize a Job row into a unified LogEntry dict."""
    detail = json.loads(job.payload) if job.payload else {}
    result = json.loads(job.result) if job.result else None
    error_msg = None
    busy_detail = None
    if result:
        error_msg = result.get("error")
        busy_detail = result.get("busy")
    has_detail = bool(detail or result or error_msg or busy_detail)
    return {
        "id": f"job-{job.id}",
        "source": "job",
        "source_id": job.id,
        "timestamp": job.created_at,
        "timestamp_fmt": _fmt_local(job.created_at),
        "timestamp_rel": _relative_time(job.created_at),
        "action": _LOG_ACTION_LABELS.get(
            job.kind, job.kind.replace("_", " ").title(),
        ),
        "action_raw": job.kind,
        "status": job.status,
        "status_label": job.status.replace("_", " "),
        "status_class": _status_class(job.status),
        "detail": {**(detail or {}), **({"result": result} if result else {})},
        "detail_json": json.dumps(
            {"payload": detail, "result": result}, indent=2, default=str,
        ) if has_detail else "",
        "error_msg": error_msg,
        "busy_detail": busy_detail,
        "has_detail": has_detail,
        "finished_at_fmt": _fmt_local(job.finished_at) if job.finished_at else None,
    }


def _normalize_activity_entry(act: ActivityLog) -> dict:
    """Normalize an ActivityLog row into a unified LogEntry dict."""
    detail = json.loads(act.detail) if act.detail else {}
    job_id = detail.get("job_id")
    result_obj = detail.get("result")
    error_msg = None
    if isinstance(result_obj, dict):
        error_msg = result_obj.get("error") or detail.get("error")
    else:
        error_msg = detail.get("error")
    busy_detail = detail.get("busy")
    outcome_status, outcome_class, outcome_label = _ACT_OUTCOME_MAP.get(
        act.outcome, (act.outcome, act.outcome, act.outcome),
    )
    has_detail = bool(detail)
    return {
        "id": f"act-{act.id}",
        "source": "action",
        "source_id": act.id,
        "source_job_id": job_id,  # for dedup linking
        "timestamp": act.ts,
        "timestamp_fmt": _fmt_local(act.ts),
        "timestamp_rel": _relative_time(act.ts),
        "action": _LOG_ACTION_LABELS.get(
            act.action, act.action.replace("_", " ").title(),
        ),
        "action_raw": act.action,
        "status": outcome_status,
        "status_class": outcome_class,
        "status_label": outcome_label,
        "duration": "instant",
        "detail": detail,
        "detail_json": json.dumps(detail, indent=2, default=str) if has_detail else "",
        "error_msg": error_msg,
        "busy_detail": busy_detail,
        "has_detail": has_detail,
        "finished_at_fmt": None,
    }


def _unified_log_entries(
    db: Session, *, status: str = "", source: str = "",
    q: str = "", sort: str = "desc",
) -> tuple[list[dict], int, bool]:
    """Build the unified log entry list from jobs + activity_log.

    Returns ``(entries, total_count, truncated)``.
    """
    LIMIT = 100

    # Collect job_ids that have a matching activity_log entry (for dedup).
    job_ids_with_activity: set[int] = set()
    for row in db.scalars(
        select(ActivityLog.detail).where(
            ActivityLog.action.in_(("push", "verify", "github_backup"))
        )
    ):
        try:
            d = json.loads(row) if isinstance(row, str) else row
            jid = d.get("job_id")
            if jid is not None:
                job_ids_with_activity.add(jid)
        except (json.JSONDecodeError, TypeError):
            pass

    job_query = select(Job).where(
        or_(
            Job.status.in_(("queued", "running", "awaiting_input")),
            and_(
                Job.status.in_(("done", "error")),
                ~Job.id.in_(job_ids_with_activity) if job_ids_with_activity else true(),
            ),
        )
    )
    jobs = list(db.scalars(job_query))

    # Query activity_log
    act_query = select(ActivityLog)
    activities = list(db.scalars(act_query))

    # Normalize both into unified entries
    entries: list[dict] = []
    for job in jobs:
        entries.append(_normalize_job_entry(job))
    for act in activities:
        entries.append(_normalize_activity_entry(act))

    # Deduplicate: if a job and an activity share the same job_id,
    # keep only the activity (it has the richer result).
    seen_job_ids: set[int] = set()
    deduped: list[dict] = []
    for e in entries:
        if e["source"] == "job" and e.get("linked_activity"):
            continue
        if e["source"] == "action" and e.get("source_job_id") is not None:
            seen_job_ids.add(e["source_job_id"])
        deduped.append(e)
    entries = [
        e for e in deduped
        if not (e["source"] == "job" and e.get("source_id") in seen_job_ids)
    ]

    # Filter
    if status:
        entries = [e for e in entries if e["status"] == status]
    if source:
        entries = [e for e in entries if e["source"] == source]
    if q:
        ql = q.lower()
        entries = [
            e for e in entries
            if ql in str(e.get("id", "")).lower()
            or ql in e.get("action_raw", "").lower()
            or ql in e.get("action", "").lower()
            or ql in str(e.get("error_msg", "")).lower()
            or ql in str(e.get("detail_json", "")).lower()
        ]

    # Sort
    entries.sort(key=lambda e: e["timestamp"], reverse=(sort != "asc"))

    total_count = len(entries)
    truncated = total_count > LIMIT
    entries = entries[:LIMIT]

    return entries, total_count, truncated


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, db: DbSession) -> HTMLResponse:
    """Render the unified logs page with default filters (all, newest first)."""
    entries, total_count, truncated = _unified_log_entries(db)
    return _templates(request).TemplateResponse(
        request, "logs.html",
        {"entries": entries, "total_count": total_count, "truncated": truncated,
         "has_filters": False},
    )


@router.get("/logs/table", response_class=HTMLResponse)
def logs_table_fragment(
    request: Request, db: DbSession,
    status: str = "", source: str = "", q: str = "", sort: str = "desc",
) -> HTMLResponse:
    """HTMX partial: filtered/searched/sorted log table."""
    entries, total_count, truncated = _unified_log_entries(
        db, status=status, source=source, q=q, sort=sort,
    )
    has_filters = bool(status or source or q)
    return _templates(request).TemplateResponse(
        request, "_logs_table.html",
        {"entries": entries, "total_count": total_count, "truncated": truncated,
         "has_filters": has_filters},
    )
