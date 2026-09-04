# Filament Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a hosted web app that manages filament profiles + spool inventory and keeps the Creality K2 printer DB and OrcaSlicer presets derived from it.

**Architecture:** Catalog-as-source-of-truth (spec Approach 1). FastAPI + SQLite + Jinja2/HTMX app in an Unraid Docker container; thin `fm-agent` companion on the desktop pulls jobs from the app API and applies presets locally. Sync logic is lifted from the proven skill repo (`cfs.py`, `orca.py`, `preset_utils.py`).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 + SQLite, Jinja2 + HTMX, uvicorn, pytest, websocket-client, sshpass/openssh (runtime).

**Spec:** `docs/superpowers/specs/2026-08-26-filament-manager-design.md` (read together with this plan; the plan argues from the spec).

## Global Constraints

Iron rules — violating any of these is a failed task:

1. **Push order:** MIN_DB_ENTRIES check → WS busy-check → backup on printer → version auto-increment (≥ 9876543210) → SCP upload → sleep 3 → `sync; sync; reboot` → wait online → verify.
2. **Version:** auto-increment when current ≥ override, else set to override (9876543210). Verify uses `>=` floor, never `==`.
3. **Name rule:** profile `name` must contain `brand` (case-insensitive). Validation warns/blocks.
4. **IDs:** custom printer IDs only in 99xxx range, auto-increment from `FM_ID_RANGE_START` (99001). Stock IDs protected against edit/delete.
5. **Backup before every write**, rotate keep 5 (`material_database.json.bak.<timestamp>`).
6. **MIN_DB_ENTRIES = 30**: refuse push below it unless explicitly forced.
7. **Double-confirm delete** (typed token matching) for profiles with printer entry.
8. **Presets:** never apply while OrcaSlicer runs; `.info` gets **empty** `sync_info` and empty `setting_id`/`base_id`; `filament_id = "P" + md5(f"{name} {db_id}")[:13]` (space separator!); remove `type` field; `filament_density` scalar; vendor/type arrays.
9. **Temp-sync skips:** tie → ambiguous; top score < 30 → no_match; system-preset winner → system; missing nozzle_temperature → missing_temp. Only `kvParam.nozzle_temperature` ever syncs.
10. **Companion scans FIRST** `user/<UUID>/filament/` dir only (`find_orca_user_dir` parity).
11. Env config: `FM_PRINTER_IP`, `FM_SSH_USER`, `FM_SSH_PASSWORD`, `FM_DB_REMOTE_PATH`, `FM_WS_PORT` (9999), `FM_VERSION_OVERRIDE` (9876543210), `FM_ID_RANGE_START` (99001), `FM_AGENT_TOKEN`; agent-side `FM_ORCASLICER_CONFIG_DIR` (~/.config/OrcaSlicer).
12. Repeating actions go into `scripts/`; skill repo at `~/Dokumente/Daten/Development/skills/Creality-custom-filament` is read-only reference.
13. UI design work uses the omp **designer** agent (AGENTS.md rule).

## Source Material (lift sites)

Lift logic from the skill repo — do NOT rewrite from scratch, do NOT modify the source repo:

| Lift into | From | Functions |
|---|---|---|
| `fm/printer/dbcodec.py` | `skill/cfs.py` | `load_db`, `save_db`, `find_entry`, `find_custom_entries`, `_is_custom_id`, `next_free_id`, `insert_entry`, `patch_entry`, `remove_entry`, `bump_version`, `count_autofix`, `build_entry`, `_first`, `validate_entry` |
| `fm/printer/transport.py` | `skill/cfs.py` | `_ssh_base_cmd`, `ssh_cmd`, `scp_pull`, `scp_push`, `wait_for_reboot`, `ssh_backup`, `ssh_reboot`, `ws_request`, `get_printer_status`, `check_printer_busy` |
| `fm/printer/pipeline.py` | `skill/cfs.py` | `push_local_db` (restructure to take injected transport), `verify_version`, `_warn_small_db` |
| `fm/presets/builder.py` | `skill/orca.py` | `generate_filament_id`, `find_system_preset`, `write_info_file`, `build_standalone_preset` |
| `fm/presets/matching.py` | `skill/orca.py` | `load_presets`, `get_filament_type`, `get_filament_id`, `match_presets`, `find_sync_targets`, `get_preset_temp` |
| `fm/presets/flattener.py` | `skill/preset_utils.py` | `SYS_PROFILES`, `find_preset_by_name`, `flatten_preset` |

Adaptation rule for every lift: replace `die(code,msg)` process exits with raising a module-level exception (`FmError(code, msg)`); replace direct `subprocess`/WS calls with the injected transport; replace config dicts with `Config`. Everything else copies verbatim including comments.

---

### Task 1: Project scaffold, config, test script

