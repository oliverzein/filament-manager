# Printer DB Name-Based Entry Adoption Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically detect and adopt existing custom printer DB entries by name/type matching during printer DB rebuild to eliminate duplicate entries across instances and after database resets.

**Architecture:** In `fm/service/sync_handlers.py`, when mapping a preset (cloud or catalog fallback) to a `printer_db_id`, if no active `SyncTarget` mapping exists, check existing custom entries in the printer DB via `_find_matching_custom_entry()`. If an unassigned entry matching the name, material type, and vendor is found, adopt its ID instead of generating a new one with `next_free_id()`, replace the entry contents, and persist the mapping in `SyncTarget`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0, pytest.

**Spec:** `docs/superpowers/specs/2026-09-04-filament-manager-rework.md`

## Global Constraints

- Never mutate stock printer entries (IDs < 99000); only custom entries in 99xxx range may be matched or updated.
- Maintain Iron Rule 4: Never delete custom 99xxx entries from the printer unless explicitly replaced or double-confirmed.
- Zero model schema migrations (Iron Rule 6): use existing `SyncTarget` columns.
- An existing printer entry ID can only be adopted if it is not already mapped to another active `SyncTarget`.
- All automated tests must pass via `scripts/test.sh`.

---

### Task 1: Matching Helper Function `_find_matching_custom_entry`

**Files:**
- Modify: `fm/service/sync_handlers.py:40-55` (import `find_custom_entries`), `fm/service/sync_handlers.py:530-560` (helper definition)
- Test: `tests/test_sync_handlers.py`

**Interfaces:**
- Consumes:
  - `fm.printer.dbcodec.find_custom_entries(db: dict) -> list[dict]`
- Produces:
  - `_find_matching_custom_entry(db: dict, name: str, ftype: str, vendor: str = "", assigned_ids: set[str] | None = None) -> dict | None`

- [ ] **Step 1: Write the failing tests**

Add `TestFindMatchingCustomEntry` to `tests/test_sync_handlers.py`:

```python
class TestFindMatchingCustomEntry:
    def test_find_matching_custom_entry_exact(self, sample_db):
        """Finds custom entry matching name, type, and vendor exactly (case-insensitive)."""
        entry = build_entry(sample_db, {
            "id": "99008",
            "brand": "Elegoo",
            "name": "Elegoo Rapid PETG Optimized",
            "type": "PETG",
            "minTemp": 220,
            "maxTemp": 250,
        })
        insert_entry(sample_db, entry)

        matched = sh._find_matching_custom_entry(
            sample_db,
            name="elegoo rapid petg optimized",
            ftype="petg",
            vendor="elegoo",
        )
        assert matched is not None
        assert matched["base"]["id"] == "99008"

    def test_find_matching_custom_entry_vendor_mismatch(self, sample_db):
        """Returns None when vendor does not match the existing custom entry brand."""
        entry = build_entry(sample_db, {
            "id": "99008",
            "brand": "Elegoo",
            "name": "Elegoo Rapid PETG Optimized",
            "type": "PETG",
            "minTemp": 220,
            "maxTemp": 250,
        })
        insert_entry(sample_db, entry)

        matched = sh._find_matching_custom_entry(
            sample_db,
            name="Elegoo Rapid PETG Optimized",
            ftype="PETG",
            vendor="eSUN",
        )
        assert matched is None

    def test_find_matching_custom_entry_assigned_id_skipped(self, sample_db):
        """Skips entries whose ID is present in assigned_ids."""
        entry = build_entry(sample_db, {
            "id": "99008",
            "brand": "Elegoo",
            "name": "Elegoo Rapid PETG Optimized",
            "type": "PETG",
            "minTemp": 220,
            "maxTemp": 250,
        })
        insert_entry(sample_db, entry)

        matched = sh._find_matching_custom_entry(
            sample_db,
            name="Elegoo Rapid PETG Optimized",
            ftype="PETG",
            vendor="Elegoo",
            assigned_ids={"99008"},
        )
        assert matched is None
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/test_sync_handlers.py::TestFindMatchingCustomEntry -v
```
Expected: FAIL with `AttributeError: module 'fm.service.sync_handlers' has no attribute '_find_matching_custom_entry'`.

- [ ] **Step 3: Implement helper in `fm/service/sync_handlers.py`**

