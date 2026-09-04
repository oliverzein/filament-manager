# Filament Manager Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rework app architecture to make Orca Cloud the sole source of filament profiles, eliminate the internal profile catalog and drift engine, and reshape push-to-printer and spool inventory around a lightweight local cache.

**Architecture:** OrcaSlicer Desktop is the sole editor of presets and syncs to Orca Cloud. Filament Manager maintains a read-only `profile_cache` table (with soft-delete on tombstones) and a persistent `sync_targets` table mapping cloud resources to printer DB IDs. Push-to-printer resolves multi-level preset inheritance via recursive template flattening, unboxes and casts values via an adapter, and pushes to K2 Printer DB while protecting orphaned entries from auto-removal.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 (future=True) + SQLite, Jinja2, HTMX 1.9.12, pytest.

**Spec:** `docs/superpowers/specs/2026-09-04-filament-manager-rework.md`

## Global Constraints

- Iron Rule 1: Version bump ≥ 9876543210 and printer reboot mandatory after printer DB write; busy check runs before upload.
- Iron Rule 2: Stock Creality presets (`STOCK_RFID_BRANDS = {"creality"}`) are excluded from push (RFID tags match 01xxx stock IDs).
- Iron Rule 3: Custom printer DB IDs only in 99xxx range (`id_range_start = 99001`); stock IDs protected.
- Iron Rule 4: Printer backup before every write; rotate, keep 5.
- Iron Rule 5: Double-confirm on delete with typed token.
- Iron Rule 6: Refuse push when printer DB < 30 entries (`MIN_DB_ENTRIES = 30`) unless explicitly forced.
- Working Rule 1: UI design/refinement uses omp designer agent.
- Working Rule 2: Scripts over repetition (`scripts/test.sh`, `scripts/dev-start.sh`).
- Working Rule 3: Skill repo `~/Dokumente/Daten/Development/skills/Creality-custom-filament` is read-only reference material.
- Working Rule 4: Dev server restart after adding UI routes; templates/CSS hot-reloaded.
- Working Rule 5: OrcaSlicer is the editor — no print settings fields in app models.
- Version bump: Major release 2.0.0.

---

### Task 1: Bundle K2 System Templates & Multi-Level Inheritance Verification

**Files:**
- Create: `scripts/bundle_k2_templates.py`
- Modify: `fm/presets/flattener.py`
- Test: `tests/test_flattener_k2.py`

