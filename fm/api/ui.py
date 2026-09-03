# fm/api/ui.py
"""UI page routes — server-side rendered Jinja2 + HTMX interactivity.

Functional pages only; visual/layout refinement is a separate designer pass
(see AGENTS.md "Designer role for UI"). Reuses catalog service logic
(``_validate``) and sync ``drift_status`` directly via the shared
``SessionLocal`` dependency rather than self-HTTP calls.
"""
from __future__ import annotations

import html as _html
import json
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..config import Config
from ..models import ActivityLog, Job, Profile, Spool, SyncTarget
from ..service import jobs
from ..service import sync_handlers
from ..service.sync_handlers import _entry_hash, refresh_preset_drift, _cloud_presets_to_local, canonical_hash
from .sync import drift_status, _get_config
from .catalog import _validate, perform_delete
from ..settings_store import (
    load_settings, save_settings, build_config,
    load_orca_token, save_orca_token,
    load_github_config,
)
from ..orca_cloud import OrcaCloudClient, OrcaCloudError
from ..github_backup import GitHubBackupClient, GitHubBackupError
from ..errors import FmError

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


def _parse_colors(raw: str) -> list[str]:
    """Comma-separated hex string -> list of trimmed hex values."""
    return [c.strip() for c in raw.split(",") if c.strip()]


def _profile_from_form(
    brand: str, name: str, type_: str, colors: str,
    min_temp: int, max_temp: int, density: float,
    drying_temp: int, drying_time: int, notes: str,
    template_override: str = "",
) -> dict:
    """Build a profile field dict from submitted form values."""
    return {
        "brand": brand, "name": name, "type": type_,
        "colors": json.dumps(_parse_colors(colors)),
        "min_temp": min_temp, "max_temp": max_temp,
        "density": density, "drying_temp": drying_temp,
        "drying_time": drying_time, "notes": notes,
        "template_override": template_override or None,
    }


def _submitted_namespace(
    pid: int | None, brand: str, name: str, type_: str, colors: str,
    min_temp: int, max_temp: int, density: float,
    drying_temp: int, drying_time: int, notes: str,
    template_override: str = "",
) -> SimpleNamespace:
    """A profile-like namespace carrying submitted values back into the form
    on validation error re-render."""
    ns = SimpleNamespace(
        id=pid, brand=brand, name=name, type=type_,
        colors=_parse_colors(colors), min_temp=min_temp, max_temp=max_temp,
        density=density, drying_temp=drying_temp, drying_time=drying_time,
        notes=notes, printer_db_id=None, filament_id=None,
        template_override=template_override or None,
    )
    return ns


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: DbSession) -> HTMLResponse:
    """Spool count by profile (top 10), drift matrix, last 20 activity rows."""
    spool_counts = db.execute(
        select(
            Profile.id, Profile.brand, Profile.name,
            func.count(Spool.id).label("spool_count"),
        )
        .join(Spool, Spool.profile_id == Profile.id, isouter=True)
        .group_by(Profile.id)
        .order_by(func.count(Spool.id).desc(), Profile.id)
        .limit(10)
    ).all()

    drift = drift_status(request, db)

    activity = list(
        db.scalars(
            select(ActivityLog).order_by(ActivityLog.ts.desc()).limit(20)
        )
    )

    return _templates(request).TemplateResponse(
        request, "dashboard.html",
        {"spool_counts": spool_counts, "drift": drift, "activity": activity},
    )


# ---------------------------------------------------------------------------
# Profiles — list + filter
# ---------------------------------------------------------------------------

@router.get("/profiles", response_class=HTMLResponse)
def profiles_page(
    request: Request,
    db: DbSession,
    type: str | None = None,
    brand: str | None = None,
) -> HTMLResponse:
    stmt = select(Profile).order_by(Profile.id)
    if type:
        stmt = stmt.where(Profile.type == type)
    if brand:
        stmt = stmt.where(Profile.brand == brand)
    rows = list(db.scalars(stmt))

    brands = sorted({p.brand for p in rows} | ({brand} if brand else set()))
    types = sorted({p.type for p in rows} | ({type} if type else set()))

    orca_synced = request.query_params.get("orca_synced") == "1"
    deleted = request.query_params.get("deleted") == "1"
    cloud_failed = request.query_params.get("cloud_failed") == "1"
    return _templates(request).TemplateResponse(
        request, "profiles.html",
        {
            "profiles": rows,
            "filter_type": type or "",
            "filter_brand": brand or "",
            "brands": brands,
            "types": types,
            "orca_synced": orca_synced,
            "deleted": deleted,
            "cloud_failed": cloud_failed,
        },
    )


# ---------------------------------------------------------------------------
# Profile form — new / edit
# ---------------------------------------------------------------------------