In `fm/service/sync_handlers.py`:
1. Update `dbcodec` imports to include `find_custom_entries`:
```python
from ..printer.dbcodec import (
    build_entry,
    find_custom_entries,
    find_entry,
    insert_entry,
    load_db,
    next_free_id,
    remove_entry,
    save_db,
)
```

2. Add `_find_matching_custom_entry`:
```python
def _find_matching_custom_entry(
    db: dict,
    name: str,
    ftype: str,
    vendor: str = "",
    assigned_ids: set[str] | None = None,
) -> dict | None:
    """Find an existing custom (99xxx) entry matching name, type, and optional vendor.

    Skips entries whose ID is in ``assigned_ids``. Compares name, material
    type, and vendor case-insensitively. Returns the matching entry dict or None.
    """
    assigned = assigned_ids or set()
    target_name = name.strip().casefold()
    target_type = ftype.strip().casefold()
    target_vendor = vendor.strip().casefold()

    for entry in find_custom_entries(db):
        base = entry.get("base", {})
        eid = str(base.get("id", "")).strip()
        if not eid or eid in assigned:
            continue

        entry_name = str(base.get("name", "")).strip().casefold()
        if entry_name != target_name:
            continue

        entry_type = str(
            base.get("meterialType") or base.get("type") or ""
        ).strip().casefold()
        if entry_type != target_type:
            continue

        if target_vendor:
            entry_vendor = str(
                base.get("brand") or entry.get("kvParam", {}).get("filament_vendor") or ""
            ).strip().casefold()
            if entry_vendor and entry_vendor != target_vendor:
                continue

        return entry

    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
pytest tests/test_sync_handlers.py::TestFindMatchingCustomEntry -v
```
Expected: PASS (`3 passed`).

- [ ] **Step 5: Commit**

```bash
git add fm/service/sync_handlers.py tests/test_sync_handlers.py
git commit -m "feat(sync): add _find_matching_custom_entry helper"
```

---

### Task 2: Adoption in Cloud Rebuild (`_rebuild_from_cloud`)

**Files:**
- Modify: `fm/service/sync_handlers.py:555-635`
- Test: `tests/test_sync_handlers.py`

**Interfaces:**
- Consumes:
  - `_find_matching_custom_entry(db, name, ftype, vendor, assigned_ids)`
  - `fm.printer.dbcodec.remove_entry(db, entry_id)`
  - `fm.printer.dbcodec.insert_entry(db, entry)`
- Produces:
  - Updated `_rebuild_from_cloud` that adopts matching custom entries and persists `SyncTarget.printer_db_id`.

- [ ] **Step 1: Write the failing test**

Add `TestRebuildFromCloudAdoption` to `tests/test_sync_handlers.py`:

```python
class TestRebuildFromCloudAdoption:
    def test_rebuild_from_cloud_adopts_existing_printer_entry_by_name(
        self, inmemory_session, cfg, sample_db
    ):
        """When SyncTarget has no printer_db_id, adopts an unassigned custom entry by name/type/vendor."""
        from unittest.mock import MagicMock
        s = inmemory_session

        # Seed sample_db with an existing custom entry 99008
        existing_entry = build_entry(sample_db, {
            "id": "99008",
            "brand": "Elegoo",
            "name": "Elegoo Rapid PETG Optimized",
            "type": "PETG",
            "minTemp": 220,
            "maxTemp": 250,
        })
        insert_entry(sample_db, existing_entry)

        # Fake Orca Cloud client returning a matching preset
        fake_client = MagicMock()
        fake_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [{
                "id": "cloud-uuid-elegoo-petg",
                "content": {
                    "type": "filament",
                    "name": "Elegoo Rapid PETG Optimized",
                    "filament_type": "PETG",
                    "filament_vendor": "Elegoo",
                    "nozzle_temperature": "235",
                    "nozzle_temperature_range_low": "220",
                    "nozzle_temperature_range_high": "250",
                },
            }],
            "deletes": [],
        }

        db_result, pushed, skipped_ids = sh._rebuild_from_cloud(
            s, cfg, sample_db, fake_client
        )

        assert len(pushed) == 1
        target, pushed_entry = pushed[0]
        # Must adopt existing 99008 rather than generating a new ID (e.g. 99002)
        assert target.printer_db_id == "99008"
        assert pushed_entry["base"]["id"] == "99008"
        # The entry in the DB result has the updated nozzle temperature
        adopted_entry = find_entry(db_result, "99008")
        assert adopted_entry is not None
        assert adopted_entry["kvParam"]["nozzle_temperature"] == "235"
        # No duplicate entry exists
        custom_ids = [e["base"]["id"] for e in db_result["result"]["list"] if e["base"]["id"].startswith("99")]
        assert custom_ids.count("99008") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/test_sync_handlers.py::TestRebuildFromCloudAdoption -v
```
Expected: FAIL with `AssertionError: assert '99002' == '99008'` (or other newly generated ID).

