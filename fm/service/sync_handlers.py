# fm/service/sync_handlers.py
"""Sync orchestration handlers — registered into ``jobs.HANDLERS``.

Each handler receives ``(session, config, payload)`` and returns a result dict
that becomes the job's ``result`` on success.  Handlers are the integration
layer wiring the lifted cores (dbcodec, transport, pipeline) into the job
queue.

Handlers:
  * ``push`` — rebuild printer DB from cache + catalog, ``run_push``, update
    ``SyncTarget(printer_db)``, enqueue ``verify``.  Busy printer → returns
    ``{"_awaiting_input": True, ...}`` sentinel so ``run_pending`` parks the
    job; user resolves via ``POST /api/jobs/{id}/resolve``.
  * ``verify`` — ``scp_pull`` fresh DB, floor-check + presence + hash compare,
    update ``SyncTarget.status`` (``in_sync`` / ``error``).  Cloud-sync
    rollback (version < floor) → ``error`` + activity warn.
  * ``github_backup`` — pull all Orca Cloud presets, write each as a JSON
    file plus an index.json, commit atomically to GitHub.

Transport / cache DB / Orca Cloud client are injected via module-level
factories so tests can substitute ``FakeTransport`` and mock clients without
touching real SSH or the Orca Cloud API.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Config
from ..models import Profile, SyncTarget
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
from ..presets.flattener import flatten_preset
from ..orca_cloud import OrcaCloudClient
from ..github_backup import GitHubBackupClient
from ..settings_store import load_github_config, load_orca_token, save_orca_token
from ..errors import FmError
from . import jobs

log = __import__("logging").getLogger(__name__)

# ---------------------------------------------------------------------------
# Injection points — tests override via monkeypatch.
# ---------------------------------------------------------------------------

_transport_factory: Callable[[Config], PrinterTransport] = lambda cfg: SshTransport(cfg)
_cache_db_path: Path | None = None


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


def _default_github_client() -> GitHubBackupClient | None:
    """Create a GitHubBackupClient from stored settings, or None if unconfigured."""
    config = load_github_config()
    if config is None:
        return None
    token, repo, branch = config
    return GitHubBackupClient(token, repo, branch)


# GitHub client factory — tests override via monkeypatch.
_github_client_factory: Callable[[], GitHubBackupClient | None] = _default_github_client


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


# ---------------------------------------------------------------------------
# Cloud preset → printer-DB entry values adapter
# ---------------------------------------------------------------------------

def _cloud_preset_to_entry_values(preset: dict, entry_id: str) -> dict:
    """Map an Orca Cloud / flattened preset dict to dbcodec build_entry values convention."""
    def _unwrap(v, default=""):
        if isinstance(v, list):
            v = v[0] if v else default
        if v is None:
            return default
        s = str(v).strip()
        if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
            s = s[1:-1].strip()
        return s

    def _to_int(v, default=0):
        s = _unwrap(v)
        try:
            return int(float(s))
        except (ValueError, TypeError):
            return default

    def _to_float(v, default=None):
        s = _unwrap(v)
        try:
            return float(s)
        except (ValueError, TypeError):
            return default

    name = _unwrap(preset.get("name") or preset.get("filament_settings_id") or "Custom Filament")
    brand = _unwrap(preset.get("filament_vendor") or "Generic")
    ftype = _unwrap(preset.get("filament_type") or "PLA")

    min_temp = _to_int(preset.get("nozzle_temperature_range_low"), 200)
    max_temp = _to_int(preset.get("nozzle_temperature_range_high"), 230)
    if min_temp >= max_temp:
        max_temp = min_temp + 20

    values = {
        "id": str(entry_id),
        "brand": brand,
        "name": name,
        "type": ftype,
        "minTemp": min_temp,
        "maxTemp": max_temp,
        "dryingTemp": 0,
        "dryingTime": 0,
    }

    nozzle_temp = _to_int(preset.get("nozzle_temperature"), None)
    if nozzle_temp is not None:
        values["nozzle_temperature"] = nozzle_temp

    density = _to_float(preset.get("filament_density"))
    if density is not None:
        values["density"] = density

    flow_ratio = _to_float(preset.get("filament_flow_ratio"))
    if flow_ratio is not None:
        values["flowRatio"] = flow_ratio

    pa = _to_float(preset.get("pressure_advance"))
    if pa is not None:
        values["pa"] = pa

    max_vol = _to_float(preset.get("filament_max_volumetric_speed"))
    if max_vol is not None:
        values["maxVolumetric"] = max_vol

    return values


def _is_stock_rfid_preset(preset: dict) -> bool:
    """Check if a cloud preset dict has a stock-RFID brand (e.g. Creality)."""
    vendor = preset.get("filament_vendor")
    if isinstance(vendor, list):
        vendor = vendor[0] if vendor else ""
    return (str(vendor or "").strip().lower()) in STOCK_RFID_BRANDS


def _flatten_cloud_preset(preset: dict) -> dict:
    """Flatten a cloud preset dict via ``flatten_preset`` (temp-file round-trip).

    Writes the preset to a temporary JSON file so ``flatten_preset`` can
    resolve ``inherits`` against bundled system templates, then returns the
    flattened dict.
    """
    with tempfile.NamedTemporaryFile(
        suffix=".json", delete=False, mode="w"
    ) as f:
        json.dump(preset, f)
        path = Path(f.name)
    try:
        return flatten_preset(path)
    finally:
        path.unlink(missing_ok=True)

# Brands whose spool RFID tags already match stock 01xxx printer DB
# entries (tag carries "1" + stock entry ID — direct ID lookup, no
# name matching).  A 99xxx duplicate would never match a tag.
STOCK_RFID_BRANDS = {"creality"}


def _is_stock_rfid(profile: Profile) -> bool:
    return (profile.brand or "").strip().lower() in STOCK_RFID_BRANDS


def _entry_hash_fields(entry: dict) -> dict:
    """Extract the canonical-hash input dict from a printer-DB entry."""
    b = entry["base"]
    kv = entry.get("kvParam", {})
    return {
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
    }


def _entry_hash(entry: dict) -> str:
    """Canonical hash of an entry's identity + parameter fields."""
    return canonical_hash(_entry_hash_fields(entry))