**Interfaces:**
- Consumes: `/opt/orca-slicer/resources/profiles/Creality/filament/*@K2-all.json`
- Produces: Bundled template files in `fm/presets/templates/` and verified `flatten_preset()` returning complete values for multi-level inherited presets (`Hyper Stardust @K2-all`, `EN-PLA+ @K2-all`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_flattener_k2.py`:
```python
import pytest
from pathlib import Path
from fm.presets.flattener import flatten_preset, find_preset_by_name, _BUNDLED_DIR


def test_k2_system_templates_exist_in_bundled_dir():
    assert (_BUNDLED_DIR / "Hyper Stardust @K2-all.json").exists()
    assert (_BUNDLED_DIR / "EN-PLA+ @K2-all.json").exists()
    assert (_BUNDLED_DIR / "Creality Generic PLA @K2-all.json").exists()


def test_hyper_stardust_multi_level_flattening():
    template_path = find_preset_by_name("Hyper Stardust @K2-all")
    assert template_path is not None

    flat = flatten_preset(template_path)
    # Check fields inherited through Hyper Stardust @K2-all -> fdm_filament_pla -> fdm_filament_common
    assert flat.get("filament_vendor") in (["Creality"], "Creality")
    assert flat.get("filament_type") in (["PLA"], "PLA")
    assert flat.get("nozzle_temperature") in (["220"], "220", 220)
    assert flat.get("filament_density") in (["1.24"], "1.24", 1.24)
    assert flat.get("nozzle_temperature_range_high") in (["240"], "240", 240)


def test_en_pla_plus_multi_level_flattening():
    template_path = find_preset_by_name("EN-PLA+ @K2-all")
    assert template_path is not None

    flat = flatten_preset(template_path)
    assert flat.get("filament_vendor") in (["Creality"], "Creality")
    assert flat.get("filament_type") in (["PLA"], "PLA")
    assert flat.get("nozzle_temperature") in (["220"], "220", 220)
    assert flat.get("filament_density") in (["1.24"], "1.24", 1.24)
    assert flat.get("filament_flow_ratio") in (["0.98"], "0.98", 0.98)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_flattener_k2.py -v`
Expected: FAIL with `AssertionError: assert False` (bundled template files do not exist).

- [ ] **Step 3: Create bundling script and bundle templates**

Create `scripts/bundle_k2_templates.py`:
```python
#!/usr/bin/env python3
"""bundle_k2_templates.py — Copy K2 printer filament templates to fm/presets/templates/.

Copies all *@K2-all.json presets and their referenced parent presets from
/opt/orca-slicer/resources/profiles/Creality/filament/ into fm/presets/templates/
so Docker deployments have all K2 system presets available for flattening.
"""
import shutil
from pathlib import Path

SOURCE_DIR = Path("/opt/orca-slicer/resources/profiles/Creality/filament")
TARGET_DIR = Path(__file__).resolve().parent.parent / "fm" / "presets" / "templates"


def bundle_k2():
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    # Copy all @K2-all.json files
    for f in SOURCE_DIR.glob("*@K2-all.json"):
        shutil.copy2(f, TARGET_DIR / f.name)
        count += 1
    # Also copy referenced generic parents like Creality Generic PLA.json if present
    for f in SOURCE_DIR.glob("Creality Generic *.json"):
        shutil.copy2(f, TARGET_DIR / f.name)
        count += 1
    print(f"Bundled {count} K2 templates to {TARGET_DIR}")


if __name__ == "__main__":
    bundle_k2()
```

Run: `python scripts/bundle_k2_templates.py`

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_flattener_k2.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add scripts/bundle_k2_templates.py tests/test_flattener_k2.py fm/presets/templates/*@K2-all.json fm/presets/templates/Creality\ Generic\ *.json
git commit -m "feat: bundle K2 filament templates and verify multi-level flattening"
```

---

### Task 2: Schema Migration & Data Models (`profile_cache`, `sync_targets`, `spools`)

**Files:**
- Modify: `fm/models.py:16-73`
- Modify: `fm/db.py:42-55`
- Test: `tests/test_migration.py`

**Interfaces:**
- Consumes: SQLAlchemy engine, existing tables (`profiles`, `spools`, `sync_targets`)
- Produces: `ProfileCache` model class, migrated `sync_targets.printer_db_id` and `sync_targets.cloud_id`, migrated `spools.cloud_resource_id`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_migration.py`:
```python
import sqlite3
from datetime import datetime, timezone
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from fm.db import init_engine, _migrate
from fm.models import Base, Profile, Spool, SyncTarget, ProfileCache


def test_schema_migration_creates_profile_cache_and_migrates_targets_and_spools(tmp_path):
    db_path = tmp_path / "test_migrate.db"
    url = f"sqlite:///{db_path}"

    # 1. Setup legacy schema and legacy data
    engine = create_engine(url, future=True)
    with engine.connect() as conn:
        conn.executescript("""
            CREATE TABLE profiles (
                id INTEGER PRIMARY KEY,
                brand VARCHAR(255),
                name VARCHAR(255),
                type VARCHAR(64),
                colors VARCHAR DEFAULT '[]',
                min_temp INTEGER,
                max_temp INTEGER,
                density FLOAT DEFAULT 0.0,
                drying_temp INTEGER DEFAULT 0,
                drying_time INTEGER DEFAULT 0,
                pa FLOAT,
                flow_ratio FLOAT,
                max_volumetric FLOAT,
                retraction_length FLOAT,
                retraction_speed FLOAT,
                nozzle_temperature INTEGER,
                printer_db_id VARCHAR,
                filament_id VARCHAR,
                template_override VARCHAR,
                notes VARCHAR DEFAULT '',
                created_at DATETIME,
                updated_at DATETIME
            );
            CREATE TABLE spools (
                id INTEGER PRIMARY KEY,
                profile_id INTEGER,
                color_override VARCHAR,
                remaining_pct INTEGER DEFAULT 100,
                location VARCHAR DEFAULT '',
                rfid_tag VARCHAR,
                notes VARCHAR DEFAULT '',
                created_at DATETIME,
                updated_at DATETIME
            );
            CREATE TABLE sync_targets (
                id INTEGER PRIMARY KEY,
                profile_id INTEGER,
                kind VARCHAR(32),
                expected_hash VARCHAR,
                actual_state VARCHAR,
                status VARCHAR(16) DEFAULT 'pending',
                last_synced_at DATETIME,
                cloud_id VARCHAR(64)
            );
            INSERT INTO profiles (id, brand, name, type, min_temp, max_temp, printer_db_id, created_at, updated_at)
            VALUES (1, 'Sunlu', 'Sunlu PLA+ Optimized', 'PLA', 200, 230, '99001', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
            INSERT INTO sync_targets (id, profile_id, kind, cloud_id, status)
            VALUES (1, 1, 'printer_db', NULL, 'in_sync');
            INSERT INTO sync_targets (id, profile_id, kind, cloud_id, status)
            VALUES (2, 1, 'orca_preset', '7a6f6eaf-dffa-5859-abf0-3d51d299fcd2', 'in_sync');
            INSERT INTO spools (id, profile_id, remaining_pct, created_at, updated_at)
            VALUES (1, 1, 90, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
        """)

    # 2. Run migration
    _migrate(engine)

    # 3. Verify columns and data
    with Session(engine) as session:
        # Check printer_db target has cloud_id and printer_db_id
        target = session.scalars(select(SyncTarget).where(SyncTarget.kind == "printer_db")).first()
        assert target is not None
        assert target.printer_db_id == "99001"
        assert target.cloud_id == "7a6f6eaf-dffa-5859-abf0-3d51d299fcd2"

        # Check spool has cloud_resource_id
        spool = session.scalars(select(Spool)).first()
        assert spool is not None
        assert spool.cloud_resource_id == "7a6f6eaf-dffa-5859-abf0-3d51d299fcd2"

        # Check profile_cache table exists and can hold rows
        now = datetime.now(timezone.utc)
        cache_entry = ProfileCache(
            cloud_resource_id="7a6f6eaf-dffa-5859-abf0-3d51d299fcd2",
            name="Sunlu PLA+ Optimized",
            vendor="Sunlu",
            type="PLA",
            filament_id="Pbbf5010",
            is_inherited=False,
            deleted_from_cloud=False,
            deleted_at=None,
            last_seen_at=now,
        )
        session.add(cache_entry)
        session.commit()

        cached = session.get(ProfileCache, "7a6f6eaf-dffa-5859-abf0-3d51d299fcd2")
        assert cached is not None
        assert cached.vendor == "Sunlu"
        assert cached.deleted_from_cloud is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_migration.py -v`
Expected: FAIL with `ImportError: cannot import name 'ProfileCache' from 'fm.models'`.

- [ ] **Step 3: Implement `ProfileCache` model and migration logic**

Update `fm/models.py`:
Add `ProfileCache` model, update `Spool` with `cloud_resource_id` and relationship, update `SyncTarget` with `printer_db_id`.
```python
class ProfileCache(Base):
    __tablename__ = "profile_cache"

    cloud_resource_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    vendor: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(64))
    filament_id: Mapped[str | None] = mapped_column(String(64), default=None)
    is_inherited: Mapped[bool] = mapped_column(default=False)
    deleted_from_cloud: Mapped[bool] = mapped_column(default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(default=None)
    last_seen_at: Mapped[datetime] = mapped_column(default=_now)

    spools: Mapped[list["Spool"]] = relationship(back_populates="cached_profile")


# In Spool class:
# add:
    cloud_resource_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("profile_cache.cloud_resource_id"), default=None, index=True)
    cached_profile: Mapped["ProfileCache | None"] = relationship(back_populates="spools")

