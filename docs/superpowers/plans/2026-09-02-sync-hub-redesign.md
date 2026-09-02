# Sync Hub Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate the /sync page into a two-column Sync Hub, introduce direction-clear naming, merge /migration + /import-orca into /import, and add per-profile push/pull endpoints — all without changing sync logic or models.

**Architecture:** UI-only redesign on top of existing FastAPI + Jinja2/HTMX. New API endpoints `POST /api/sync/pull` and `POST /api/sync/pull/{profile_id}` wrap the existing `refresh_preset_drift(adopt=True)` function. The existing `POST /api/sync/push` already accepts `profile_ids` in its JSON body — no new push endpoint needed. One minimal logic extension: `refresh_preset_drift` gains an optional `profile_id` keyword parameter (defaults to `None` = all profiles, preserving existing behavior).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy + SQLite, Jinja2 + HTMX, pytest with TestClient

**Spec:** `docs/superpowers/specs/2026-09-02-sync-hub-redesign-design.md`

## Global Constraints

- **No model migrations** (AGENTS.md Rule 6): no new columns, no new tables. All data stays in existing `Profile`, `SyncTarget`, `Job`, `ActivityLog` structures.
- **OrcaSlicer is the editor** (AGENTS.md Rule 5): no print-settings fields in Profile model or edit form. Print settings come from Orca Cloud via `SyncTarget.actual_state`, displayed read-only.
- **Iron Rules unchanged**: version ≥ 9876543210 + reboot mandatory after every printer-DB write; busy-check before upload; refuse push when DB < 30 entries; backup rotation keep 5; double-confirm on delete; custom IDs only in 99xxx range.
- **Scripts over ad-hoc** (AGENTS.md Rule 2): use `scripts/test.sh` for tests, `scripts/dev-start.sh` / `scripts/dev-stop.sh` for dev server. Never re-type command sequences.
- **Dev server restart** (AGENTS.md Rule 4): after changing UI routes in `fm/api/ui.py`, restart the dev server via `scripts/dev-stop.sh && scripts/dev-start.sh`. Templates and CSS are picked up without restart.
- **"Sync" naming rule** (Spec §3.1): "Sync" appears only as page name ("Sync Hub") and nav label. Every action button must contain a direction verb: "Push" (Catalog → target), "Pull" (source → Catalog), or "Backup" (→ GitHub). No button, badge, or action label uses "Sync" without direction. The old labels "Sync from Orca", "Sync now", "Push all profiles" must not appear anywhere in templates.
- **Designer role for UI** (AGENTS.md Rule 1): the `sync_hub.html` template layout follows the approved M3 mockup at `fm/api/static/mockups/m3.html`. Visual/layout refinement uses the omp designer agent.
- **No old labels**: "Sync from Orca", "Sync now", "Push all profiles", "Generate preset" (as button text) must not appear in any template after this plan is complete.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `fm/service/sync_handlers.py` | Modify line 813 | Add `profile_id` keyword param to `refresh_preset_drift` |
| `fm/api/sync.py` | Modify after line 133 | Add `POST /sync/pull` and `POST /sync/pull/{profile_id}` endpoints |
| `fm/api/app.py` | Modify line 79 | Register `status_class` Jinja2 filter |
| `fm/api/ui.py` | Modify lines 727-746, 183-188, 863-1147 | Change sync_page to render `sync_hub.html`; remove `profiles_sync_from_orca`; migrate `/migration` + `/import-orca` routes to `/import` |
| `fm/api/templates/sync_hub.html` | Create | New Sync Hub template (M3 two-column layout) |
| `fm/api/templates/sync.html` | Delete | Replaced by `sync_hub.html` |
| `fm/api/templates/import.html` | Create | Tab shell with two tab contents (printer DB + Orca Cloud) |
| `fm/api/templates/import_printer.html` | Create | Tab 1 content (from `migration.html` body) |
| `fm/api/templates/import_orca_tab.html` | Create | Tab 2 content (from `import_orca.html` body) |
| `fm/api/templates/migration.html` | Delete | Content moved to `import_printer.html` |
| `fm/api/templates/import_orca.html` | Delete | Content moved to `import_orca_tab.html` |
| `fm/api/templates/base.html` | Modify lines 37-41 | Nav: remove Migration + Orca Cloud Import, add Import |
| `fm/api/templates/profiles.html` | Modify lines 5-9, 34-46 | Remove "Sync from Orca" button + toast; add "Sync status →" link |
| `fm/api/templates/orca_fragment.html` | Modify lines 40, 51 | Change "Sync from Orca" → "Pull from Cloud" |
| `tests/test_api_sync.py` | Modify | Add `TestPullEndpoint` class with per-profile and bulk pull tests |
| `tests/test_ui_sync.py` | Modify | Update sync page tests for new labels; add import route tests |
| `tests/test_ui_import_orca.py` | Modify | Update import-orca tests for new `/import` routes |
| `tests/test_ui.py` | Modify | Add nav tests (6 items, "Import" present, old labels absent) |
| `tests/test_status_class_filter.py` | Create | Test the `status_class` Jinja2 filter |
| `docs/sync-workflow-analysis.md` | Modify | Update UI-Map, V1/V2/V6/V7/V11 status, route references |
| `AGENTS.md` | Modify | Update Key Files table if template list changes |

---

### Task 1: Extend `refresh_preset_drift` with `profile_id` parameter

**Files:**
- Modify: `fm/service/sync_handlers.py:813`
- Test: `tests/test_sync_handlers.py`

**Interfaces:**
- Consumes: `refresh_preset_drift(session, *, adopt=False)` — existing signature
- Produces: `refresh_preset_drift(session, *, adopt=False, profile_id=None)` — new optional keyword param. When `profile_id` is not None, only that profile's `orca_preset` SyncTarget is processed. When None, all targets processed (existing behavior preserved).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_sync_handlers.py`:

```python
class TestRefreshPresetDriftProfileId:
    """Tests for the profile_id parameter of refresh_preset_drift."""

    def test_profile_id_filters_to_one_profile(self, inmemory_session, monkeypatch):
        """When profile_id is set, only that profile's orca_preset target
        is processed — other profiles' targets remain untouched."""
        from fm.models import Profile, SyncTarget
        from fm.service.sync_handlers import refresh_preset_drift

        # Create two profiles with orca_preset targets
        p1 = Profile(brand="Sunlu", name="Sunlu PLA+", type="PLA",
                     min_temp=205, max_temp=215, density=1.24,
                     nozzle_temperature=220, filament_id="Paaa111")
        p2 = Profile(brand="eSUN", name="eSUN PETG", type="PETG",
                     min_temp=230, max_temp=240, density=1.27,
                     nozzle_temperature=240, filament_id="Pbbb222")
        inmemory_session.add_all([p1, p2])
        inmemory_session.flush()

        t1 = SyncTarget(profile_id=p1.id, kind="orca_preset",
                        cloud_id="cloud-1", expected_hash="hash_old_1",
                        status="in_sync")
        t2 = SyncTarget(profile_id=p2.id, kind="orca_preset",
                        cloud_id="cloud-2", expected_hash="hash_old_2",
                        status="in_sync")
        inmemory_session.add_all([t1, t2])
        inmemory_session.flush()

        # Mock the Orca client to return presets for both profiles
        from unittest.mock import MagicMock
        fake_client = MagicMock()
        fake_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [
                {"id": "cloud-1", "content": {
                    "type": "filament", "name": "Sunlu PLA+",
                    "filament_id": "Paaa111", "filament_vendor": ["Sunlu"],
                    "filament_type": ["PLA"], "nozzle_temperature": ["210"],
                }},
                {"id": "cloud-2", "content": {
                    "type": "filament", "name": "eSUN PETG",
                    "filament_id": "Pbbb222", "filament_vendor": ["eSUN"],
                    "filament_type": ["PETG"], "nozzle_temperature": ["250"],
                }},
            ],
            "deletes": [],
        }
        monkeypatch.setattr(
            "fm.service.sync_handlers._orca_client_factory",
            lambda: fake_client,
        )

        # Call with profile_id=p1.id, adopt=True
        refresh_preset_drift(inmemory_session, adopt=True, profile_id=p1.id)

        # p1 should be adopted (nozzle_temp updated to 210)
        # p2 should be untouched (nozzle_temp stays 240)
        inmemory_session.refresh(p1)
        inmemory_session.refresh(p2)
        assert p1.nozzle_temperature == 210
        assert p2.nozzle_temperature == 240  # unchanged

    def test_profile_id_none_processes_all(self, inmemory_session, monkeypatch):
        """When profile_id is None (default), all profiles are processed
        — same as existing behavior."""
        from fm.models import Profile, SyncTarget
        from fm.service.sync_handlers import refresh_preset_drift

        p1 = Profile(brand="Sunlu", name="Sunlu PLA+", type="PLA",
                     min_temp=205, max_temp=215, density=1.24,
                     nozzle_temperature=220, filament_id="Paaa111")
        p2 = Profile(brand="eSUN", name="eSUN PETG", type="PETG",
                     min_temp=230, max_temp=240, density=1.27,
                     nozzle_temperature=240, filament_id="Pbbb222")
        inmemory_session.add_all([p1, p2])
        inmemory_session.flush()

        t1 = SyncTarget(profile_id=p1.id, kind="orca_preset",
                        cloud_id="cloud-1", expected_hash="hash_old_1",
                        status="in_sync")
        t2 = SyncTarget(profile_id=p2.id, kind="orca_preset",
                        cloud_id="cloud-2", expected_hash="hash_old_2",
                        status="in_sync")
        inmemory_session.add_all([t1, t2])
        inmemory_session.flush()

        from unittest.mock import MagicMock
        fake_client = MagicMock()
        fake_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [
                {"id": "cloud-1", "content": {
                    "type": "filament", "name": "Sunlu PLA+",
                    "filament_id": "Paaa111", "filament_vendor": ["Sunlu"],
                    "filament_type": ["PLA"], "nozzle_temperature": ["210"],
                }},
                {"id": "cloud-2", "content": {
                    "type": "filament", "name": "eSUN PETG",
                    "filament_id": "Pbbb222", "filament_vendor": ["eSUN"],
                    "filament_type": ["PETG"], "nozzle_temperature": ["250"],
                }},
            ],
            "deletes": [],
        }
        monkeypatch.setattr(
            "fm.service.sync_handlers._orca_client_factory",
            lambda: fake_client,
        )

        refresh_preset_drift(inmemory_session, adopt=True)

        inmemory_session.refresh(p1)
        inmemory_session.refresh(p2)
        assert p1.nozzle_temperature == 210
        assert p2.nozzle_temperature == 250
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sync_handlers.py::TestRefreshPresetDriftProfileId -v`
Expected: FAIL with `TypeError: refresh_preset_drift() got an unexpected keyword argument 'profile_id'`

- [ ] **Step 3: Write minimal implementation**

Modify `fm/service/sync_handlers.py` line 813. Change the function signature and add a filter on the targets query:

```python
def refresh_preset_drift(
    session: Session, *, adopt: bool = False, profile_id: int | None = None,
) -> None:
```

Then at line 855, change the targets query to filter by `profile_id` when provided:

```python
    # Process existing orca_preset targets only
    stmt = select(SyncTarget).where(SyncTarget.kind == "orca_preset")
    if profile_id is not None:
        stmt = stmt.where(SyncTarget.profile_id == profile_id)
    targets = list(session.scalars(stmt))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sync_handlers.py::TestRefreshPresetDriftProfileId -v`
Expected: PASS

- [ ] **Step 5: Run full test suite to verify no regressions**

Run: `scripts/test.sh`
Expected: All 392+ tests PASS (existing behavior unchanged when `profile_id=None`)

- [ ] **Step 6: Commit**

```bash
git add fm/service/sync_handlers.py tests/test_sync_handlers.py
git commit -m "feat: add profile_id param to refresh_preset_drift for per-profile pull"
```

---

### Task 2: Add `POST /api/sync/pull` and `POST /api/sync/pull/{profile_id}` endpoints

**Files:**
- Modify: `fm/api/sync.py` (after line 133, before `drift_status` at line 136)
- Test: `tests/test_api_sync.py`

**Interfaces:**
- Consumes: `refresh_preset_drift(session, *, adopt=False, profile_id=None)` from Task 1
- Produces: `POST /api/sync/pull` → `{"status": "ok"}` (bulk adopt); `POST /api/sync/pull/{profile_id}` → `{"status": "ok", "profile_id": id}` (per-profile adopt)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api_sync.py` after the `TestPresetEndpoint` class (before `TestDriftStatus`):

```python
# ---------------------------------------------------------------------------
# Pull endpoints (Cloud → Catalog, adopt=True)
# ---------------------------------------------------------------------------