_HASH_FIELD_LABELS = {
    "brand": "Brand", "name": "Name", "type": "Type",
    "minTemp": "Min temp", "maxTemp": "Max temp",
    "nozzle_temperature": "Nozzle temp", "density": "Density",
    "dryingTemp": "Drying temp", "dryingTime": "Drying time",
    "pa": "Pressure advance", "flow_ratio": "Flow ratio",
    "max_volumetric": "Max volumetric speed",
}


def _values_differ(a: object, e: object) -> bool:
    """True when ``a`` and ``e`` differ, normalizing numeric coercion.

    Float ORM columns can stringify differently than the printer DB
    (e.g. ``21`` vs ``21.0``); parse both as float when possible so
    numerically-equal values are not reported as drift.
    """
    try:
        return float(a) != float(e)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return a != e


def _entry_drift_detail(actual: dict, expected: dict) -> str:
    """Human-readable diff of hash fields, '' when equal. id always equal."""
    a, e = _entry_hash_fields(actual), _entry_hash_fields(expected)
    return "; ".join(
        f"{_HASH_FIELD_LABELS[k]}: {a[k]} → {e[k]}"
        for k in e if k in _HASH_FIELD_LABELS and _values_differ(a.get(k), e.get(k))
    )

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


def load_printer_db_for_drift(config: Config | None) -> dict | None:
    """Fresh printer DB for drift checks; cache file as fallback.

    Returns None when config is None, the pull fails, and no cache file
    exists — callers then keep the SyncTarget-based status.
    """
    if config is None:
        return None
    path = _cache_db_path if _cache_db_path is not None else _resolve_cache_path()
    transport = _get_transport(config)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = Path(f.name)
    try:
        transport.scp_pull(tmp)
        db = load_db(tmp)
    except (FmError, ValueError, OSError):
        if path is not None and Path(path).exists():
            return load_db(path)
        return None
    else:
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            save_db(path, db)
        return db
    finally:
        tmp.unlink(missing_ok=True)


