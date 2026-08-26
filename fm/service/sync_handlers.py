# fm/service/sync_handlers.py
"""Sync orchestration handlers — registered into ``jobs.HANDLERS``.

Each handler receives ``(session, config, payload)`` and returns a result dict
that becomes the job's ``result`` on success.  Handlers are the integration
layer wiring the lifted cores (dbcodec, transport, pipeline, builder,
matching) into the job queue.

Handlers:
  * ``push`` — rebuild printer DB from cache + catalog, ``run_push``, update
    ``SyncTarget(printer_db)``, enqueue ``verify``.  Busy printer → returns
    ``{"_awaiting_input": True, ...}`` sentinel so ``run_pending`` parks the
    job; user resolves via ``POST /api/jobs/{id}/resolve``.
  * ``verify`` — ``scp_pull`` fresh DB, floor-check + presence + hash compare,
    update ``SyncTarget.status`` (``in_sync`` / ``error``).  Cloud-sync
    rollback (version < floor) → ``error`` + activity warn.
  * ``generate_preset`` — build preset dict + ``.info`` content via Task 6,
    enqueue ``apply_preset`` for the agent queue, set
    ``SyncTarget(orca_preset).expected_hash``.
  * ``temp_sync`` — requires recent ``report_presets`` result; computes
    ``temp_sync_targets``, applies ``nozzle_temperature`` changes to catalog,
    enqueues ``push``.

Transport / cache DB / preset template are injected via module-level factories
so tests can substitute ``FakeTransport`` and temp fixtures without touching
real SSH or ``/opt/orca-slicer``.
"""
from __future__ import annotations

import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Config
from ..models import Job, Profile, SyncTarget
from ..printer.dbcodec import (
    build_entry,
    find_entry,
    insert_entry,
    load_db,
    next_free_id,
    remove_entry,
    save_db,
)
from ..printer.pipeline import canonical_hash, run_push
from ..printer.transport import PrinterTransport, SshTransport
from ..presets.builder import build_standalone_preset
from ..presets.flattener import find_system_preset
from ..presets.matching import temp_sync_targets
from . import jobs

log = __import__("logging").getLogger(__name__)

# ---------------------------------------------------------------------------
# Injection points — tests override via monkeypatch.
# ---------------------------------------------------------------------------

_transport_factory: Callable[[Config], PrinterTransport] = lambda cfg: SshTransport(cfg)
_cache_db_path: Path | None = None
_preset_template_factory: Callable[[dict], dict | None] = lambda p: find_system_preset(p["type"])
_sleep = time.sleep  # tests override to no-op


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_transport(config: Config) -> PrinterTransport:
    return _transport_factory(config)


# ---------------------------------------------------------------------------
# SyncTarget helpers
# ---------------------------------------------------------------------------

def _get_or_create_target(session: Session, profile_id: int, kind: str) -> SyncTarget:
    target = session.scalars(
        select(SyncTarget).where(
            SyncTarget.profile_id == profile_id,
            SyncTarget.kind == kind,
        )
    ).first()
    if target is None:
        target = SyncTarget(profile_id=profile_id, kind=kind, status="pending")
        session.add(target)
        session.flush()
    return target


# ---------------------------------------------------------------------------
# Profile ↔ printer-DB entry mapping
# ---------------------------------------------------------------------------

def _profile_to_values(profile: Profile, entry_id: str) -> dict:
    """Map a catalog ``Profile`` to the ``build_entry`` values convention."""
    colors: list = []
    if profile.colors:
        try:
            colors = json.loads(profile.colors)
        except (json.JSONDecodeError, ValueError):
            colors = []
    values: dict = {
        "id": entry_id,
        "brand": profile.brand,
        "name": profile.name,
        "type": profile.type,
        "minTemp": profile.min_temp,
        "maxTemp": profile.max_temp,
        "density": profile.density,
        "dryingTemp": profile.drying_temp,
        "dryingTime": profile.drying_time,
    }
    if colors:
        values["color"] = colors[0]
    if profile.flow_ratio is not None:
        values["flowRatio"] = profile.flow_ratio
    if profile.pa is not None:
        values["pa"] = profile.pa
    if profile.max_volumetric is not None:
        values["maxVolumetric"] = profile.max_volumetric
    return values


def _entry_hash(entry: dict) -> str:
    """Canonical hash of an entry's identity + parameter fields."""
    b = entry["base"]
    kv = entry.get("kvParam", {})
    return canonical_hash({
        "id": b["id"],
        "brand": b["brand"],
        "name": b["name"],
        "type": b["meterialType"],
        "minTemp": b["minTemp"],
        "maxTemp": b["maxTemp"],
        "nozzle_temperature": kv.get("nozzle_temperature"),
    })