**Files:**
- Create: `pyproject.toml`, `fm/__init__.py`, `fm/config.py`, `fm/errors.py`, `.gitignore`
- Test: `tests/test_config.py`, `tests/conftest.py`
- Create: `scripts/test.sh`

**Interfaces:**
- Produces: `Config` frozen dataclass (fields: `printer_ip: str, ssh_user: str, ssh_password: str, db_remote_path: str, ws_port: int, version_override: int, id_range_start: int, agent_token: str`); `load_config(env: Mapping[str,str] = os.environ) -> Config`; `FmError(code: int, msg: str)` exception. Later tasks consume these exact names.

- [ ] **Step 1: Write failing config test**

```python
# tests/test_config.py
import pytest
from fm.config import load_config
from fm.errors import FmError

BASE = {"FM_PRINTER_IP": "192.168.0.101", "FM_SSH_USER": "root",
        "FM_SSH_PASSWORD": "pw", "FM_DB_REMOTE_PATH": "/mnt/UDISK/x.json",
        "FM_AGENT_TOKEN": "tok"}

def test_loads_with_defaults():
    c = load_config(BASE)
    assert c.ws_port == 9999 and c.version_override == 9876543210
    assert c.id_range_start == 99001

def test_overrides():
    c = load_config({**BASE, "FM_WS_PORT": "8888"})
    assert c.ws_port == 8888

@pytest.mark.parametrize("missing", list(BASE))
def test_missing_required_fails(missing):
    env = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(FmError):
        load_config(env)
```

```python
# tests/conftest.py
import pytest
from fm.config import Config

@pytest.fixture
def cfg():
    return Config(printer_ip="192.168.0.101", ssh_user="root",
                  ssh_password="pw", db_remote_path="/mnt/UDISK/x.json",
                  ws_port=9999, version_override=9876543210,
                  id_range_start=99001, agent_token="testtoken")
```

- [ ] **Step 2: Run, expect failure** — `python -m pytest tests/test_config.py -v` → FAIL (no module `fm`)
- [ ] **Step 3: Implement**

```toml
# pyproject.toml
[project]
name = "filament-manager"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["fastapi>=0.115", "uvicorn[standard]", "sqlalchemy>=2.0",
                "jinja2", "python-multipart", "websocket-client", "requests", "pytest"]
[project.scripts]
fm-agent = "fm.agent.cli:main"
[tool.pytest.ini_options]
testpaths = ["tests"]
```

```python
# fm/errors.py
class FmError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg); self.code = code
```

```python
# fm/config.py
import os
from dataclasses import dataclass
from typing import Mapping
from .errors import FmError

@dataclass(frozen=True)
class Config:
    printer_ip: str; ssh_user: str; ssh_password: str; db_remote_path: str
    ws_port: int = 9999; version_override: int = 9876543210
    id_range_start: int = 99001; agent_token: str = ""

_REQUIRED = ("FM_PRINTER_IP", "FM_SSH_USER", "FM_SSH_PASSWORD",
             "FM_DB_REMOTE_PATH")

def load_config(env: Mapping[str, str] = os.environ) -> Config:
    missing = [k for k in _REQUIRED if not env.get(k)]
    if missing:
        raise FmError(1, f"missing required env vars: {', '.join(missing)}")
    return Config(
        printer_ip=env["FM_PRINTER_IP"], ssh_user=env["FM_SSH_USER"],
        ssh_password=env["FM_SSH_PASSWORD"],
        db_remote_path=env.get("FM_DB_REMOTE_PATH",
            "/mnt/UDISK/creality/userdata/box/material_database.json"),
        ws_port=int(env.get("FM_WS_PORT", "9999")),
        version_override=int(env.get("FM_VERSION_OVERRIDE", "9876543210")),
        id_range_start=int(env.get("FM_ID_RANGE_START", "99001")),
        agent_token=env.get("FM_AGENT_TOKEN", ""))
```

`.gitignore`: `.local/`, `__pycache__/`, `*.egg-info/`, `.venv/`, `dist/`.

- [ ] **Step 4: Create `scripts/test.sh`, chmod +x**

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m pytest "$@"
```

- [ ] **Step 5: Run `./scripts/test.sh` → PASS; commit** `git commit -m "chore: scaffold, config from FM_* env"`

---

### Task 2: SQLAlchemy models

**Files:**
- Create: `fm/models.py`, `fm/db.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `Base` (SQLAlchemy DeclarativeBase); models `Profile, Spool, SyncTarget, Job, ActivityLog` with the columns from spec §3; `init_engine(url: str) -> Engine`; `SessionLocal`. Column names are the contract for every later task.

Schema (exact):

