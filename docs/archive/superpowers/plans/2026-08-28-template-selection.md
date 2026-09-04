# Template Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow users to select vendor/brand-specific OrcaSlicer preset templates (e.g. "Bambu PETG Translucent") when creating or editing filament profiles, so generated presets carry brand-specific tuning instead of only generic type-level defaults.

**Architecture:** Bundle OrcaSlicer `@base` filament presets (~356 files, ~1.5MB) into the app with a pre-built index. Add a `template_override` column to `Profile`. Render a template picker (vendor filter + template dropdown + preview) as an HTMX fragment in the profile form. The preset builder uses `template_override` to select and flatten the brand-specific template instead of the generic type-level default.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy, Jinja2, HTMX, SQLite

**Spec:** `docs/superpowers/specs/2026-08-28-template-selection-design.md` — the binding authority. UI details in `docs/superpowers/specs/2026-08-28-template-ui-review.md`.

## Global Constraints

- OrcaSlicer is installed at `/opt/orca-slicer` on the development machine
- Existing `flatten_preset()` in `fm/presets/flattener.py` resolves inheritance chains recursively — reuse it, do not reimplement
- Existing `find_preset_by_name()` in `fm/presets/flattener.py` searches `_BUNDLED_DIR` (the `fm/presets/templates/` directory) — new `@base` files placed there are automatically discoverable
- Existing `_TYPE_TO_TEMPLATE` map in `flattener.py` maps filament type → `fdm_filament_*.json` filename — do not modify it
- DB migrations use the `_migrate()` pattern in `fm/db.py` (PRAGMA table_info + ALTER TABLE ADD COLUMN)
- Tests use `pytest` with `inmemory_session` fixture from `conftest.py` and `TestClient` from `fastapi.testclient`
- UI tests assert content markers in HTML (see `test_ui.py` pattern), not visual layout
- Form handlers in `ui.py` use `Form(...)` parameters and `_profile_from_form()` / `_submitted_namespace()` helpers
- The `_preset_template_factory` in `sync_handlers.py` is a module-level callable that tests monkeypatch — preserve this pattern
- No branching — work directly on main per user instruction

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `scripts/build_templates.py` | Create | One-time script: scan OrcaSlicer `@base` presets, deduplicate, copy to `fm/presets/templates/`, generate `template_index.json` |
| `fm/presets/templates/*.json` | Generated | ~356 `@base` preset files + existing 22 `fdm_filament_*.json` |
| `fm/presets/template_index.json` | Generated | Pre-built index: name, vendor, type, file for each template |
| `fm/presets/template_index.py` | Create | Runtime loader: query templates by type/vendor, extract preview values |
| `fm/models.py` | Modify | Add `template_override` column to `Profile` |
| `fm/db.py` | Modify | Add migration for `template_override` column |
| `fm/api/catalog.py` | Modify | Add `template_override` to Pydantic schemas + `/api/templates` endpoint |
| `fm/api/ui.py` | Modify | Fragment endpoint, form handler updates, orca fragment template name |
| `fm/api/templates/profile_form.html` | Modify | Add template picker HTML |
| `fm/api/templates/template_dropdown.html` | Create | HTMX fragment for template picker fields |
| `fm/api/templates/orca_fragment.html` | Modify | Show human-readable template name |
| `fm/api/static/app.css` | Modify | Add template picker CSS |
| `fm/service/sync_handlers.py` | Modify | Update `_preset_template_factory` + `handle_generate_preset` |
| `tests/test_template_index.py` | Create | Test template index loader + preview |
| `tests/test_api_templates.py` | Create | Test `/api/templates` endpoint |
| `tests/test_ui_template_picker.py` | Create | Test fragment endpoint + form integration |
| `tests/test_sync_handlers.py` | Modify | Add template override builder test |

---

### Task 1: Bundle Script + Generate Template Files

**Files:**
- Create: `scripts/build_templates.py`
- Generated: `fm/presets/templates/*.json` (~356 `@base` files)
- Generated: `fm/presets/template_index.json`
- Test: verify output counts manually

**Interfaces:**
- Consumes: `fm/presets/flattener.py` (`flatten_preset`, `find_preset_by_name`, `_TYPE_TO_TEMPLATE`, `_BUNDLED_DIR`)
- Produces: `fm/presets/template_index.json` with structure `{"templates": [{"name": str, "vendor": str, "type": str, "file": str}]}`

- [ ] **Step 1: Write the bundle script**

