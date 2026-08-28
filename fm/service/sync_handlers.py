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
  * ``generate_preset`` — build preset dict via Task 6, push directly to
    Orca Cloud via ``OrcaCloudClient.sync_push``, set
    ``SyncTarget(orca_preset).expected_hash``.
  * ``temp_sync`` — pull presets from Orca Cloud via ``sync_pull``, compute
    ``temp_sync_targets``, apply ``nozzle_temperature`` changes to catalog,
    enqueue ``push``.

Transport / cache DB / preset template / Orca Cloud client are injected via
module-level factories so tests can substitute ``FakeTransport`` and mock
clients without touching real SSH or the Orca Cloud API.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Config
from ..models import Profile, SyncTarget
from ..printer.dbcodec import (
    build_entry,
    find_entry,
    find_custom_entries,
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
from ..orca_cloud import OrcaCloudClient, OrcaCloudConflict, OrcaCloudError
from ..settings_store import load_orca_token, save_orca_token
from . import jobs

log = __import__("logging").getLogger(__name__)

# ---------------------------------------------------------------------------
# Injection points — tests override via monkeypatch.
# ---------------------------------------------------------------------------

_transport_factory: Callable[[Config], PrinterTransport] = lambda cfg: SshTransport(cfg)
_cache_db_path: Path | None = None
_import_db_path: Path | None = None  # import_scan: pre-pulled DB for tests


def _preset_template_factory(profile_dict: dict) -> dict | None:
    """Resolve the preset template for a profile.

    If template_override is set, use the brand-specific @base template.
    Otherwise fall back to the type-based generic default.
    """
    override = profile_dict.get("template_override")
    if override:
        from ..presets.flattener import find_preset_by_name, flatten_preset
        path = find_preset_by_name(override + " @base")
        if path:
            return flatten_preset(path)
        log.warning(
            "template_override %r not found in bundled templates; "
            "falling back to type default %r",
            override, profile_dict["type"],
        )
    return find_system_preset(profile_dict["type"])


_sleep = time.sleep  # tests override to no-op
def _default_orca_client() -> OrcaCloudClient | None:
    """Create an OrcaCloudClient from stored settings, or None if unconfigured."""
    token_info = load_orca_token()
    if token_info is None:
        return None
    refresh_token, user_id = token_info
    return OrcaCloudClient(
        refresh_token=refresh_token,
        on_token_rotated=lambda new_token: save_orca_token(new_token, user_id),
    )


# Orca Cloud client factory — tests override via monkeypatch.
# Production default loads refresh token from settings; returns None when
# no Orca Cloud credentials are configured.
_orca_client_factory: Callable[[], OrcaCloudClient | None] = _default_orca_client


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_transport(config: Config) -> PrinterTransport:
    return _transport_factory(config)


# ---------------------------------------------------------------------------
# Orca Cloud format conversion
# ---------------------------------------------------------------------------

# Fields that the local preset format stores as arrays (Orca Cloud stores
# them as flat strings).  All other fields stay strings.
_ARRAY_FIELDS = frozenset({
    "filament_type",
    "filament_vendor",
    "nozzle_temperature",
    "nozzle_temperature_initial_layer",
    "pressure_advance",
    "enable_pressure_advance",
    "filament_flow_ratio",
    "filament_max_volumetric_speed",
    "filament_retraction_length",
    "filament_retraction_speed",
    "fan_min_speed",
    "fan_max_speed",
    "cool_plate_temp",
    "eng_plate_temp",
    "hot_plate_temp",
})


def _cloud_presets_to_local(upserts: list[dict]) -> list[dict]:
    """Convert Orca Cloud sync_pull upserts to local preset format.

    Filters to filament presets only (``type=filament``).  Process and
    printer presets are excluded — they share the same sync endpoint
    but are irrelevant for filament drift detection.

    Cloud content is a flat JSON object with string values.  The local
    OrcaSlicer preset format (and what ``match_presets`` / templates expect)
    uses arrays for parameter fields.  This wraps known parameter fields in
    arrays and adds ``_path`` / ``_system`` internal keys so ``match_presets``
    works.
    """
    presets: list[dict] = []
    for upsert in upserts:
        content = upsert.get("content") or {}
        if content.get("type") != "filament":
            continue
        preset: dict = {}
        for key, value in content.items():
            if key in _ARRAY_FIELDS and isinstance(value, str):
                preset[key] = [value]
            else:
                preset[key] = value
        preset["_path"] = upsert.get("id", "")
        preset["_system"] = False
        presets.append(preset)
    return presets


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
    if profile.nozzle_temperature is not None:
        values["nozzle_temperature"] = profile.nozzle_temperature
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
        "density": b.get("density"),
        "dryingTemp": b.get("dryingTemp"),
        "dryingTime": b.get("dryingTime"),
        "pa": kv.get("pressure_advance"),
        "flow_ratio": kv.get("filament_flow_ratio"),
        "max_volumetric": kv.get("filament_max_volumetric_speed"),
    })