```python
class Profile(Base):
    __tablename__ = "profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand: Mapped[str]; name: Mapped[str]; type: Mapped[str]
    colors: Mapped[str] = mapped_column(default='[]')  # JSON array of hex
    min_temp: Mapped[int]; max_temp: Mapped[int]
    density: Mapped[float] = mapped_column(default=0.0)
    drying_temp: Mapped[int] = mapped_column(default=0)
    drying_time: Mapped[int] = mapped_column(default=0)
    pa: Mapped[float | None]; flow_ratio: Mapped[float | None]
    max_volumetric: Mapped[float | None]
    retraction_length: Mapped[float | None]; retraction_speed: Mapped[float | None]
    nozzle_temperature: Mapped[int | None]   # mirrors kvParam.nozzle_temperature (temp-sync target)
    printer_db_id: Mapped[str | None]        # "99xxx" once pushed
    filament_id: Mapped[str | None]          # preset filament_id once generated
    notes: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime]; updated_at: Mapped[datetime]

class Spool(Base):
    id PK; profile_id FK(indexed); color_override: str|None; remaining_pct: int = 100
    location: str = ""; rfid_tag: str|None; notes: str = ""
    created_at, updated_at

class SyncTarget(Base):
    id PK; profile_id FK; kind: str  # 'printer_db' | 'orca_preset'
    expected_hash: str|None; actual_state: str|None  # JSON strings
    status: str = "pending"  # in_sync|drift|pending|error
    last_synced_at: datetime|None

class Job(Base):
    id PK; kind: str  # push|generate_preset|apply_preset|report_presets|temp_sync|import
    payload: str = "{}"; status: str = "queued"  # queued|running|done|error|awaiting_input
    result: str|None; created_at, finished_at

class ActivityLog(Base):
    id PK; ts: datetime; action: str; detail: str = "{}"; outcome: str  # ok|warn|error
```

- [ ] **Step 1: Failing test**

```python
# tests/test_models.py
from sqlalchemy import select, func
from fm.db import init_engine, SessionLocal
from fm.models import Profile, Spool, SyncTarget, Job, ActivityLog

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
```

- [ ] **Step 2: Run → FAIL** (no `fm.db`)
- [ ] **Step 3: Implement** `fm/db.py` with `init_engine(url)` creating engine + tables (`Base.metadata.create_all`) + sessionmaker; `fm/models.py` exactly as the schema above (add imports, `datetime` defaults `lambda: datetime.now(timezone.utc)` where sensible).
- [ ] **Step 4: Run → PASS; commit** `feat: data model`

---

### Task 3: Printer DB codec (lift from cfs.py)

**Files:**
- Create: `fm/printer/__init__.py`, `fm/printer/dbcodec.py`
- Test: `tests/fixtures/sample_db.json` (41-entry stock-shaped DB, see below), `tests/test_dbcodec.py`

**Interfaces:**
- Consumes: `FmError`.
- Produces (signatures identical to cfs.py, minus `die`): `load_db(path)->dict`, `save_db(path, db)`, `find_entry(db, entry_id)->dict|None`, `find_custom_entries(db)->list`, `next_free_id(db, start)->str`, `insert_entry(db, entry)`, `patch_entry(db, entry_id, changes)->dict`, `remove_entry(db, entry_id)`, `bump_version(db, version:int)->int` (auto-increment if current ≥ version), `count_autofix(db)`, `build_entry(db, values)->dict`, `validate_entry(values)->list[str]` (warnings), `MIN_DB_ENTRIES = 30`.

- [ ] **Step 1: Copy functions from `skill/cfs.py` lines 88–315** per the lift table, replacing each `die(...)` with `raise FmError(...)`. Keep docstrings/comments verbatim.
- [ ] **Step 2: Create fixture** `tests/fixtures/sample_db.json`: stock-shaped DB `{"code":0,"msg":"","reqId":"","result":{"list":[...],"count":N,"version":"1781668740"}}` with 40 fake stock entries (id `"01001"`… pattern, minimal `base`+`kvParam` keys copied from one real entry shape documented in the vault note) + custom `99001` Sunlu PLA+ entry. Build it once by hand-copying the structure from the vault note's schema section.
- [ ] **Step 3: Port the relevant unit tests from `skill-repo/tests/test_db.py` and `test_validate.py`** (they exist and pass against identical code — adapt imports/assertions only):