```python
#!/usr/bin/env python3
"""build_templates.py — Extract, deduplicate, and index OrcaSlicer @base filament presets.

Scans /opt/orca-slicer/resources/profiles for @base filament presets,
deduplicates by display name (preferring vendor-specific copies over
OrcaFilamentLibrary copies), copies them to fm/presets/templates/,
and generates fm/presets/template_index.json with vendor + type metadata.

Run once during development. Generated files are committed to git.
"""
import json
import shutil
import sys
from pathlib import Path

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fm.presets.flattener import flatten_preset, _TYPE_TO_TEMPLATE, _BUNDLED_DIR

ORCA_PROFILES = Path("/opt/orca-slicer/resources/profiles")
TEMPLATES_DIR = _BUNDLED_DIR  # fm/presets/templates/
INDEX_PATH = TEMPLATES_DIR.parent / "template_index.json"

KNOWN_BRANDS = {
    "Snapmaker", "Anker", "Bambu", "Creality", "Polymaker", "SUNLU",
    "eSUN", "Overture", "Elegoo", "Cubicon", "Flashforge", "FusRock",
    "Numakers", "COEX", "FILL3D", "FDplast", "DREMC", "Elas", "FilAr",
    "NIT", "Valment", "GreenGate3D", "BETA", "AliZ", "OrcaArena",
}


def find_base_presets():
    """Find all @base filament presets, deduplicated by display name.
    Returns dict: display_name -> Path (preferring vendor-specific copy)."""
    presets = {}
    ofl_dir = ORCA_PROFILES / "OrcaFilamentLibrary" / "filament"

    for f in ORCA_PROFILES.rglob("*@base.json"):
        if "/filament/" not in str(f):
            continue
        display_name = f.stem.replace(" @base", "")
        # Prefer non-OFL copies (vendor-specific dirs have richer data)
        is_ofl = ofl_dir in f.parents
        if display_name not in presets:
            presets[display_name] = f
        elif is_ofl and display_name in presets:
            # Keep existing non-OFL copy
            pass
        elif not is_ofl:
            # Replace OFL copy with vendor-specific copy
            presets[display_name] = f

    return presets


def resolve_vendor(flat, display_name):
    """Extract vendor from flattened preset, or derive from name prefix."""
    fv = flat.get("filament_vendor")
    if isinstance(fv, list) and fv:
        return fv[0]
    if isinstance(fv, str) and fv:
        return fv
    # Derive from name prefix
    parts = display_name.split()
    if parts and parts[0] in KNOWN_BRANDS:
        if parts[0] == "Bambu":
            return "Bambu Lab"
        return parts[0]
    return "Generic"


def resolve_type(flat):
    """Extract filament_type from flattened preset."""
    ft = flat.get("filament_type")
    if isinstance(ft, list) and ft:
        return ft[0]
    if isinstance(ft, str) and ft:
        return ft
    return ""


def main():
    if not ORCA_PROFILES.exists():
        print(f"ERROR: OrcaSlicer profiles not found at {ORCA_PROFILES}", file=sys.stderr)
        sys.exit(1)

    presets = find_base_presets()
    print(f"Found {len(presets)} unique @base presets")

    templates = []

    # Copy @base files and build index entries
    for display_name, src_path in sorted(presets.items()):
        dest = TEMPLATES_DIR / f"{display_name} @base.json"
        shutil.copy2(src_path, dest)

        # Flatten to resolve vendor + type
        try:
            flat = flatten_preset(dest)
        except Exception as e:
            print(f"WARN: failed to flatten {display_name}: {e}", file=sys.stderr)
            continue

        vendor = resolve_vendor(flat, display_name)
        ftype = resolve_type(flat)

        if not ftype:
            print(f"WARN: no filament_type for {display_name}", file=sys.stderr)
            continue

        templates.append({
            "name": display_name,
            "vendor": vendor,
            "type": ftype,
            "file": f"{display_name} @base.json",
        })

    # Add existing fdm_filament_*.json as Generic templates
    for ftype, filename in sorted(_TYPE_TO_TEMPLATE.items()):
        # Skip duplicate type entries (e.g. "PLA Silk" and "PLA-Silk" both map to same file)
        generic_name = f"Generic {ftype}"
        # Avoid duplicates if a Generic @base already exists for this type
        if any(t["name"] == generic_name and t["type"] == ftype for t in templates):
            continue
        templates.append({
            "name": generic_name,
            "vendor": "Generic",
            "type": ftype,
            "file": filename,
        })

    # Sort: Generic first, then by vendor, then by name
    templates.sort(key=lambda t: (t["vendor"] != "Generic", t["vendor"], t["name"]))

    index = {"templates": templates}
    INDEX_PATH.write_text(json.dumps(index, indent=2, ensure_ascii=False))
    print(f"Wrote {len(templates)} templates to {INDEX_PATH}")
    print(f"Files in {TEMPLATES_DIR}: {len(list(TEMPLATES_DIR.glob('*.json')))}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the script**

Run: `cd "/home/oliverzein/Dokumente/Daten/Development/3D Printing/filament-manager" && python scripts/build_templates.py`
Expected: Output like "Found 356 unique @base presets" and "Wrote 378 templates to ..."

- [ ] **Step 3: Verify output**

Run: `python -c "import json; d=json.load(open('fm/presets/template_index.json')); print(len(d['templates']), 'templates'); print('PETG:', [t['name'] for t in d['templates'] if t['type']=='PETG']); assert any(t['name']=='Bambu PETG Translucent' for t in d['templates']), 'Missing Bambu PETG Translucent'; print('OK')"`
Expected: Lists PETG templates including "Bambu PETG Translucent", prints "OK"

- [ ] **Step 4: Commit**

```bash
cd "/home/oliverzein/Dokumente/Daten/Development/3D Printing/filament-manager"
git add scripts/build_templates.py fm/presets/templates/ fm/presets/template_index.json
git commit -m "feat: bundle OrcaSlicer @base presets + template index"
```

---

### Task 2: Template Index Loader

**Files:**
- Create: `fm/presets/template_index.py`
- Test: `tests/test_template_index.py`

**Interfaces:**
- Consumes: `fm/presets/template_index.json` (generated in Task 1), `fm/presets/flattener.py` (`find_preset_by_name`, `flatten_preset`)
- Produces: `load_index() -> dict`, `templates_for_type(type) -> list[dict]`, `templates_for_type_and_vendor(type, vendor) -> list[dict]`, `all_vendors_for_type(type) -> list[str]`, `preview_for_template(name) -> dict | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_template_index.py
"""Tests for the template index loader."""
from fm.presets.template_index import (
    load_index,
    templates_for_type,
    templates_for_type_and_vendor,
    all_vendors_for_type,
    preview_for_template,
)


def test_load_index_has_templates():
    idx = load_index()
    assert "templates" in idx
    assert len(idx["templates"]) > 300


def test_templates_for_type_petg():
    result = templates_for_type("PETG")
    assert len(result) > 0
    names = [t["name"] for t in result]
    assert "Bambu PETG Translucent" in names
    # Generic first
    assert result[0]["vendor"] == "Generic"


