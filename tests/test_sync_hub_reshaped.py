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