```python
# tests/test_dbcodec.py (representative cases; port the rest from skill suite)
from fm.printer import dbcodec as dc

def test_bump_version_increments_above_floor():
    db = {"result": {"version": "9876543210"}}
    assert dc.bump_version(db) == 9876543211
def test_bump_version_sets_floor_from_below():
    db = {"result": {"version": "1781668740"}}
    assert dc.bump_version(db) == 9876543210
def test_patch_stock_entry_refused(sample_db):
    with pytest.raises(FmError): dc.patch_entry(sample_db, "01001", {})
def test_remove_then_count_autofix(sample_db):
    before = len(sample_db["result"]["list"])
    dc.remove_entry(sample_db, "99001"); dc.count_autofix(sample_db)
    assert sample_db["result"]["count"] == before - 1
def test_validate_warns_missing_vendor_in_name():
    warns = dc.validate_entry({"brand": "Sunlu", "name": "PLA+", "type": "PLA",
                               "minTemp": 200, "maxTemp": 230})
    assert any("vendor" in w.lower() for w in warns)
```

- [ ] **Step 4: `./scripts/test.sh` → all PASS (ported tests prove lift fidelity); commit** `feat: printer db codec lifted from cfs.py`

---

### Task 4: Printer transport (SSH/SCP/WS behind a Protocol)

**Files:**
- Create: `fm/printer/transport.py`
- Test: `tests/test_transport.py`

**Interfaces:**
- Produces: `PrinterTransport` Protocol: `run(cmd: str, timeout: int = 30) -> subprocess.CompletedProcess`, `scp_push(local: Path) -> None`, `scp_pull(local: Path) -> None`, `ws_request(method: str, params: dict) -> dict`; `SshTransport(cfg: Config)` implementing it (lift `_ssh_base_cmd`, `ssh_cmd`, `scp_pull`, `scp_push`, `ws_request`, `get_printer_status`, `check_printer_busy`, `wait_for_reboot`, `ssh_backup`, `ssh_reboot` from cfs.py lines 316–477, methods on the class, subprocess calls intact). `check_printer_busy()` returns `None` (idle) or a dict with `filename/progress/layer`.

- [ ] **Step 1: Failing tests with a fake transport**

```python
# tests/test_transport.py
class FakeTransport:
    """Test double satisfying PrinterTransport; also used by later tasks."""
    def __init__(self):
        self.commands: list[str] = []
        self.busy: dict | None = None
    def run(self, cmd, timeout=30):
        self.commands.append(cmd)
        return subprocess.CompletedProcess([], 0, "", "")
    def scp_push(self, local): self.commands.append(f"scp-push:{local}")
    def scp_pull(self, local): self.commands.append(f"scp-pull:{local}")
    def ws_request(self, method, params):
        return {"data": {"boxsInfo": []}} if not self.busy else {"status": "printing"}

def test_protocol_satisfied():
    t = FakeTransport()
    pt.check_printer_busy(t)  # must not raise
def test_busy_detection_parses_job(fake_busy_ws): ...
```

- [ ] **Step 2: Run → FAIL; Step 3: Implement `SshTransport`** as the lifted class. `ssh_reboot` keeps the fixed sequence: `sync; sync; sleep 2; reboot` with timeout 15. `wait_for_reboot` polls SSH up to 300 s then raises `FmError(6, ...)`.
- [ ] **Step 4: PASS; commit** `feat: printer transport with injectable boundary`

---

### Task 5: Push pipeline + drift primitives

**Files:**
- Create: `fm/printer/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `dbcodec`, `PrinterTransport`, `Config`.
- Produces:
  - `run_push(config, local_db_path: Path, transport, *, force_reboot=False, no_reboot=False, force_push=False, sleep=time.sleep) -> PushResult` where `PushResult = dict(status="ok"|"busy"|"no_reboot", job=dict|None)`. Order enforced per Global Constraint 1. Busy → returns `{"status": "busy", job=<details>}` WITHOUT touching the printer.
  - `verify_printer(config, transport, expected_ids: list[str]) -> dict(version_ok: bool, present: list[str], missing: list[str])` (lift `verify_version` floor check + entry presence).
  - `canonical_hash(fields: dict) -> str` — sha256 of canonical JSON (`sort_keys=True`) of identity+parameter fields only.
- Tests use `FakeTransport` from Task 4 and assert the EXACT command order:

```python
def test_push_order_and_guards(tmp_path, cfg):
    t = FakeTransport(); db_file = make_db_file(tmp_path, entries=41)
    res = run_push(cfg, db_file, t, sleep=lambda s: None)
    assert res["status"] == "ok"
    backup_idx = idx_of(t.commands, "backup")
    busy_idx   = idx_of(t.commands, "busy-check")     # busy BEFORE backup/upload
    upload_idx = idx_of(t.commands, "scp-push:")
    reboot_idx = idx_of(t.commands, "sync; sync;")
    assert busy_idx < backup_idx < upload_idx < reboot_idx

def test_refuses_small_db(tmp_path, cfg):
    t = FakeTransport(); db_file = make_db_file(tmp_path, entries=10)
    with pytest.raises(FmError): run_push(cfg, db_file, t)
    assert t.commands == []                      # nothing touched the printer

