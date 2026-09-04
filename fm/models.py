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
    nozzle_temperature: Mapped[int | None] = mapped_column(default=None)  # mirrors kvParam.nozzle_temperature
    printer_db_id: Mapped[str | None] = mapped_column(default=None)  # "99xxx" once pushed
    filament_id: Mapped[str | None] = mapped_column(default=None)  # preset filament_id once generated
    template_override: Mapped[str | None] = mapped_column(String(255), default=None)
    notes: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column()

    spools: Mapped[list["Spool"]] = relationship(back_populates="profile")


class Spool(Base):
    __tablename__ = "spools"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("profiles.id"), default=None, index=True)
    color_override: Mapped[str | None] = mapped_column(default=None)
    remaining_pct: Mapped[int] = mapped_column(default=100)
    location: Mapped[str] = mapped_column(default="")
    rfid_tag: Mapped[str | None] = mapped_column(default=None)
    notes: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column()

    profile: Mapped["Profile"] = relationship(back_populates="spools")
    cloud_resource_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("profile_cache.cloud_resource_id"), default=None, index=True)
    cached_profile: Mapped["ProfileCache | None"] = relationship(back_populates="spools")


class SyncTarget(Base):
    __tablename__ = "sync_targets"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("profiles.id"), default=None)
    kind: Mapped[str] = mapped_column(String(32))  # 'printer_db' | 'orca_preset'
    cloud_id: Mapped[str | None] = mapped_column(String(64), default=None)  # Orca Cloud resource UUID (like setting_id in .info files)
    expected_hash: Mapped[str | None] = mapped_column(default=None)
    actual_state: Mapped[str | None] = mapped_column(default=None)  # JSON strings
    status: Mapped[str] = mapped_column(String(16), default="pending")  # in_sync|drift|pending|error
    last_synced_at: Mapped[datetime | None] = mapped_column(default=None)
    printer_db_id: Mapped[str | None] = mapped_column(String(16), default=None)  # "99xxx" printer DB row this target mirrors


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # push|verify|github_backup
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