def compute_printer_db_drift(session: Session, config: Config | None) -> dict[int, dict] | None:
    """Live printer_db drift per profile id: {"status", "detail"}.

    Statuses: stock | new | in_sync | modified | missing.
    Returns None when no printer DB is available (live or cache).
    """
    db = load_printer_db_for_drift(config)
    if db is None:
        return None
    drift: dict[int, dict] = {}
    for profile in session.scalars(select(Profile).order_by(Profile.id)):
        if _is_stock_rfid(profile):
            drift[profile.id] = {"status": "stock", "detail": ""}
        elif not profile.printer_db_id:
            drift[profile.id] = {"status": "new", "detail": ""}
        else:
            entry = find_entry(db, profile.printer_db_id)
            if entry is None:
                drift[profile.id] = {
                    "status": "missing",
                    "detail": f"entry {profile.printer_db_id} not on printer",
                }
            else:
                expected = build_entry(
                    db, _profile_to_values(profile, profile.printer_db_id)
                )
                detail = _entry_drift_detail(entry, expected)
                drift[profile.id] = {
                    "status": "in_sync" if not detail else "modified",
                    "detail": detail,
                }
    return drift


# ---------------------------------------------------------------------------
# DB rebuild
# ---------------------------------------------------------------------------
def _rebuild_printer_db(
    session: Session, config: Config, transport: PrinterTransport,
    profile_ids: list[int] | None = None,
    cloud_resource_ids: list[str] | None = None,
) -> tuple[dict, list[tuple[Any, dict]], list[int]]:
    """Rebuild the local printer DB from live cloud presets or catalog fallback.

    **Primary path** (Orca Cloud configured): pull filament presets via
    ``sync_pull``, filter by ``cloud_resource_ids`` if provided, skip stock
    Creality presets, flatten inherited presets, and build entries via
    ``build_entry``.  ``printer_db_id`` is mapped via existing ``SyncTarget``
    rows (``cloud_id == res_id``) or allocated as ``next_free_id``.

    **Fallback path** (no Orca Cloud client): rebuild from cache DB + catalog
    ``Profile`` rows, filtered by ``profile_ids`` if provided.  Stock-RFID
    profiles (e.g. Creality) are excluded and reported as skipped IDs.

    **Orphan Rule F3/B**: existing custom 99xxx entries missing from the
    cloud / catalog are *never* removed here — they persist on the printer
    until explicitly deleted via a separate action.

    Returns ``(db, pushed, skipped_ids)`` where ``pushed`` is a list of
    ``(SyncTarget | Profile | None, entry)`` tuples and ``skipped_ids`` is a
    list of profile IDs that were not pushed (fallback path only).
    """
    db = _load_cache_db(config, transport)
    client = _orca_client_factory()

    if client is not None:
        db, pushed, skipped = _rebuild_from_cloud(
            session, config, db, client, cloud_resource_ids,
        )
        if pushed:
            return db, pushed, skipped
        # Cloud returned no presets → fall back to cache + catalog targets.
    return _rebuild_from_catalog(session, config, db, profile_ids)


