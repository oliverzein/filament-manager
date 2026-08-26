# fm/models.py
from datetime import datetime, timezone

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    brand: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(64))
    colors: Mapped[str] = mapped_column(default="[]")  # JSON array of hex
    min_temp: Mapped[int] = mapped_column()
    max_temp: Mapped[int] = mapped_column()
    density: Mapped[float] = mapped_column(default=0.0)
    drying_temp: Mapped[int] = mapped_column(default=0)
    drying_time: Mapped[int] = mapped_column(default=0)
    pa: Mapped[float | None] = mapped_column(default=None)
    flow_ratio: Mapped[float | None] = mapped_column(default=None)
    max_volumetric: Mapped[float | None] = mapped_column(default=None)
    retraction_length: Mapped[float | None] = mapped_column(default=None)
    retraction_speed: Mapped[float | None] = mapped_column(default=None)
    nozzle_temperature: Mapped[int | None] = mapped_column(default=None)  # mirrors kvParam.nozzle_temperature (temp-sync target)
    printer_db_id: Mapped[str | None] = mapped_column(default=None)  # "99xxx" once pushed
    filament_id: Mapped[str | None] = mapped_column(default=None)  # preset filament_id once generated
    notes: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column()

    spools: Mapped[list["Spool"]] = relationship(back_populates="profile")


class Spool(Base):
    __tablename__ = "spools"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), index=True)
    color_override: Mapped[str | None] = mapped_column(default=None)
    remaining_pct: Mapped[int] = mapped_column(default=100)
    location: Mapped[str] = mapped_column(default="")
    rfid_tag: Mapped[str | None] = mapped_column(default=None)
    notes: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column()

    profile: Mapped["Profile"] = relationship(back_populates="spools")


class SyncTarget(Base):
    __tablename__ = "sync_targets"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"))
    kind: Mapped[str] = mapped_column(String(32))  # 'printer_db' | 'orca_preset'
    expected_hash: Mapped[str | None] = mapped_column(default=None)
    actual_state: Mapped[str | None] = mapped_column(default=None)  # JSON strings
    status: Mapped[str] = mapped_column(String(16), default="pending")  # in_sync|drift|pending|error
    last_synced_at: Mapped[datetime | None] = mapped_column(default=None)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # push|generate_preset|apply_preset|report_presets|temp_sync|import
    payload: Mapped[str] = mapped_column(default="{}")
    status: Mapped[str] = mapped_column(String(24), default="queued")  # queued|running|done|error|awaiting_input
    result: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column()
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(default="{}")
    outcome: Mapped[str] = mapped_column(String(8))  # ok|warn|error