def test_busy_returns_without_upload(tmp_path, cfg):
    t = FakeTransport(busy={"filename": "x.gcode", "progress": "50%"})
    res = run_push(cfg, db_file, t)
    assert res["status"] == "busy"
    assert not any(c.startswith("scp-push") for c in t.commands)
```

Steps: failing tests → implement (lift `push_local_db` body, restructured to call `transport.run`/`scp_push`/`check_printer_busy`, keep comment "Busy check BEFORE upload") → pass → commit `feat: push pipeline with enforced ordering`.

---

### Task 6: Preset builder (lift from orca.py)

**Files:**
- Create: `fm/presets/__init__.py`, `fm/presets/builder.py`
- Test: `tests/test_builder.py`

**Interfaces:**
- Consumes: `Profile` fields.
- Produces:
  - `generate_filament_id(name: str, db_id: str = "") -> str` — MUST be `"P" + md5(f"{name} {db_id}")[:13]` (space separator; port skill's test verbatim to pin it).
  - `build_standalone_preset(profile_dict: dict, template: dict) -> dict` — lift `build_standalone_preset`, adapted input: profile dict has catalog keys (`brand/name/type/min_temp/max_temp/...`). Preserve ALL field rules from Global Constraint 8 + spec §4 critical rules (`type` removed, `SCALAR_KEYS={"filament_density"}`, kvParam skip-keys list, `default_filament_colour=[""]` for non-hex, temperature overrides `[max_temp]`).
  - `write_info_file(preset_path: Path, *, setting_id: str = "")` — writes key=value `.info` with `sync_info = ` **empty**.
  - `find_system_preset(filament_type, hint_name=None)` + `flatten_preset(path)` + `SYS_PROFILES` lifted from orca.py/preset_utils.py into `fm/presets/flattener.py`.

```python
# tests/test_builder.py (key pins; port remaining from tests/test_orca.py + test_preset_utils.py)
def test_filament_id_uses_space_separator():
    assert builder.generate_filament_id("Sunlu PLA+", "99001") == \
           "P" + hashlib.md5("Sunlu PLA+ 99001".encode()).hexdigest()[:13]

def test_generated_preset_has_no_type_field(profile_dict, user_template):
    out = builder.build_standalone_preset(profile_dict, user_template)
    assert "type" not in out
    assert out["inherits"] == "" and out["from"] == "User"
    assert out["filament_vendor"] == ["Sunlu"]      # array
    assert out["filament_density"] == "1.23"        # scalar string

def test_info_file_empty_sync_info(tmp_path):
    p = tmp_path / "X.json"; p.write_text("{}")
    builder.write_info_file(p)
    text = p.with_suffix(".info").read_text()
    assert "sync_info = \n" in text and "setting_id = \n" in text
```

Steps: port failing pins → lift code → port remaining skill tests until green → commit `feat: preset builder lifted from orca.py`.

---

### Task 7: Matching + temp-sync targets (lift from orca.py)

**Files:**
- Create: `fm/presets/matching.py`
- Test: `tests/test_matching.py`

**Interfaces:**
- Produces: `match_presets(presets: list[dict], vendor, brand_name, base_type) -> list[tuple[dict,int]]`; `temp_sync_targets(profiles_state: list[dict], presets: list[dict]) -> tuple[list[dict], list[dict]]` returning `(updates, skipped)` where each skipped item has `reason in {"ambiguous","no_match","system","missing_temp"}` and each update has `{profile_id, db_temp, orca_temp, initial_layer_warning}`.

Port `match_presets`, `find_sync_targets`, `get_preset_temp`, `get_filament_type`, `get_filament_id` verbatim (score +20/+10, type hard filter). Adapt only the input shape (`find_sync_targets` reads catalog state dicts instead of raw printer DB).

```python
# tests/test_matching.py — port skip-rule cases from skill tests plus:
def test_score_below_thirty_skipped(): ...       # reason == "no_match"
def test_system_winner_skipped(): ...            # reason == "system"
def test_missing_temp_skipped(): ...             # reason == "missing_temp"
def test_tie_skipped(): ...                      # reason == "ambiguous"
def test_score30_user_preset_wins(): ...         # update proposed
```

Steps: port failing tests → lift → green → commit `feat: preset matching + temp sync target selection`.

---

### Task 8: Job queue + activity log service

**Files:**
- Create: `fm/service/__init__.py`, `fm/service/jobs.py`
- Test: `tests/test_jobs.py`

**Interfaces:**
- Produces: `enqueue(session, kind: str, payload: dict) -> Job`; `claim_next(session) -> Job | None` (queued→running, FIFO); `complete(job, result: dict)`, `fail(job, error: dict)`, `await_input(job, options: list[str])`; `log_activity(session, action, outcome, detail)`. Jobs execute via a registry: `HANDLERS: dict[str, Callable[[Session, Config, dict], dict]]` — populated by later tasks; unknown kind → fail.

```python
def test_fifo_claim_and_complete(inmemory_session):
    j1 = jobs.enqueue(s, "noop", {}); jobs.enqueue(s, "noop", {})
    claimed = jobs.claim_next(s)
    assert claimed.id == j1.id and claimed.status == "running"