def _rebuild_from_cloud(
    session: Session, config: Config, db: dict,
    client: OrcaCloudClient,
    cloud_resource_ids: list[str] | None = None,
) -> tuple[dict, list[tuple[Any, dict]], list[int]]:
    """Cloud-first rebuild: source entries from live Orca Cloud presets."""
    pulled = client.sync_pull(cursor=0)
    presets = _cloud_presets_to_local(pulled.get("upserts", []))

    if cloud_resource_ids:
        id_set = set(cloud_resource_ids)
        presets = [p for p in presets if p.get("_path") in id_set]

    pushed: list[tuple[Any, dict]] = []
    skipped_ids: list[int] = []

    for preset in presets:
        if _is_stock_rfid_preset(preset):
            continue

        res_id = preset.get("_path", "")

        # Map printer_db_id via SyncTarget where cloud_id == res_id
        target = session.scalars(
            select(SyncTarget).where(
                SyncTarget.cloud_id == res_id,
                SyncTarget.kind == "printer_db",
            )
        ).first()

        if target is not None and target.printer_db_id:
            entry_id = target.printer_db_id
            existing = find_entry(db, entry_id)
            if existing is not None:
                remove_entry(db, entry_id)
        else:
            entry_id = str(next_free_id(db, config.id_range_start))
            if target is not None:
                target.printer_db_id = entry_id
            else:
                # No existing SyncTarget — create one so verify can find it.
                target = SyncTarget(
                    profile_id=None,
                    kind="printer_db",
                    cloud_id=res_id,
                    printer_db_id=entry_id,
                    status="pending",
                )
                session.add(target)
                session.flush()

        flat = _flatten_cloud_preset(preset)
        values = _cloud_preset_to_entry_values(flat, entry_id)
        entry = build_entry(db, values)
        insert_entry(db, entry)
        pushed.append((target, entry))

    # Orphan Rule F3/B: do NOT remove custom 99xxx entries missing from cloud.
    return db, pushed, skipped_ids


def _rebuild_from_catalog(
    session: Session, config: Config, db: dict,
    profile_ids: list[int] | None = None,
) -> tuple[dict, list[tuple[Any, dict]], list[int]]:
    """Fallback rebuild: source entries from cache DB + catalog Profile rows.

    Used when no Orca Cloud client is configured.  Preserves the pre-rework
    behavior minus orphan removal (Orphan Rule F3/B).
    """
    if profile_ids:
        all_profiles = list(session.scalars(
            select(Profile).where(Profile.id.in_(profile_ids))
            .order_by(Profile.id)
        ))
    else:
        all_profiles = list(session.scalars(select(Profile).order_by(Profile.id)))

    profiles = [p for p in all_profiles if not _is_stock_rfid(p)]
    skipped_ids = [p.id for p in all_profiles if _is_stock_rfid(p)]

    pushed: list[tuple[Any, dict]] = []
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

    # Orphan Rule F3/B: do NOT remove orphaned custom 99xxx entries.
    return db, pushed, skipped_ids


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def handle_push(session: Session, config: Config, payload: dict) -> dict:
    """Push handler: rebuild printer DB, ``run_push``, update SyncTargets,
    enqueue verify.  Busy → ``_awaiting_input`` sentinel.

    ``payload`` may contain ``cloud_resource_ids`` (list of Orca Cloud
    resource UUIDs) to filter which cloud presets are pushed, or
    ``profile_ids`` for the catalog fallback path.
    """
    profile_ids = payload.get("profile_ids")
    cloud_resource_ids = payload.get("cloud_resource_ids")
    force_reboot = payload.get("force_reboot", False)
    no_reboot = payload.get("no_reboot", False)

    transport = _get_transport(config)
    db, pushed, skipped = _rebuild_printer_db(
        session, config, transport, profile_ids,
        cloud_resource_ids=cloud_resource_ids,
    )

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

    # Success — update SyncTargets: set expected_hash, printer_db_id, status
    for item, entry in pushed:
        if isinstance(item, Profile):
            target = _get_or_create_target(session, item.id, "printer_db")
            target.printer_db_id = item.printer_db_id
        elif isinstance(item, SyncTarget):
            target = item
        else:
            continue
        target.expected_hash = _entry_hash(entry)
        target.status = "pending"  # verify will set in_sync
        target.last_synced_at = _now()
    session.flush()

    # Enqueue verify
    jobs.enqueue(session, "verify", {})

    return {"status": result["status"], "pushed": len(pushed), "skipped": skipped}