class TestPullEndpoint:
    def test_bulk_pull_returns_ok(self, client, fake_orca_client):
        """POST /api/sync/pull adopts all profiles from Cloud."""
        # Create a profile with an orca_preset target
        p = _make_profile(client)
        with SessionLocal() as s:
            from fm.models import SyncTarget
            target = SyncTarget(
                profile_id=p["id"], kind="orca_preset",
                cloud_id="test-cloud-uuid", expected_hash="old_hash",
                status="drift",
            )
            s.add(target)
            s.commit()

        # Set up Cloud to return a matching preset
        fake_orca_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [{
                "id": "test-cloud-uuid",
                "content": {
                    "type": "filament", "name": "Sunlu PLA+",
                    "filament_id": p.get("filament_id", "Ptest123"),
                    "filament_vendor": ["Sunlu"],
                    "filament_type": ["PLA"],
                    "nozzle_temperature": ["210"],
                },
            }],
            "deletes": [],
        }

        r = client.post("/api/sync/pull")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_per_profile_pull_returns_ok(self, client, fake_orca_client):
        """POST /api/sync/pull/{profile_id} adopts one profile from Cloud."""
        p = _make_profile(client)
        with SessionLocal() as s:
            from fm.models import SyncTarget
            target = SyncTarget(
                profile_id=p["id"], kind="orca_preset",
                cloud_id="test-cloud-uuid", expected_hash="old_hash",
                status="drift",
            )
            s.add(target)
            s.commit()

        fake_orca_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [{
                "id": "test-cloud-uuid",
                "content": {
                    "type": "filament", "name": "Sunlu PLA+",
                    "filament_id": p.get("filament_id", "Ptest123"),
                    "filament_vendor": ["Sunlu"],
                    "filament_type": ["PLA"],
                    "nozzle_temperature": ["210"],
                },
            }],
            "deletes": [],
        }

        r = client.post(f"/api/sync/pull/{p['id']}")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert r.json()["profile_id"] == p["id"]

    def test_per_profile_pull_404_for_missing_profile(self, client):
        """POST /api/sync/pull/9999 returns 404."""
        r = client.post("/api/sync/pull/9999")
        assert r.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api_sync.py::TestPullEndpoint -v`
Expected: FAIL with `404` or `AttributeError` (endpoints don't exist yet)

- [ ] **Step 3: Write minimal implementation**

Add to `fm/api/sync.py` after line 133 (after `sync_backup_github`, before `drift_status`):

```python
@router.post("/sync/pull")
def sync_pull_all(db: DbSession) -> dict:
    """Pull all Orca Cloud presets and adopt (replaces /profiles/sync-from-orca)."""
    refresh_preset_drift(db, adopt=True)
    db.flush()
    return {"status": "ok"}


@router.post("/sync/pull/{profile_id}")
def sync_pull_profile(profile_id: int, db: DbSession) -> dict:
    """Pull a single profile's preset from Orca Cloud (adopt=True)."""
    profile = db.get(Profile, profile_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="profile not found",
        )
    refresh_preset_drift(db, adopt=True, profile_id=profile_id)
    db.flush()
    return {"status": "ok", "profile_id": profile_id}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_api_sync.py::TestPullEndpoint -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `scripts/test.sh`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add fm/api/sync.py tests/test_api_sync.py
git commit -m "feat: add POST /api/sync/pull and /api/sync/pull/{profile_id} endpoints"
```

---

### Task 3: Add `status_class` Jinja2 filter

**Files:**
- Modify: `fm/api/app.py:79` (after `from_json` filter registration)
- Test: `tests/test_status_class_filter.py`

**Interfaces:**
- Consumes: nothing
- Produces: Jinja2 filter `status_class(value: str) -> str` that maps `"drift"` → `"drifted"` and `"done"` → `"completed"`. All other values pass through unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/test_status_class_filter.py`:

```python
"""Tests for the status_class Jinja2 filter."""
from fm.api.app import _status_class_map, status_class


def test_drift_maps_to_drifted():
    assert status_class("drift") == "drifted"


def test_done_maps_to_completed():
    assert status_class("done") == "completed"


def test_in_sync_passes_through():
    assert status_class("in_sync") == "in_sync"


def test_pending_passes_through():
    assert status_class("pending") == "pending"


def test_error_passes_through():
    assert status_class("error") == "error"


def test_modified_passes_through():
    assert status_class("modified") == "modified"


def test_missing_passes_through():
    assert status_class("missing") == "missing"


def test_new_passes_through():
    assert status_class("new") == "new"


def test_stock_passes_through():
    assert status_class("stock") == "stock"


def test_queued_passes_through():
    assert status_class("queued") == "queued"


def test_running_passes_through():
    assert status_class("running") == "running"


def test_awaiting_input_passes_through():
    assert status_class("awaiting_input") == "awaiting_input"


def test_map_contains_only_two_entries():
    assert _STATUS_CLASS_MAP == {"drift": "drifted", "done": "completed"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_status_class_filter.py -v`
Expected: FAIL with `ImportError: cannot import name '_status_class_map'` or `status_class`

- [ ] **Step 3: Write minimal implementation**

Add to `fm/api/app.py` before the `create_app` function (after `_package_version`):

```python
_STATUS_CLASS_MAP = {
    "drift": "drifted",
    "done": "completed",
}


def status_class(value: str) -> str:
    """Map internal status values to app.css badge class names.

    Only ``drift`` → ``drifted`` and ``done`` → ``completed`` need mapping;
    all other status values (in_sync, pending, error, modified, missing,
    new, stock, queued, running, awaiting_input) have direct CSS classes.
    """
    return _STATUS_CLASS_MAP.get(value, value)
```

Then in `create_app`, after line 79 (`from_json` filter), add:

```python
    templates.env.filters["status_class"] = status_class
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_status_class_filter.py -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `scripts/test.sh`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add fm/api/app.py tests/test_status_class_filter.py
git commit -m "feat: add status_class Jinja2 filter for CSS badge mapping"
```

---

### Task 4: Create `sync_hub.html` template and update `GET /sync` route

**Files:**
- Create: `fm/api/templates/sync_hub.html`
- Delete: `fm/api/templates/sync.html`
- Modify: `fm/api/ui.py:727-746` (sync_page route — change template name)
- Test: `tests/test_ui_sync.py` (update `TestSyncPage` assertions)

