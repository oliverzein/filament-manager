# tests/test_pipeline.py
# Task 5: Push pipeline + drift primitives.
# Lifted from skill/cfs.py push_local_db (restructured to take injected transport),
# verify_version floor check, and canonical_hash for drift detection.
import json
from pathlib import Path

import pytest
from fm.errors import FmError
from fm.printer import pipeline as pl

from conftest import FakeTransport


# --- helpers ---

def make_db_file(tmp_path, entries):
    """Build a DB with `entries` list entries, write to tmp_path/db.json, return Path."""
    lst = []
    for i in range(entries):
        lst.append({
            "engineVersion": "1.0",
            "printerIntName": "creality_k2",
            "nozzleDiameter": "0.4",
            "base": {
                "id": f"{1000 + i:05d}",
                "brand": "Brand",
                "name": f"Filament {i}",
                "meterialType": "PLA",
                "colors": ["#ffffff"],
                "density": 1.24,
                "diameter": "1.75",
                "minTemp": 190,
                "maxTemp": 220,
                "dryingTemp": 0,
                "dryingTime": 0,
            },
            "kvParam": {
                "nozzle_temperature": "210",
                "filament_type": "PLA",
                "filament_vendor": "Brand",
            },
        })
    db = {"code": 0, "msg": "", "reqId": "",
          "result": {"list": lst, "count": entries, "version": 9876543210}}
    p = tmp_path / "db.json"
    p.write_text(json.dumps(db))
    return p


def idx_of(commands, marker):
    """Index of the first command containing `marker`; raises if absent."""
    for i, c in enumerate(commands):
        if marker in c:
            return i
    raise AssertionError(f"marker {marker!r} not found in {commands}")


# --- push order + guards (brief, transcribed) ---

def test_push_order_and_guards(tmp_path, cfg):
    t = FakeTransport(); db_file = make_db_file(tmp_path, entries=41)
    res = pl.run_push(cfg, db_file, t, sleep=lambda s: None)
    assert res["status"] == "ok"
    backup_idx = idx_of(t.commands, ".bak.")
    busy_idx   = idx_of(t.commands, "ws:get")        # busy BEFORE backup/upload
    upload_idx = idx_of(t.commands, "scp-push:")
    reboot_idx = idx_of(t.commands, "sync; sync;")
    assert busy_idx < backup_idx < upload_idx < reboot_idx


def test_refuses_small_db(tmp_path, cfg):
    t = FakeTransport(); db_file = make_db_file(tmp_path, entries=10)
    with pytest.raises(FmError): pl.run_push(cfg, db_file, t)
    assert t.commands == []                      # nothing touched the printer


def test_busy_returns_without_upload(tmp_path, cfg):
    t = FakeTransport(busy={"filename": "x.gcode", "progress": "50%"})
    db_file = make_db_file(tmp_path, entries=41)
    res = pl.run_push(cfg, db_file, t)
    assert res["status"] == "busy"
    assert not any(c.startswith("scp-push") for c in t.commands)


# --- push semantics ---

def test_busy_result_carries_job(tmp_path, cfg):
    t = FakeTransport(busy={"filename": "benchy.gcode", "progress": 42, "layer": 17})
    db_file = make_db_file(tmp_path, entries=41)
    res = pl.run_push(cfg, db_file, t)
    assert res["status"] == "busy"
    assert res["job"]["filename"] == "benchy.gcode"
    assert res["job"]["progress"] == 42


def test_busy_does_not_backup(tmp_path, cfg):
    """Busy must return BEFORE backup touches the printer."""
    t = FakeTransport(busy={"filename": "x.gcode", "progress": "50%"})
    db_file = make_db_file(tmp_path, entries=41)
    pl.run_push(cfg, db_file, t)
    assert not any(".bak." in c for c in t.commands)
    assert not any("sync; sync;" in c for c in t.commands)


def test_no_reboot_status(tmp_path, cfg):
    t = FakeTransport(); db_file = make_db_file(tmp_path, entries=41)
    res = pl.run_push(cfg, db_file, t, no_reboot=True, sleep=lambda s: None)
    assert res["status"] == "no_reboot"
    assert not any("sync; sync;" in c for c in t.commands)  # no reboot issued


