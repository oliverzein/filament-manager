# tests/test_models.py
from datetime import datetime, timezone

from sqlalchemy import select, func

from fm.db import init_engine, SessionLocal
from fm.models import Profile, Spool, SyncTarget, Job, ActivityLog

NOW = datetime.now(timezone.utc)


def test_tables_roundtrip(tmp_path):
    init_engine(f"sqlite:///{tmp_path}/t.db")
    with SessionLocal() as s:
        p = Profile(brand="Sunlu", name="Sunlu PLA+", type="PLA",
                    min_temp=200, max_temp=230, created_at=NOW, updated_at=NOW)
        s.add(p); s.flush()
        s.add(Spool(profile_id=p.id, remaining_pct=80, created_at=NOW, updated_at=NOW))
        s.add(SyncTarget(profile_id=p.id, kind="printer_db"))
        s.add(Job(kind="push"))
        s.add(ActivityLog(ts=NOW, action="test", outcome="ok"))
    with SessionLocal() as s:
        assert s.scalar(select(func.count(Profile.id))) == 1
        spool = s.scalar(select(Spool))
        assert spool.remaining_pct == 80