def _sqlite_path_from_url(url: str) -> Path | None:
    """Extract the filesystem path from a ``sqlite:///`` URL."""
    if not url.startswith("sqlite:///"):
        return None
    rest = url[len("sqlite:///"):]
    # sqlite:////data/filaments.db -> /data/filaments.db (absolute, 4 slashes)
    if rest.startswith("/"):
        return Path(rest)
    # sqlite:///.local/filaments.db -> .local/filaments.db (relative)
    return Path(rest)


def _resolve_cache_path() -> Path | None:
    """Production default cache DB path.

    Resolution order: explicit ``FM_CACHE_DB_PATH`` env, then derived from
    ``FM_DATABASE_URL``'s parent dir (``<data-dir>/printer-db-cache.json``).
    Returns ``None`` when neither is set (tests leave ``_cache_db_path`` as
    ``None`` and monkeypatch it).
    """
    env_path = os.environ.get("FM_CACHE_DB_PATH")
    if env_path:
        return Path(env_path)
    db_url = os.environ.get("FM_DATABASE_URL")
    if db_url:
        db_path = _sqlite_path_from_url(db_url)
        if db_path is not None:
            return db_path.parent / "printer-db-cache.json"
    return None


def _load_cache_db(config: Config, transport: PrinterTransport) -> dict:
    """Load the cached printer DB, pulling from the printer on first run.

    When a cache file exists (explicit ``_cache_db_path``, ``FM_CACHE_DB_PATH``,
    or the derived default) it is loaded directly.  When no cache file exists
    the current printer DB is fetched via ``transport.scp_pull`` and persisted
    to the resolved cache path for subsequent rebuilds.
    """
    path = _cache_db_path if _cache_db_path is not None else _resolve_cache_path()
    if path is not None and Path(path).exists():
        return load_db(path)
    # No cache file — pull fresh from the printer.
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = Path(f.name)
    try:
        transport.scp_pull(tmp)
        db = load_db(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        save_db(path, db)
    return db


# ---------------------------------------------------------------------------
# DB rebuild
# ---------------------------------------------------------------------------

def _rebuild_printer_db(
    session: Session, config: Config, transport: PrinterTransport,
    profile_ids: list[int] | None = None,
) -> tuple[dict, list[tuple[Profile, dict]]]:
    """Rebuild the local printer DB from cache copy + catalog.

    For each target profile: remove any existing custom entry (by
    ``printer_db_id``), assign a new 99xxx ID if the profile has none, then
    insert a fresh entry via ``build_entry``.  After processing, orphaned
    custom entries (99xxx IDs with no matching catalog profile) are removed so
    deleted profiles don't linger on the printer forever.

    Returns ``(db, pushed)`` where ``pushed`` is a list of
    ``(profile, entry)`` tuples.
    """
    db = _load_cache_db(config, transport)

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

    # Remove orphaned custom entries — 99xxx IDs with no current catalog
    # profile.  Deleted profiles would otherwise be re-pushed indefinitely.
    current_db_ids = set(session.scalars(
        select(Profile.printer_db_id).where(Profile.printer_db_id.isnot(None))
    ))
    for entry in find_custom_entries(db):
        eid = entry["base"]["id"]
        if eid not in current_db_ids:
            remove_entry(db, eid)

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

    transport = _get_transport(config)
    db, pushed = _rebuild_printer_db(session, config, transport, profile_ids)

    cache_path = _cache_db_path if _cache_db_path is not None else _resolve_cache_path()
    local_path = Path(cache_path) if cache_path is not None else Path(
        tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    )
    save_db(local_path, db)

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
    """Generate preset handler: build preset, push directly to Orca Cloud,
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
        "template_override": profile.template_override,
    }

    template = _preset_template_factory(profile_dict)
    if template is None:
        raise ValueError(
            f"no system preset template for type {profile.type}"
        )

    preset = build_standalone_preset(profile_dict, template)

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

    # Set SyncTarget with cloud_id (Orca Cloud resource UUID, like setting_id)
    target = _get_or_create_target(session, profile.id, "orca_preset")
    target.expected_hash = expected_hash
    target.cloud_id = None  # set after successful push
    target.status = "pending"

    # Push preset directly to Orca Cloud
    client = _orca_client_factory()
    if client is None:
        raise ValueError(
            "Orca Cloud not configured — set up the connection in Settings"
        )

    try:
        client.sync_push(
            profile_id=preset["filament_id"],
            name=preset["name"],
            content=preset,
        )
    except OrcaCloudConflict as exc:
        # Conflict means the profile already exists on Orca Cloud.
        # Retry with the server's updated_time to force an update.
        server_profile = exc.server_profile or {}
        server_time = server_profile.get("updated_time", "")
        if server_time:
            try:
                client.sync_push(
                    profile_id=preset["filament_id"],
                    name=preset["name"],
                    content=preset,
                    original_updated_time=str(server_time),
                )
            except OrcaCloudConflict:
                log.warning(
                    "Orca Cloud conflict for profile %s (filament_id=%s) — "
                    "server has a newer version",
                    profile_id, preset["filament_id"],
                )
                target.status = "drift"
                return {
                    "filament_id": preset["filament_id"],
                    "expected_hash": expected_hash,
                    "status": "drift",
                }
        else:
            target.status = "drift"
            return {
                "filament_id": preset["filament_id"],
                "expected_hash": expected_hash,
                "status": "drift",
            }
    # Store cloud resource UUID for exact-match on future pulls
    target.cloud_id = client.cloud_id(preset["filament_id"])
    target.status = "in_sync"
    target.last_synced_at = _now()

    return {
        "filament_id": preset["filament_id"],
        "expected_hash": expected_hash,
        "status": "in_sync",
    }


def handle_temp_sync(session: Session, config: Config, payload: dict) -> dict:
    """Temp sync handler: pull presets from Orca Cloud, compute
    temp_sync_targets, apply nozzle_temperature changes to catalog,
    enqueue push.
    """
    # Pull presets directly from Orca Cloud
    client = _orca_client_factory()
    if client is None:
        raise ValueError(
            "Orca Cloud not configured — set up the connection in Settings"
        )
    pull_result = client.sync_pull()
    presets = _cloud_presets_to_local(pull_result.get("upserts", []))

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
# Preset drift computation
# ---------------------------------------------------------------------------

def refresh_preset_drift(session: Session) -> None:
    """Update ``orca_preset`` SyncTarget status from Orca Cloud sync_pull.

    Matching mirrors OrcaSlicer desktop's ``setting_id`` approach:
    1. If a SyncTarget has ``cloud_id``, match by it against the cloud
       resource ``id`` (exact, no scoring).
    2. If a SyncTarget has no ``cloud_id`` (legacy, pushed by the old
       desktop agent), discover it via name matching and store the
       cloud ``id`` as ``cloud_id`` for future exact matches.
    3. If no match at all, mark as ``drift`` (deleted from cloud).

    Profiles without a SyncTarget (never pushed) are skipped — they
    appear as ``pending`` in the drift table.
    """
    from ..presets.matching import match_presets

    # Pull presets directly from Orca Cloud
    client = _orca_client_factory()
    if client is None:
        return
    pull_result = client.sync_pull()
    presets = _cloud_presets_to_local(pull_result.get("upserts", []))
    deleted_ids = set(pull_result.get("deletes", []))

    # Index by cloud resource id (the UUID in the upsert, not filament_id)
    by_cloud_id = {upsert.get("id"): upsert for upsert in pull_result.get("upserts", [])
                   if (upsert.get("content") or {}).get("type") == "filament"}
    by_name = {p.get("name", ""): p for p in presets}

    # Process existing orca_preset targets only
    targets = list(session.scalars(
        select(SyncTarget).where(SyncTarget.kind == "orca_preset")
    ))

    for target in targets:
        profile = session.get(Profile, target.profile_id)
        if profile is None:
            continue

        upsert = None

        # Strategy 1: exact match by cloud_id (like OrcaSlicer's setting_id)
        if target.cloud_id:
            upsert = by_cloud_id.get(target.cloud_id)

        # Strategy 2: legacy migration — discover cloud_id via name matching
        if upsert is None and not target.cloud_id:
            matches, _ = match_presets(
                presets, profile.brand, profile.name, profile.type,
            )
            if matches and matches[0]["score"] >= 30:
                matched_name = matches[0]["name"]
                preset = by_name.get(matched_name)
                if preset:
                    # Find the cloud resource id for this preset
                    for up in pull_result.get("upserts", []):
                        content = up.get("content") or {}
                        if content.get("type") == "filament" and content.get("name") == matched_name:
                            upsert = up
                            # Store cloud_id for future exact matches
                            target.cloud_id = up.get("id")
                            break

        # Check explicit deletes
        if target.cloud_id and target.cloud_id in deleted_ids:
            target.status = "drift"
            target.actual_state = None
            target.last_synced_at = _now()
            continue

        if upsert is None:
            # Not in cloud — preset was deleted or never pushed via cloud
            target.status = "drift"
            target.actual_state = None
            target.last_synced_at = _now()
            continue

        # Convert to local format for hash comparison
        preset = None
        for p in presets:
            if p.get("_path") == upsert.get("id"):
                preset = p
                break

        if preset is None:
            continue

        # Compute drift hash if expected_hash is set
        if target.expected_hash:
            actual_hash = canonical_hash({
                "name": preset.get("name"),
                "filament_id": preset.get("filament_id"),
                "filament_vendor": preset.get("filament_vendor"),
                "filament_type": preset.get("filament_type"),
                "nozzle_temperature": preset.get("nozzle_temperature"),
            })
            target.status = "in_sync" if actual_hash == target.expected_hash else "drift"
        else:
            target.status = "in_sync"

        target.actual_state = json.dumps(preset)
        target.last_synced_at = _now()

    session.flush()


# ---------------------------------------------------------------------------
# Migration: import_scan
# ---------------------------------------------------------------------------

def _entry_to_proposal(entry: dict) -> dict:
    """Convert a printer-DB custom (99xxx) entry to a migration proposal dict.

    Adapted from ``cfs.py:convert_orca_to_db_values`` (line 230) — maps
    printer-DB ``base``/``kvParam`` fields to catalog ``Profile`` column names.
    The temp-range logic mirrors the Orca preset conversion: prefer
    ``nozzle_temperature_range_low/high``; fall back to ``nozzle_temperature``.
    """
    b = entry["base"]
    kv = entry.get("kvParam", {})

    range_low = kv.get("nozzle_temperature_range_low")
    range_high = kv.get("nozzle_temperature_range_high")
    if range_low is not None and range_high is not None:
        min_temp = int(range_low)
        max_temp = int(range_high)
    else:
        nozzle = kv.get("nozzle_temperature")
        temp = int(nozzle) if nozzle else 0
        min_temp = temp
        max_temp = temp

    proposal: dict = {
        "printer_db_id": b["id"],
        "brand": b.get("brand", ""),
        "name": b.get("name", ""),
        "type": b.get("meterialType", ""),
        "min_temp": min_temp,
        "max_temp": max_temp,
        "density": b.get("density", 0.0),
        "drying_temp": b.get("dryingTemp", 0),
        "drying_time": b.get("dryingTime", 0),
        "colors": json.dumps(b.get("colors", [])),
        "nozzle_temperature": int(kv["nozzle_temperature"])
        if kv.get("nozzle_temperature") else None,
    }

    flow_ratio = kv.get("filament_flow_ratio")
    if flow_ratio and flow_ratio != "nil":
        proposal["flow_ratio"] = float(flow_ratio)

    pa = kv.get("pressure_advance")
    if pa and pa != "nil":
        proposal["pa"] = float(pa)

    max_volumetric = kv.get("filament_max_volumetric_speed")
    if max_volumetric and max_volumetric != "nil":
        proposal["max_volumetric"] = float(max_volumetric)

    return proposal


def handle_import_scan(session: Session, config: Config, payload: dict) -> dict:
    """Import-scan handler: pull printer DB, extract all 99xxx entries as
    migration proposals.

    In production, ``scp_pull`` fetches the live printer DB.  Tests override
    ``_import_db_path`` to point at a fixture file, bypassing SSH.
    """
    if _import_db_path is not None:
        db = load_db(_import_db_path)
    else:
        transport = _get_transport(config)
        local_path = Path(tempfile.NamedTemporaryFile(suffix=".json", delete=False).name)
        try:
            transport.scp_pull(local_path)
            db = load_db(local_path)
        finally:
            local_path.unlink(missing_ok=True)

    entries = find_custom_entries(db)
    proposals = [_entry_to_proposal(e) for e in entries]

    return {"proposals": proposals}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_handlers() -> None:
    """Register all sync handlers into ``jobs.HANDLERS``."""
    jobs.HANDLERS["push"] = handle_push
    jobs.HANDLERS["verify"] = handle_verify
    jobs.HANDLERS["generate_preset"] = handle_generate_preset
    jobs.HANDLERS["temp_sync"] = handle_temp_sync
    jobs.HANDLERS["import_scan"] = handle_import_scan


# Auto-register on import so including the sync router wires handlers.
register_handlers()