def handle_verify(session: Session, config: Config, payload: dict) -> dict:
    """Verify handler: pull fresh DB, floor-check + presence + hash compare,
    update SyncTarget.status.  Cloud-sync rollback → error + warn.

    Verifies entries against ``SyncTarget`` rows where ``kind == "printer_db"``
    and ``printer_db_id`` is set (cloud-first and catalog fallback alike).
    """
    transport = _get_transport(config)

    targets = list(session.scalars(
        select(SyncTarget).where(
            SyncTarget.kind == "printer_db",
            SyncTarget.printer_db_id.isnot(None),
        )
        .order_by(SyncTarget.id)
    ))
    expected_ids = [t.printer_db_id for t in targets]

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

    # Presence + hash check per SyncTarget
    ids = {e.get("base", {}).get("id")
           for e in db.get("result", {}).get("list", [])}

    for target in targets:
        if cloud_rollback:
            target.status = "error"
        elif target.printer_db_id not in ids:
            target.status = "error"
        else:
            entry = find_entry(db, target.printer_db_id)
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




# ---------------------------------------------------------------------------
# GitHub backup
# ---------------------------------------------------------------------------

_UNSAFE_CHARS = re.compile(r'[/\\:*?"<>|+ ]')


def _sanitize_filename(name: str) -> str:
    """Replace filesystem-unsafe characters with '-'."""
    return _UNSAFE_CHARS.sub('-', name)


def _enqueue_github_backup(session: Session) -> bool:
    """Enqueue a github_backup job if GitHub is configured. No-op otherwise.

    Returns True when a job was enqueued, False when GitHub is unconfigured.
    """
    if _github_client_factory() is None:
        return False
    jobs.enqueue(session, "github_backup", {})
    return True


def handle_github_backup(session: Session, config: Config, payload: dict) -> dict:
    """GitHub backup handler: pull all Orca Cloud presets, write each as a
    JSON file plus an index.json, commit atomically to GitHub.
    """
    client = _github_client_factory()
    if client is None:
        raise ValueError(
            "GitHub not configured — set up the connection in Settings"
        )

    orca = _orca_client_factory()
    if orca is None:
        raise ValueError(
            "Orca Cloud not connected — set up the connection in Settings"
        )

    # Full pull from Orca Cloud (cursor=0 → all presets)
    pull_result = orca.sync_pull(cursor=0)
    upserts = pull_result.get("upserts", [])
    presets = _cloud_presets_to_local(upserts)

    # Build file dict: presets/{safe_name}.json + index.json
    files: dict[str, str] = {}
    index: list[dict] = []
    seen_names: dict[str, int] = {}

    for preset in presets:
        raw_name = preset.get("name", "")
        if not raw_name:
            continue  # skip empty names
        safe = _sanitize_filename(raw_name)
        if safe in seen_names:
            suffix = seen_names[safe]
            seen_names[safe] += 1
            safe = f"{safe}_{suffix}"
        else:
            seen_names[safe] = 0

        path = f"presets/{safe}.json"
        files[path] = json.dumps(preset, indent=2, sort_keys=True)
        index.append({
            "name": raw_name,
            "filament_id": preset.get("filament_id", ""),
            "cloud_id": preset.get("_path", ""),
            "updated_time": preset.get("updated_time", ""),
            "file": path,
        })

    index.sort(key=lambda e: e["name"])
    files["index.json"] = json.dumps(index, indent=2, sort_keys=True)

    message = f"Backup: {_now().isoformat()} — {len(presets)} presets"
    result = client.backup(files, message)

    jobs.log_activity(
        session, "github_backup", "ok",
        {
            "commit_sha": result["commit_sha"],
            "files_written": result["files_written"],
            "preset_count": len(presets),
        },
    )

    return {
        "commit_sha": result["commit_sha"],
        "files_written": result["files_written"],
        "preset_count": len(presets),
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_handlers() -> None:
    """Register all sync handlers into ``jobs.HANDLERS``."""
    jobs.HANDLERS["push"] = handle_push
    jobs.HANDLERS["verify"] = handle_verify
    jobs.HANDLERS["github_backup"] = handle_github_backup

# Auto-register on import so including the sync router wires handlers.
register_handlers()