def test_handler_registry_runs(inmemory_session):
    jobs.HANDLERS["noop"] = lambda s, c, p: {"ok": True}
    jobs.enqueue(s, "noop", {}); jobs.run_pending(s, cfg)
    assert last_activity_outcome(s) == "ok"
```

Implement runner loop `run_pending(session, config)` executing queued jobs through `HANDLERS`, wrapping exceptions into `fail()` + `ActivityLog(error)`. Commit `feat: job queue`.

---

### Task 9: FastAPI app — catalog CRUD API

**Files:**
- Create: `fm/api/__init__.py`, `fm/api/app.py` (app factory), `fm/api/catalog.py` (router)
- Test: `tests/test_api_catalog.py` (httpx/TestClient against app factory with tmp SQLite)

**Interfaces:**
- Produces: REST endpoints used by UI (Task 12) and migration:
  - `GET/POST /api/profiles`, `GET/PATCH/DELETE /api/profiles/{id}`
  - `GET/POST /api/spools`, `PATCH /api/spools/{id}`, `DELETE /api/spools/{id}`
  - `POST /api/profiles/{id}/validate` → warnings list (vendor-in-name etc.)
- DELETE `/api/profiles/{id}`: 409 if referenced by spools or has `printer_db_id`, unless query param `force=true`; with `printer_db_id` set, requires `confirm=<name>` typed-token match (Rule 5/7 semantics server-side too).

```python
def test_create_profile_rejects_bad_temps(client):
    r = client.post("/api/profiles", json={..., "min_temp": 400, "max_temp": 200})
    assert r.status_code == 422
def test_delete_with_printer_entry_needs_token(client, seeded_profile):
    assert client.delete(f"/api/profiles/{pid}").status_code == 409
    assert client.delete(f"/api/profiles/{pid}?confirm=wrong").status_code == 409
    assert client.delete(f"/api/profiles/{pid}?confirm=Sunlu PLA%2B").status_code == 204
```

App factory `create_app(settings) -> FastAPI` mounts routers, Jinja2 templates dir, static. Commit `feat: catalog API`.

---

### Task 10: Sync orchestration handlers + drift status

**Files:**
- Create: `fm/service/sync_handlers.py`; Modify: `fm/api/app.py` (register)
- Test: `tests/test_sync_handlers.py`, `tests/test_api_sync.py`

**Interfaces:**
- Consumes: Tasks 3–8.
- Produces handlers registered into `HANDLERS`:
  - `push` payload `{profile_ids?: list[int], force_reboot?, no_reboot?}`: rebuild full printer DB from cache copy + catalog (insert/patch 99xxx entries via `dbcodec.build_entry`), write local JSON, `run_push`, on success update `SyncTarget(printer_db).expected_hash` + `printer_db_id`, enqueue verify.
  - `verify` payload `{}`: `scp_pull` fresh DB, floor-check + presence, update `SyncTarget.status` (`in_sync`/`error`) — cloud-sync rollback detected here → status `error` + activity warn.
  - `generate_preset` payload `{profile_id}`: build preset payload dict + `.info` content via Task 6, store as job `apply_preset` payload for the agent queue, set `SyncTarget(orca_preset).expected_hash = canonical_hash(preset_fields)`.
  - `temp_sync` payload `{profile_ids?}`: requires recent `report_presets` result; computes `temp_sync_targets`, applies `kvParam.nozzle_temperature` changes to catalog `nozzle_temperature` column, enqueues `push`.
- API: `POST /api/sync/push|preset/{profile_id}|tempsync`, `GET /api/status/drift` (per-profile matrix), `POST /api/jobs/{id}/resolve` with `{choice: "force_reboot"|"wait"|"no_reboot"}` for busy-paused pushes (job `awaiting_input`).

Busy flow test pins the spec behavior:

```python
def test_busy_push_pauses_for_user(client, monkeypatch, fake_transport_busy):
    r = client.post("/api/sync/push", json={})
    job_id = r.json()["job_id"]
    assert get_job_status(job_id) == "awaiting_input"
    r = client.post(f"/api/jobs/{job_id}/resolve", json={"choice": "force_reboot"})
    assert wait_job_done(job_id)["status"] == "done"