@router.get("/profiles/new", response_class=HTMLResponse)
def profile_new(request: Request) -> HTMLResponse:
    return _templates(request).TemplateResponse(
        request, "profile_form.html",
        {"profile": None, "errors": [], "is_edit": False},
    )


@router.get("/profiles/{pid}/edit", response_class=HTMLResponse)
def profile_edit(request: Request, pid: int, db: DbSession) -> HTMLResponse:
    p = db.get(Profile, pid)
    if p is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return _templates(request).TemplateResponse(
        request, "profile_form.html",
        {"profile": p, "errors": [], "is_edit": True},
    )

def _template_dropdown_impl(
    request: Request, pid: int | None,
    type: str, template_vendor: str, template_override: str, reason: str,
) -> HTMLResponse:
    """Shared implementation for template dropdown fragment."""
    from ..presets.template_index import (
        templates_for_type_and_vendor,
        all_vendors_for_type,
        preview_for_template,
    )
    from collections import OrderedDict

    selected_type = type
    selected_vendor = template_vendor if reason != "type-change" else ""
    selected_template = template_override if reason != "type-change" else ""

    vendors = all_vendors_for_type(selected_type)
    templates = templates_for_type_and_vendor(selected_type, selected_vendor)

    # Group templates by vendor
    groups = OrderedDict()
    for t in templates:
        groups.setdefault(t["vendor"], []).append(t)
    template_groups = [{"vendor": v, "templates": ts} for v, ts in groups.items()]

    # Get preview for selected template
    preview = None
    if selected_template:
        preview = preview_for_template(selected_template)

    status_message = ""
    if reason == "type-change":
        status_message = f"Template reset to Generic {selected_type} defaults because material type changed."

    pid_or_new = pid if pid else "new"

    return _templates(request).TemplateResponse(
        request, "template_dropdown.html",
        {
            "pid_or_new": pid_or_new,
            "selected_type": selected_type,
            "selected_vendor": selected_vendor,
            "selected_template": selected_template,
            "vendors": vendors,
            "template_groups": template_groups,
            "preview": preview,
            "status_message": status_message,
        },
    )


@router.get("/profiles/new/template-dropdown", response_class=HTMLResponse)
def template_dropdown_new(
    request: Request,
    type: str = "PLA",
    template_vendor: str = "",
    template_override: str = "",
    reason: str = "",
) -> HTMLResponse:
    """HTMX fragment for new profile (no pid)."""
    return _template_dropdown_impl(request, None, type, template_vendor, template_override, reason)


@router.get("/profiles/{pid}/template-dropdown", response_class=HTMLResponse)
def template_dropdown_edit(
    request: Request,
    pid: int,
    type: str = "PLA",
    template_vendor: str = "",
    template_override: str = "",
    reason: str = "",
) -> HTMLResponse:
    """HTMX fragment for existing profile."""
    return _template_dropdown_impl(request, pid, type, template_vendor, template_override, reason)