**Interfaces:**
- Consumes: `status_class` filter from Task 3; template context `{drift, awaiting_jobs, recent_jobs, github_configured}` (same as current sync_page)
- Produces: `GET /sync` renders `sync_hub.html` with M3 two-column layout

- [ ] **Step 1: Write the failing test**

Update `tests/test_ui_sync.py` `TestSyncPage` class. Replace `test_sync_page_renders` and `test_sync_page_shows_actions`:

```python
class TestSyncPage:
    def test_sync_page_renders(self, client):
        r = client.get("/sync")
        assert r.status_code == 200
        assert b"Sync Hub" in r.content
        assert b"drift-table" in r.content

    def test_sync_page_has_direction_clear_labels(self, client):
        """No old labels; new direction-clear labels present."""
        r = client.get("/sync")
        assert b"Push to Printer" in r.content
        assert b"Push to Cloud" in r.content
        assert b"Pull from Cloud" in r.content
        assert b"Backup to GitHub" in r.content
        # Old labels must NOT appear
        assert b"Sync from Orca" not in r.content
        assert b"Push all" not in r.content
        assert b"Sync now" not in r.content

    def test_sync_page_has_target_cards(self, client):
        """M3 layout: three target cards (Printer DB, Orca Cloud, Backup)."""
        r = client.get("/sync")
        assert b"Printer DB" in r.content
        assert b"Orca Cloud" in r.content
        assert b"GitHub backup" in r.content

    def test_sync_page_job_progress_polling(self, client):
        r = client.get("/sync")
        assert b"job-progress" in r.content
        assert b"hx-get" in r.content
        assert b"/sync/progress" in r.content

    def test_sync_progress_fragment(self, client):
        r = client.get("/sync/progress?since=0")
        assert r.status_code == 200
        assert b"No new jobs" in r.content
```

Keep the existing busy-dialog and resolve tests (`test_sync_page_busy_dialog_when_awaiting`, `test_sync_resolve_*`) unchanged — they test behavior, not labels.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ui_sync.py::TestSyncPage -v`
Expected: FAIL — `test_sync_page_renders` fails because "Sync Hub" not in content (old template has "Device synchronization"); `test_sync_page_has_direction_clear_labels` fails because "Push to Printer" not in content.

- [ ] **Step 3: Create `sync_hub.html` template**

This is a large template. Use the omp designer agent to create it from the M3 mockup (`fm/api/static/mockups/m3.html`), adapted to Jinja2 template syntax with the real data context. The template must:

1. Extend `base.html` with `{% block content %}`
2. Page header: eyebrow "Connections", h1 "Sync Hub", description "Keep the catalog, printer database, Orca Cloud, and GitHub backup aligned."
3. Two-column layout: left column (2fr) with drift table + busy dialog, right column (1fr) with three target cards
4. Drift table: `{% for d in drift %}` rows with `status-badge status-{{ d.printer_db.status | status_class }}` and `status-{{ d.orca_preset.status | status_class }}`. Actions column with text buttons:
   - "Push to Printer" — only if `d.printer_db_id` is set: `hx-post="/api/sync/push"` with `hx-vals='{"profile_ids": [{{ d.profile_id }}]}'`
   - If no `printer_db_id`: `<span class="muted">Not linked</span>`
   - "Push to Cloud": `hx-post="/api/sync/preset/{{ d.profile_id }}"`
   - "Pull from Cloud": `hx-post="/api/sync/pull/{{ d.profile_id }}"`
5. Busy dialog: `{% if awaiting_jobs %}` — same structure as current sync.html, with Force reboot / Wait / No reboot buttons
6. Right column cards:
   - Printer DB card: status summary, "Push to Printer" button (`hx-post="/api/sync/push"`)
   - Orca Cloud card: status summary, description, "Pull from Cloud (all)" button (`hx-post="/api/sync/pull"`)
   - Backup card: `{% if github_configured %}` "Backup to GitHub" button (`hx-post="/api/sync/backup-github"`), auto-trigger note
7. Job queue: full-width panel below columns, `hx-get="/sync/progress?since=0"`, `hx-trigger="every 3s"`, `hx-swap="innerHTML"`. Job cards with `status-{{ job.status | status_class }}` badges.

Use inline `<style>` for the 2fr/1fr grid split:
```html
<style>
.sync-hub-grid { display: grid; grid-template-columns: 2fr 1fr; gap: 24px; margin-bottom: 24px; }
.sync-hub-left { display: flex; flex-direction: column; gap: 24px; }
.sync-hub-right { display: flex; flex-direction: column; gap: 18px; }
.target-card { padding: 24px 28px; }
.target-card .eyebrow { margin-bottom: 8px; }
.target-card h2 { margin-bottom: 8px; }
.target-card p { margin: 0 0 16px; font-size: 13px; }
.target-card .button { width: 100%; }
.target-status { display: flex; gap: 12px; margin-bottom: 16px; font-size: 12px; color: var(--text-secondary); }
.target-status span { display: inline-flex; align-items: center; gap: 5px; }
@media (max-width: 1100px) { .sync-hub-grid { grid-template-columns: 1fr; } }
</style>
```

- [ ] **Step 4: Update `GET /sync` route in `fm/api/ui.py`**

Change line 739 from `"sync.html"` to `"sync_hub.html"`:

```python
    return _templates(request).TemplateResponse(
        request, "sync_hub.html",
        {
            "drift": drift,
            "awaiting_jobs": awaiting_jobs,
            "recent_jobs": recent_jobs,
            "github_configured": load_github_config() is not None,
        },
    )
```

- [ ] **Step 5: Delete old `sync.html`**

```bash
rm fm/api/templates/sync.html
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_ui_sync.py::TestSyncPage -v`
Expected: PASS

- [ ] **Step 7: Run full test suite**

Run: `scripts/test.sh`
Expected: All tests PASS (busy-dialog tests still pass — they check for `busy-dialog` id and button text, not the page-level template)

- [ ] **Step 8: Commit**

```bash
git add fm/api/templates/sync_hub.html fm/api/ui.py tests/test_ui_sync.py
git rm fm/api/templates/sync.html
git commit -m "feat: replace sync.html with sync_hub.html (M3 two-column layout)"
```

---

### Task 5: Merge `/migration` + `/import-orca` into `/import` with tabs

**Files:**
- Create: `fm/api/templates/import.html`
- Create: `fm/api/templates/import_printer.html`
- Create: `fm/api/templates/import_orca_tab.html`
- Delete: `fm/api/templates/migration.html`
- Delete: `fm/api/templates/import_orca.html`
- Modify: `fm/api/ui.py:863-1147` (migrate routes)
- Modify: `fm/api/templates/base.html:37-41` (nav)
- Test: `tests/test_ui_sync.py` (migration tests → import route tests)
- Test: `tests/test_ui_import_orca.py` (import-orca tests → import route tests)

**Interfaces:**
- Consumes: `_latest_import_scan`, `_pull_orca_presets`, `_unmatched_orca_presets` from `fm/api/ui.py`
- Produces: `GET /import?tab=printer|orca`, `POST /import/printer/scan`, `POST /import/printer/apply`, `POST /import/orca/apply`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ui_sync.py` after the `TestSyncPage` class (replacing `TestMigration`):