- [ ] **Step 3: Implement adoption in `_rebuild_from_cloud`**

In `fm/service/sync_handlers.py`, modify `_rebuild_from_cloud`:
1. Initialize `assigned_ids` tracking set:
```python
    assigned_ids: set[str] = {
        row.printer_db_id
        for row in session.scalars(
            select(SyncTarget).where(
                SyncTarget.kind == "printer_db",
                SyncTarget.printer_db_id.is_not(None),
            )
        ).all()
        if row.printer_db_id
    }
```
2. In the preset loop, extract preset name, material type, and vendor from `flat`:
```python
        flat = _flatten_cloud_preset(preset)

        raw_name = flat.get("name") or flat.get("filament_settings_id") or "Custom Filament"
        preset_name = raw_name[0] if isinstance(raw_name, list) else str(raw_name)
        preset_name = preset_name.strip().strip('"').strip("'")

        raw_vendor = flat.get("filament_vendor") or ""
        preset_vendor = raw_vendor[0] if isinstance(raw_vendor, list) else str(raw_vendor)
        preset_vendor = preset_vendor.strip().strip('"').strip("'")

        raw_type = flat.get("filament_type") or "PLA"
        preset_type = raw_type[0] if isinstance(raw_type, list) else str(raw_type)
        preset_type = preset_type.strip().strip('"').strip("'")
```
3. Map or adopt `entry_id`:
```python
        # Map printer_db_id via SyncTarget where cloud_id == res_id
        target = session.scalars(
            select(SyncTarget).where(
                SyncTarget.cloud_id == res_id,
                SyncTarget.kind == "printer_db",
            )
        ).first()

        if target is not None and target.printer_db_id:
            entry_id = target.printer_db_id
            assigned_ids.add(entry_id)
            existing = find_entry(db, entry_id)
            if existing is not None:
                remove_entry(db, entry_id)
        else:
            # Check existing unassigned custom entries on printer DB by name & type
            cand = _find_matching_custom_entry(
                db,
                name=preset_name,
                ftype=preset_type,
                vendor=preset_vendor,
                assigned_ids=assigned_ids,
            )
            if cand is not None:
                entry_id = str(cand["base"]["id"])
                remove_entry(db, entry_id)
                if target is not None:
                    target.printer_db_id = entry_id
                else:
                    target = SyncTarget(
                        profile_id=None,
                        kind="printer_db",
                        cloud_id=res_id,
                        printer_db_id=entry_id,
                        status="pending",
                    )
                    session.add(target)
                    session.flush()
                assigned_ids.add(entry_id)
            else:
                candidate = config.id_range_start
                used = {
                    int(e["base"]["id"])
                    for e in db["result"]["list"]
                    if e.get("base", {}).get("id", "").isdigit()
                } | {int(x) for x in assigned_ids if x.isdigit()}
                while candidate in used:
                    candidate += 1
                entry_id = str(candidate)
                if target is not None:
                    target.printer_db_id = entry_id
                else:
                    target = SyncTarget(
                        profile_id=None,
                        kind="printer_db",
                        cloud_id=res_id,
                        printer_db_id=entry_id,
                        status="pending",
                    )
                    session.add(target)
                    session.flush()
                assigned_ids.add(entry_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
pytest tests/test_sync_handlers.py::TestRebuildFromCloudAdoption -v
```
Expected: PASS (`1 passed`).

- [ ] **Step 5: Commit**

```bash
git add fm/service/sync_handlers.py tests/test_sync_handlers.py
git commit -m "feat(sync): adopt existing custom printer DB entries in _rebuild_from_cloud"
```