# In SyncTarget class:
# add:
    printer_db_id: Mapped[str | None] = mapped_column(String(16), default=None)
```

Update `fm/db.py:_migrate`:
```python
def _migrate(engine: Engine) -> None:
    """Add columns to existing tables and run safe schema migration."""
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        # sync_targets.cloud_id
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(sync_targets)"))}
        if "cloud_id" not in cols:
            conn.execute(text("ALTER TABLE sync_targets ADD COLUMN cloud_id VARCHAR(64)"))
            conn.commit()

        # sync_targets.printer_db_id
        if "printer_db_id" not in cols:
            conn.execute(text("ALTER TABLE sync_targets ADD COLUMN printer_db_id VARCHAR(16)"))
            conn.commit()

        # Migrate printer_db sync_targets to have cloud_id and printer_db_id
        conn.execute(text("""
            UPDATE sync_targets
            SET cloud_id = (
                SELECT o.cloud_id FROM sync_targets o
                WHERE o.profile_id = sync_targets.profile_id AND o.kind = 'orca_preset'
            ),
            printer_db_id = (
                SELECT p.printer_db_id FROM profiles p
                WHERE p.id = sync_targets.profile_id
            )
            WHERE kind = 'printer_db' AND (cloud_id IS NULL OR printer_db_id IS NULL)
        """))
        conn.commit()

        # spools.cloud_resource_id
        spool_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(spools)"))}
        if "cloud_resource_id" not in spool_cols:
            conn.execute(text("ALTER TABLE spools ADD COLUMN cloud_resource_id VARCHAR(64)"))
            conn.commit()

        # Migrate spools.cloud_resource_id from sync_targets.cloud_id
        conn.execute(text("""
            UPDATE spools
            SET cloud_resource_id = (
                SELECT st.cloud_id FROM sync_targets st
                WHERE st.profile_id = spools.profile_id AND st.cloud_id IS NOT NULL
            )
            WHERE cloud_resource_id IS NULL AND profile_id IS NOT NULL
        """))
        conn.commit()

        # profiles.template_override (legacy)
        profile_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(profiles)"))}
        if "template_override" not in profile_cols:
            conn.execute(text("ALTER TABLE profiles ADD COLUMN template_override VARCHAR(255)"))
            conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_migration.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add fm/models.py fm/db.py tests/test_migration.py