```python
# ---------------------------------------------------------------------------
# Import page (merged migration + import-orca)
# ---------------------------------------------------------------------------

class TestImportPage:
    def test_import_page_default_tab(self, client):
        """GET /import defaults to printer tab."""
        r = client.get("/import")
        assert r.status_code == 200
        assert b"From Printer DB" in r.content
        assert b"From Orca Cloud" in r.content

    def test_import_page_printer_tab(self, client):
        """GET /import?tab=printer shows printer DB import content."""
        r = client.get("/import?tab=printer")
        assert r.status_code == 200
        assert b"Start import scan" in r.content

    def test_import_page_orca_tab(self, client):
        """GET /import?tab=orca shows Orca Cloud import content."""
        r = client.get("/import?tab=orca")
        assert r.status_code == 200

    def test_old_migration_route_404(self, client):
        """GET /migration returns 404 (route removed)."""
        r = client.get("/migration")
        assert r.status_code == 404

    def test_old_import_orca_route_404(self, client):
        """GET /import-orca returns 404 (route removed)."""
        r = client.get("/import-orca")
        assert r.status_code == 404

    def test_import_printer_scan_redirects(self, client):
        """POST /import/printer/scan enqueues import_scan and redirects."""
        r = client.post("/import/printer/scan", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/import?tab=printer"

    def test_import_printer_scan_proposes_99001_to_99004(self, client):
        """After scan, /import?tab=printer shows proposals."""
        client.post("/import/printer/scan")
        r = client.get("/import?tab=printer")
        assert r.status_code == 200
        assert b"99001" in r.content
        assert b"Sunlu" in r.content

    def test_import_printer_apply_creates_profiles(self, client):
        """POST /import/printer/apply with selected entries creates profiles."""
        client.post("/import/printer/scan")
        r = client.post("/import/printer/apply", data={
            "include": ["99001", "99003"],
        }, follow_redirects=False)
        assert r.status_code == 303

        with SessionLocal() as s:
            profiles = list(s.scalars(select(Profile).order_by(Profile.id)))
            assert len(profiles) == 2
            ids = {p.printer_db_id for p in profiles}
            assert ids == {"99001", "99003"}

    def test_import_printer_apply_no_scan_404(self, client):
        """POST /import/printer/apply without a prior scan → 404."""
        r = client.post("/import/printer/apply", data={"include": "99001"})
        assert r.status_code == 404
```

Also update `tests/test_ui_import_orca.py` — change route references from `/import-orca` to `/import/orca`:

In `TestImportOrcaApply`, change:
```python
client.post("/import-orca/apply", data={"include": ["Pabc123def456"]},
            follow_redirects=False)
```
to:
```python
client.post("/import/orca/apply", data={"include": ["Pabc123def456"]},
            follow_redirects=False)
```

In `TestSyncFromOrcaAdopt`, change:
```python
client.post("/import-orca/apply", data={"include": ["Pabc123def456"]},
            follow_redirects=False)
```
to:
```python
client.post("/import/orca/apply", data={"include": ["Pabc123def456"]},
            follow_redirects=False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ui_sync.py::TestImportPage -v`
