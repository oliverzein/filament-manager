# fm/api/ui.py
"""UI page routes — server-side rendered Jinja2 + HTMX interactivity.

Functional pages only; visual/layout refinement is a separate designer pass
(see AGENTS.md "Designer role for UI"). Reuses catalog service logic
(``_validate``) and sync ``drift_status`` directly via the shared
``SessionLocal`` dependency rather than self-HTTP calls.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import ActivityLog, Profile, Spool
from .catalog import _validate
from .sync import drift_status

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


def _parse_colors(raw: str) -> list[str]:
    """Comma-separated hex string -> list of trimmed hex values."""
    return [c.strip() for c in raw.split(",") if c.strip()]


def _profile_from_form(
    brand: str, name: str, type_: str, colors: str,
    min_temp: int, max_temp: int, density: float,
    drying_temp: int, drying_time: int,
    pa: float | None, flow_ratio: float | None, max_volumetric: float | None,
    retraction_length: float | None, retraction_speed: float | None,
    nozzle_temperature: int | None, notes: str,
) -> dict:
    """Build a profile field dict from submitted form values."""
    return {
        "brand": brand, "name": name, "type": type_,
        "colors": json.dumps(_parse_colors(colors)),
        "min_temp": min_temp, "max_temp": max_temp,
        "density": density, "drying_temp": drying_temp,
        "drying_time": drying_time, "pa": pa, "flow_ratio": flow_ratio,
        "max_volumetric": max_volumetric, "retraction_length": retraction_length,
        "retraction_speed": retraction_speed,
        "nozzle_temperature": nozzle_temperature, "notes": notes,
    }


def _submitted_namespace(
    pid: int | None, brand: str, name: str, type_: str, colors: str,
    min_temp: int, max_temp: int, density: float,
    drying_temp: int, drying_time: int,
    pa: float | None, flow_ratio: float | None, max_volumetric: float | None,
    retraction_length: float | None, retraction_speed: float | None,
    nozzle_temperature: int | None, notes: str,
) -> SimpleNamespace:
    """A profile-like namespace carrying submitted values back into the form
    on validation error re-render."""
    ns = SimpleNamespace(
        id=pid, brand=brand, name=name, type=type_,
        colors=_parse_colors(colors), min_temp=min_temp, max_temp=max_temp,
        density=density, drying_temp=drying_temp, drying_time=drying_time,
        pa=pa, flow_ratio=flow_ratio, max_volumetric=max_volumetric,
        retraction_length=retraction_length, retraction_speed=retraction_speed,
        nozzle_temperature=nozzle_temperature, notes=notes,
        printer_db_id=None, filament_id=None,
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

    drift = drift_status(db)

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

    return _templates(request).TemplateResponse(
        request, "profiles.html",
        {
            "profiles": rows,
            "filter_type": type or "",
            "filter_brand": brand or "",
            "brands": brands,
            "types": types,
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
    pa: float | None = Form(None),
    flow_ratio: float | None = Form(None),
    max_volumetric: float | None = Form(None),
    retraction_length: float | None = Form(None),
    retraction_speed: float | None = Form(None),
    nozzle_temperature: int | None = Form(None),
    notes: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    errors, _ = _validate(
        brand=brand, name=name, type_=type, min_temp=min_temp,
        max_temp=max_temp, density=density, drying_temp=drying_temp,
        drying_time=drying_time,
    )
    if errors:
        profile = _submitted_namespace(
            None, brand, name, type, colors, min_temp, max_temp, density,
            drying_temp, drying_time, pa, flow_ratio, max_volumetric,
            retraction_length, retraction_speed, nozzle_temperature, notes,
        )
        return _templates(request).TemplateResponse(
            request, "profile_form.html",
            {"profile": profile, "errors": errors, "is_edit": False},
            status_code=422,
        )

    now = _now()
    p = Profile(
        **_profile_from_form(
            brand, name, type, colors, min_temp, max_temp, density,
            drying_temp, drying_time, pa, flow_ratio, max_volumetric,
            retraction_length, retraction_speed, nozzle_temperature, notes,
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
    pa: float | None = Form(None),
    flow_ratio: float | None = Form(None),
    max_volumetric: float | None = Form(None),
    retraction_length: float | None = Form(None),
    retraction_speed: float | None = Form(None),
    nozzle_temperature: int | None = Form(None),
    notes: str = Form(""),
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
            drying_temp, drying_time, pa, flow_ratio, max_volumetric,
            retraction_length, retraction_speed, nozzle_temperature, notes,
        )
        return _templates(request).TemplateResponse(
            request, "profile_form.html",
            {"profile": profile, "errors": errors, "is_edit": True},
            status_code=422,
        )

    for key, val in _profile_from_form(
        brand, name, type, colors, min_temp, max_temp, density,
        drying_temp, drying_time, pa, flow_ratio, max_volumetric,
        retraction_length, retraction_speed, nozzle_temperature, notes,
    ).items():
        setattr(p, key, val)
    p.updated_at = _now()
    db.flush()
    return RedirectResponse(url="/profiles", status_code=303)


# ---------------------------------------------------------------------------
# Spools — list + filters + add form + HTMX remaining patch
# ---------------------------------------------------------------------------
@router.get("/spools", response_class=HTMLResponse)
def spools_page(
    request: Request,
    db: DbSession,
    profile_id: int | None = None,
    location: str | None = None,
) -> HTMLResponse:
    stmt = select(Spool).order_by(Spool.id)
    if profile_id:
        stmt = stmt.where(Spool.profile_id == profile_id)
    if location:
        stmt = stmt.where(Spool.location == location)
    rows = list(db.scalars(stmt))

    profiles = list(db.scalars(select(Profile).order_by(Profile.id)))

    return _templates(request).TemplateResponse(
        request, "spools.html",
        {
            "spools": rows,
            "profiles": profiles,
            "filter_profile_id": profile_id or "",
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
    rfid_tag: str | None = Form(None),
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
        rfid_tag=rfid_tag or None,
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
