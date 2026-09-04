import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from fm.api.app import create_app, Settings
from fm.db import init_engine
from sqlalchemy.orm import Session
from fm.models import Base, ProfileCache, SyncTarget
from fm.service import sync_handlers as sh


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


def test_orphan_remove_token_mismatch_returns_422(client):
    """Double-Confirm: wrong typed token returns HTTP 422."""
    resp = client.post("/sync/printer-db/99001/remove", data={"confirm": "wrong"})
    assert resp.status_code == 422


def test_orphan_remove_missing_target_returns_404(client):
    """No SyncTarget for the given entry_id returns HTTP 404."""
    resp = client.post("/sync/printer-db/99999/remove", data={"confirm": "99999"})
    assert resp.status_code == 404


def test_orphan_remove_success_returns_fragment_and_deletes_target(client):
    """Successful remove returns the hub fragment (200) and deletes the SyncTarget."""
    from sqlalchemy import select as _sel
    from fm.db import SessionLocal

    resp = client.post("/sync/printer-db/99001/remove", data={"confirm": "99001"})
    assert resp.status_code == 200
    # Fragment should contain the preset table, not a full page redirect
    assert "preset-table" in resp.text
    # SyncTarget should be gone
    with SessionLocal() as s:
        target = s.scalars(
            _sel(SyncTarget).where(SyncTarget.printer_db_id == "99001")
        ).first()
        assert target is None


def test_orphan_remove_updates_cached_printer_db(client, monkeypatch, tmp_path, sample_db):
    """With a live cache file present, removal also drops the 99xxx entry
    from the cached printer DB — exercises load_db/find_entry/remove_entry/
    save_db (regression: missing dbcodec import silently skipped this path).
    """
    from sqlalchemy import select as _sel
    from fm.db import SessionLocal
    from fm.printer.dbcodec import find_entry as _find_entry

    cache = tmp_path / "printer-db-cache.json"
    cache.write_text(json.dumps(sample_db))
    monkeypatch.setattr(sh, "_cache_db_path", cache)
    # The fixture seeded a SyncTarget with printer_db_id=99001; sample_db
    # carries the matching 99001 custom entry.
    with SessionLocal() as s:
        assert s.scalars(
            _sel(SyncTarget).where(SyncTarget.printer_db_id == "99001")
        ).first() is not None
    assert _find_entry(json.loads(cache.read_text()), "99001") is not None

    resp = client.post("/sync/printer-db/99001/remove", data={"confirm": "99001"})
    assert resp.status_code == 200
    assert "preset-table" in resp.text

    # SyncTarget gone
    with SessionLocal() as s:
        assert s.scalars(
            _sel(SyncTarget).where(SyncTarget.printer_db_id == "99001")
        ).first() is None
    # 99001 entry removed from the cache file
    assert _find_entry(json.loads(cache.read_text()), "99001") is None


def test_cache_sync_endpoint_returns_hub_fragment(client, monkeypatch):
    """POST /sync/cache-sync syncs Orca Cloud presets and returns the hub fragment."""
    from unittest.mock import MagicMock
    fake_client = MagicMock()
    fake_client.sync_pull.return_value = {
        "next_cursor": 0,
        "upserts": [{
            "id": "uuid-new",
            "content": {
                "type": "filament",
                "name": "New Cloud Preset",
                "filament_type": "PLA",
                "filament_vendor": "Generic",
            },
        }],
        "deletes": [],
    }
    monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)
    resp = client.post("/sync/cache-sync")
    assert resp.status_code == 200
    assert "preset-table" in resp.text
    assert "New Cloud Preset" in resp.text
    assert "Sync Cache" in resp.text


def test_cache_sync_endpoint_without_client_returns_fragment(client, monkeypatch):
    """POST /sync/cache-sync with no Orca Cloud client still returns the fragment."""
    monkeypatch.setattr(sh, "_orca_client_factory", lambda: None)
    resp = client.post("/sync/cache-sync")
    assert resp.status_code == 200


def test_sync_hub_renders_stock_rfid_for_creality_presets(tmp_path):
    """Creality presets must be displayed with Creality Stock, status 'stock',
    no 'Push to Printer' button, and excluded from the 'not pushed' summary count."""
    db_url = f"sqlite:///{tmp_path}/test_sync_stock.db"
    engine = init_engine(db_url)
    with Session(engine) as s:
        p_custom = ProfileCache(
            cloud_resource_id="uuid-custom",
            name="Sunlu PLA+ Optimized",
            vendor="Sunlu",
            type="PLA",
            last_seen_at=datetime.now(timezone.utc),
        )
        p_creality = ProfileCache(
            cloud_resource_id="uuid-creality",
            name="Hyper Stardust Optimized",
            vendor="Creality",
            type="PLA",
            is_inherited=True,
            last_seen_at=datetime.now(timezone.utc),
        )
        s.add_all([p_custom, p_creality])
        st = SyncTarget(
            cloud_id="uuid-custom",
            printer_db_id="99001",
            kind="printer_db",
            status="in_sync",
        )
        s.add(st)
        s.commit()

    app = create_app(Settings(database_url=db_url))
    client = TestClient(app)
    resp = client.get("/sync")
    assert resp.status_code == 200
    html = resp.text

    # Creality row should show 'Creality Stock', status 'stock', and 'Stock RFID'
    assert "Creality Stock" in html
    assert "status-stock" in html
    assert "Stock RFID" in html

    # Summary card must show "1 in sync, 0 not pushed" (Creality is stock, not counted as not pushed)
    assert "1 in sync, 0 not pushed" in html
    assert "preset-table" in resp.text