@router.post("/profiles/form", response_class=HTMLResponse, response_model=None)
def profile_create_form(
    request: Request,
    db: DbSession,
    brand: str = Form(...),
    name: str = Form(...),
    type: str = Form(...),
    colors: str = Form(""),
    min_temp: int = Form(...),
    max_temp: int = Form(...),
    density: float = Form(0.0),
    drying_temp: int = Form(0),
    drying_time: int = Form(0),
    notes: str = Form(""),
    template_override: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    errors, _ = _validate(
        brand=brand, name=name, type_=type, min_temp=min_temp,
        max_temp=max_temp, density=density, drying_temp=drying_temp,
        drying_time=drying_time,
    )
    if errors:
        profile = _submitted_namespace(
            None, brand, name, type, colors, min_temp, max_temp, density,
            drying_temp, drying_time, notes, template_override,
        )
        return _templates(request).TemplateResponse(
            request, "profile_form.html",
            {"profile": profile, "errors": errors, "is_edit": False},
            status_code=422,
        )

    # Validate template_override belongs to the submitted type
    if template_override:
        from ..presets.template_index import load_index
        idx = load_index()
        entry = next((t for t in idx["templates"] if t["name"] == template_override), None)
        if entry is None or entry["type"] != type:
            template_override = ""  # clear mismatched override
    now = _now()
    p = Profile(
        **_profile_from_form(
            brand, name, type, colors, min_temp, max_temp, density,
            drying_temp, drying_time, notes, template_override,
        ),
        created_at=now, updated_at=now,
    )
    db.add(p)
    db.flush()
    return RedirectResponse(url="/profiles", status_code=303)


@router.post("/profiles/{pid}/form", response_class=HTMLResponse, response_model=None)
def profile_update_form(
    request: Request,
    pid: int,
    db: DbSession,
    brand: str = Form(...),
    name: str = Form(...),
    type: str = Form(...),
    colors: str = Form(""),
    min_temp: int = Form(...),
    max_temp: int = Form(...),
    density: float = Form(0.0),
    drying_temp: int = Form(0),
    drying_time: int = Form(0),
    notes: str = Form(""),
    template_override: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    p = db.get(Profile, pid)
    if p is None:
        raise HTTPException(status_code=404, detail="profile not found")

    errors, _ = _validate(
        brand=brand, name=name, type_=type, min_temp=min_temp,
        max_temp=max_temp, density=density, drying_temp=drying_temp,
        drying_time=drying_time,
    )
    if errors:
        profile = _submitted_namespace(
            pid, brand, name, type, colors, min_temp, max_temp, density,
            drying_temp, drying_time, notes, template_override,
        )
        return _templates(request).TemplateResponse(
            request, "profile_form.html",
            {"profile": profile, "errors": errors, "is_edit": True},
            status_code=422,
        )

    # Validate template_override belongs to the submitted type
    if template_override:
        from ..presets.template_index import load_index
        idx = load_index()
        entry = next((t for t in idx["templates"] if t["name"] == template_override), None)
        if entry is None or entry["type"] != type:
            template_override = ""  # clear mismatched override
    for key, val in _profile_from_form(
        brand, name, type, colors, min_temp, max_temp, density,
        drying_temp, drying_time, notes, template_override,
    ).items():
        setattr(p, key, val)
    p.updated_at = _now()
    db.flush()
    return RedirectResponse(url="/profiles", status_code=303)



@router.get("/profiles/{pid}/delete", response_class=HTMLResponse)
def profile_delete_confirm(request: Request, pid: int, db: DbSession) -> HTMLResponse:
    """Confirmation page — Rule 5 double-confirm for destructive delete."""
    p = db.get(Profile, pid)
    if p is None:
        raise HTTPException(status_code=404, detail="profile not found")
    spool_count = db.scalar(
        select(func.count(Spool.id)).where(Spool.profile_id == pid)
    ) or 0
    has_orca_cloud = db.scalar(
        select(SyncTarget.id).where(
            SyncTarget.profile_id == pid,
            SyncTarget.kind == "orca_preset",
            SyncTarget.cloud_id.isnot(None),
        )
    ) is not None
    return _templates(request).TemplateResponse(
        request, "profile_delete.html",
        {
            "profile": p,
            "spool_count": spool_count,
            "has_orca_cloud": has_orca_cloud,
            "errors": [],
        },
    )


@router.post("/profiles/{pid}/delete", response_class=HTMLResponse, response_model=None)
def profile_delete_form(
    request: Request,
    pid: int,
    db: DbSession,
    confirm: str | None = Form(None),
    force: str | None = Form(None),
    delete_cloud: str | None = Form(None),
) -> Response:
    """Delete a profile from the catalog UI (mirrors API delete semantics).

    When ``delete_cloud`` is checked, also attempt to remove the Orca Cloud
    preset via ``sync_delete``.  Cloud failures are fail-soft: the catalog
    delete proceeds, the redirect carries ``cloud_failed=1``, and a warn
    ActivityLog row is recorded.
    """
    p = db.get(Profile, pid)
    if p is None:
        raise HTTPException(status_code=404, detail="profile not found")
    spool_count = db.scalar(
        select(func.count(Spool.id)).where(Spool.profile_id == pid)
    ) or 0

    errors: list[str] = []
    if p.printer_db_id and (confirm or "") != p.name:
        errors.append(
            f'Typed confirmation must match the profile name exactly: "{p.name}".'
        )
    if spool_count > 0 and not force:
        errors.append(
            f"Profile has {spool_count} spool(s) in stock — tick the checkbox to "
            "delete them together with the profile."
        )
    if errors:
        has_orca_cloud = db.scalar(
            select(SyncTarget.id).where(
                SyncTarget.profile_id == pid,
                SyncTarget.kind == "orca_preset",
                SyncTarget.cloud_id.isnot(None),
            )
        ) is not None
        return _templates(request).TemplateResponse(
            request, "profile_delete.html",
            {
                "profile": p,
                "spool_count": spool_count,
                "has_orca_cloud": has_orca_cloud,
                "errors": errors,
            },
        )

    # Resolve cloud IDs before perform_delete wipes the SyncTarget rows.
    cloud_ids = list(db.scalars(
        select(SyncTarget.cloud_id).where(
            SyncTarget.profile_id == pid,
            SyncTarget.kind == "orca_preset",
            SyncTarget.cloud_id.isnot(None),
        )
    ))

    perform_delete(db, pid, force=bool(force), confirm=confirm)

    # Opt-in cloud preset deletion (fail-soft).
    cloud_failed = False
    if delete_cloud and cloud_ids:
        try:
            client = sync_handlers._orca_client_factory()
            if client is None:
                cloud_failed = True
            else:
                client.sync_delete(cloud_ids)
        except OrcaCloudError:
            cloud_failed = True
        jobs.log_activity(
            db, "orca_cloud_delete", "warn" if cloud_failed else "ok",
            {"profile_id": pid, "cloud_ids": cloud_ids,
             "error": "cloud unavailable" if cloud_failed else None},
        )
        db.commit()

    target = "/profiles?deleted=1"
    if cloud_failed:
        target += "&cloud_failed=1"
    return RedirectResponse(url=target, status_code=303)


@router.post("/profiles/{pid}/sync-from-orca", response_class=HTMLResponse)
def sync_from_orca(pid: int, request: Request, db: DbSession) -> HTMLResponse:
    """Pull Orca Cloud presets synchronously and return the fragment."""
    if sync_handlers._orca_client_factory() is None:
        profile = db.get(Profile, pid)
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")
        target = db.scalars(
            select(SyncTarget).where(
                SyncTarget.profile_id == pid,
                SyncTarget.kind == "orca_preset",
            )
        ).first()
        orca_data = None
        last_synced = None
        if target and target.actual_state:
            try:
                orca_data = json.loads(target.actual_state)
            except (json.JSONDecodeError, ValueError):
                orca_data = None
        if target and target.last_synced_at:
            last_synced = _fmt_local(target.last_synced_at)
        if profile.template_override:
            template_name = profile.template_override
        else:
            template_name = f"Generic {profile.type} defaults"
        return _templates(request).TemplateResponse(
            request, "orca_fragment.html",
            {"pid": pid, "orca_data": orca_data, "last_synced": last_synced,
             "syncing": False, "stale": False, "job_id": None,
             "synced_success": False, "template_name": template_name,
             "target_status": target.status if target else None,
             "orca_error": "Orca Cloud not connected — set up the connection in Settings"},
        )
    try:
        refresh_preset_drift(db, adopt=True)
        db.flush()
    except Exception:
        profile = db.get(Profile, pid)
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")
        target = db.scalars(
            select(SyncTarget).where(
                SyncTarget.profile_id == pid,
                SyncTarget.kind == "orca_preset",
            )
        ).first()
        orca_data = None
        last_synced = None
        if target and target.actual_state:
            try:
                orca_data = json.loads(target.actual_state)
            except (json.JSONDecodeError, ValueError):
                orca_data = None
        if target and target.last_synced_at:
            last_synced = _fmt_local(target.last_synced_at)
        if profile.template_override:
            template_name = profile.template_override
        else:
            template_name = f"Generic {profile.type} defaults"
        return _templates(request).TemplateResponse(
            request, "orca_fragment.html",
            {"pid": pid, "orca_data": orca_data, "last_synced": last_synced,
             "syncing": False, "stale": False, "job_id": None,
             "synced_success": False, "template_name": template_name,
             "target_status": target.status if target else None,
             "orca_error": "Pull failed — check your Orca Cloud connection and try again"},
        )

    profile = db.get(Profile, pid)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")

    target = db.scalars(
        select(SyncTarget).where(
            SyncTarget.profile_id == pid,
            SyncTarget.kind == "orca_preset",
        )
    ).first()

    orca_data = None
    last_synced = None
    if target and target.actual_state:
        try:
            orca_data = json.loads(target.actual_state)
        except (json.JSONDecodeError, ValueError):
            orca_data = None
    if target and target.last_synced_at:
        last_synced = _fmt_local(target.last_synced_at)

    if profile.template_override:
        template_name = profile.template_override
    else:
        template_name = f"Generic {profile.type} defaults"

    return _templates(request).TemplateResponse(
        request, "orca_fragment.html",
        {"pid": pid, "orca_data": orca_data, "last_synced": last_synced,
         "syncing": False, "stale": False, "job_id": None,
         "synced_success": True, "template_name": template_name,
         "target_status": target.status if target else None},
    )


@router.get("/profiles/{pid}/orca-fragment", response_class=HTMLResponse)
def orca_fragment(
    pid: int, request: Request, db: DbSession,
) -> HTMLResponse:
    """HTMX fragment: read-only Orca preset values from SyncTarget."""
    profile = db.get(Profile, pid)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")

    target = db.scalars(
        select(SyncTarget).where(
            SyncTarget.profile_id == pid,
            SyncTarget.kind == "orca_preset",
        )
    ).first()

    orca_data = None
    last_synced = None
    if target and target.actual_state:
        try:
            orca_data = json.loads(target.actual_state)
        except (json.JSONDecodeError, ValueError):
            orca_data = None
    if target and target.last_synced_at:
        last_synced = _fmt_local(target.last_synced_at)

    if profile.template_override:
        template_name = profile.template_override
    else:
        template_name = f"Generic {profile.type} defaults"

    return _templates(request).TemplateResponse(
        request, "orca_fragment.html",
        {
            "pid": pid,
            "orca_data": orca_data,
            "last_synced": last_synced,
            "syncing": False,
            "stale": False,
            "job_id": None,
            "template_name": template_name,
            "target_status": target.status if target else None,
        },
    )

# ---------------------------------------------------------------------------
# Spools — list + filters + add form + HTMX remaining patch
# ---------------------------------------------------------------------------
@router.get("/spools", response_class=HTMLResponse)
def spools_page(
    request: Request,
    db: DbSession,
    profile_id: str | None = None,
    location: str | None = None,
) -> HTMLResponse:
    pid = int(profile_id) if profile_id and profile_id.isdigit() else None
    stmt = select(Spool).order_by(Spool.id.desc())
    if pid:
        stmt = stmt.where(Spool.profile_id == pid)
    if location:
        stmt = stmt.where(Spool.location == location)
    rows = list(db.scalars(stmt))

    profiles = list(db.scalars(select(Profile).order_by(Profile.id)))

    return _templates(request).TemplateResponse(
        request, "spools.html",
        {
            "spools": rows,
            "profiles": profiles,
            "filter_profile_id": pid or "",
            "filter_location": location or "",
        },
    )


@router.post("/spools/form", response_class=HTMLResponse)
def spool_create_form(
    request: Request,
    db: DbSession,
    profile_id: int = Form(...),
    color_override: str | None = Form(None),
    remaining_pct: int = Form(100),
    location: str = Form(""),
    notes: str = Form(""),
) -> RedirectResponse:
    if db.get(Profile, profile_id) is None:
        raise HTTPException(status_code=404, detail="profile not found")
    now = _now()
    s = Spool(
        profile_id=profile_id,
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
    profile_id: int = Form(...),
    color_override: str | None = Form(None),
    remaining_pct: int = Form(100),
    location: str = Form(""),
    notes: str = Form(""),
) -> RedirectResponse:
    """Update a spool's editable fields from the inline edit form."""
    s = db.get(Spool, sid)
    if s is None:
        raise HTTPException(status_code=404, detail="spool not found")
    if db.get(Profile, profile_id) is None:
        raise HTTPException(status_code=404, detail="profile not found")
    s.profile_id = profile_id
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
        profile_id=s.profile_id,
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
# Sync page — drift table, action buttons, busy dialog, job progress
# ---------------------------------------------------------------------------

@router.get("/sync", response_class=HTMLResponse)
def sync_page(request: Request, db: DbSession) -> HTMLResponse:
    """Drift table per profile, HTMX action buttons, busy dialog for
    awaiting_input jobs, and a job progress area."""
    drift = drift_status(request, db)
    awaiting_jobs = list(db.scalars(
        select(Job).where(Job.status == "awaiting_input").order_by(Job.id)
    ))
    recent_jobs = list(db.scalars(
        select(Job).order_by(Job.id.desc()).limit(20)
    ))
    return _templates(request).TemplateResponse(
        request, "sync_hub.html",
        {
            "drift": drift,
            "awaiting_jobs": awaiting_jobs,
            "recent_jobs": recent_jobs,
            "github_configured": load_github_config() is not None,
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
# Generalized from the push-modal pattern to serve push/preset/pull/backup.
# ---------------------------------------------------------------------------

# Maps job.kind → modal kind for fragment rendering.
_JOB_KIND_TO_MODAL: dict[str, str] = {
    "push": "push",
    "generate_preset": "preset",
    "pull": "pull",
    "github_backup": "backup",
}

# Per-kind text for running / done / error states.
_ACTION_MODAL_RUNNING_TEXT: dict[str, str] = {
    "push": "Pushing to printer…",
    "preset": "Generating preset & pushing to Orca Cloud…",
    "pull": "Pulling from Orca Cloud…",
    "backup": "Backing up to GitHub…",
}
_ACTION_MODAL_ERROR_TITLE: dict[str, str] = {
    "push": "Push failed",
    "preset": "Push failed",
    "pull": "Pull failed",
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

    ``kind`` is one of "push", "preset", "pull", "backup" and controls
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

        if kind == "preset":
            preset_status = (result or {}).get("status", "in_sync")
            if preset_status == "drift":
                return (
                    f'<div class="push-modal-state push-modal-done">'
                    f'<div class="push-modal-icon push-modal-icon-err">⚠</div>'
                    f'<h3>Drift detected</h3>'
                    f'<p>Orca Cloud has a newer version of this preset.</p>'
                    f'<p class="push-modal-sub">Pull from Cloud to adopt the cloud baseline.</p>'
                    f'<button class="button button-secondary" onclick="closeActionModal()">Close</button>'
                    f'</div>'
                    + _hub_refresh_div()
                )
            github_note = ""
            if (result or {}).get("github_backup_scheduled"):
                github_note = '<p class="push-modal-sub">GitHub backup scheduled</p>'
            return (
                f'<div class="push-modal-state push-modal-done">'
                f'<div class="push-modal-icon push-modal-icon-ok">✓</div>'
                f'<h3>Preset pushed</h3>'
                f'<p>Preset pushed to Orca Cloud.</p>'
                f'{github_note}'
                f'<button class="button button-primary" onclick="closeActionModal()">Close</button>'
                f'</div>'
                + _hub_refresh_div()
            )

        if kind == "pull":
            return (
                f'<div class="push-modal-state push-modal-done">'
                f'<div class="push-modal-icon push-modal-icon-ok">✓</div>'
                f'<h3>Pull complete</h3>'
                f'<p>Pulled from Orca Cloud — baseline adopted.</p>'
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
) -> HTMLResponse:
    """Enqueue a push job, start background drain, return modal fragment.

    HTMX form endpoint: the push buttons POST here with optional
    ``profile_ids`` (comma-separated). The job is committed immediately
    so the background drain thread can see it. Returns the modal body
    fragment (spinner + polling).
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
    payload = {"profile_ids": ids, "force_reboot": False, "no_reboot": False}
    return _enqueue_and_modal(request, db, "push", "push", payload)


@router.get("/sync/push-modal/{job_id}", response_class=HTMLResponse)
def sync_push_modal_poll(job_id: int, db: DbSession) -> HTMLResponse:
    """Poll endpoint: return modal fragment for the job's current state."""
    return _action_modal_poll_impl(job_id, db)


@router.post("/sync/preset-modal", response_class=HTMLResponse)
def sync_preset_modal(
    request: Request, db: DbSession,
    profile_id: int = Form(...),
) -> HTMLResponse:
    """Enqueue a generate_preset job, start background drain, return modal fragment."""
    return _enqueue_and_modal(
        request, db, "preset", "generate_preset", {"profile_id": profile_id},
    )


@router.get("/sync/preset-modal/{job_id}", response_class=HTMLResponse)
def sync_preset_modal_poll(job_id: int, db: DbSession) -> HTMLResponse:
    """Poll endpoint for preset-modal: return modal fragment for the job's state."""
    return _action_modal_poll_impl(job_id, db)


@router.post("/sync/pull-modal", response_class=HTMLResponse)
def sync_pull_modal(
    request: Request, db: DbSession,
    profile_id: int | None = Form(None),
) -> HTMLResponse:
    """Enqueue a pull job, start background drain, return modal fragment.

    When ``profile_id`` is provided, pulls a single profile; otherwise
    pulls all profiles.  The pull handler wraps ``refresh_preset_drift``
    with ``adopt=True``.
    """
    payload = {"profile_id": profile_id} if profile_id else {}
    return _enqueue_and_modal(request, db, "pull", "pull", payload)


@router.get("/sync/pull-modal/{job_id}", response_class=HTMLResponse)
def sync_pull_modal_poll(job_id: int, db: DbSession) -> HTMLResponse:
    """Poll endpoint for pull-modal: return modal fragment for the job's state."""
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
    """Return the drift table + target cards as an HTMX fragment.

    Used by the action-modal completion path to refresh the Sync Hub's
    main content (drift table + right-column summaries) without a
    """
    drift = drift_status(request, db)
    awaiting_jobs = list(db.scalars(
        select(Job).where(Job.status == "awaiting_input").order_by(Job.id)
    ))
    return _templates(request).TemplateResponse(
        request, "_sync_hub_grid.html",
        {
            "drift": drift,
            "awaiting_jobs": awaiting_jobs,
            "github_configured": load_github_config() is not None,
        },
    )

# ---------------------------------------------------------------------------
# Migration wizard — one-time import of existing printer DB entries
# ---------------------------------------------------------------------------

def _latest_import_scan(db: Session) -> Job | None:
    """Most recent completed import_scan job, or None."""
    return db.scalars(
        select(Job).where(
            Job.kind == "import_scan", Job.status == "done",
        ).order_by(Job.id.desc())
    ).first()

@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request, db: DbSession) -> HTMLResponse:
    """Tabbed import page: 'From Printer DB' (migration wizard) and
    'From Orca Cloud' (unmatched preset import)."""
    tab = request.query_params.get("tab", "printer")

    # Printer DB tab data (same as old migration_page)
    applied = request.query_params.get("applied") == "1"
    show_scan = request.query_params.get("step") == "scan"
    import_job = _latest_import_scan(db)
    proposals: list[dict] = []
    if import_job and import_job.result:
        try:
            result = json.loads(import_job.result)
            proposals = result.get("proposals", [])
        except (json.JSONDecodeError, ValueError):
            proposals = []

    # Check for the latest import_scan job regardless of status — if it
    # errored, surface the error message instead of silently returning
    # to step 1.
    scan_error = None
    latest_scan = db.scalars(
        select(Job).where(Job.kind == "import_scan").order_by(Job.id.desc())
    ).first()
    if latest_scan and latest_scan.status == "error" and latest_scan.result:
        try:
            err_result = json.loads(latest_scan.result)
            scan_error = err_result.get("error", "Unknown error")
        except (json.JSONDecodeError, ValueError):
            scan_error = "Unknown error"

    existing_ids = set(
        db.scalars(
            select(Profile.printer_db_id).where(Profile.printer_db_id.isnot(None))
        )
    )

    # Orca Cloud tab data (same as old import_orca_page)
    all_presets = _pull_orca_presets()
    orca_presets = _unmatched_orca_presets(db)
    orca_proposals = []
    for p in orca_presets:
        orca_proposals.append({
            "filament_id": p.get("filament_id", ""),
            "name": p.get("name", ""),
            "brand": (p.get("filament_vendor") or [""])[0] if p.get("filament_vendor") else "",
            "type": (p.get("filament_type") or [""])[0] if p.get("filament_type") else "",
            "nozzle_temperature": (p.get("nozzle_temperature") or [""])[0] if p.get("nozzle_temperature") else "",
            "pressure_advance": (p.get("pressure_advance") or [""])[0] if p.get("pressure_advance") else "",
            "filament_flow_ratio": (p.get("filament_flow_ratio") or [""])[0] if p.get("filament_flow_ratio") else "",
            "inherits": p.get("inherits", ""),
            "user_tuned": not p.get("inherits", ""),
        })

    return _templates(request).TemplateResponse(
        request, "import.html",
        {
            "active_tab": tab,
            "proposals": proposals,
            "existing_ids": existing_ids,
            "import_job": import_job,
            "applied": applied,
            "show_scan": show_scan,
            "scan_error": scan_error,
            "orca_proposals": orca_proposals,
            "has_presets": len(all_presets) > 0,
        },
    )


@router.post("/import/printer/scan", response_class=HTMLResponse)
def import_printer_scan(request: Request, db: DbSession) -> RedirectResponse:
    """Enqueue import_scan job, drain, redirect to /import?tab=printer."""
    config = _get_config(request)
    job = jobs.enqueue(db, "import_scan", {})
    db.flush()
    jobs.run_pending(db, config)
    db.flush()
    return RedirectResponse(url="/import?tab=printer", status_code=303)


@router.post("/import/printer/apply", response_class=HTMLResponse)
def import_printer_apply(
    request: Request,
    db: DbSession,
    include: list[str] = Form(default=[]),
) -> RedirectResponse:
    """Create/update Profiles + SyncTargets from selected proposals.
    (Same logic as old migration_apply, redirect target changed.)
    """
    import_job = _latest_import_scan(db)
    if import_job is None:
        raise HTTPException(status_code=404, detail="no import scan result")

    result = json.loads(import_job.result) if import_job.result else {}
    proposals_by_id = {
        p["printer_db_id"]: p for p in result.get("proposals", [])
    }

    now = _now()
    for pid_str in include:
        proposal = proposals_by_id.get(pid_str)
        if proposal is None:
            continue

        existing = db.scalars(
            select(Profile).where(Profile.printer_db_id == pid_str)
        ).first()

        if existing is not None:
            existing.brand = proposal["brand"]
            existing.name = proposal["name"]
            existing.type = proposal["type"]
            existing.colors = proposal.get("colors", "[]")
            existing.min_temp = proposal["min_temp"]
            existing.max_temp = proposal["max_temp"]
            existing.density = proposal.get("density", 0.0)
            existing.drying_temp = proposal.get("drying_temp", 0)
            existing.drying_time = proposal.get("drying_time", 0)
            existing.nozzle_temperature = proposal.get("nozzle_temperature")
            if "flow_ratio" in proposal:
                existing.flow_ratio = proposal["flow_ratio"]
            if "pa" in proposal:
                existing.pa = proposal["pa"]
            if "max_volumetric" in proposal:
                existing.max_volumetric = proposal["max_volumetric"]
            existing.updated_at = now
            profile = existing
        else:
            profile = Profile(
                brand=proposal["brand"],
                name=proposal["name"],
                type=proposal["type"],
                colors=proposal.get("colors", "[]"),
                min_temp=proposal["min_temp"],
                max_temp=proposal["max_temp"],
                density=proposal.get("density", 0.0),
                drying_temp=proposal.get("drying_temp", 0),
                drying_time=proposal.get("drying_time", 0),
                nozzle_temperature=proposal.get("nozzle_temperature"),
                flow_ratio=proposal.get("flow_ratio"),
                pa=proposal.get("pa"),
                max_volumetric=proposal.get("max_volumetric"),
                printer_db_id=pid_str,
                filament_id=None,
                created_at=now,
                updated_at=now,
            )
            db.add(profile)
            db.flush()

        target = db.scalars(
            select(SyncTarget).where(
                SyncTarget.profile_id == profile.id,
                SyncTarget.kind == "printer_db",
            )
        ).first()
        if target is None:
            target = SyncTarget(
                profile_id=profile.id, kind="printer_db",
                status="pending",
            )
            db.add(target)
        target.status = "pending"
        target.last_synced_at = now
        db.flush()

    return RedirectResponse(url="/import?tab=printer&applied=1", status_code=303)

# ---------------------------------------------------------------------------
# Import from Orca Cloud — bring standard filament presets into the catalog
# ---------------------------------------------------------------------------


def _pull_orca_presets() -> list[dict]:
    """Pull all presets from Orca Cloud and convert to local format.

    Creates an OrcaCloudClient from stored credentials, calls sync_pull,
    and converts the upserts to local preset format (arrays for known
    fields).  Returns an empty list if no credentials are stored or the
    API call fails.
    """
    token_info = load_orca_token()
    if token_info is None:
        return []
    refresh_token, user_id = token_info
    try:
        client = OrcaCloudClient(refresh_token)
        result = client.sync_pull()
    except OrcaCloudError:
        return []
    upserts = result.get("upserts", [])
    return _cloud_presets_to_local(upserts)


def _unmatched_orca_presets(db: Session) -> list[dict]:
    """Return Orca Cloud presets not linked to any existing profile.

    A preset is matched if its ``filament_id`` matches a profile's
    ``filament_id``.  Unmatched presets are candidates for import.
    """
    presets = _pull_orca_presets()

    existing_fids = set(
        db.scalars(
            select(Profile.filament_id).where(Profile.filament_id.isnot(None))
        )
    )

    unmatched = []
    for p in presets:
        fid = p.get("filament_id", "")
        if fid and fid in existing_fids:
            continue
        # Only presets with identity (filament_id + type) are importable
        if not fid or not p.get("filament_type"):
            continue
        unmatched.append(p)
    return unmatched


@router.post("/import/orca/apply", response_class=HTMLResponse)
def import_orca_apply(
    request: Request,
    db: DbSession,
    include: list[str] = Form(default=[]),
) -> RedirectResponse:
    """Create profiles from selected OrcaSlicer presets.
    (Same logic as old import_orca_apply, redirect target changed.)
    """
    presets = _unmatched_orca_presets(db)
    by_fid = {p.get("filament_id", ""): p for p in presets}

    now = _now()
    for fid in include:
        p = by_fid.get(fid)
        if p is None:
            continue

        vendor = (p.get("filament_vendor") or [""])[0] if p.get("filament_vendor") else ""
        ftype = (p.get("filament_type") or ["PLA"])[0] if p.get("filament_type") else "PLA"
        temp = (p.get("nozzle_temperature") or ["220"])[0] if p.get("nozzle_temperature") else "220"
        try:
            temp_int = int(temp)
        except (ValueError, TypeError):
            temp_int = 220

        profile = Profile(
            brand=vendor,
            name=p.get("name", ""),
            type=ftype,
            colors="[]",
            min_temp=max(temp_int - 15, 0),
            max_temp=temp_int + 15,
            density=0.0,
            drying_temp=0,
            drying_time=0,
            nozzle_temperature=temp_int,
            filament_id=fid,
            printer_db_id=None,
            created_at=now,
            updated_at=now,
        )
        db.add(profile)
        db.flush()

        target = SyncTarget(
            profile_id=profile.id,
            kind="orca_preset",
            cloud_id=p.get("_path"),
            expected_hash=canonical_hash({
                "name": p.get("name"),
                "filament_id": p.get("filament_id"),
                "filament_vendor": p.get("filament_vendor"),
                "filament_type": p.get("filament_type"),
                "nozzle_temperature": p.get("nozzle_temperature"),
            }),
            actual_state=json.dumps(p),
            status="pending",
            last_synced_at=now,
        )
        db.add(target)

    return RedirectResponse(url="/import?tab=orca&applied=1", status_code=303)

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