```

Commit `feat: sync orchestration`.

---

### Task 11: Agent API (pull endpoint, token auth)

**Files:**
- Create: `fm/api/agent.py` router
- Test: `tests/test_api_agent.py`

**Interfaces:**
- Produces:
  - `GET /api/agent/jobs` (Authorization: Bearer `agent_token`; 401 otherwise) → oldest queued `apply_preset`/`report_presets` jobs (claims them → `running`).
  - `POST /api/agent/results` body `{job_id, status: "done"|"error", result: {...}}` → completes job, logs activity. `report_presets` results stored where `temp_sync` handler reads them.

```python
def test_requires_token(client): assert client.get("/api/agent/jobs").status_code == 401
def test_poll_claims_apply_preset_job(client, seeded_preset_job):
    r = client.get("/api/agent/jobs", headers=AUTH)
    assert r.json()["jobs"][0]["kind"] == "apply_preset"
def test_result_completes_job(client, seeded_report_job): ...
```

Commit `feat: agent pull API`.

---

### Task 12: Companion agent CLI (`fm-agent`)

**Files:**
- Create: `fm/agent/__init__.py`, `fm/agent/cli.py`
- Test: `tests/test_agent_cli.py`

**Interfaces:**
- Consumes: Task 11 API contract.
- Produces console-script `fm-agent` with subcommands:
  - `poll [--base-url URL] [--token TOK]` — one shot: GET jobs; for `apply_preset`: refuse (post back error result) if `is_orcaslicer_running()` (lift from orca.py:513); else write `<name>.json` + `<name>.info` into the first-user-dir (lift `find_orca_user_dir` from orca.py:69), post done. For `report_presets`: scan first user dir only (Global Constraint 10), return per-preset `{name, filament_type, filament_vendor, nozzle_temperature, nozzle_temperature_initial_layer, inherits, from}`; post done.
  - `watch` — poll loop every 60 s (v1 simple loop).
- Agent config: `~/.config/filament-manager/agent.json` `{base_url, token}`; `--base-url/--token` override; env `FM_ORCASLICER_CONFIG_DIR` overrides config dir discovery.

Tests hit `poll` end-to-end against a stub HTTP server (or monkeypatched requests session) using a temp OrcaSlicer tree:

```python
def test_apply_preset_writes_files_and_reports_done(stub_api, tmp_orca_tree): ...
def test_refuses_while_orcaslicer_running(monkeypatch, stub_api):
    monkeypatch.setattr(agent.cli, "is_orcaslicer_running", lambda: True)
    ... # job completed with error result "OrcaSlicer running"