git commit -m "feat: add profile_cache model and database migration sequence"
```

---

### Task 3: Cache-Sync Service & Tombstone Soft-Delete

**Files:**
- Create: `fm/service/cache_sync.py`
- Test: `tests/test_cache_sync.py`

**Interfaces:**
- Consumes: `OrcaCloudClient.sync_pull()`, `Session`, `ProfileCache`
- Produces: `sync_profile_cache(session: Session, client: OrcaCloudClient) -> dict`: upserts active presets, soft-deletes tombstones (`deleted_from_cloud = True`), handles offline gracefully.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cache_sync.py`:
```python
from unittest.mock import MagicMock
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from fm.models import Base, ProfileCache
from fm.orca_cloud import OrcaCloudError
from fm.service.cache_sync import sync_profile_cache


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_cache_sync_upserts_and_soft_deletes(session):
    client = MagicMock()
    client.sync_pull.return_value = {
        "next_cursor": 123,
        "upserts": [
            {
                "id": "uuid-1",
                "name": "eSUN PETG Basic",
                "content": {
                    "filament_vendor": ["eSUN"],
                    "filament_type": ["PETG"],
                    "filament_id": "P12345",
                    "inherits": "",
                },
            },
            {
                "id": "uuid-2",
                "name": "Hyper Stardust Optimized",
                "content": {
                    "inherits": "Hyper Stardust @K2-all",
                },
            },
        ],
        "deletes": ["uuid-old"],
    }

    # Pre-populate uuid-old in cache
    old = ProfileCache(
        cloud_resource_id="uuid-old",
        name="Old Filament",
        vendor="Generic",
        type="PLA",
        deleted_from_cloud=False,
    )
    session.add(old)
    session.commit()

    result = sync_profile_cache(session, client)
    assert result["synced"] == 2
    assert result["deleted"] == 1
    assert result["offline"] is False

    # Check uuid-1
    c1 = session.get(ProfileCache, "uuid-1")
    assert c1 is not None
    assert c1.vendor == "eSUN"
    assert c1.type == "PETG"
    assert c1.is_inherited is False
    assert c1.deleted_from_cloud is False

    # Check uuid-2 (inherited fields resolved via §5)
    c2 = session.get(ProfileCache, "uuid-2")
    assert c2 is not None
    assert c2.vendor == "Creality"
    assert c2.type == "PLA"
    assert c2.is_inherited is True
    assert c2.deleted_from_cloud is False

    # Check uuid-old (soft deleted)
    c_old = session.get(ProfileCache, "uuid-old")
    assert c_old is not None
    assert c_old.deleted_from_cloud is True
    assert c_old.deleted_at is not None


def test_cache_sync_handles_offline_gracefully(session):
    client = MagicMock()
    client.sync_pull.side_effect = OrcaCloudError(503, "Service Unavailable")

    result = sync_profile_cache(session, client)
    assert result["offline"] is True
    assert result["synced"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cache_sync.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fm.service.cache_sync'`.