---

### Task 3: Adoption in Catalog Rebuild Fallback (`_rebuild_from_catalog`)

**Files:**
- Modify: `fm/service/sync_handlers.py:637-675`
- Test: `tests/test_sync_handlers.py`

**Interfaces:**
- Consumes:
  - `_find_matching_custom_entry(db, name, ftype, vendor, assigned_ids)`
- Produces:
  - Updated `_rebuild_from_catalog` that adopts matching custom entries and sets `profile.printer_db_id`.

- [ ] **Step 1: Write the failing test**

Add `TestRebuildFromCatalogAdoption` to `tests/test_sync_handlers.py`:

```python
class TestRebuildFromCatalogAdoption:
    def test_rebuild_from_catalog_adopts_existing_printer_entry(
        self, inmemory_session, cfg, sample_db
    ):
        """When profile.printer_db_id is None, adopts an unassigned custom entry by name/type/vendor."""
        s = inmemory_session

        # Seed sample_db with an existing custom entry 99008
        existing_entry = build_entry(sample_db, {
            "id": "99008",
            "brand": "Elegoo",
            "name": "Elegoo Rapid PETG Optimized",
            "type": "PETG",
            "minTemp": 220,
            "maxTemp": 250,
        })
        insert_entry(sample_db, existing_entry)

        # Profile with matching attributes and no printer_db_id
        profile = _make_profile(
            s,
            brand="Elegoo",
            name="Elegoo Rapid PETG Optimized",
            type="PETG",
            printer_db_id=None,
        )

        db_result, pushed, skipped_ids = sh._rebuild_from_catalog(
            s, cfg, sample_db, [profile.id]
        )

        assert len(pushed) == 1
        p_obj, pushed_entry = pushed[0]
        # profile.printer_db_id must adopt 99008
        assert profile.printer_db_id == "99008"
        assert pushed_entry["base"]["id"] == "99008"
        # Total count of 99008 entries in db is 1
        custom_ids = [e["base"]["id"] for e in db_result["result"]["list"] if e["base"]["id"].startswith("99")]
        assert custom_ids.count("99008") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/test_sync_handlers.py::TestRebuildFromCatalogAdoption -v
```
Expected: FAIL with `AssertionError: assert '99002' == '99008'` (or other newly generated ID).

- [ ] **Step 3: Implement adoption in `_rebuild_from_catalog`**

In `fm/service/sync_handlers.py`, modify `_rebuild_from_catalog`:
```python
def _rebuild_from_catalog(
    session: Session, config: Config, db: dict,
    profile_ids: list[int] | None = None,
) -> tuple[dict, list[tuple[Any, dict]], list[int]]:
    """Fallback rebuild: source entries from cache DB + catalog Profile rows.

    Used when no Orca Cloud client is configured. Preserves the pre-rework
    behavior minus orphan removal (Orphan Rule F3/B). Adopts existing custom
    entries matching name, type, and vendor when printer_db_id is not yet set.
    """
    if profile_ids:
        all_profiles = list(session.scalars(
            select(Profile).where(Profile.id.in_(profile_ids))
            .order_by(Profile.id)
        ))
    else:
        all_profiles = list(session.scalars(select(Profile).order_by(Profile.id)))

    profiles = all_profiles
    skipped_ids: list[int] = []

    assigned_ids: set[str] = {
        p.printer_db_id
        for p in session.scalars(
            select(Profile).where(Profile.printer_db_id.is_not(None))
        ).all()
        if p.printer_db_id
    }

    pushed: list[tuple[Any, dict]] = []
    for profile in profiles:
        entry_id = profile.printer_db_id
        if entry_id:
            assigned_ids.add(entry_id)
            existing = find_entry(db, entry_id)
            if existing is not None:
                remove_entry(db, entry_id)
        else:
            cand = _find_matching_custom_entry(
                db,
                name=profile.name,
                ftype=profile.type,
                vendor=profile.brand or "",
                assigned_ids=assigned_ids,
            )
            if cand is not None:
                entry_id = str(cand["base"]["id"])
                remove_entry(db, entry_id)
                profile.printer_db_id = entry_id
                assigned_ids.add(entry_id)
            else:
                candidate = config.id_range_start
                used = {
                    int(e["base"]["id"])
                    for e in db["result"]["list"]
                    if e.get("base", {}).get("id", "").isdigit()
                } | {int(x) for x in assigned_ids if x.isdigit()}
                while candidate in used:
                    candidate += 1
                entry_id = str(candidate)
                profile.printer_db_id = entry_id
                assigned_ids.add(entry_id)

        values = _profile_to_values(profile, entry_id)
        entry = build_entry(db, values)
        insert_entry(db, entry)
        pushed.append((profile, entry))

    # Orphan Rule F3/B: do NOT remove orphaned custom 99xxx entries.
    return db, pushed, skipped_ids
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
pytest tests/test_sync_handlers.py::TestRebuildFromCatalogAdoption -v
```
Expected: PASS (`1 passed`).