def test_templates_for_type_and_vendor_bambu():
    result = templates_for_type_and_vendor("PETG", "Bambu")
    assert len(result) > 0
    assert all("bambu" in t["vendor"].lower() for t in result)


def test_templates_for_type_and_vendor_empty():
    result = templates_for_type_and_vendor("PETG", "")
    # Empty vendor = no filter, should return all PETG templates
    assert len(result) == len(templates_for_type("PETG"))


def test_all_vendors_for_type_pla():
    vendors = all_vendors_for_type("PLA")
    assert "Generic" in vendors
    assert vendors[0] == "Generic"
    assert "Bambu Lab" in vendors


def test_preview_for_template_bambu_petg():
    preview = preview_for_template("Bambu PETG Translucent")
    assert preview is not None
    assert preview["filament_flow_ratio"] == "0.95"
    assert preview["fan_max_speed"] == "30"


def test_preview_for_template_nonexistent():
    preview = preview_for_template("Nonexistent Template")
    assert preview is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_template_index.py -v`
Expected: FAIL with `ImportError` or `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
# fm/presets/template_index.py
"""template_index.py — Runtime loader for the pre-built template index.

Queries the template index (generated by scripts/build_templates.py) to
list available preset templates filtered by filament type and vendor,
and extract preview values from flattened templates.
"""
import json
from pathlib import Path

_INDEX_PATH = Path(__file__).parent / "template_index.json"
_index_cache: dict | None = None


def load_index() -> dict:
    """Load template_index.json (cached after first call)."""
    global _index_cache
    if _index_cache is None:
        _index_cache = json.loads(_INDEX_PATH.read_text())
    return _index_cache


def templates_for_type(filament_type: str) -> list[dict]:
    """Return templates matching the given filament type.

    Sorted: Generic first, then by vendor, then by name.
    """
    idx = load_index()
    matches = [t for t in idx["templates"] if t["type"] == filament_type]
    matches.sort(key=lambda t: (t["vendor"] != "Generic", t["vendor"], t["name"]))
    return matches


def templates_for_type_and_vendor(filament_type: str, vendor: str) -> list[dict]:
    """Return templates matching type and vendor (case-insensitive partial match).

    Empty vendor string returns all templates for the type (no filter).
    """
    idx = load_index()
    vendor_lower = vendor.lower().strip()
    matches = [t for t in idx["templates"] if t["type"] == filament_type]
    if vendor_lower:
        matches = [t for t in matches if vendor_lower in t["vendor"].lower()]
    matches.sort(key=lambda t: (t["vendor"] != "Generic", t["vendor"], t["name"]))
    return matches


def all_vendors_for_type(filament_type: str) -> list[str]:
    """Return unique vendor names that have templates for the given type.

    Generic is always first if present.
    """
    idx = load_index()
    vendors = sorted({t["vendor"] for t in idx["templates"] if t["type"] == filament_type})
    if "Generic" in vendors:
        vendors.remove("Generic")
        vendors.insert(0, "Generic")
    return vendors