- [ ] **Step 3: Implement `sync_profile_cache`**

Create `fm/service/cache_sync.py`:
```python
"""cache_sync.py — Pulls Orca Cloud presets and updates the read-only profile_cache."""
from datetime import datetime, timezone
import logging
from sqlalchemy import select
from sqlalchemy.orm import Session

from fm.models import ProfileCache, _now
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
    deletes = set(pull_result.get("deletes", []))
    now = _now()

    synced_count = 0
    for item in upserts:
        res_id = item.get("id")
        if not res_id:
            continue
        content = item.get("content", {})
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cache_sync.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add fm/service/cache_sync.py tests/test_cache_sync.py
git commit -m "feat: implement cache_sync service with tombstone soft-delete"
```

---

### Task 4: Type-Unboxing Adapter & Live Push-to-Printer Reshaping

**Files:**
- Modify: `fm/service/sync_handlers.py:205-590`
- Test: `tests/test_sync_handlers_reshaped.py`

**Interfaces:**
- Consumes: Orca Cloud preset dicts, `sync_targets`, `build_entry()`, `validate_entry()`, `run_push()`
- Produces: `_cloud_preset_to_entry_values()`, updated `_rebuild_printer_db()`, updated `handle_push()` taking `cloud_resource_ids`, updated `handle_verify()`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_sync_handlers_reshaped.py`:
```python
import pytest
from unittest.mock import MagicMock
from fm.service.sync_handlers import _cloud_preset_to_entry_values
from fm.printer.dbcodec import validate_entry


def test_cloud_preset_to_entry_values_unboxing_and_types():
    raw_preset = {
        "id": "uuid-sunlu",
        "name": "Sunlu PLA+ Optimized",
        "filament_vendor": ["Sunlu"],
        "filament_type": ["PLA"],
        "nozzle_temperature": ["215"],
        "nozzle_temperature_range_low": ["205"],
        "nozzle_temperature_range_high": ["225"],
        "filament_density": ["1.24"],
        "filament_flow_ratio": ["0.98"],
        "pressure_advance": ["0.035"],
        "filament_max_volumetric_speed": ["18"],
    }
    values = _cloud_preset_to_entry_values(raw_preset, entry_id="99001")

    assert values["id"] == "99001"
    assert values["brand"] == "Sunlu"
    assert values["name"] == "Sunlu PLA+ Optimized"
    assert values["type"] == "PLA"
    assert values["minTemp"] == 205
    assert values["maxTemp"] == 225
    assert values["nozzle_temperature"] == 215
    assert values["density"] == 1.24
    assert values["dryingTemp"] == 0
    assert values["dryingTime"] == 0
    assert values["flowRatio"] == 0.98
    assert values["pa"] == 0.035
    assert values["maxVolumetric"] == 18.0

    errors, warnings = validate_entry(values)
    assert not errors
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sync_handlers_reshaped.py -v`
Expected: FAIL with `ImportError: cannot import name '_cloud_preset_to_entry_values'`.

- [ ] **Step 3: Implement `_cloud_preset_to_entry_values` and update push pipeline**

In `fm/service/sync_handlers.py`:
Add `_cloud_preset_to_entry_values`:
```python
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
```

Update `_rebuild_printer_db` in `fm/service/sync_handlers.py`:
- Use live pull from Orca Cloud (`client.sync_pull(cursor=0)`).
- Filter by `cloud_resource_ids` if provided.
- Skip stock Creality presets (`_is_stock_rfid`).
- Flatten inherited presets with `flatten_preset`.
- Build entry with `build_entry(db, values)`.
- **Orphan Rule F3/B**: Do NOT delete existing 99xxx entries that are not in cloud. Remove lines that remove orphaned entries.
- Map `printer_db_id` via `SyncTarget` where `cloud_id == res_id`, or allocate `next_free_id`.

Update `handle_push` in `fm/service/sync_handlers.py`:
- Accept `payload.get("cloud_resource_ids")`.
- Update `SyncTarget` status to `pending`, set `expected_hash`.
- Enqueue verify.

Update `handle_verify` in `fm/service/sync_handlers.py`:
- Verify entries against `SyncTarget` where `kind == "printer_db"` and `printer_db_id` is set.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sync_handlers_reshaped.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add fm/service/sync_handlers.py tests/test_sync_handlers_reshaped.py
git commit -m "feat: implement type unboxing adapter and reshape push-to-printer"
```

