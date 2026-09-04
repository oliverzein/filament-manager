from datetime import datetime, timezone
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from fm.db import _migrate
from fm.models import Spool, SyncTarget, ProfileCache


def test_schema_migration_creates_profile_cache_and_migrates_targets_and_spools(tmp_path):
    db_path = tmp_path / "test_migrate.db"
    url = f"sqlite:///{db_path}"

    # 1. Setup legacy schema and legacy data
    engine = create_engine(url, future=True)
    with engine.raw_connection() as conn:
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


def test_migrate_makes_sync_target_profile_id_nullable(tmp_path):
    """Existing SQLite databases have sync_targets.profile_id as NOT NULL.
    After _migrate, inserting a SyncTarget with profile_id=None must succeed."""
    db_path = tmp_path / "test_nullable.db"
    url = f"sqlite:///{db_path}"

    # 1. Setup legacy schema with profile_id NOT NULL (the original schema)
    engine = create_engine(url, future=True)
    with engine.raw_connection() as conn:
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
                profile_id INTEGER NOT NULL,
                kind VARCHAR(32),
                expected_hash VARCHAR,
                actual_state VARCHAR,
                status VARCHAR(16) DEFAULT 'pending',
                last_synced_at DATETIME,
                cloud_id VARCHAR(64)
            );
            INSERT INTO profiles (id, brand, name, type, min_temp, max_temp, created_at, updated_at)
            VALUES (1, 'Sunlu', 'Sunlu PLA+', 'PLA', 200, 230, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
            INSERT INTO sync_targets (id, profile_id, kind, status)
            VALUES (1, 1, 'printer_db', 'in_sync');
        """)

    # 2. Run migration
    _migrate(engine)

    # 3. Verify profile_id is now nullable by inserting a NULL-profile_id target
    with Session(engine) as session:
        target = SyncTarget(
            profile_id=None,
            kind="printer_db",
            cloud_id="uuid-test",
            printer_db_id="99099",
        )
        session.add(target)
        session.commit()

        # Verify it was inserted
        result = session.scalars(
            select(SyncTarget).where(SyncTarget.cloud_id == "uuid-test")
        ).first()
        assert result is not None
        assert result.profile_id is None
        assert result.printer_db_id == "99099"

        # Existing data preserved
        existing = session.scalars(
            select(SyncTarget).where(SyncTarget.id == 1)
        ).first()
        assert existing is not None
        assert existing.profile_id == 1
        assert existing.kind == "printer_db"