def test_force_push_overrides_small_db(tmp_path, cfg):
    t = FakeTransport(); db_file = make_db_file(tmp_path, entries=10)
    res = pl.run_push(cfg, db_file, t, force_push=True, sleep=lambda s: None)
    assert res["status"] == "ok"


def test_force_reboot_pushes_despite_busy(tmp_path, cfg):
    t = FakeTransport(busy={"filename": "x.gcode", "progress": "50%"})
    db_file = make_db_file(tmp_path, entries=41)
    res = pl.run_push(cfg, db_file, t, force_reboot=True, sleep=lambda s: None)
    assert res["status"] == "ok"
    assert any(c.startswith("scp-push") for c in t.commands)


def test_version_bumped_before_upload(tmp_path, cfg):
    """Version bump happens locally before SCP upload (Global Constraint 1/2)."""
    t = FakeTransport(); db_file = make_db_file(tmp_path, entries=41)
    pl.run_push(cfg, db_file, t, sleep=lambda s: None)
    db = json.loads(db_file.read_text())
    # version was 9876543210 (>= override) -> auto-incremented to 9876543211
    assert int(db["result"]["version"]) == 9876543211


# --- verify_printer ---

class VerifyTransport(FakeTransport):
    """FakeTransport that materializes a DB on scp_pull."""
    def __init__(self, db):
        super().__init__()
        self.db = db

    def scp_pull(self, local):
        super().scp_pull(local)
        Path(local).write_text(json.dumps(self.db))


def test_verify_printer_version_ok_and_present():
    from fm.config import Config
    cfg = Config(printer_ip="p", ssh_user="u", ssh_password="pw",
                 db_remote_path="/r.json")
    db = {"result": {"list": [{"base": {"id": "99001"}}, {"base": {"id": "99002"}}],
                     "version": 9876543211}}
    t = VerifyTransport(db)
    res = pl.verify_printer(cfg, t, ["99001", "99002", "99003"])
    assert res["version_ok"] is True
    assert res["present"] == ["99001", "99002"]
    assert res["missing"] == ["99003"]


def test_verify_printer_version_below_floor():
    from fm.config import Config
    cfg = Config(printer_ip="p", ssh_user="u", ssh_password="pw",
                 db_remote_path="/r.json")
    # cloud sync overwrote with epoch version (< override floor)
    db = {"result": {"list": [{"base": {"id": "99001"}}], "version": 1781668740}}
    t = VerifyTransport(db)
    res = pl.verify_printer(cfg, t, ["99001"])
    assert res["version_ok"] is False
    assert res["present"] == ["99001"]
    assert res["missing"] == []


def test_verify_printer_pulls_via_transport():
    """verify_printer must scp_pull from the printer (fresh DB, ignoring cache)."""
    from fm.config import Config
    cfg = Config(printer_ip="p", ssh_user="u", ssh_password="pw",
                 db_remote_path="/r.json")
    db = {"result": {"list": [], "version": 9876543210}}
    t = VerifyTransport(db)
    pl.verify_printer(cfg, t, [])
    assert any(c.startswith("scp-pull:") for c in t.commands)


# --- canonical_hash ---

def test_canonical_hash_deterministic():
    h1 = pl.canonical_hash({"brand": "Sunlu", "name": "PLA+", "maxTemp": 215})
    h2 = pl.canonical_hash({"brand": "Sunlu", "name": "PLA+", "maxTemp": 215})
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_canonical_hash_key_order_invariant():
    """sort_keys=True: key order must not affect the hash."""
    a = pl.canonical_hash({"a": 1, "b": 2, "c": 3})
    b = pl.canonical_hash({"c": 3, "a": 1, "b": 2})
    assert a == b


def test_canonical_hash_differs_on_fields():
    a = pl.canonical_hash({"brand": "Sunlu", "name": "PLA+", "maxTemp": 215})
    b = pl.canonical_hash({"brand": "Sunlu", "name": "PLA+", "maxTemp": 220})
    assert a != b


def test_canonical_hash_known_vector():
    """Pin a known sha256 to guard against accidental algorithm drift."""
    import hashlib
    expected = hashlib.sha256(
        json.dumps({"brand": "Sunlu", "maxTemp": 215}, sort_keys=True).encode()
    ).hexdigest()
    assert pl.canonical_hash({"brand": "Sunlu", "maxTemp": 215}) == expected