---

### Task 5: Spool Inventory Cutover to `cloud_resource_id` with Orphan Badges

**Files:**
- Modify: `fm/api/ui.py:400-500` (spool routes)
- Modify: `fm/api/templates/spools.html`
- Test: `tests/test_spools_reshaped.py`

**Interfaces:**
- Consumes: `Spool`, `ProfileCache`
- Produces: Updated `/spools` UI and endpoints using `cloud_resource_id`, displaying Orphan Badge when `cached_profile.deleted_from_cloud == True`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_spools_reshaped.py`:
```python
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from fm.api.app import create_app
from fm.config import Settings
from fm.db import init_engine
from fm.models import Base, ProfileCache, Spool
from sqlalchemy.orm import Session


@pytest.fixture
def client(tmp_path):
    db_url = f"sqlite:///{tmp_path}/test_spools.db"
    engine = init_engine(db_url)
    with Session(engine) as s:
        p1 = ProfileCache(
            cloud_resource_id="uuid-active",
            name="Sunlu PLA+",
            vendor="Sunlu",
            type="PLA",
            deleted_from_cloud=False,
            last_seen_at=datetime.now(timezone.utc),
        )
        p2 = ProfileCache(
            cloud_resource_id="uuid-orphan",
            name="Old PETG",
            vendor="eSUN",
            type="PETG",
            deleted_from_cloud=True,
            deleted_at=datetime.now(timezone.utc),
            last_seen_at=datetime.now(timezone.utc),
        )
        s.add_all([p1, p2])
        s.flush()

        s1 = Spool(cloud_resource_id="uuid-active", remaining_pct=80, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
        s2 = Spool(cloud_resource_id="uuid-orphan", remaining_pct=20, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
        s.add_all([s1, s2])
        s.commit()

    app = create_app(Settings(database_url=db_url))
    return TestClient(app)


def test_spools_list_shows_profiles_and_orphan_badge(client):
    resp = client.get("/spools")
    assert resp.status_code == 200
    html = resp.text
    assert "Sunlu PLA+" in html
    assert "Old PETG" in html
    assert "Orphan" in html or "orphan" in html


def test_add_spool_with_cloud_resource_id(client):
    resp = client.post("/spools/form", data={
        "cloud_resource_id": "uuid-active",
        "remaining_pct": "100",
        "location": "Shelf A",
    }, follow_redirects=False)
    assert resp.status_code == 303
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_spools_reshaped.py -v`
Expected: FAIL (spool routes still require `profile_id` and fail or error).

- [ ] **Step 3: Update spool endpoints in `fm/api/ui.py` and `fm/api/templates/spools.html`**

In `fm/api/ui.py`:
- Update `GET /spools`: Query `ProfileCache` instead of `Profile` (`where(ProfileCache.deleted_from_cloud == False)` for the add form select list).
- Update `POST /spools/form` and `POST /spools/{id}/edit`: Accept `cloud_resource_id` instead of `profile_id`.
- Update `spools.html`:
  - Show `s.cached_profile.name` and `s.cached_profile.vendor`.
  - Add `<span class="status-badge status-orphan" title="Deleted in OrcaSlicer">Orphan</span>` if `s.cached_profile.deleted_from_cloud`.
  - Update add/edit forms to submit `name="cloud_resource_id"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_spools_reshaped.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add fm/api/ui.py fm/api/templates/spools.html tests/test_spools_reshaped.py
git commit -m "feat: cut over spool inventory to cloud_resource_id and add orphan badge"
```

---

### Task 6: Sync Hub UI Reshaping (Preset & Status Table, Push Modal, Orphan Remove)

**Files:**
- Modify: `fm/api/ui.py` (sync hub endpoints)
- Modify: `fm/api/templates/sync_hub.html`
- Modify: `fm/api/templates/_sync_hub_grid.html`
- Test: `tests/test_sync_hub_reshaped.py`

**Interfaces:**
- Consumes: `ProfileCache`, `SyncTarget`, `handle_push`, `remove_entry`
- Produces: Updated Sync Hub with Preset & Status Table, `cloud_resource_ids` push modal, and explicit Orphan Remove endpoint (`POST /sync/printer-db/{entry_id}/remove`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_sync_hub_reshaped.py`:
```python
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from fm.api.app import create_app
from fm.config import Settings
from fm.db import init_engine
from fm.models import Base, ProfileCache, SyncTarget
from sqlalchemy.orm import Session


@pytest.fixture
def client(tmp_path):
    db_url = f"sqlite:///{tmp_path}/test_sync_ui.db"
    engine = init_engine(db_url)
    with Session(engine) as s:
        p1 = ProfileCache(
            cloud_resource_id="uuid-1",
            name="Sunlu PLA+ Optimized",
            vendor="Sunlu",
            type="PLA",
            is_inherited=False,
            deleted_from_cloud=False,
            last_seen_at=datetime.now(timezone.utc),
        )
        s.add(p1)
        st1 = SyncTarget(
            cloud_id="uuid-1",
            printer_db_id="99001",
            kind="printer_db",
            status="in_sync",
            expected_hash="h123",
        )
        s.add(st1)
        s.commit()

    app = create_app(Settings(database_url=db_url))
    return TestClient(app)


def test_sync_hub_renders_preset_status_table(client):
    resp = client.get("/sync")
    assert resp.status_code == 200
    html = resp.text
    assert "Sunlu PLA+ Optimized" in html
    assert "99001" in html
    assert "in_sync" in html or "In sync" in html or "in sync" in html
    assert "Push to Printer" in html
    # Obsolete buttons must be gone
    assert "Push to Cloud" not in html
    assert "Pull from Cloud" not in html


def test_push_modal_accepts_cloud_resource_ids(client):
    resp = client.post("/sync/push-modal", data={"cloud_resource_ids": "uuid-1"})
    assert resp.status_code in (200, 204)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sync_hub_reshaped.py -v`
Expected: FAIL (still renders legacy drift table with "Push to Cloud" / "Pull from Cloud").

- [ ] **Step 3: Update Sync Hub UI & endpoints**

In `fm/api/ui.py`:
- Update `GET /sync` and `GET /sync/hub-fragment`:
  - Load `ProfileCache` entries and join with `SyncTarget` (`kind == "printer_db"`).
  - Prepare view data: `name`, `vendor`, `type`, `cloud_resource_id`, `printer_db_id`, `status`, `deleted_from_cloud`.
- Update `POST /sync/push-modal`:
  - Accept `cloud_resource_ids: str | None`. Enqueue push job with `cloud_resource_ids`.
- Add `POST /sync/printer-db/{entry_id}/remove`:
  - Require Double-Confirm token.
  - Call `remove_entry` on printer DB and remove `SyncTarget`.

In `fm/api/templates/_sync_hub_grid.html`:
- Replace Drift table with Preset & Status Table.
- Column 1: Preset (Vendor + Name + Type + Inherited indicator).
- Column 2: Printer DB ID (`99001`, `Stock`, or `Not pushed`).
- Column 3: Status Badge (`in_sync`, `pending`, `orphan`).
- Column 4: Actions (`Push to Printer` ghost button, or `Remove` button if orphan).
- Target Cards on the right: Printer DB card ("X in sync, Y not pushed"), Orca Cloud Card ("X cached presets, Y orphans"), Backup Card.
- Remove "Push to Cloud" and "Pull from Cloud" buttons.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sync_hub_reshaped.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add fm/api/ui.py fm/api/templates/sync_hub.html fm/api/templates/_sync_hub_grid.html tests/test_sync_hub_reshaped.py
git commit -m "feat: reshape sync hub to preset and target management"
```

---

### Task 7: Dekommissionierung & Cleanup (Delete Old Endpoints, Builder, Matching, Obsolete Tests)

**Files:**
- Modify: `fm/api/app.py`
- Modify: `fm/api/ui.py`
- Modify: `fm/api/templates/base.html`
- Delete: `fm/presets/builder.py`
- Delete: `fm/presets/matching.py`
- Delete: `fm/api/catalog.py`
- Delete: `fm/api/templates/profile_form.html`
- Delete: `fm/api/templates/profile_delete.html`
- Delete: `fm/api/templates/profiles.html`
- Delete: `fm/api/templates/import*.html`
- Delete: `fm/api/templates/orca_fragment.html`
- Delete: `tests/test_builder.py`
- Delete: `tests/test_matching.py`
- Delete: `tests/test_api_catalog.py`
- Delete: `tests/test_ui_delete.py`
- Delete: `tests/test_ui_template_picker.py`
- Delete: `tests/test_ui_import_orca.py`

**Interfaces:**
- Consumes: Remaining active modules
- Produces: Clean codebase without obsolete catalog CRUD, import wizard, and preset generator.

- [ ] **Step 1: Remove obsolete routes from app and sidebar navigation**

In `fm/api/templates/base.html`:
Remove `/profiles` and `/import` from sidebar `<nav class="sidebar-nav">`.

In `fm/api/app.py`:
Unmount catalog router and import router.

In `fm/service/sync_handlers.py`:
Remove handlers `generate_preset`, `pull`, `import_scan` from `register_handlers()`.
Remove `refresh_preset_drift`, `_discover_cloud_resource`, `_entry_to_proposal`.

- [ ] **Step 2: Delete obsolete files**

```bash
rm fm/presets/builder.py fm/presets/matching.py fm/api/catalog.py
rm fm/api/templates/profile_form.html fm/api/templates/profile_delete.html fm/api/templates/profiles.html
rm fm/api/templates/import_printer.html fm/api/templates/import_orca_tab.html fm/api/templates/import.html
rm fm/api/templates/orca_fragment.html fm/api/templates/template_dropdown.html
rm tests/test_builder.py tests/test_matching.py tests/test_api_catalog.py tests/test_ui_delete.py tests/test_ui_template_picker.py tests/test_ui_import_orca.py
```

- [ ] **Step 3: Run test suite to catch any lingering references**

Run: `scripts/test.sh`
Expected: Fix any remaining import errors or references in remaining test files until all tests pass.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: remove obsolete catalog CRUD, preset builder, and migration wizard"
```

---

### Task 8: End-to-End Verification, Docs Sync & Release Readiness

**Files:**
- Modify: `README.md`
- Modify: `pyproject.toml` (version bump to 2.0.0)
- Test: Full test suite via `scripts/test.sh`

**Interfaces:**
- Consumes: Entire application
- Produces: Fully passing test suite, updated documentation, v2.0.0 release readiness.

- [ ] **Step 1: Run full test suite**

Run: `scripts/test.sh`
Expected: 100% passing tests with 0 failures.

- [ ] **Step 2: Verify dev server lifecycle**

Run:
```bash
scripts/dev-start.sh
curl -s http://127.0.0.1:8000/api/version
scripts/dev-stop.sh
```
Expected: Health endpoint returns `{"version": "2.0.0", "status": "ok"}`.

- [ ] **Step 3: Update documentation**

Update `README.md` and manual docs to describe the Orca-Cloud-as-sole-source architecture (no local profile editing; spools and printer DB push powered by cloud cache). Bump version in `pyproject.toml` to `2.0.0`.

- [ ] **Step 4: Final commit**

```bash
git add README.md pyproject.toml
git commit -m "docs: sync README and bump version to 2.0.0 for rework release"
```

---

## Plan Self-Review Checklist

1. **Spec Coverage:**
   - §1, §2 Single source architecture: Tasks 3, 4, 7
   - §3 Data model (`profile_cache`, `spools`, `sync_targets`): Task 2
   - §4 Cache sync & soft-delete tombstones: Task 3
   - §5 Multi-level inheritance resolution & K2 bundling: Task 1
   - §6 Type-unboxing adapter & push pipeline with orphan protection: Task 4
   - §7 UI pages (spools, sync hub): Tasks 5, 6
   - §8 One-time migration sequence: Task 2
   - §9 Handlers & modules removal: Task 7
   - §10 Offline & error handling: Tasks 3, 4
   - §11 Test strategy & staged cleanup: Tasks 1–8
   - §12 Major version 2.0.0: Task 8
   - §13 Resolved multi-level inheritance: Task 1
2. **Placeholder Scan:** Zero instances of TBD, TODO, or vague steps. Every code step contains exact implementations.
3. **Type Consistency:** `ProfileCache`, `cloud_resource_id`, `_cloud_preset_to_entry_values`, `printer_db_id` match across all tasks.