- [ ] **Step 5: Commit**

```bash
git add fm/service/sync_handlers.py tests/test_sync_handlers.py
git commit -m "feat(sync): adopt existing custom printer DB entries in _rebuild_from_catalog"
```

---

### Task 4: Multi-Instance Clean DB Reconciliation Integration Test

**Files:**
- Test: `tests/test_sync_handlers.py`

**Interfaces:**
- Consumes:
  - `handle_push(session, config, payload)`
  - `handle_verify(session, config, payload)`
  - `fm.printer.dbcodec.find_custom_entries(db)`
- Produces:
  - End-to-end integration test `test_clean_db_reconciliation_adopts_existing_entries_without_ghost_duplicates`.

- [ ] **Step 1: Write integration test**

Add `TestCleanDBReconciliation` to `tests/test_sync_handlers.py`:

```python
class TestCleanDBReconciliation:
    def test_clean_db_reconciliation_adopts_existing_entries_without_ghost_duplicates(
        self, inmemory_session, cfg, tmp_path, sample_db, monkeypatch
    ):
        """End-to-end multi-instance reconciliation test.

        Simulates a fresh FM database session with no SyncTargets connecting to a
        printer DB that already has custom entries (99008 Elegoo Rapid PETG Optimized
        and 99014 eSUN PLA+ Optimized).

        Cloud returns:
        1) 'Elegoo Rapid PETG Optimized' (matches 99008)
        2) 'eSUN PLA+ Optimized' (matches 99014)
        3) 'Overture ABS Turbo' (new preset, no match)

        Verifies:
        1) 99008 and 99014 are adopted and updated with fresh hashes (not duplicated).
        2) Overture ABS gets allocated 99015 (or next available free ID).
        3) Custom entry count on printer DB equals exactly 3 (no ghost duplicates).
        4) handle_verify passes with in_sync for all targets.
        """
        from unittest.mock import MagicMock
        s = inmemory_session

        # Clear any existing custom entries in sample_db fixture so only test entries exist
        sample_db["result"]["list"] = [
            e for e in sample_db["result"]["list"]
            if not e.get("base", {}).get("id", "").startswith("99")
        ]

        # Seed printer DB with existing custom entries 99008 and 99014
        entry_petg = build_entry(sample_db, {
            "id": "99008",
            "brand": "Elegoo",
            "name": "Elegoo Rapid PETG Optimized",
            "type": "PETG",
            "minTemp": 220,
            "maxTemp": 250,
        })
        entry_pla = build_entry(sample_db, {
            "id": "99014",
            "brand": "eSUN",
            "name": "eSUN PLA+ Optimized",
            "type": "PLA",
            "minTemp": 205,
            "maxTemp": 225,
        })
        insert_entry(sample_db, entry_petg)
        insert_entry(sample_db, entry_pla)

        cache = _cache_file(tmp_path, sample_db)
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        monkeypatch.setattr(sh, "_transport_factory", lambda c: FakeTransport())

        # Cloud returns 2 matching presets + 1 new preset
        fake_client = MagicMock()
        fake_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [
                {
                    "id": "cloud-elegoo-petg",
                    "content": {
                        "type": "filament",
                        "name": "Elegoo Rapid PETG Optimized",
                        "filament_type": "PETG",
                        "filament_vendor": "Elegoo",
                        "nozzle_temperature": "240",
                        "nozzle_temperature_range_low": "220",
                        "nozzle_temperature_range_high": "250",
                    },
                },
                {
                    "id": "cloud-esun-pla",
                    "content": {
                        "type": "filament",
                        "name": "eSUN PLA+ Optimized",
                        "filament_type": "PLA",
                        "filament_vendor": "eSUN",
                        "nozzle_temperature": "215",
                        "nozzle_temperature_range_low": "205",
                        "nozzle_temperature_range_high": "225",
                    },
                },
                {
                    "id": "cloud-overture-abs",
                    "content": {
                        "type": "filament",
                        "name": "Overture ABS Turbo",
                        "filament_type": "ABS",
                        "filament_vendor": "Overture",
                        "nozzle_temperature": "250",
                        "nozzle_temperature_range_low": "240",
                        "nozzle_temperature_range_high": "260",
                    },
                },
            ],
            "deletes": [],
        }
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)

        # 1. Run push
        push_result = sh.handle_push(s, cfg, {})
        assert push_result["status"] == "ok"
        assert push_result["pushed"] == 3

        # 2. Check SyncTargets in database
        target_petg = s.scalars(
            select(SyncTarget).where(SyncTarget.cloud_id == "cloud-elegoo-petg")
        ).first()
        target_pla = s.scalars(
            select(SyncTarget).where(SyncTarget.cloud_id == "cloud-esun-pla")
        ).first()
        target_abs = s.scalars(
            select(SyncTarget).where(SyncTarget.cloud_id == "cloud-overture-abs")
        ).first()

        assert target_petg is not None
        assert target_petg.printer_db_id == "99008"

        assert target_pla is not None
        assert target_pla.printer_db_id == "99014"

        assert target_abs is not None
        assert target_abs.printer_db_id not in ("99008", "99014")
        assert target_abs.printer_db_id.startswith("99")

        # 3. Verify printer DB contents: exactly 3 custom entries (no duplicates)
        pushed_db = json.loads(cache.read_text())
        custom_entries = [
            e for e in pushed_db["result"]["list"]
            if e.get("base", {}).get("id", "").startswith("99")
        ]
        assert len(custom_entries) == 3
        custom_ids = {e["base"]["id"] for e in custom_entries}
        assert custom_ids == {"99008", "99014", target_abs.printer_db_id}

        # 4. Verify step: verify must report in_sync for all targets
        verify_transport = VerifyTransport(db=pushed_db)
        monkeypatch.setattr(sh, "_transport_factory", lambda c: verify_transport)

        verify_result = sh.handle_verify(s, cfg, {})
        assert verify_result["version_ok"] is True
        assert verify_result["cloud_rollback"] is False

        s.refresh(target_petg)
        s.refresh(target_pla)
        s.refresh(target_abs)
        assert target_petg.status == "in_sync"
        assert target_pla.status == "in_sync"
        assert target_abs.status == "in_sync"
```

