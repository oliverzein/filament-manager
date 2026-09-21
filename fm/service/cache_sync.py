"""cache_sync.py — Pulls Orca Cloud presets and updates the read-only profile_cache."""
import logging
from sqlalchemy import select
from sqlalchemy.orm import Session

from fm.models import ProfileCache, Profile, Spool, SyncTarget, _now
from fm.orca_cloud import OrcaCloudClient, OrcaCloudError
from fm.presets.flattener import flatten_preset, find_preset_by_name

logger = logging.getLogger(__name__)


def _unwrap(val):
    if isinstance(val, list):
        return val[0] if val else ""
    return str(val) if val is not None else ""


def sync_profile_cache(session: Session, client: OrcaCloudClient) -> dict:
    """Pull presets from Orca Cloud and upsert into profile_cache.

    Soft-deletes presets present in the deletes tombstone feed.
    Returns summary dict {synced, deleted, offline}.
    """
    try:
        pull_result = client.sync_pull(cursor=0)
    except (OrcaCloudError, Exception) as exc:
        logger.warning("Orca Cloud sync pull failed: %s", exc)
        return {"synced": 0, "deleted": 0, "offline": True, "error": str(exc)}

    upserts = pull_result.get("upserts", [])
    # A resource id reported in both feeds means the cloud deleted and
    # re-created it within one sync window — the upsert wins, otherwise the
    # row would be tombstoned immediately after being (re)created.
    upsert_ids = {item.get("id") for item in upserts if item.get("id")}
    deletes = set(pull_result.get("deletes", [])) - upsert_ids
    now = _now()

    synced_count = 0
    for item in upserts:
        res_id = item.get("id")
        if not res_id:
            continue
        content = item.get("content", {})

        # Orca Cloud sync API returns filament, print, and printer presets.
        # Filament Manager only caches and manages filament presets.
        content_type = content.get("type") or item.get("type")
        if content_type != "filament":
            if content_type in ("print", "printer") or (content_type is not None and content_type != "filament"):
                stale = session.get(ProfileCache, res_id)
                if stale is not None:
                    session.delete(stale)
                continue
            if content_type is None and not (content.get("filament_type") or content.get("filament_vendor") or content.get("inherits")):
                stale = session.get(ProfileCache, res_id)
                if stale is not None:
                    session.delete(stale)
                continue

        name = item.get("name") or content.get("name") or "Unnamed Preset"
        inherits = _unwrap(content.get("inherits", "")).strip()
        is_inherited = bool(inherits)
        vendor = _unwrap(content.get("filament_vendor", ""))
        ftype = _unwrap(content.get("filament_type", ""))
        fid = _unwrap(content.get("filament_id", "")) or None

        # Resolve inheritance for vendor and type if missing
        if is_inherited and (not vendor or not ftype):
            template_path = find_preset_by_name(inherits)
            if template_path:
                flat = flatten_preset(template_path)
                if not vendor:
                    vendor = _unwrap(flat.get("filament_vendor", ""))
                if not ftype:
                    ftype = _unwrap(flat.get("filament_type", ""))
                if not fid:
                    fid = _unwrap(flat.get("filament_id", "")) or None

        cache_entry = session.get(ProfileCache, res_id)
        if cache_entry is None:
            cache_entry = ProfileCache(
                cloud_resource_id=res_id,
                name=name,
                vendor=vendor or "Generic",
                type=ftype or "PLA",
                filament_id=fid,
                is_inherited=is_inherited,
                deleted_from_cloud=False,
                deleted_at=None,
                last_seen_at=now,
            )
            session.add(cache_entry)
        else:
            cache_entry.name = name
            cache_entry.vendor = vendor or cache_entry.vendor
            cache_entry.type = ftype or cache_entry.type
            cache_entry.filament_id = fid or cache_entry.filament_id
            cache_entry.is_inherited = is_inherited
            cache_entry.deleted_from_cloud = False
            cache_entry.deleted_at = None
            cache_entry.last_seen_at = now

        # Reconcile SyncTarget and Spool if preset was re-uploaded with a new cloud_resource_id:
        target = session.scalars(
            select(SyncTarget).where(
                SyncTarget.cloud_id == res_id,
                SyncTarget.kind == "printer_db",
            )
        ).first()
        if target is None:
            reconciled = None
            if deletes:
                reconciled = session.scalars(
                    select(SyncTarget).where(
                        SyncTarget.kind == "printer_db",
                        SyncTarget.cloud_id.in_(deletes),
                    )
                ).first()
            if reconciled is None:
                for st in session.scalars(select(SyncTarget).where(SyncTarget.kind == "printer_db")):
                    if st.profile_id:
                        prof = session.get(Profile, st.profile_id)
                        if prof and prof.name.strip().lower() == name.strip().lower():
                            reconciled = st
                            break
            if reconciled is not None:
                old_cloud_id = reconciled.cloud_id
                reconciled.cloud_id = res_id
                if old_cloud_id:
                    for sp in session.scalars(select(Spool).where(Spool.cloud_resource_id == old_cloud_id)):
                        sp.cloud_resource_id = res_id
        synced_count += 1

    # Process soft deletes
    deleted_count = 0
    for del_id in deletes:
        entry = session.get(ProfileCache, del_id)
        if entry and not entry.deleted_from_cloud:
            entry.deleted_from_cloud = True
            entry.deleted_at = now
            deleted_count += 1

    session.commit()
    return {"synced": synced_count, "deleted": deleted_count, "offline": False}
