# fm/printer/dbcodec.py
"""Printer DB codec — load/save/mutate the Creality K2 material_database.json.

Lifted verbatim from skill/cfs.py (DB + Validation sections); each die(code,msg)
replaced with raise FmError(code,msg).
"""
import json
import copy as _copy
from pathlib import Path

from fm.errors import FmError

EXIT_DB = 3

MIN_DB_ENTRIES = 30  # Stock DB has ~40 entries; anything below is suspicious


# === DB Section ===

def load_db(path):
    p = Path(path)
    if not p.exists():
        raise FmError(EXIT_DB, f"DB file not found: {p}")
    try:
        with open(p) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise FmError(EXIT_DB, f"DB not parseable: {e}. Restore from backup.")


def save_db(path, db):
    with open(path, "w") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)


def find_entry(db, entry_id):
    for e in db["result"]["list"]:
        if e.get("base", {}).get("id") == entry_id:
            return e
    return None


def find_custom_entries(db):
    return [e for e in db["result"]["list"]
            if e.get("base", {}).get("id", "").startswith("99")]


def next_free_id(db, start=99001):
    used = {int(e["base"]["id"]) for e in db["result"]["list"]
            if e.get("base", {}).get("id", "").isdigit()}
    candidate = start
    while candidate in used:
        candidate += 1
    return candidate


def _is_custom_id(entry_id):
    return entry_id.startswith("99")


def insert_entry(db, entry):
    entry_id = entry["base"]["id"]
    if find_entry(db, entry_id) is not None:
        raise FmError(EXIT_DB, f"ID collision: {entry_id} already exists")
    db["result"]["list"].append(entry)
    count_autofix(db)


def patch_entry(db, entry_id, changes):
    if not _is_custom_id(entry_id):
        raise FmError(EXIT_DB, f"Stock entries protected (not 99xxx): {entry_id}")
    e = find_entry(db, entry_id)
    if e is None:
        raise FmError(EXIT_DB, f"Entry not found: {entry_id}")
    for section, fields in changes.items():
        if section not in e:
            e[section] = {}
        e[section].update(fields)
    count_autofix(db)


def remove_entry(db, entry_id):
    if not _is_custom_id(entry_id):
        raise FmError(EXIT_DB, f"Stock entries protected (not 99xxx): {entry_id}")
    e = find_entry(db, entry_id)
    if e is None:
        raise FmError(EXIT_DB, f"Entry not found: {entry_id}")
    db["result"]["list"].remove(e)
    count_autofix(db)


def bump_version(db, version=9876543210):
    """Bump DB version. Auto-increment if current >= override (ensures CFS RFID app sees update).
    App compares newVer > storedVer, so constant version = 'no update available'."""
    try:
        current = int(db.get("result", {}).get("version", 0))
    except (ValueError, TypeError):
        current = 0
    if current >= version:
        new_ver = current + 1
    else:
        new_ver = version
    db["result"]["version"] = str(new_ver)
    return new_ver


def count_autofix(db):
    db["result"]["count"] = len(db["result"]["list"])


TEMPLATE_ID = "01001"  # Hyper PLA


def build_entry(db, values):
    template = find_entry(db, TEMPLATE_ID)
    if template is None:
        # fallback: first PLA entry
        template = next((e for e in db["result"]["list"]
                         if e.get("base", {}).get("meterialType") == "PLA"), None)
        if template is None:
            raise FmError(EXIT_DB, f"Template entry {TEMPLATE_ID} missing and no PLA fallback found")
    entry = _copy.deepcopy(template)
    b = entry["base"]
    b["id"] = str(values["id"])
    b["brand"] = values["brand"]
    b["name"] = values["name"]
    b["meterialType"] = values["type"]
    if "density" in values:
        b["density"] = values["density"]
    b["minTemp"] = values["minTemp"]
    b["maxTemp"] = values["maxTemp"]
    b["dryingTemp"] = values.get("dryingTemp", 0)
    b["dryingTime"] = values.get("dryingTime", 0)
    if "color" in values:
        b["colors"] = [values["color"]]
    kv = entry["kvParam"]
    kv["nozzle_temperature"] = str(values.get("nozzle_temperature", values["maxTemp"]))
    kv["nozzle_temperature_range_high"] = str(values["maxTemp"])
    kv["nozzle_temperature_range_low"] = str(values["minTemp"])
    kv["filament_type"] = values["type"]
    kv["filament_vendor"] = values["brand"]
    if "density" in values:
        kv["filament_density"] = str(values["density"])
    if "flowRatio" in values:
        kv["filament_flow_ratio"] = str(values["flowRatio"])
    if "pa" in values:
        kv["pressure_advance"] = str(values["pa"])
    if "maxVolumetric" in values:
        kv["filament_max_volumetric_speed"] = str(values["maxVolumetric"])
    return entry


# === Validation Section ===

KNOWN_TYPES = {"PLA", "PETG", "ABS", "TPU", "PA", "PC", "ASA", "PVA", "HIPS", "PETG-CF", "PLA-CF"}
REQUIRED_FIELDS = ["brand", "name", "type", "minTemp", "maxTemp"]


def validate_entry(values):
    errors = []
    warnings = []
    for f in REQUIRED_FIELDS:
        if f not in values or values[f] in (None, "", []):
            errors.append(f"Required field missing: {f}")
    if errors:
        return errors, warnings
    min_t = values["minTemp"]
    max_t = values["maxTemp"]
    if not (isinstance(min_t, (int, float)) and isinstance(max_t, (int, float))):
        errors.append("minTemp/maxTemp must be numeric")
        return errors, warnings
    if min_t >= max_t:
        errors.append(f"minTemp ({min_t}) must be < maxTemp ({max_t})")
    if not (100 <= min_t <= 400) or not (100 <= max_t <= 400):
        errors.append(f"Temp outside 100-400°C (min={min_t}, max={max_t})")
    density = values.get("density")
    if density is not None and not (0.9 <= density <= 1.6):
        errors.append(f"density outside 0.9-1.6: {density}")
    drying_temp = values.get("dryingTemp")
    if drying_temp is not None and not (0 <= drying_temp <= 100):
        errors.append(f"dryingTemp outside 0-100°C: {drying_temp}")
    drying_time = values.get("dryingTime")
    if drying_time is not None and not (0 <= drying_time <= 24):
        errors.append(f"dryingTime outside 0-24h: {drying_time}")
    # warnings
    if values["brand"].lower() not in values["name"].lower():
        warnings.append(f"name '{values['name']}' does not contain vendor '{values['brand']}' — OrcaSlicer tie risk")
    if values["type"] not in KNOWN_TYPES:
        warnings.append(f"Unknown type '{values['type']}' — OrcaSlicer match may fail")
    return errors, warnings