def _load_cache_db() -> dict:
    if _cache_db_path is not None and Path(_cache_db_path).exists():
        return load_db(_cache_db_path)
    raise FileNotFoundError(
        "cache DB not configured — set sync_handlers._cache_db_path"
    )


# ---------------------------------------------------------------------------
# DB rebuild
# ---------------------------------------------------------------------------

def _rebuild_printer_db(
    session: Session, config: Config, profile_ids: list[int] | None = None
) -> tuple[dict, list[tuple[Profile, dict]]]:
    """Rebuild the local printer DB from cache copy + catalog.

    For each target profile: remove any existing custom entry (by
    ``printer_db_id``), assign a new 99xxx ID if the profile has none, then
    insert a fresh entry via ``build_entry``.

    Returns ``(db, pushed)`` where ``pushed`` is a list of
    ``(profile, entry)`` tuples.
    """
    db = _load_cache_db()

    if profile_ids:
        profiles = list(session.scalars(
            select(Profile).where(Profile.id.in_(profile_ids))
            .order_by(Profile.id)
        ))
    else:
        profiles = list(session.scalars(select(Profile).order_by(Profile.id)))

    pushed: list[tuple[Profile, dict]] = []
    for profile in profiles:
        entry_id = profile.printer_db_id
        if entry_id:
            existing = find_entry(db, entry_id)
            if existing is not None:
                remove_entry(db, entry_id)
        else:
            entry_id = str(next_free_id(db, config.id_range_start))
            profile.printer_db_id = entry_id

        values = _profile_to_values(profile, entry_id)
        entry = build_entry(db, values)
        insert_entry(db, entry)
        pushed.append((profile, entry))

    return db, pushed


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def handle_push(session: Session, config: Config, payload: dict) -> dict:
    """Push handler: rebuild printer DB, ``run_push``, update SyncTargets,
    enqueue verify.  Busy → ``_awaiting_input`` sentinel.
    """
    profile_ids = payload.get("profile_ids")
    force_reboot = payload.get("force_reboot", False)
    no_reboot = payload.get("no_reboot", False)

    db, pushed = _rebuild_printer_db(session, config, profile_ids)

    local_path = Path(_cache_db_path) if _cache_db_path else Path(
        tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    )
    save_db(local_path, db)

    transport = _get_transport(config)
    result = run_push(
        config, local_path, transport,
        force_reboot=force_reboot, no_reboot=no_reboot,
        sleep=_sleep,
    )

    if result["status"] == "busy":
        return {
            "_awaiting_input": True,
            "options": ["force_reboot", "wait", "no_reboot"],
            "busy": result["job"],
        }

    # Success — update SyncTargets + printer_db_id
    for profile, entry in pushed:
        target = _get_or_create_target(session, profile.id, "printer_db")
        target.expected_hash = _entry_hash(entry)
        target.status = "pending"  # verify will set in_sync
        target.last_synced_at = _now()
    session.flush()

    # Enqueue verify
    jobs.enqueue(session, "verify", {})

    return {"status": result["status"], "pushed": len(pushed)}


def handle_verify(session: Session, config: Config, payload: dict) -> dict:
    """Verify handler: pull fresh DB, floor-check + presence + hash compare,
    update SyncTarget.status.  Cloud-sync rollback → error + warn.
    """
    transport = _get_transport(config)

    profiles = list(session.scalars(
        select(Profile).where(Profile.printer_db_id.isnot(None))
        .order_by(Profile.id)
    ))
    expected_ids = [p.printer_db_id for p in profiles]

    # Pull fresh DB from printer (ignoring cache)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        local = Path(f.name)
    try:
        transport.scp_pull(local)
        db = load_db(local)
    finally:
        local.unlink(missing_ok=True)

    # Version floor check (cloud sync sets ~epoch < override)
    version = db.get("result", {}).get("version")
    version_ok = False
    try:
        version_ok = int(version) >= int(config.version_override)
    except (ValueError, TypeError):
        version_ok = str(version) == str(config.version_override)

    cloud_rollback = not version_ok

    # Presence + hash check per profile
    ids = {e.get("base", {}).get("id")
           for e in db.get("result", {}).get("list", [])}

    for profile in profiles:
        target = _get_or_create_target(session, profile.id, "printer_db")
        if cloud_rollback:
            target.status = "error"
        elif profile.printer_db_id not in ids:
            target.status = "error"
        else:
            entry = find_entry(db, profile.printer_db_id)
            actual = _entry_hash(entry) if entry else None
            if target.expected_hash and actual == target.expected_hash:
                target.status = "in_sync"
            else:
                target.status = "error"
        target.last_synced_at = _now()

    if cloud_rollback:
        jobs.log_activity(
            session, "verify", "warn",
            {"reason": "cloud_sync_rollback", "version_ok": False},
        )

    present = [i for i in expected_ids if i in ids]
    missing = [i for i in expected_ids if i not in ids]

    return {
        "version_ok": version_ok,
        "present": present,
        "missing": missing,
        "cloud_rollback": cloud_rollback,
    }