Expected: FAIL with `404` (route `/import` doesn't exist yet)

- [ ] **Step 3: Create templates**

Create `fm/api/templates/import_printer.html` — extract the body content from `migration.html` (lines 4-10, the `<section id="migration-page">` content) but change form actions:
- `/migration/import` → `/import/printer/scan`
- `/migration/apply` → `/import/printer/apply`
- Redirect target `/migration?applied=1` → `/import?tab=printer&applied=1`

This is a partial template (no `extends base.html`) — it's included by `import.html`.

Create `fm/api/templates/import_orca_tab.html` — extract the body content from `import_orca.html` (lines 4-108, the `<section id="import-orca">` content + script) but:
- Remove the "Sync now" button and its form (lines 24-27) — replace with a `<p class="muted">Connect your Orca Cloud account in <a href="/settings">Settings</a>, then use Pull from Cloud in the <a href="/sync">Sync Hub</a>.</p>`
- Change `/import-orca/apply` → `/import/orca/apply`
- Redirect target `/import-orca?applied=1` → `/import?tab=orca&applied=1`

This is a partial template (no `extends base.html`).

Create `fm/api/templates/import.html`:

```html
{% extends "base.html" %}
{% block title %}Import — Filament Manager{% endblock %}
{% block content %}
<section id="import-page">
  <header class="page-header">
    <div>
      <p class="eyebrow">Data tools</p>
      <h1>Import</h1>
      <p>Bring existing filament definitions into the catalog from your printer database or Orca Cloud.</p>
    </div>
  </header>

  <div class="import-tabs">
    <button class="import-tab {% if active_tab == 'printer' %}active{% endif %}"
            onclick="switchImportTab('printer')">From Printer DB</button>
    <button class="import-tab {% if active_tab == 'orca' %}active{% endif %}"
            onclick="switchImportTab('orca')">From Orca Cloud</button>
  </div>

  <div id="tab-printer" class="tab-content {% if active_tab == 'printer' %}active{% endif %}">
    {% include "import_printer.html" %}
  </div>
  <div id="tab-orca" class="tab-content {% if active_tab == 'orca' %}active{% endif %}">
    {% include "import_orca_tab.html" %}
  </div>
</section>

<style>
.import-tabs { display: flex; gap: 0; margin-bottom: 24px; border-bottom: 1px solid var(--border); }
.import-tab { padding: 12px 20px; border: 0; background: transparent; color: var(--text-secondary); font-weight: 600; cursor: pointer; border-bottom: 2px solid transparent; transition: color .15s, border-color .15s; }
.import-tab:hover { color: var(--text); }
.import-tab.active { color: var(--text); border-bottom-color: var(--accent); }
.tab-content { display: none; }
.tab-content.active { display: block; }
</style>

<script>
function switchImportTab(tab) {
  document.querySelectorAll('.import-tab').forEach(function(b) { b.classList.remove('active'); });
  document.querySelectorAll('.tab-content').forEach(function(c) { c.classList.remove('active'); });
  document.querySelector('.import-tab[onclick*="' + tab + '"]').classList.add('active');
  document.getElementById('tab-' + tab).classList.add('active');
  // Update URL without reload
  var url = new URL(window.location);
  url.searchParams.set('tab', tab);
  window.history.replaceState({}, '', url);
}
</script>
{% endblock %}
```

- [ ] **Step 4: Migrate routes in `fm/api/ui.py`**

Replace the three migration routes (lines 863-996) and the two import-orca routes (lines 1050-1147) with new `/import` routes. The route functions keep the same logic, only the paths and template names change:

```python
# ---------------------------------------------------------------------------
# Import page — merged migration + Orca Cloud import
# ---------------------------------------------------------------------------

@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request, db: DbSession) -> HTMLResponse:
    """Tabbed import page: 'From Printer DB' (migration wizard) and
    'From Orca Cloud' (unmatched preset import)."""
    tab = request.query_params.get("tab", "printer")

    # Printer DB tab data (same as old migration_page)
    applied = request.query_params.get("applied") == "1"
    import_job = _latest_import_scan(db)
    proposals: list[dict] = []
    if import_job and import_job.result:
        try:
            result = json.loads(import_job.result)
            proposals = result.get("proposals", [])
        except (json.JSONDecodeError, ValueError):
            proposals = []

    existing_ids = set(
        db.scalars(
            select(Profile.printer_db_id).where(Profile.printer_db_id.isnot(None))
        )
    )

    # Orca Cloud tab data (same as old import_orca_page)
    all_presets = _pull_orca_presets()
    orca_presets = _unmatched_orca_presets(db)
    orca_proposals = []
    for p in orca_presets:
        orca_proposals.append({
            "filament_id": p.get("filament_id", ""),
            "name": p.get("name", ""),
            "brand": (p.get("filament_vendor") or [""])[0] if p.get("filament_vendor") else "",
            "type": (p.get("filament_type") or [""])[0] if p.get("filament_type") else "",
            "nozzle_temperature": (p.get("nozzle_temperature") or [""])[0] if p.get("nozzle_temperature") else "",
            "pressure_advance": (p.get("pressure_advance") or [""])[0] if p.get("pressure_advance") else "",
            "filament_flow_ratio": (p.get("filament_flow_ratio") or [""])[0] if p.get("filament_flow_ratio") else "",
            "inherits": p.get("inherits", ""),
            "user_tuned": not p.get("inherits", ""),
        })

    return _templates(request).TemplateResponse(
        request, "import.html",
        {
            "active_tab": tab,
            "proposals": proposals,
            "existing_ids": existing_ids,
            "import_job": import_job,
            "applied": applied,
            "orca_proposals": orca_proposals,
            "has_presets": len(all_presets) > 0,
        },
    )


@router.post("/import/printer/scan", response_class=HTMLResponse)
def import_printer_scan(request: Request, db: DbSession) -> RedirectResponse:
    """Enqueue import_scan job, drain, redirect to /import?tab=printer."""
    config = _get_config(request)
    job = jobs.enqueue(db, "import_scan", {})
    db.flush()
    jobs.run_pending(db, config)
    db.flush()
    return RedirectResponse(url="/import?tab=printer", status_code=303)


@router.post("/import/printer/apply", response_class=HTMLResponse)
def import_printer_apply(
    request: Request,
    db: DbSession,
    include: list[str] = Form(default=[]),
) -> RedirectResponse:
    """Create/update Profiles + SyncTargets from selected proposals.
    (Same logic as old migration_apply, redirect target changed.)
    """
    import_job = _latest_import_scan(db)
    if import_job is None:
        raise HTTPException(status_code=404, detail="no import scan result")

    result = json.loads(import_job.result) if import_job.result else {}
    proposals_by_id = {
        p["printer_db_id"]: p for p in result.get("proposals", [])
    }

    now = _now()
    for pid_str in include:
        proposal = proposals_by_id.get(pid_str)
        if proposal is None:
            continue

        existing = db.scalars(
            select(Profile).where(Profile.printer_db_id == pid_str)
        ).first()

        if existing is not None:
            existing.brand = proposal["brand"]
            existing.name = proposal["name"]
            existing.type = proposal["type"]
            existing.colors = proposal.get("colors", "[]")
            existing.min_temp = proposal["min_temp"]
            existing.max_temp = proposal["max_temp"]
            existing.density = proposal.get("density", 0.0)
            existing.drying_temp = proposal.get("drying_temp", 0)
            existing.drying_time = proposal.get("drying_time", 0)
            existing.nozzle_temperature = proposal.get("nozzle_temperature")
            if "flow_ratio" in proposal:
                existing.flow_ratio = proposal["flow_ratio"]
            if "pa" in proposal:
                existing.pa = proposal["pa"]
            if "max_volumetric" in proposal:
                existing.max_volumetric = proposal["max_volumetric"]
            existing.updated_at = now
            profile = existing
        else:
            profile = Profile(
                brand=proposal["brand"],
                name=proposal["name"],
                type=proposal["type"],
                colors=proposal.get("colors", "[]"),
                min_temp=proposal["min_temp"],
                max_temp=proposal["max_temp"],
                density=proposal.get("density", 0.0),
                drying_temp=proposal.get("drying_temp", 0),
                drying_time=proposal.get("drying_time", 0),
                nozzle_temperature=proposal.get("nozzle_temperature"),
                flow_ratio=proposal.get("flow_ratio"),
                pa=proposal.get("pa"),
                max_volumetric=proposal.get("max_volumetric"),
                printer_db_id=pid_str,
                filament_id=None,
                created_at=now,
                updated_at=now,
            )
            db.add(profile)
            db.flush()

        target = db.scalars(
            select(SyncTarget).where(
                SyncTarget.profile_id == profile.id,
                SyncTarget.kind == "printer_db",
            )
        ).first()
        if target is None:
            target = SyncTarget(
                profile_id=profile.id, kind="printer_db",
                status="pending",
            )
            db.add(target)
        target.status = "pending"
        target.last_synced_at = now
        db.flush()

    return RedirectResponse(url="/import?tab=printer&applied=1", status_code=303)


@router.post("/import/orca/apply", response_class=HTMLResponse)
def import_orca_apply(
    request: Request,
    db: DbSession,
    include: list[str] = Form(default=[]),
) -> RedirectResponse:
    """Create profiles from selected OrcaSlicer presets.
    (Same logic as old import_orca_apply, redirect target changed.)
    """
    presets = _unmatched_orca_presets(db)
    by_fid = {p.get("filament_id", ""): p for p in presets}

    now = _now()
    for fid in include:
        p = by_fid.get(fid)
        if p is None:
            continue

        vendor = (p.get("filament_vendor") or [""])[0] if p.get("filament_vendor") else ""
        ftype = (p.get("filament_type") or ["PLA"])[0] if p.get("filament_type") else "PLA"
        temp = (p.get("nozzle_temperature") or ["220"])[0] if p.get("nozzle_temperature") else "220"
        try:
            temp_int = int(temp)
        except (ValueError, TypeError):
            temp_int = 220

        profile = Profile(
            brand=vendor,
            name=p.get("name", ""),
            type=ftype,
            colors="[]",
            min_temp=max(temp_int - 15, 0),
            max_temp=temp_int + 15,
            density=0.0,
            drying_temp=0,
            drying_time=0,
            nozzle_temperature=temp_int,
            filament_id=fid,
            printer_db_id=None,
            created_at=now,
            updated_at=now,
        )
        db.add(profile)
        db.flush()

        target = SyncTarget(
            profile_id=profile.id,
            kind="orca_preset",
            cloud_id=p.get("_path"),
            expected_hash=canonical_hash({
                "name": p.get("name"),
                "filament_id": p.get("filament_id"),
                "filament_vendor": p.get("filament_vendor"),
                "filament_type": p.get("filament_type"),
                "nozzle_temperature": p.get("nozzle_temperature"),
            }),
            actual_state=json.dumps(p),
            status="pending",
            last_synced_at=now,
        )
        db.add(target)

    return RedirectResponse(url="/import?tab=orca&applied=1", status_code=303)
```

Remove the old routes: `migration_page`, `migration_import`, `migration_apply`, `import_orca_page`, and the old `import_orca_apply`.

- [ ] **Step 5: Update `base.html` navigation**

In `fm/api/templates/base.html`, replace the Migration and Orca Cloud Import nav items (lines 37-41) with a single Import item:

```html
      <a href="/import" class="nav-item {% if request.url.path.startswith('/import') %}active{% endif %}">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2Zm-7 4 4 4h-3v6h-2v-6H8l4-4Z"/></svg><span>Import</span>
      </a>
```

Place it between the Sync nav item and the Settings nav item. Remove both the Migration and Orca Cloud Import nav items.

- [ ] **Step 6: Delete old templates**

```bash
rm fm/api/templates/migration.html
rm fm/api/templates/import_orca.html
```

- [ ] **Step 7: Run test to verify it passes**

Run: `pytest tests/test_ui_sync.py::TestImportPage tests/test_ui_import_orca.py -v`
Expected: PASS

- [ ] **Step 8: Run full test suite**

Run: `scripts/test.sh`
Expected: All tests PASS

- [ ] **Step 9: Commit**

```bash
git add fm/api/templates/import.html fm/api/templates/import_printer.html fm/api/templates/import_orca_tab.html fm/api/templates/base.html fm/api/ui.py tests/test_ui_sync.py tests/test_ui_import_orca.py
git rm fm/api/templates/migration.html fm/api/templates/import_orca.html
git commit -m "feat: merge /migration + /import-orca into /import with tabs, nav 7→6"
```

---

### Task 6: Clean up profiles.html and orca_fragment.html labels

**Files:**
- Modify: `fm/api/templates/profiles.html:5-9` (remove Sync from Orca button) and `:34-46` (remove toast)
- Modify: `fm/api/templates/orca_fragment.html:40,51` (change button label)
- Modify: `fm/api/ui.py:183-188` (remove `profiles_sync_from_orca` route)
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: nothing new
- Produces: profiles.html without "Sync from Orca" button; orca_fragment.html with "Pull from Cloud" label; `POST /profiles/sync-from-orca` route removed

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ui.py`:

```python
# ---------------------------------------------------------------------------
# Sync Hub redesign — label cleanup
# ---------------------------------------------------------------------------

def test_profiles_page_no_sync_from_orca(client, seeded_profile):
    """Profiles page must not contain 'Sync from Orca' button."""
    r = client.get("/profiles")
    assert r.status_code == 200
    assert b"Sync from Orca" not in r.content
    # Should have a link to sync hub instead
    assert b"Sync status" in r.content or b"/sync" in r.content


def test_profiles_page_no_orca_toast(client, seeded_profile):
    """Profiles page must not contain the Orca toast script."""
    r = client.get("/profiles")
    assert b"Synced from Orca Cloud" not in r.content


def test_orca_fragment_has_pull_from_cloud_label(client, seeded_profile):
    """Orca fragment button says 'Pull from Cloud', not 'Sync from Orca'."""
    r = client.get(f"/profiles/{seeded_profile['id']}/orca-fragment")
    assert r.status_code == 200
    assert b"Pull from Cloud" in r.content
    assert b"Sync from Orca" not in r.content


def test_profiles_sync_from_orca_route_removed(client):
    """POST /profiles/sync-from-orca returns 404 (route removed)."""
    r = client.post("/profiles/sync-from-orca")
    assert r.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ui.py -k "sync_from_orca or orca_fragment or orca_toast" -v`
Expected: FAIL — "Sync from Orca" still in profiles.html; "Pull from Cloud" not in orca_fragment.html

- [ ] **Step 3: Update `profiles.html`**

Remove lines 6-8 (the sync-from-orca form). Add a "Sync status →" link after the "New profile" button on line 5:

```html
  <header class="page-header"><div><p class="eyebrow">Catalog</p><h1>Filament profiles</h1><p>Manage material definitions shared with your printer and slicer.</p></div><div class="button-group"><a href="/profiles/new" class="button button-primary"><span>＋</span> New profile</a><a href="/sync" class="text-link">Sync status →</a></div></header>
```

Remove lines 34-46 (the toast div and script):

```html
</section>
{% endblock %}
```

- [ ] **Step 4: Update `orca_fragment.html`**

Change line 40 from `>Sync from Orca</button>` to `>Pull from Cloud</button>`.
Change line 51 from `>Sync from Orca</button>` to `>Pull from Cloud</button>`.

- [ ] **Step 5: Remove `profiles_sync_from_orca` route from `fm/api/ui.py`**

Delete lines 183-188 (the `profiles_sync_from_orca` function and its decorator).

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_ui.py -k "sync_from_orca or orca_fragment or orca_toast" -v`
Expected: PASS

- [ ] **Step 7: Run full test suite**

Run: `scripts/test.sh`
Expected: All tests PASS — note: `test_ui_import_orca.py::TestSyncFromOrcaAdopt` tests `POST /profiles/sync-from-orca` which is now removed. Update that test to use `POST /api/sync/pull` instead:

```python
    # Explicit pull → adopt path
    r = client.post("/api/sync/pull")
    assert r.status_code == 200
```

- [ ] **Step 8: Commit**

```bash
git add fm/api/templates/profiles.html fm/api/templates/orca_fragment.html fm/api/ui.py tests/test_ui.py tests/test_ui_import_orca.py
git commit -m "feat: remove 'Sync from Orca' from profiles, relabel orca fragment to 'Pull from Cloud'"
```

---

### Task 7: Nav tests — 6 items, no old labels

**Files:**
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: Tasks 4, 5, 6 (nav changes, new templates, removed old labels)
- Produces: test coverage for nav structure and label absence

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ui.py`:

```python
# ---------------------------------------------------------------------------
# Navigation structure
# ---------------------------------------------------------------------------

def test_nav_has_six_items(client):
    """base.html nav has exactly 6 items (not 7)."""
    r = client.get("/")
    # Count nav-item elements
    content = r.text
    assert content.count('class="nav-item') == 6 or content.count('class="nav-item active') + content.count('class="nav-item"') == 6


def test_nav_has_import(client):
    """Nav contains 'Import' linking to /import."""
    r = client.get("/")
    assert b"Import" in r.content
    assert b'href="/import"' in r.content


def test_nav_no_migration(client):
    """Nav does not contain 'Migration'."""
    r = client.get("/")
    assert b"Migration" not in r.content


def test_nav_no_orca_cloud_import(client):
    """Nav does not contain 'Orca Cloud Import'."""
    r = client.get("/")
    assert b"Orca Cloud Import" not in r.content


def test_no_old_labels_in_sync_page(client):
    """Sync Hub page must not contain any old labels."""
    r = client.get("/sync")
    assert b"Sync from Orca" not in r.content
    assert b"Push all profiles" not in r.content
    assert b"Sync now" not in r.content
    assert b"Generate preset" not in r.content


def test_no_old_labels_in_profiles_page(client, seeded_profile):
    """Profiles page must not contain old labels."""
    r = client.get("/profiles")
    assert b"Sync from Orca" not in r.content
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/test_ui.py -k "nav_ or old_labels" -v`
Expected: PASS (Tasks 4-6 already made the changes; these tests verify them)

Note: If any test fails, it means a previous task didn't fully clean up — fix the template or route before proceeding.

- [ ] **Step 3: Run full test suite**

Run: `scripts/test.sh`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_ui.py
git commit -m "test: add nav structure and old-label absence assertions"
```

---

### Task 8: Final verification, docs update, and cleanup

**Files:**
- Modify: `docs/sync-workflow-analysis.md` (UI-Map, V-status updates)
- Modify: `AGENTS.md` (Key Files table if needed)
- Delete: `fm/api/static/mockups/` (mockup artifacts — disposable, spec approved)

**Interfaces:**
- Consumes: All previous tasks
- Produces: Updated docs reflecting the new UI structure

- [ ] **Step 1: Run full test suite**

Run: `scripts/test.sh`
Expected: All tests PASS, zero failures

- [ ] **Step 2: Restart dev server and smoke test**

```bash
scripts/dev-stop.sh && scripts/dev-start.sh
```

Verify with curl:
```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8404/          # 200
curl -s -o /dev/null -w "%{http_code}" http://localhost:8404/sync       # 200
curl -s -o /dev/null -w "%{http_code}" http://localhost:8404/import     # 200
curl -s -o /dev/null -w "%{http_code}" http://localhost:8404/migration  # 404
curl -s -o /dev/null -w "%{http_code}" http://localhost:8404/import-orca # 404
```

Expected: `/` → 200, `/sync` → 200, `/import` → 200, `/migration` → 404, `/import-orca` → 404

- [ ] **Step 3: Update `docs/sync-workflow-analysis.md`**

Update section (c) UI-Map:
- Navigation: change from 7 to 6 entries, remove Migration + Orca Cloud Import, add Import
- Sync page: update to "Sync Hub (M3 two-column layout)", update element table with new labels
- Migration section: replace with "Import page — Tab 'From Printer DB'"
- Import-Orca section: replace with "Import page — Tab 'From Orca Cloud'"
- Profiles section: remove "Sync from Orca" button, add "Sync status →" link

Update section (d) Verwirrungs-Punkte:
- V1: add "**GEFIXT (Sync Hub Redesign)**" — all buttons now have direction verbs
- V2: add "**GEFIXT**" — "Sync from Orca" removed from Profiles page; bulk pull moved to Sync Hub
- V6: add "**GEFIXT**" — Migration + Import-Orca merged into /import with tabs
- V7: add "**GEFIXT**" — Icon-Button replaced with "Push to Cloud" text button
- V11: add "**GEFIXT**" — Per-profile "Push to Printer" button added to drift table

- [ ] **Step 4: Delete mockup artifacts**

```bash
rm -rf fm/api/static/mockups/
```

- [ ] **Step 5: Commit**

```bash
git add docs/sync-workflow-analysis.md
git rm -r fm/api/static/mockups/
git commit -m "docs: update workflow analysis for sync hub redesign, remove mockup artifacts"
```

- [ ] **Step 6: Final full test run**

Run: `scripts/test.sh`
Expected: All tests PASS

- [ ] **Step 7: Final dev server smoke test**

```bash
scripts/dev-stop.sh && scripts/dev-start.sh
curl -s http://localhost:8404/sync | grep -c "Sync Hub"       # ≥1
curl -s http://localhost:8404/sync | grep -c "Sync from Orca" # 0
curl -s http://localhost:8404/import | grep -c "From Printer DB" # ≥1
curl -s http://localhost:8404/ | grep -c "Import"             # ≥1
curl -s http://localhost:8404/ | grep -c "Migration"          # 0
```

Expected: All grep counts match the expected values.