def preview_for_template(name: str) -> dict | None:
    """Flatten the named template and extract 4 key preview values.

    Returns None if the template file cannot be found.
    """
    from .flattener import find_preset_by_name, flatten_preset

    idx = load_index()
    entry = next((t for t in idx["templates"] if t["name"] == name), None)
    if entry is None:
        return None
    path = find_preset_by_name(entry["file"])
    if path is None:
        return None
    flat = flatten_preset(path)

    def _first(key):
        v = flat.get(key)
        if isinstance(v, list) and v:
            return v[0]
        return v

    return {
        "nozzle_temperature": _first("nozzle_temperature"),
        "filament_flow_ratio": _first("filament_flow_ratio"),
        "fan_min_speed": _first("fan_min_speed"),
        "fan_max_speed": _first("fan_max_speed"),
        "filament_max_volumetric_speed": _first("filament_max_volumetric_speed"),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_template_index.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add fm/presets/template_index.py tests/test_template_index.py
git commit -m "feat: add template index loader with type/vendor filtering + preview"
```

---

### Task 3: Model + Migration + API Schemas

**Files:**
- Modify: `fm/models.py` (add `template_override` to `Profile`)
- Modify: `fm/db.py` (add migration)
- Modify: `fm/api/catalog.py` (add `template_override` to schemas)
- Test: `tests/test_template_index.py` (no new test file — verify via existing test suite + manual check)

**Interfaces:**
- Consumes: existing `Profile` model, `_migrate()` pattern
- Produces: `Profile.template_override: Mapped[str | None]`, migration in `_migrate()`, `template_override` field in `ProfileCreate`, `ProfileUpdate`, `ProfileOut`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_template_index.py — append to existing file

def test_profile_template_override_persists(inmemory_session):
    """Verify template_override column exists and persists."""
    from datetime import datetime, timezone
    from fm.models import Profile
    s = inmemory_session
    now = datetime.now(timezone.utc)
    p = Profile(
        brand="Bambu Lab", name="PETG Translucent", type="PETG",
        colors='[]', min_temp=230, max_temp=270, density=1.25,
        drying_temp=65, drying_time=8, created_at=now, updated_at=now,
        template_override="Bambu PETG Translucent",
    )
    s.add(p)
    s.flush()
    assert p.template_override == "Bambu PETG Translucent"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_template_index.py::test_profile_template_override_persists -v`
Expected: FAIL with `TypeError: 'template_override' is not a valid keyword argument` or similar

- [ ] **Step 3: Add `template_override` to Profile model**

In `fm/models.py`, add after line 36 (`filament_id` line):

```python
    template_override: Mapped[str | None] = mapped_column(String(255), default=None)
```

Insert this line between the `filament_id` field (line 36) and the `notes` field (line 37). The exact insertion point is after:
```python
    filament_id: Mapped[str | None] = mapped_column(default=None)  # preset filament_id once generated
```
and before:
```python
    notes: Mapped[str] = mapped_column(default="")
```

- [ ] **Step 4: Add migration to `fm/db.py`**

In `_migrate()`, after the existing `sync_targets.cloud_id` migration block, add:

```python
        # profiles.template_override — added for preset template selection
        profile_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(profiles)"))}
        if "template_override" not in profile_cols:
            conn.execute(text("ALTER TABLE profiles ADD COLUMN template_override VARCHAR(255)"))
            conn.commit()
```

- [ ] **Step 5: Add `template_override` to Pydantic schemas in `fm/api/catalog.py`**

In `ProfileCreate` (after `nozzle_temperature` field, before `notes`):
```python
    template_override: str | None = None
```

In `ProfileUpdate` (after `nozzle_temperature` field, before `notes`):
```python
    template_override: str | None = None
```

In `ProfileOut` (after `filament_id` field):
```python
    template_override: str | None = None
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_template_index.py::test_profile_template_override_persists -v`
Expected: PASS

- [ ] **Step 7: Run full test suite to verify no regressions**

Run: `pytest tests/ -x -q`
Expected: All existing tests still pass

- [ ] **Step 8: Commit**

```bash
git add fm/models.py fm/db.py fm/api/catalog.py tests/test_template_index.py
git commit -m "feat: add template_override column to Profile + migration + API schemas"
```

---

### Task 4: API Templates Endpoint

**Files:**
- Modify: `fm/api/catalog.py` (add `/api/templates` endpoint)
- Create: `tests/test_api_templates.py`

**Interfaces:**
- Consumes: `fm/presets/template_index.py` (`load_index`, `all_vendors_for_type`)
- Produces: `GET /api/templates?type=PETG&vendor=Bambu` → `{"templates": [...], "vendors": [...]}`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_templates.py
"""Tests for the /api/templates endpoint."""
import pytest
from fastapi.testclient import TestClient

from fm.api.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FM_DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    with TestClient(create_app()) as c:
        yield c


def test_list_templates_no_filter(client):
    r = client.get("/api/templates")
    assert r.status_code == 200
    data = r.json()
    assert "templates" in data
    assert len(data["templates"]) > 300


def test_list_templates_filtered_by_type(client):
    r = client.get("/api/templates?type=PETG")
    assert r.status_code == 200
    data = r.json()
    assert all(t["type"] == "PETG" for t in data["templates"])
    assert "Bambu PETG Translucent" in [t["name"] for t in data["templates"]]


def test_list_templates_filtered_by_type_and_vendor(client):
    r = client.get("/api/templates?type=PETG&vendor=Bambu")
    assert r.status_code == 200
    data = r.json()
    assert all("bambu" in t["vendor"].lower() for t in data["templates"])
    assert "vendors" in data


def test_list_templates_vendors_for_type(client):
    r = client.get("/api/templates?type=PLA")
    assert r.status_code == 200
    data = r.json()
    assert "Generic" in data["vendors"]
    assert data["vendors"][0] == "Generic"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api_templates.py -v`
Expected: FAIL with 404 or route not found

- [ ] **Step 3: Add the endpoint to `fm/api/catalog.py`**

Add after the existing profile endpoints (before the validate endpoint or at the end of the file):

```python
@router.get("/templates")
def list_templates(type: str | None = None, vendor: str | None = None) -> dict:
    """List available preset templates, optionally filtered by type and vendor."""
    from ..presets.template_index import load_index, all_vendors_for_type
    idx = load_index()
    templates = idx["templates"]
    if type:
        templates = [t for t in templates if t["type"] == type]
    if vendor:
        vendor_lower = vendor.lower()
        templates = [t for t in templates if vendor_lower in t["vendor"].lower()]
    templates = sorted(templates, key=lambda t: (t["vendor"] != "Generic", t["vendor"], t["name"]))
    if type:
        vendors = all_vendors_for_type(type)
    else:
        vendors = sorted({t["vendor"] for t in idx["templates"]})
    return {"templates": templates, "vendors": vendors}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_api_templates.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add fm/api/catalog.py tests/test_api_templates.py
git commit -m "feat: add /api/templates endpoint with type+vendor filtering"
```

---

### Task 5: Fragment Endpoint + Template Dropdown HTML

**Files:**
- Create: `fm/api/templates/template_dropdown.html`
- Modify: `fm/api/ui.py` (add fragment routes)
- Create: `tests/test_ui_template_picker.py`

**Interfaces:**
- Consumes: `fm/presets/template_index.py` (`templates_for_type`, `templates_for_type_and_vendor`, `all_vendors_for_type`, `preview_for_template`), `fm/models.py` (`Profile`)
- Produces: `GET /profiles/{pid}/template-dropdown` and `GET /profiles/new/template-dropdown` → HTML fragment

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ui_template_picker.py
"""Tests for the template picker HTMX fragment endpoint."""
import pytest
from fastapi.testclient import TestClient

from fm.api.app import create_app
from fm.db import SessionLocal
from fm.models import Profile


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FM_DATABASE_URL", f"sqlite:///{tmp_path}/picker.db")
    with TestClient(create_app()) as c:
        yield c


def test_template_dropdown_new_profile(client):
    """Fragment for new profile with type=PETG returns vendor + template selects."""
    r = client.get("/profiles/new/template-dropdown?type=PETG")
    assert r.status_code == 200
    assert b'template-vendor' in r.content
    assert b'template-override' in r.content
    assert b'All vendors' in r.content
    assert b'Use Generic PETG defaults' in r.content
    assert b'Bambu PETG Translucent' in r.content


def test_template_dropdown_filtered_by_vendor(client):
    """Fragment with vendor filter narrows template list."""
    r = client.get("/profiles/new/template-dropdown?type=PETG&template_vendor=Bambu+Lab")
    assert r.status_code == 200
    assert b'Bambu PETG Translucent' in r.content
    # Non-Bambu templates should not appear in optgroups
    assert b'Creality Generic PETG' not in r.content


def test_template_dropdown_type_change_resets(client):
    """Type change resets vendor and template to generic default."""
    r = client.get("/profiles/new/template-dropdown?type=PLA&reason=type-change")
    assert r.status_code == 200
    assert b'Use Generic PLA defaults' in r.content
    assert b'reset' in r.content.lower() or b'Generic PLA' in r.content


def test_template_dropdown_with_preview(client):
    """Selecting a template shows preview values."""
    r = client.get("/profiles/new/template-dropdown?type=PETG&template_override=Bambu+PETG+Translucent")
    assert r.status_code == 200
    assert b'template-preview' in r.content
    assert b'0.95' in r.content  # flow ratio


def test_template_dropdown_edit_profile(client):
    """Fragment for existing profile shows saved template_override selected."""
    # Create a profile via API
    r = client.post("/api/profiles", json={
        "brand": "Bambu Lab", "name": "PETG Translucent", "type": "PETG",
        "min_temp": 230, "max_temp": 270, "density": 1.25,
        "template_override": "Bambu PETG Translucent",
    })
    assert r.status_code == 201
    pid = r.json()["id"]

    r = client.get(f"/profiles/{pid}/template-dropdown?type=PETG&template_override=Bambu+PETG+Translucent")
    assert r.status_code == 200
    assert b'selected' in r.content
    assert b'Bambu PETG Translucent' in r.content


def test_profile_form_has_template_picker(client):
    """Profile form page includes the template picker container."""
    r = client.get("/profiles/new")
    assert r.status_code == 200
    assert b'template-picker' in r.content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ui_template_picker.py -v`
Expected: FAIL with 404 or missing content

- [ ] **Step 3: Create the template dropdown fragment**

```html
{# fm/api/templates/template_dropdown.html — HTMX fragment for template picker fields #}
<div id="template-picker-fields" class="template-picker-fields" aria-busy="false">
  <label for="template-vendor">
    <span>Filter by vendor</span>
    <select id="template-vendor" name="template_vendor"
            hx-get="/profiles/{{ pid_or_new }}/template-dropdown"
            hx-trigger="change"
            hx-target="#template-picker-fields"
            hx-swap="innerHTML focus-scroll:false"
            hx-include="[name='type'], this, [name='template_override']"
            hx-indicator="closest .template-picker">
      <option value="">All vendors</option>
      {% for vendor in vendors %}
      <option value="{{ vendor }}" {% if vendor == selected_vendor %}selected{% endif %}>{{ vendor }}</option>
      {% endfor %}
    </select>
    <small>Optional filter; it is not saved with the profile.</small>
  </label>

  <label for="template-override">
    <span>Template</span>
    <select id="template-override" name="template_override"
            aria-describedby="template-picker-help template-preview-note"
            hx-get="/profiles/{{ pid_or_new }}/template-dropdown"
            hx-trigger="change"
            hx-target="#template-picker-fields"
            hx-swap="innerHTML focus-scroll:false"
            hx-include="[name='type'], [name='template_vendor'], this"
            hx-indicator="closest .template-picker">
      <option value="">Use Generic {{ selected_type }} defaults (recommended)</option>
      {% for group in template_groups %}
      <optgroup label="{{ group.vendor }}">
        {% for template in group.templates %}
        <option value="{{ template.name }}" {% if template.name == selected_template %}selected{% endif %}>{{ template.name }}</option>
        {% endfor %}
      </optgroup>
      {% endfor %}
    </select>
  </label>

  <div class="template-preview full-width" aria-labelledby="template-preview-title">
    <h4 id="template-preview-title">Template preview</h4>
    {% if preview %}
    <dl class="template-preview-values">
      <div><dt>Nozzle</dt><dd>{{ preview.nozzle_temperature or '—' }}{% if preview.nozzle_temperature %} °C{% endif %}</dd></div>
      <div><dt>Flow ratio</dt><dd>{{ preview.filament_flow_ratio or '—' }}</dd></div>
      <div><dt>Fan</dt><dd>{{ preview.fan_min_speed or '—' }}–{{ preview.fan_max_speed or '—' }}{% if preview.fan_max_speed %} %{% endif %}</dd></div>
      <div><dt>Max volumetric</dt><dd>{{ preview.filament_max_volumetric_speed or '—' }}{% if preview.filament_max_volumetric_speed %} mm³/s{% endif %}</dd></div>
    </dl>
    {% else %}
    <dl class="template-preview-values">
      <div><dt>Nozzle</dt><dd>—</dd></div>
      <div><dt>Flow ratio</dt><dd>—</dd></div>
      <div><dt>Fan</dt><dd>—</dd></div>
      <div><dt>Max volumetric</dt><dd>—</dd></div>
    </dl>
    {% endif %}
    <p id="template-preview-note">Base values; profile settings may override them.</p>
  </div>

  {% if status_message %}
  <p class="template-picker-status sr-only" role="status" aria-live="polite" aria-atomic="true">{{ status_message }}</p>
  {% endif %}
</div>
```

- [ ] **Step 4: Add fragment routes to `fm/api/ui.py`**

Add these routes after the existing `profile_edit` route (around line 200):

```python
def _template_dropdown_impl(
    request: Request, pid: int | None,
    type: str, template_vendor: str, template_override: str, reason: str,
) -> HTMLResponse:
    """Shared implementation for template dropdown fragment."""
    from ..presets.template_index import (
        templates_for_type_and_vendor,
        all_vendors_for_type,
        preview_for_template,
    )
    from collections import OrderedDict

    selected_type = type
    selected_vendor = template_vendor if reason != "type-change" else ""
    selected_template = template_override if reason != "type-change" else ""

    vendors = all_vendors_for_type(selected_type)
    templates = templates_for_type_and_vendor(selected_type, selected_vendor)

    # Group templates by vendor
    groups = OrderedDict()
    for t in templates:
        groups.setdefault(t["vendor"], []).append(t)
    template_groups = [{"vendor": v, "templates": ts} for v, ts in groups.items()]

    # Get preview for selected template
    preview = None
    if selected_template:
        preview = preview_for_template(selected_template)

    status_message = ""
    if reason == "type-change":
        status_message = f"Template reset to Generic {selected_type} defaults because material type changed."

    pid_or_new = pid if pid else "new"

    return _templates(request).TemplateResponse(
        request, "template_dropdown.html",
        {
            "pid_or_new": pid_or_new,
            "selected_type": selected_type,
            "selected_vendor": selected_vendor,
            "selected_template": selected_template,
            "vendors": vendors,
            "template_groups": template_groups,
            "preview": preview,
            "status_message": status_message,
        },
    )


@router.get("/profiles/new/template-dropdown", response_class=HTMLResponse)
def template_dropdown_new(
    request: Request,
    type: str = "PLA",
    template_vendor: str = "",
    template_override: str = "",
    reason: str = "",
) -> HTMLResponse:
    """HTMX fragment for new profile (no pid)."""
    return _template_dropdown_impl(request, None, type, template_vendor, template_override, reason)


@router.get("/profiles/{pid}/template-dropdown", response_class=HTMLResponse)
def template_dropdown_edit(
    request: Request,
    pid: int,
    type: str = "PLA",
    template_vendor: str = "",
    template_override: str = "",
    reason: str = "",
) -> HTMLResponse:
    """HTMX fragment for existing profile."""
    return _template_dropdown_impl(request, pid, type, template_vendor, template_override, reason)
```

- [ ] **Step 5: Add template picker container to `profile_form.html`**

In `fm/api/templates/profile_form.html`, after the Colors field (line 13) and before the Notes field (line 14), insert:

```html
      <div class="template-picker full-width" aria-labelledby="template-picker-title">
        <div class="template-picker-heading">
          <div>
            <h3 id="template-picker-title">Preset source</h3>
            <p id="template-picker-help">Choose the OrcaSlicer base tuning used when generating this profile.</p>
          </div>
          <span class="template-picker-indicator htmx-indicator" aria-hidden="true"></span>
        </div>
        {% set pid_or_new = profile.id if profile else 'new' %}
        {% set selected_type = profile.type if profile else 'PLA' %}
        {% set selected_template = profile.template_override if profile else '' %}
        <div id="template-picker-fields"
             hx-get="/profiles/{{ pid_or_new }}/template-dropdown?type={{ selected_type }}&template_override={{ selected_template }}"
             hx-trigger="load"
             hx-swap="innerHTML"
             hx-indicator="closest .template-picker">
          <p class="orca-loading">Loading templates…</p>
        </div>
      </div>
```

Also update the Type `<select>` to trigger template reload on change. Find the existing type select:
```html
<select name="type" required>
```
Replace with:
```html
<select id="profile-type" name="type" required
        hx-get="/profiles/{{ pid_or_new }}/template-dropdown"
        hx-trigger="change"
        hx-target="#template-picker-fields"
        hx-swap="innerHTML focus-scroll:false"
        hx-include="this"
        hx-vals='{"reason":"type-change"}'
        hx-indicator=".template-picker-indicator">
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_ui_template_picker.py -v`
Expected: All 6 tests PASS

- [ ] **Step 7: Commit**

```bash
git add fm/api/templates/template_dropdown.html fm/api/ui.py fm/api/templates/profile_form.html tests/test_ui_template_picker.py
git commit -m "feat: add template picker fragment endpoint + HTML"
```

---

### Task 6: CSS + Form Handler Integration

**Files:**
- Modify: `fm/api/static/app.css` (add template picker styles)
- Modify: `fm/api/ui.py` (add `template_override` to form handlers)
- Modify: `fm/api/templates/orca_fragment.html` (show human-readable template name)
- Test: `tests/test_ui_template_picker.py` (extend with form submission test)

**Interfaces:**
- Consumes: Task 3 (`template_override` on Profile), Task 5 (fragment endpoint)
- Produces: CSS for `.template-picker`, form handlers that persist `template_override`, orca fragment with template name

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_ui_template_picker.py

def test_profile_form_submit_with_template(client):
    """Submitting the form with template_override persists it."""
    r = client.post("/profiles/form", data={
        "brand": "Bambu Lab", "name": "PETG Translucent", "type": "PETG",
        "min_temp": "230", "max_temp": "270", "density": "1.25",
        "drying_temp": "65", "drying_time": "8",
        "template_override": "Bambu PETG Translucent",
    }, follow_redirects=False)
    assert r.status_code == 303
    # Verify it persisted
    from fm.db import SessionLocal
    from fm.models import Profile
    with SessionLocal() as s:
        p = s.scalars(select(Profile).order_by(Profile.id.desc())).first()
        assert p.template_override == "Bambu PETG Translucent"


def test_profile_form_submit_without_template(client):
    """Submitting without template_override sets it to None."""
    r = client.post("/profiles/form", data={
        "brand": "Sunlu", "name": "PLA+", "type": "PLA",
        "min_temp": "200", "max_temp": "240", "density": "1.24",
        "drying_temp": "60", "drying_time": "8",
        "template_override": "",
    }, follow_redirects=False)
    assert r.status_code == 303
    from fm.db import SessionLocal
    from fm.models import Profile
    with SessionLocal() as s:
        p = s.scalars(select(Profile).order_by(Profile.id.desc())).first()
        assert p.template_override is None
```

Add `from sqlalchemy import select` to the imports if not already present.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ui_template_picker.py::test_profile_form_submit_with_template -v`
Expected: FAIL — `template_override` not accepted by form handler

- [ ] **Step 3: Add `template_override` to form handlers in `fm/api/ui.py`**

In `_profile_from_form` (around line 74), add `template_override: str = ""` parameter and add to the returned dict:
```python
def _profile_from_form(
    brand: str, name: str, type_: str, colors: str,
    min_temp: int, max_temp: int, density: float,
    drying_temp: int, drying_time: int, notes: str,
    template_override: str = "",
) -> dict:
    return {
        "brand": brand, "name": name, "type": type_,
        "colors": json.dumps(_parse_colors(colors)),
        "min_temp": min_temp, "max_temp": max_temp,
        "density": density, "drying_temp": drying_temp,
        "drying_time": drying_time, "notes": notes,
        "template_override": template_override or None,
    }
```

In `_submitted_namespace` (around line 89), add `template_override: str = ""` parameter and add to the namespace:
```python
def _submitted_namespace(
    pid: int | None, brand: str, name: str, type_: str, colors: str,
    min_temp: int, max_temp: int, density: float,
    drying_temp: int, drying_time: int, notes: str,
    template_override: str = "",
) -> SimpleNamespace:
    ns = SimpleNamespace(
        id=pid, brand=brand, name=name, type=type_,
        colors=_parse_colors(colors), min_temp=min_temp, max_temp=max_temp,
        density=density, drying_temp=drying_temp, drying_time=drying_time,
        notes=notes, printer_db_id=None, filament_id=None,
        template_override=template_override or None,
    )
    return ns
```

In `profile_create_form` (around line 204), add parameter and pass through:
```python
    template_override: str = Form(""),
```
Add it to the `_validate` call is NOT needed (no validation for template_override). Add it to the `_profile_from_form` call and `_submitted_namespace` call.

In `profile_update_form` (around line 248), add the same `template_override: str = Form("")` parameter and pass through to `_profile_from_form` and `_submitted_namespace`.

- [ ] **Step 4: Update orca fragment to show human-readable template name**

In `fm/api/ui.py`, find the `orca_fragment` function (around line 332) and the `sync_from_orca` function (around line 310). Replace the `template_name` logic in both:

For `orca_fragment`:
```python
    if profile.template_override:
        template_name = profile.template_override
    else:
        template_name = f"Generic {profile.type} defaults"
```

For `sync_from_orca`, same pattern — replace `template_name = template_name_for_type(profile.type)` with:
```python
    if profile.template_override:
        template_name = profile.template_override
    else:
        template_name = f"Generic {profile.type} defaults"
```

- [ ] **Step 5: Add CSS for template picker**

Append to `fm/api/static/app.css`:

```css
.template-picker { display: grid; gap: var(--space-4); padding-top: var(--space-5); border-top: 1px solid var(--border); }
.template-picker-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-4); }
.template-picker-heading h3 { margin: 0; font-family: var(--font-heading); font-size: 14px; line-height: 1.3; }
.template-picker-heading p { margin: var(--space-1) 0 0; font-size: 11px; }
.template-picker-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-4) var(--space-5); }
.template-picker-indicator { width: 18px; height: 18px; flex: 0 0 auto; border: 2px solid var(--border-strong); border-top-color: var(--accent); border-radius: 50%; animation: tc-spin .7s linear infinite; }
.template-preview { padding: var(--space-4); border: 1px solid var(--border); border-radius: var(--radius-input); background: var(--surface-alt); }
.template-preview h4 { margin-bottom: var(--space-3); color: var(--text-secondary); font-size: 11px; }
.template-preview-values { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--space-4); margin: 0; }
.template-preview-values div { min-width: 0; }
.template-preview-values dt { color: var(--text-muted); font-size: 9px; font-weight: 600; }
.template-preview-values dd { margin: var(--space-1) 0 0; color: var(--text); font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }
.template-preview > p { margin: var(--space-3) 0 0; color: var(--text-muted); font-size: 9px; }
.template-picker[aria-busy='true'] .template-picker-fields, .template-picker-fields[aria-busy='true'] { opacity: .7; }
.template-picker-error { grid-column: 1 / -1; margin: 0; padding: var(--space-3) var(--space-4); border: 1px solid rgba(255, 90, 101, .25); border-radius: var(--radius-input); background: rgba(255, 90, 101, .08); color: #ff9ba2; font-size: 11px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
@media (max-width: 760px) { .template-picker-fields, .template-preview-values { grid-template-columns: 1fr; } }
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_ui_template_picker.py -v`
Expected: All 8 tests PASS

- [ ] **Step 7: Run full test suite**

Run: `pytest tests/ -x -q`
Expected: All tests pass

- [ ] **Step 8: Commit**

```bash
git add fm/api/static/app.css fm/api/ui.py fm/api/templates/orca_fragment.html tests/test_ui_template_picker.py
git commit -m "feat: template picker CSS + form handler integration + orca fragment template name"
```

---

### Task 7: Builder Integration

**Files:**
- Modify: `fm/service/sync_handlers.py` (update `_preset_template_factory` + `handle_generate_preset`)
- Modify: `tests/test_sync_handlers.py` (add template override test)

**Interfaces:**
- Consumes: `fm/presets/flattener.py` (`find_preset_by_name`, `flatten_preset`, `find_system_preset`), `fm/models.py` (`Profile.template_override`)
- Produces: `_preset_template_factory` that resolves `template_override` → flattened `@base` template, `handle_generate_preset` that passes `template_override` through

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_sync_handlers.py, inside TestGeneratePresetHandler class

    def test_template_override_uses_brand_template(self, inmemory_session, cfg, monkeypatch):
        """When template_override is set, the builder uses the brand-specific template."""
        s = inmemory_session
        profile = _make_profile(s, template_override="Bambu PETG Translucent", type="PETG")
        fake_client = MagicMock()
        fake_client.cloud_id.return_value = "test-cloud-uuid"
        fake_client.sync_push.return_value = {"updated_time": 12345, "created_time": 12345}
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)

        # Use the real _preset_template_factory (not monkeypatched)
        result = sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        assert result["filament_id"].startswith("P")
        # The generated preset should have Bambu-specific flow ratio
        fake_client.sync_push.assert_called_once()
        content = fake_client.sync_push.call_args[1]["content"]
        import json as _json
        preset = _json.loads(content)
        assert preset["filament_flow_ratio"] == ["0.95"]
        assert preset["fan_max_speed"] == ["30"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sync_handlers.py::TestGeneratePresetHandler::test_template_override_uses_brand_template -v`
Expected: FAIL — the current `_preset_template_factory` ignores `template_override`

- [ ] **Step 3: Update `_preset_template_factory` in `fm/service/sync_handlers.py`**

Replace the existing factory (line 71):
```python
_preset_template_factory: Callable[[dict], dict | None] = lambda p: find_system_preset(p["type"])
```
with:
```python
def _preset_template_factory(profile_dict: dict) -> dict | None:
    """Resolve the preset template for a profile.

    If template_override is set, use the brand-specific @base template.
    Otherwise fall back to the type-based generic default.
    """
    override = profile_dict.get("template_override")
    if override:
        from ..presets.flattener import find_preset_by_name, flatten_preset
        path = find_preset_by_name(override + " @base")
        if path:
            return flatten_preset(path)
        # Fall back to type-based default if override file not found
    return find_system_preset(profile_dict["type"])
```

- [ ] **Step 4: Update `handle_generate_preset` to pass `template_override`**

In `handle_generate_preset` (around line 467), add `template_override` to the `profile_dict`:

```python
    profile_dict = {
        "id": profile.id,
        "brand": profile.brand,
        "name": profile.name,
        "type": profile.type,
        "min_temp": profile.min_temp,
        "max_temp": profile.max_temp,
        "density": profile.density,
        "colors": profile.colors,
        "pa": profile.pa,
        "flow_ratio": profile.flow_ratio,
        "max_volumetric": profile.max_volumetric,
        "retraction_length": profile.retraction_length,
        "retraction_speed": profile.retraction_speed,
        "nozzle_temperature": profile.nozzle_temperature,
        "printer_db_id": profile.printer_db_id,
        "template_override": profile.template_override,
    }
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_sync_handlers.py::TestGeneratePresetHandler::test_template_override_uses_brand_template -v`
Expected: PASS

- [ ] **Step 6: Run full test suite to verify no regressions**

Run: `pytest tests/ -x -q`
Expected: All tests pass (existing tests that monkeypatch `_preset_template_factory` still work because it's still a module-level callable that can be replaced)

- [ ] **Step 7: Commit**

```bash
git add fm/service/sync_handlers.py tests/test_sync_handlers.py
git commit -m "feat: builder uses template_override for brand-specific preset generation"
```

---

### Task 8: Server-Side Validation + Smoke Test

**Files:**
- Modify: `fm/api/ui.py` (add type-mismatch validation for `template_override`)
- Modify: `tests/test_ui_template_picker.py` (add validation test)
- Test: manual smoke test via dev server

**Interfaces:**
- Consumes: `fm/presets/template_index.py` (`load_index`)
- Produces: server-side validation that clears mismatched `template_override`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_ui_template_picker.py

def test_type_mismatch_clears_template_override(client):
    """Submitting type=PLA with a PETG template_override clears the override."""
    r = client.post("/profiles/form", data={
        "brand": "Bambu Lab", "name": "PETG Translucent", "type": "PLA",
        "min_temp": "200", "max_temp": "240", "density": "1.24",
        "drying_temp": "60", "drying_time": "8",
        "template_override": "Bambu PETG Translucent",  # PETG template with PLA type
    }, follow_redirects=False)
    assert r.status_code == 303
    from fm.db import SessionLocal
    from fm.models import Profile
    with SessionLocal() as s:
        p = s.scalars(select(Profile).order_by(Profile.id.desc())).first()
        assert p.template_override is None  # cleared due to type mismatch
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ui_template_picker.py::test_type_mismatch_clears_template_override -v`
Expected: FAIL — `template_override` is persisted as "Bambu PETG Translucent" despite type mismatch

- [ ] **Step 3: Add validation to form handlers in `fm/api/ui.py`**

In `profile_create_form` and `profile_update_form`, after the `_validate` call and before creating/updating the profile, add:

```python
    # Validate template_override belongs to the submitted type
    if template_override:
        from ..presets.template_index import load_index
        idx = load_index()
        entry = next((t for t in idx["templates"] if t["name"] == template_override), None)
        if entry is None or entry["type"] != type:
            template_override = ""  # clear mismatched override
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ui_template_picker.py::test_type_mismatch_clears_template_override -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `pytest tests/ -x -q`
Expected: All tests pass

- [ ] **Step 6: Smoke test via dev server**

Run: `cd "/home/oliverzein/Dokumente/Daten/Development/3D Printing/filament-manager" && bash scripts/dev-start.sh`
Then open browser to `http://localhost:8000/profiles/new`:
1. Verify template picker appears in section 01
2. Select type=PETG, verify template dropdown loads PETG templates
3. Select vendor "Bambu Lab", verify template list narrows
4. Select "Bambu PETG Translucent", verify preview shows flow ratio 0.95
5. Fill in brand/name/temp, submit, verify profile created
6. Edit the profile, verify template selection is preserved
7. Stop dev server: `bash scripts/dev-stop.sh`

- [ ] **Step 7: Commit**

```bash
git add fm/api/ui.py tests/test_ui_template_picker.py
git commit -m "feat: server-side validation clears type-mismatched template_override"
```