def handle_generate_preset(session: Session, config: Config, payload: dict) -> dict:
    """Generate preset handler: build preset + .info, enqueue apply_preset,
    set SyncTarget(orca_preset).expected_hash.
    """
    profile_id = payload["profile_id"]
    profile = session.get(Profile, profile_id)
    if profile is None:
        raise ValueError(f"profile {profile_id} not found")

    profile_dict = {
        "id": profile.id,
        "brand": profile.brand,
        "name": profile.name,
        "type": profile.type,
        "min_temp": profile.min_temp,
        "max_temp": profile.max_temp,
        "density": profile.density,
        "colors": profile.colors,
        "pa": profile.pa,
        "flow_ratio": profile.flow_ratio,
        "max_volumetric": profile.max_volumetric,
        "retraction_length": profile.retraction_length,
        "retraction_speed": profile.retraction_speed,
        "nozzle_temperature": profile.nozzle_temperature,
        "printer_db_id": profile.printer_db_id,
    }

    template = _preset_template_factory(profile_dict)
    if template is None:
        raise ValueError(
            f"no system preset template for type {profile.type}"
        )

    preset = build_standalone_preset(profile_dict, template)

    # Build .info content (key=value format, empty sync_info/setting_id/base_id)
    info_content = "\n".join([
        "sync_info = ",
        "setting_id = ",
        "base_id = ",
        f"updated_time = {int(time.time())}",
    ]) + "\n"

    # Expected hash from preset identity + parameter fields
    preset_fields = {
        "name": preset["name"],
        "filament_id": preset["filament_id"],
        "filament_vendor": preset["filament_vendor"],
        "filament_type": preset["filament_type"],
        "nozzle_temperature": preset["nozzle_temperature"],
    }
    expected_hash = canonical_hash(preset_fields)

    # Update profile filament_id
    profile.filament_id = preset["filament_id"]

    # Set SyncTarget
    target = _get_or_create_target(session, profile.id, "orca_preset")
    target.expected_hash = expected_hash
    target.status = "pending"

    # Enqueue apply_preset for the agent queue
    jobs.enqueue(session, "apply_preset", {
        "profile_id": profile_id,
        "name": preset["name"],
        "preset": preset,
        "info": info_content,
    })

    return {
        "filament_id": preset["filament_id"],
        "expected_hash": expected_hash,
    }


def handle_temp_sync(session: Session, config: Config, payload: dict) -> dict:
    """Temp sync handler: compute temp_sync_targets from recent report_presets,
    apply nozzle_temperature changes to catalog, enqueue push.
    """
    # Find most recent completed report_presets job
    report_job = session.scalars(
        select(Job)
        .where(Job.kind == "report_presets", Job.status == "done")
        .order_by(Job.id.desc())
    ).first()
    if report_job is None:
        raise ValueError(
            "no report_presets result available; run report_presets first"
        )

    report_result = json.loads(report_job.result) if report_job.result else {}
    presets = report_result.get("presets", [])

    # Build profiles_state from catalog (only pushed profiles)
    profile_ids = payload.get("profile_ids")
    if profile_ids:
        profiles = list(session.scalars(
            select(Profile).where(Profile.id.in_(profile_ids))
            .order_by(Profile.id)
        ))
    else:
        profiles = list(session.scalars(
            select(Profile).where(Profile.printer_db_id.isnot(None))
            .order_by(Profile.id)
        ))

    profiles_state = [{
        "id": p.id,
        "brand": p.brand,
        "name": p.name,
        "type": p.type,
        "nozzle_temperature": p.nozzle_temperature,
        "printer_db_id": p.printer_db_id,
    } for p in profiles]

    updates, skipped = temp_sync_targets(profiles_state, presets)

    # Apply nozzle_temperature changes to catalog
    for update in updates:
        profile = session.get(Profile, update["profile_id"])
        if profile is not None:
            profile.nozzle_temperature = update["orca_temp"]

    # Enqueue push to propagate temp changes to printer DB
    jobs.enqueue(session, "push", {})

    return {"updates": updates, "skipped": skipped}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_handlers() -> None:
    """Register all sync handlers into ``jobs.HANDLERS``."""
    jobs.HANDLERS["push"] = handle_push
    jobs.HANDLERS["verify"] = handle_verify
    jobs.HANDLERS["generate_preset"] = handle_generate_preset
    jobs.HANDLERS["temp_sync"] = handle_temp_sync


# Auto-register on import so including the sync router wires handlers.
register_handlers()