def test_report_scans_first_user_dir_only(tmp_orca_tree_with_two_uuid_dirs): ...
```

Commit `feat: fm-agent companion`.

---

### Task 13: UI — layout, dashboard, profiles, spools (designer role)

**Files:**
- Create: `fm/api/ui.py` (page routes), `fm/templates/base.html`, `dashboard.html`, `profiles.html`, `profile_form.html`, `spools.html`, `static/app.css`
- Test: `tests/test_ui.py` (content-marker smoke tests only; visual design delegated)

Per AGENTS.md: visual/layout refinement of these pages is executed by the **omp designer agent** after this task lands functional pages. Plan fixes structure + behavior; designer owns aesthetics.

Routes render server-side; interactivity via HTMX (`hx-post` on forms, `hx-target` closest container):
- `/` dashboard: spool count by profile (top 10), drift matrix (reuse `GET /api/status/drift` data inline), last 20 activity rows.
- `/profiles` list + filter (`?type=&brand=`), `/profiles/new`, `/profiles/{id}/edit` — form groups: identity / temps / extrusion / drying; inline validation warnings from `/api/profiles/{id}/validate`.
- `/spools` list + filters, add form (profile `<select>`, color `<input type=color>`, remaining slider), HTMX inline remaining-% patch.

Smoke tests:

```python
def test_dashboard_renders(client): assert b"Spools" in client.get("/").content
def test_profile_form_groups(client): ...  # identity/temps/extrusion/drying headings present
def test_spool_create_via_form(client, seeded_profile): ...  # POST /spools/form → redirect + row visible
```

Commit `feat: functional UI pages (visual pass pending designer)`.

---

### Task 14: UI — sync page + migration wizard

**Files:**
- Create: `fm/templates/sync.html`, `migration.html`; Modify: `fm/api/ui.py`
- Test: `tests/test_ui_sync.py`

- `/sync`: drift table per profile (printer_db / orca_preset columns, colored status), buttons wired to Task 10 endpoints (`hx-post`), busy dialog block rendering `awaiting_input` jobs with the three choice buttons (`wait` / `force_reboot` / `no_reboot`), job progress area polling `/api/jobs?since=`.
- `/migration` (one-time): step 1 enqueue `import_scan` job (handler: `scp_pull` printer DB → propose catalog rows from all 99xxx entries + request `report_presets` via agent); step 2 renders proposal table (checkboxes, prefilled from `convert_orca_to_db_values`-style mapping lifted from cfs.py:230); step 3 POST selected rows → creates Profiles + SyncTargets with existing `printer_db_id` and preserved `filament_id`s (Cloud-collision avoidance).

```python
def test_migration_proposes_99001_to_99004(fake_transport, client): ...
def test_migration_import_preserves_filament_id(client, proposal): ...
```

Commit `feat: sync + migration UI`.

---

### Task 15: Local dev facility scripts

**Files:**
- Create: `scripts/dev-start.sh`, `scripts/dev-stop.sh`; Modify: `scripts/test.sh` if needed
- Test: manual verification step (script exits 0, app answers)

```bash
#!/usr/bin/env bash
# dev-start.sh — start app locally with isolated state
set -euo pipefail; cd "$(dirname "$0")/.."
mkdir -p .local
export FM_DATABASE_URL="sqlite:///.local/filaments.db"
export FM_PRINTER_IP="${FM_PRINTER_IP:-192.168.0.101}"
export FM_SSH_USER="${FM_SSH_USER:-root}" FM_SSH_PASSWORD="${FM_SSH_PASSWORD:-}"
export FM_DB_REMOTE_PATH="/mnt/UDISK/creality/userdata/box/material_database.json"
export FM_AGENT_TOKEN="devtoken"
nohup python -m uvicorn fm.api.app:create_app --factory --port 8404 >.local/server.log 2>&1 &
echo $! > .local/server.pid
for i in $(seq 1 30); do curl -sf http://127.0.0.1:8404/api/version >/dev/null && exit 0; sleep 1; done
echo "server did not become ready"; tail -50 .local/server.log; exit 1
```

`dev-stop.sh`: kill PID from `.local/server.pid`, remove file, exit non-zero if it wasn't running. App must expose `GET /api/version` → `{"version": "<git-tag-or-0.1.0>"}` (add tiny route in `fm/api/app.py` reading package metadata). Verify: start → curl health → stop → curl fails. Commit `feat: dev lifecycle scripts`.

---

### Task 16: Docker image + release/deploy scripts

**Files:**
- Create: `Dockerfile`, `docker/entrypoint.sh`, `scripts/release.sh`, `scripts/deploy-unraid.sh`, `deploy/filament-manager.xml`
- Test: build + run container locally answering `/api/version`

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends sshpass openssh-client \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY fm ./fm
RUN pip install --no-cache-dir .
COPY docker/entrypoint.sh /entrypoint.sh
EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "fm.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

- `release.sh <version>`: require clean git tree → `git tag vX.Y.Z` → `docker build -t filament-manager:X.Y.Z .` → `docker save | gzip > dist/filament-manager-X.Y.Z.tar.gz` → print checksum.
- `deploy-unraid.sh <version>`: `scp dist/*.tar.gz root@unraid:/mnt/user/appdata/filament-manager/` → ssh: `docker load`, update XML template path `deploy/filament-manager.xml` → `/boot/config/plugins/dockerMan/templates-user/`, `docker stop/rm filament-manager` + `docker run -d --name filament-manager --env-file ... -v /mnt/user/appdata/filament-manager:/data -p 8404:8000 filament-manager:X.Y.Z` → poll `http://192.168.0.31:8404/api/version`.
- XML template carries the `FM_*` env vars from Global Constraint 11 with data volume `/mnt/user/appdata/filament-manager`.

Verify: `docker build`, run container with dummy env, curl `/api/version`. Commit `feat: container packaging + unraid deploy`.

---

### Task 17: README + docs sync

**Files:**
- Create: `README.md`
- Update: BrainVault note `projects/homeassistant/filament-manager.md` (via knowledge-vault MCP at execution time)

README sections: purpose, architecture diagram (copy spec §2), setup (env vars table from Constraint 11), scripts reference, agent install (`pipx install` + `~/.config/filament-manager/agent.json`), migration how-to, iron-rules summary linking spec. Commit `docs: readme`. Vault note records deployment + decisions (documentation-sync convention).

---

## Self-Review Notes

- Spec coverage: §2 architecture → Tasks 1–16; §3 model → 2; §4 sync engine → 3–7, 10; §5 agent → 11, 12; §6 UI → 13, 14 (+designer pass); §7 errors → 8, 10; §8 testing/scripts → throughout + 15; §9 deploy → 16; §10 AGENTS.md exists; §11 open items resolved inside tasks 12 (watch ships), 14 (migration preserves filament_ids), 13/14 (designer decides dialogs).
- Type consistency: names pinned via Interfaces blocks (`run_push`, `canonical_hash`, `temp_sync_targets`, `HANDLERS`, `create_app`, `fm-agent poll`).
- Deferred to designer (deliberate, per AGENTS.md): pixel-level UI styling, HTMX dialog polish.
