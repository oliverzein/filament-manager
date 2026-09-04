from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from fm.api.app import create_app, Settings
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