- [ ] **Step 2: Run test to verify it passes**

Run:
```bash
pytest tests/test_sync_handlers.py::TestCleanDBReconciliation -v
```
Expected: PASS (`1 passed`).

- [ ] **Step 3: Run the full test suite**

Run:
```bash
./scripts/test.sh
```
Expected: All tests pass with zero regressions.

- [ ] **Step 4: Commit**

```bash
git add tests/test_sync_handlers.py
git commit -m "test(sync): add clean DB multi-instance reconciliation integration test"
```

---

## Self-Review

**1. Spec coverage:**
- Rebuild printer DB adopts matching custom entries by name/type/vendor: Covered by Tasks 1, 2, and 3.
- Custom entry ID persistence in `SyncTarget`: Covered by Tasks 2 and 3.
- Preserves existing custom entries without creating duplicate ghost entries: Covered by Tasks 2, 3, and 4.
- Verification passes without drift: Covered by Task 4.

**2. Placeholder scan:**
- Verified no placeholders, "TODO", "TBD", or incomplete code snippets exist in this plan.

**3. Type consistency:**
- `_find_matching_custom_entry` signature `(db: dict, name: str, ftype: str, vendor: str = "", assigned_ids: set[str] | None = None) -> dict | None` matches in Task 1, Task 2, and Task 3.
- `assigned_ids` set tracking pattern consistently accounts for both assigned `SyncTarget.printer_db_id` and `Profile.printer_db_id`.
- `build_entry`, `insert_entry`, `remove_entry` usage is identical across all tasks.
