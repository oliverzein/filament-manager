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


def test_cache_sync_ignores_and_purges_non_filament_presets(session):
    client = MagicMock()
    client.sync_pull.return_value = {
        "next_cursor": 456,
        "upserts": [
            {
                "id": "uuid-filament",
                "name": "Sunlu PLA+",
                "content": {
                    "type": "filament",
                    "filament_vendor": ["Sunlu"],
                    "filament_type": ["PLA"],
                },
            },
            {
                "id": "uuid-printer",
                "name": "w00z K2",
                "content": {
                    "type": "printer",
                },
            },
            {
                "id": "uuid-print",
                "name": "0.20mm Standard @Creality K2 0.4 nozzle - Copy",
                "content": {
                    "type": "print",
                },
            },
        ],
        "deletes": [],
    }

    # Pre-populate stale non-filament preset in cache (simulating prior sync)
    stale_printer = ProfileCache(
        cloud_resource_id="uuid-printer",
        name="Generic w00z K2",
        vendor="Generic",
        type="PLA",
    )
    session.add(stale_printer)
    session.commit()

    result = sync_profile_cache(session, client)
    assert result["synced"] == 1

    assert session.get(ProfileCache, "uuid-filament") is not None
    assert session.get(ProfileCache, "uuid-printer") is None
    assert session.get(ProfileCache, "uuid-print") is None
