# fm/api/catalog.py
"""Catalog CRUD router — profiles and spools.

Validation reuses ``fm.printer.dbcodec.validate_entry`` semantics (temp ranges,
density, drying, vendor-in-name warning) adapted to the catalog column names
(``min_temp``/``max_temp`` instead of ``minTemp``/``maxTemp``).

Delete semantics (spec §6 + Iron Rule 5/7):
  * 404 when the profile does not exist.
  * 409 when the profile is referenced by spools, unless ``?force=true``
    (force cascades the spool deletion).
  * 409 when the profile has a ``printer_db_id``, unless ``?confirm=<name>``
    matches the profile name exactly (typed-token double-confirm).  This
    requirement is independent of ``force`` — a printer entry always needs the
    typed token.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from ..models import Profile, Spool, SyncTarget
from ..db import SessionLocal
from ..printer.dbcodec import validate_entry

router = APIRouter(prefix="/api", tags=["catalog"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Session dependency
# ---------------------------------------------------------------------------

def get_db():
    """Yield a SessionLocal session.

    ``fm.db._Session`` auto-commits on clean context-manager exit and rolls
    back on exception, so request handlers never need an explicit commit.
    """
    with SessionLocal() as s:
        yield s


DbSession = Annotated[Session, Depends(get_db)]


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class ProfileCreate(BaseModel):
    brand: str
    name: str
    type: str
    colors: list[str] = []
    min_temp: int
    max_temp: int
    density: float = 0.0
    drying_temp: int = 0
    drying_time: int = 0
    pa: float | None = None
    flow_ratio: float | None = None
    max_volumetric: float | None = None
    retraction_length: float | None = None
    retraction_speed: float | None = None
    nozzle_temperature: int | None = None
    template_override: str | None = None
    notes: str = ""


class ProfileUpdate(BaseModel):
    brand: str | None = None
    name: str | None = None
    type: str | None = None
    colors: list[str] | None = None
    min_temp: int | None = None
    max_temp: int | None = None
    density: float | None = None
    drying_temp: int | None = None
    drying_time: int | None = None
    pa: float | None = None
    flow_ratio: float | None = None
    max_volumetric: float | None = None
    retraction_length: float | None = None
    retraction_speed: float | None = None
    nozzle_temperature: int | None = None
    template_override: str | None = None
    notes: str | None = None


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    brand: str
    name: str
    type: str
    colors: list[str]
    min_temp: int
    max_temp: int
    density: float
    drying_temp: int
    drying_time: int
    pa: float | None = None
    flow_ratio: float | None = None
    max_volumetric: float | None = None
    retraction_length: float | None = None
    retraction_speed: float | None = None
    nozzle_temperature: int | None = None
    printer_db_id: str | None = None
    filament_id: str | None = None
    template_override: str | None = None
    notes: str
    created_at: datetime
    updated_at: datetime

    @field_validator("colors", mode="before")
    @classmethod
    def _parse_colors(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return json.loads(v)
        return v  # type: ignore[return-value]


class SpoolCreate(BaseModel):
    profile_id: int
    color_override: str | None = None
    remaining_pct: int = 100
    location: str = ""
    rfid_tag: str | None = None
    notes: str = ""


class SpoolUpdate(BaseModel):
    profile_id: int | None = None
    color_override: str | None = None
    remaining_pct: int | None = None
    location: str | None = None
    rfid_tag: str | None = None
    notes: str | None = None


class SpoolOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
    color_override: str | None = None
    remaining_pct: int
    location: str
    rfid_tag: str | None = None
    notes: str
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Validation helper — adapts catalog fields to dbcodec.validate_entry
# ---------------------------------------------------------------------------

def _entry_values(
    *,
    brand: str,
    name: str,
    type_: str,
    min_temp: int,
    max_temp: int,
    density: float,
    drying_temp: int,
    drying_time: int,
) -> dict:
    """Map catalog column names to the ``validate_entry`` key convention.

    Density/drying defaults of 0 are treated as "not set" (mirroring
    ``validate_entry``'s ``None``-skip) so a create that omits them does not
    trip the 0.9–1.6 density range check.
    """
    v: dict = {
        "brand": brand,
        "name": name,
        "type": type_,
        "minTemp": min_temp,
        "maxTemp": max_temp,
    }
    if density:
        v["density"] = density
    if drying_temp:
        v["dryingTemp"] = drying_temp
    if drying_time:
        v["dryingTime"] = drying_time
    return v


def _validate(*, brand, name, type_, min_temp, max_temp, density, drying_temp, drying_time):
    return validate_entry(
        _entry_values(
            brand=brand, name=name, type_=type_,
            min_temp=min_temp, max_temp=max_temp,
            density=density, drying_temp=drying_temp, drying_time=drying_time,
        )
    )


# ---------------------------------------------------------------------------
# Profile endpoints
# ---------------------------------------------------------------------------

@router.get("/profiles", response_model=list[ProfileOut])
def list_profiles(db: DbSession) -> list[Profile]:
    return list(db.scalars(select(Profile).order_by(Profile.id)))


@router.post("/profiles", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
def create_profile(data: ProfileCreate, db: DbSession) -> Profile:
    errors, _ = _validate(
        brand=data.brand, name=data.name, type_=data.type,
        min_temp=data.min_temp, max_temp=data.max_temp,
        density=data.density, drying_temp=data.drying_temp,
        drying_time=data.drying_time,
    )
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    now = _now()
    p = Profile(
        brand=data.brand, name=data.name, type=data.type,
        colors=json.dumps(data.colors),
        min_temp=data.min_temp, max_temp=data.max_temp,
        density=data.density, drying_temp=data.drying_temp,
        drying_time=data.drying_time, pa=data.pa,
        flow_ratio=data.flow_ratio, max_volumetric=data.max_volumetric,
        retraction_length=data.retraction_length,
        retraction_speed=data.retraction_speed,
        nozzle_temperature=data.nozzle_temperature,
        template_override=data.template_override,
        notes=data.notes, created_at=now, updated_at=now,
    )
    db.add(p)
    db.flush()
    return p


@router.get("/profiles/{pid}", response_model=ProfileOut)
def get_profile(pid: int, db: DbSession) -> Profile:
    p = db.get(Profile, pid)
    if p is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return p


@router.patch("/profiles/{pid}", response_model=ProfileOut)
def patch_profile(pid: int, data: ProfileUpdate, db: DbSession) -> Profile:
    p = db.get(Profile, pid)
    if p is None:
        raise HTTPException(status_code=404, detail="profile not found")

    for key, val in data.model_dump(exclude_unset=True).items():
        if key == "colors":
            # colors is NOT NULL; treat explicit null as "clear" → empty list.
            p.colors = json.dumps(val or [])
        else:
            setattr(p, key, val)

    # Validate the merged result so a partial update cannot leave temps
    # inconsistent (e.g. min_temp raised above existing max_temp).
    errors, _ = _validate(
        brand=p.brand, name=p.name, type_=p.type,
        min_temp=p.min_temp, max_temp=p.max_temp,
        density=p.density, drying_temp=p.drying_temp,
        drying_time=p.drying_time,
    )
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    p.updated_at = _now()
    db.flush()
    return p


def perform_delete(
    db: Session,
    pid: int,
    force: bool = False,
    confirm: str | None = None,
) -> None:
    """Shared delete semantics (spec §6 + Iron Rule 5/7).

    Raises ``HTTPException``:
      * 404 when the profile does not exist.
      * 409 when referenced by spools, unless ``force`` (cascades spools).
      * 409 when the profile has a ``printer_db_id``, unless ``confirm``
        matches the profile name exactly (typed-token double-confirm).
    Also removes the profile's sync targets — the FK has no cascade, so
    skipping this orphans rows for every pushed profile.
    """
    p = db.get(Profile, pid)
    if p is None:
        raise HTTPException(status_code=404, detail="profile not found")

    spool_count = db.scalar(
        select(func.count(Spool.id)).where(Spool.profile_id == pid)
    ) or 0

    # printer_db_id always requires the typed-token confirm (Rule 5/7).
    if p.printer_db_id and confirm != p.name:
        raise HTTPException(
            status_code=409,
            detail=(
                "profile has a printer_db_id — requires ?confirm=<name> "
                "typed-token matching the profile name"
            ),
        )

    # Spool references block unless force=true (cascade deletes spools).
    if spool_count > 0 and not force:
        raise HTTPException(
            status_code=409,
            detail=(
                f"profile referenced by {spool_count} spool(s) — "
                "use ?force=true to cascade-delete them"
            ),
        )

    if spool_count > 0 and force:
        db.execute(delete(Spool).where(Spool.profile_id == pid))

    db.execute(delete(SyncTarget).where(SyncTarget.profile_id == pid))
    db.delete(p)
    db.flush()


@router.delete("/profiles/{pid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile(
    pid: int,
    db: DbSession,
    force: bool = Query(False),
    confirm: str | None = Query(None),
) -> Response:
    perform_delete(db, pid, force=force, confirm=confirm)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

@router.post("/profiles/{pid}/validate")
def validate_profile(pid: int, db: DbSession) -> dict:
    """Return validation warnings for a stored profile (vendor-in-name, etc.)."""
    p = db.get(Profile, pid)
    if p is None:
        raise HTTPException(status_code=404, detail="profile not found")
    _, warnings = _validate(
        brand=p.brand, name=p.name, type_=p.type,
        min_temp=p.min_temp, max_temp=p.max_temp,
        density=p.density, drying_temp=p.drying_temp,
        drying_time=p.drying_time,
    )
    return {"warnings": warnings}


# ---------------------------------------------------------------------------
# Spool endpoints
# ---------------------------------------------------------------------------

@router.get("/spools", response_model=list[SpoolOut])
def list_spools(db: DbSession) -> list[Spool]:
    return list(db.scalars(select(Spool).order_by(Spool.id)))


@router.post("/spools", response_model=SpoolOut, status_code=status.HTTP_201_CREATED)
def create_spool(data: SpoolCreate, db: DbSession) -> Spool:
    if db.get(Profile, data.profile_id) is None:
        raise HTTPException(status_code=404, detail="profile not found")
    now = _now()
    s = Spool(
        profile_id=data.profile_id,
        color_override=data.color_override,
        remaining_pct=data.remaining_pct,
        location=data.location,
        rfid_tag=data.rfid_tag,
        notes=data.notes,
        created_at=now,
        updated_at=now,
    )
    db.add(s)
    db.flush()
    return s


@router.patch("/spools/{sid}", response_model=SpoolOut)
def patch_spool(sid: int, data: SpoolUpdate, db: DbSession) -> Spool:
    s = db.get(Spool, sid)
    if s is None:
        raise HTTPException(status_code=404, detail="spool not found")

    updates = data.model_dump(exclude_unset=True)
    if "profile_id" in updates:
        if updates["profile_id"] is None:
            raise HTTPException(
                status_code=422,
                detail="profile_id must reference an existing profile",
            )
        if db.get(Profile, updates["profile_id"]) is None:
            raise HTTPException(status_code=404, detail="profile not found")

    for key, val in updates.items():
        setattr(s, key, val)

    s.updated_at = _now()
    db.flush()
    return s


@router.delete("/spools/{sid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_spool(sid: int, db: DbSession) -> Response:
    s = db.get(Spool, sid)
    if s is None:
        raise HTTPException(status_code=404, detail="spool not found")
    db.delete(s)
    db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Preset template endpoints
# ---------------------------------------------------------------------------

@router.get("/templates")
def list_templates(type: str | None = None, vendor: str | None = None) -> dict:
    """List available preset templates, optionally filtered by type and vendor."""
    from ..presets.template_index import load_index, all_vendors_for_type
    idx = load_index()
    templates = idx["templates"]
    if type:
        templates = [t for t in templates if t["type"] == type]
    if vendor:
        vendor_lower = vendor.lower()
        templates = [t for t in templates if vendor_lower in t["vendor"].lower()]
    templates = sorted(templates, key=lambda t: (t["vendor"] != "Generic", t["vendor"], t["name"]))
    if type:
        vendors = all_vendors_for_type(type)
    else:
        vendors = sorted({t["vendor"] for t in idx["templates"]})
    return {"templates": templates, "vendors": vendors}
