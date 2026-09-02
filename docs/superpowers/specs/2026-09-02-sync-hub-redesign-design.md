# Sync Hub Redesign — Design Spec

**Date:** 2026-09-02
**Status:** Draft (pending approval)
**Predecessor:** `docs/superpowers/specs/2026-08-26-filament-manager-design.md` (original architecture), `docs/sync-workflow-analysis.md` (workflow analysis W1–W9, confusion points V1–V14)
**Chosen layout:** M3 — Two-Column Split (see `fm/api/static/mockups/m3.html`)

---

## 1. Ziel & Problem

Die heutige Sync-Seite (`/sync`) bündelt Push-to-Printer und Generate-Preset hinter richtungslosen Labels: "Sync" wird für mindestens vier verschiedene Operationen verwendet (V1), "Sync from Orca" existiert an drei Orten (V2), der Generate-Preset-Button ist ein Icon ohne Text (V7), und "Push all profiles" ist die einzige Printer-Push-Option — es gibt keinen per-Profil-Push (V11). Gleichzeitig verteilen sich zwei Import-Workflows (Migration W5, Orca-Cloud-Import W6) auf zwei separate Seiten (`/migration`, `/import-orca`), die ähnlichen Zweck erfüllen (V6).

Dieser Spec konsolidiert die Sync-Seite zu einem **Sync Hub** (Layout M3: zweispaltig), führt ein richtungsdeutliches Naming-Schema ein (Kategorie A), fasst die Import-Seiten zusammen (Kategorie B), und macht per-Profil-Aktionen sichtbar (Kategorie D). Auto-Trigger-Sichtbarkeit (Kategorie C) und GitHub-Backup-Historie sind explizit Out-of-Scope.

Vollständige Workflow- und Verwirrungspunkt-Analyse: `docs/sync-workflow-analysis.md`.

---

## 2. Layout: Sync Hub (M3, `/sync`)

### Grundstruktur

Zweispaltig, wie im Mockup `fm/api/static/mockups/m3.html`:

```
┌─────────────────────────────────────────────────────────────┐
│ Page header: eyebrow "Connections" · h1 "Sync Hub"          │
├──────────────────────────────┬──────────────────────────────┤
│ LEFT (~65%, 2fr)             │ RIGHT (~35%, 1fr)            │
│                              │                              │
│ ┌──────────────────────────┐ │ ┌──────────────────────────┐ │
│ │ Drift-Tabelle (Panel)    │ │ │ Printer DB (Karte)       │ │
│ │ Health check · Live      │ │ │ Status + "Push to        │ │
│ │                          │ │ │ Printer" (global, alle)  │ │
│ │ Profile | Printer DB |   │ │ └──────────────────────────┘ │
│ │ Orca preset | Aktionen   │ │ ┌──────────────────────────┐ │
│ │                          │ │ │ Orca Cloud (Karte)       │ │
│ │ (5+ Zeilen, Text-Buttons)│ │ │ Status + "Pull from      │ │
│ └──────────────────────────┘ │ │ Cloud (all)"             │ │
│                              │ └──────────────────────────┘ │
│ ┌──────────────────────────┐ │ ┌──────────────────────────┐ │
│ │ Busy-Dialog (Alert,      │ │ │ Backup (Karte)           │ │
│ │ konditional)             │ │ │ Last backup + "Backup    │ │
│ └──────────────────────────┘ │ │ to GitHub"               │ │
│                              │ └──────────────────────────┘ │
├──────────────────────────────┴──────────────────────────────┤
│ Job-Queue (Panel, vollbreit)                                │
│ Recent jobs · Live (poll alle 3s)                           │
└─────────────────────────────────────────────────────────────┘
```

### 2.1 Drift-Tabelle (links, Panel)

Spalten: **Profile | Printer DB | Orca preset | Aktionen**

- **Profile-Spalte:** `cell-main` (brand + name, z.B. "Sunlu PLA+ Black"), `cell-sub` (type · diameter · nozzle_temperature, z.B. "PLA · 1.75mm · 220°C")
- **Printer DB-Spalte:** `status-badge` mit echten Status-Werten (siehe §3.2)
- **Orca preset-Spalte:** `status-badge` mit echten Status-Werten
- **Aktionen-Spalte:** Text-Buttons (`.button.button-ghost`, klein: `min-height:32px; padding:6px 10px; font-size:11px`), whitespace-nowrap

#### Zeilen-Aktionen (per Profil)

| Button | Bedingung | Endpoint | Workflow |
|--------|-----------|----------|----------|
| **"Push to Printer"** | Nur wenn `profile.printer_db_id` gesetzt | `POST /api/sync/push` mit Body `{"profile_ids": [id]}` | W1 (per-Profil-Subset) |
| **"Push to Cloud"** | Immer | `POST /api/sync/preset/{profile_id}` | W3 (generate_preset) |
| **"Pull from Cloud"** | Immer | `POST /api/sync/pull/{profile_id}` (neu, siehe §6.8) | W4 (adopt=True, per Profil) |

**"Not linked"-Fall:** Profile ohne `printer_db_id` — der "Push to Printer"-Button wird **ausgeblendet** (nicht deaktiviert). An seine Stelle tritt ein `muted`-Text "Not linked" (wie im Mockup). Grund: Ein deaktivierter Button suggeriert, dass eine Aktion möglich wäre, wenn man etwas umstellt; "Not linked" kommuniziert den Zustand klar. Das Profil kann trotzdem "Push to Cloud" und "Pull from Cloud" ausführen.

### 2.2 Ziel-Karten (rechts, drei gestapelte Karten)

Jede Karte ist ein `.panel` mit `.panel-heading` (eyebrow + h2) und Body-Bereich.

#### Karte 1: Printer DB

- Eyebrow: "Printer database"
- H2: "Printer DB"
- Status-Summary: "N in sync, M drifted, K pending" (berechnet aus drift-Daten)
- Global-Button: **"Push to Printer"** (`.button.button-primary`) → `POST /api/sync/push` ohne `profile_ids` (alle Profile, wie bisher)

#### Karte 2: Orca Cloud

- Eyebrow: "Orca Cloud"
- H2: "Orca Cloud"
- Status-Summary: "N in sync, M drifted" (orca_preset-Spalte aus drift)
- Beschreibungstext: "Pull adopts Cloud baseline and writes nozzle_temperature back. Push generates a preset from the catalog."
- Global-Button: **"Pull from Cloud (all)"** (`.button.button-secondary`) → `POST /api/sync/pull` (neuer Bulk-Endpoint, siehe §6.8) — ersetzt den bisherigen "Sync from Orca"-Button auf der Profiles-Seite

**Begründung Global-Pull:** Der bisherige "Sync from Orca"-Button auf der Profiles-Seite (`POST /profiles/sync-from-orca`, bulk, adopt=True) wird in den Sync Hub verschoben. Die Profiles-Seite ist ein Katalog-Verwaltungs-Ort, kein Sync-Ort. Der Pull-Button gehört zum Sync Hub, wo alle Cloud-Operationen zentralisiert sind. Der Profile-Edit-Orca-Fragment behält seinen per-Profil "Pull from Cloud"-Button (Kontext-Flow: nach Tuning in OrcaSlicer, siehe §4.3).

#### Karte 3: Backup

- Eyebrow: "Backup"
- H2: "GitHub backup"
- Last-Backup-Zeitstempel (aus `activity_log`, letzter `github_backup`-Eintrag mit outcome "ok")
- Auto-Trigger-Hinweis: "Auto-triggered after each Push to Cloud" (statischer Text, keine neue Funktionalität — W7 bleibt wie gehabt)
- Global-Button: **"Backup to GitHub"** (`.button.button-secondary`, nur wenn `github_configured`) → `POST /api/sync/backup-github`

### 2.3 Busy-Dialog ("Printer input required")

Platzierung: **unter der Drift-Tabelle** (im linken Spaltenbereich), als `.alert.alert-warning`.

Funktionalität **unverändert** zu heute: Wenn Jobs mit `status == "awaiting_input"` existieren, wird der Dialog gerendert. Pro awaiting-Job eine `.awaiting-job`-Zeile mit drei Buttons:
- **"Force reboot"** (`.button.button-danger`) → `POST /sync/jobs/{id}/resolve` mit `choice=force_reboot`
- **"Wait"** (`.button.button-secondary`) → `choice=wait`
- **"No reboot"** (`.button.button-ghost`) → `choice=no_reboot`

Der Dialog ersetzt nicht die Drift-Tabelle, sondern erscheint zusätzlich darunter.

### 2.4 Job-Queue ("Recent jobs")

Vollbreite Panel unter beiden Spalten. Unverändert zur heutigen Implementierung:
- Eyebrow "Queue", H2 "Recent jobs", `.live-label` "Live"
- `hx-get="/sync/progress?since=0"`, `hx-trigger="every 3s"`, `hx-swap="innerHTML"`
- Job-Cards mit `.job-card.status-{status}`, `.job-icon` (Job-ID), `.status-badge`

---

## 3. Naming-Schema (Kategorie A)

### 3.1 Label-Tabelle alt → neu (verbindlich)

| Alt | Neu | Ort | Workflow |
|-----|-----|-----|----------|
| "Sync from Orca" (Profiles-Seite) | **entfernt** — ersetzt durch "Pull from Cloud (all)" im Sync Hub | Sync Hub → Orca-Cloud-Karte | W4 (bulk, adopt=True) |
| "Sync from Orca" (Profile-Edit, Orca-Fragment) | **"Pull from Cloud"** | Profile-Edit → Orca-Fragment | W4 (per Profil, adopt=True) |
| "Sync now" (Import-Orca-Seite, empty state) | **entfernt** — Import-Orca wird Tab in /import (siehe §4) | — | — |
| "Push all profiles" (Sync-Seite) | **"Push to Printer"** (global, in Printer-DB-Karte) | Sync Hub → Printer-DB-Karte | W1 (alle) |
| Icon-Button (Upload-Icon, kein Text, Drift-Tabelle) | **"Push to Cloud"** (Text-Button) | Sync Hub → Drift-Tabelle, per Zeile | W3 |
| "Backup to GitHub" | **"Backup to GitHub"** (unverändert) | Sync Hub → Backup-Karte | W7 |
| "Generate preset" (aria-label) | **"Push to Cloud"** (sichtbarer Text) | Sync Hub → Drift-Tabelle | W3 |
| — (neu) | **"Push to Printer"** (per Zeile) | Sync Hub → Drift-Tabelle | W1 (per Profil) |
| — (neu) | **"Pull from Cloud"** (per Zeile) | Sync Hub → Drift-Tabelle | W4 (per Profil, adopt=True) |
| — (neu) | **"Pull from Cloud (all)"** (global) | Sync Hub → Orca-Cloud-Karte | W4 (bulk, adopt=True) |

### 3.2 Status-Vokabular (gepinnt aus Code)

Die echten Status-Werte aus `sync_handlers.py` und `sync.py`, NICHT die Mockup-Werte (die `drifted`/`missing` verwendeten — korrigiert):

**SyncTarget.status** (gesetzt in `sync_handlers.py`):

| Wert | Bedeutung | CSS-Klasse | Wo gesetzt |
|------|-----------|------------|------------|
| `in_sync` | Expected hash == actual hash | `status-in_sync` | verify-Erfolg, generate_preset-Erfolg, adopt-Erfolg |
| `drift` | Expected hash != actual hash, oder Cloud-Preset gelöscht/fehlt | `status-drifted` | verify-Fehler, preset-Konflikt, Cloud-Delete erkannt |
| `pending` | Push ausgeführt, warte auf Verify | `status-pending` | nach push, nach generate_preset (vor Cloud-Push) |
| `error` | Verify-Fehler (cloud-rollback, Eintrag fehlt) | `status-error` | verify mit cloud_rollback oder missing entry |

**Printer DB Live-Drift** (gesetzt in `compute_printer_db_drift`):

| Wert | Bedeutung | CSS-Klasse |
|------|-----------|------------|
| `in_sync` | Eintrag vorhanden, hash stimmt | `status-in_sync` |
| `modified` | Eintrag vorhanden, aber Felder weichen ab (detail-Text) | `status-modified` (amber — Abweichung, keine harte Drift) |
| `missing` | `printer_db_id` gesetzt, aber Eintrag nicht auf Printer | `status-missing` (red) |
| `new` | Kein `printer_db_id` (Profil noch nie gepusht) | `status-new` (blue) |
| `stock` | Stock-RFID-Profil (Creality, ausgeschlossen vom Push) | `status-stock` (muted) |

**Job.status** (gesetzt in `jobs.py`):

| Wert | Bedeutung | CSS-Klasse |
|------|-----------|------------|
| `queued` | In Warteschlange | `status-queued` |
| `running` | Wird ausgeführt | `status-running` |
| `done` | Erfolgreich abgeschlossen | `status-completed` |
| `error` | Fehlgeschlagen | `status-error` |
| `awaiting_input` | Wartet auf Benutzerentscheidung (Busy-Dialog) | `status-awaiting_input` |

**Regel:** "Sync" als Begriff erscheint nur noch als Seitenname ("Sync Hub") und Nav-Label. Kein Button, kein Badge, kein Action-Label verwendet "Sync" ohne Richtung. Jeder Action-Button enthält ein Richtungsverb: "Push" (Catalog → Ziel) oder "Pull" (Quelle → Catalog) oder "Backup" (→ GitHub).

**CSS-Klassen-Mapping:** Die app.css definiert `status-drifted` (rot), nicht `status-drift`. Template muss `status-drifted` für den SyncTarget-Wert `drift` verwenden. Ebenso definiert app.css `status-completed` (grün), nicht `status-done` für Job-Badges. Die Werte `modified`, `missing`, `new`, `stock`, `in_sync`, `pending`, `error`, `queued`, `running`, `awaiting_input` haben direkte CSS-Klassen-Namen (`status-{wert}`) und benötigen kein Mapping. Eine Jinja2-Filter-Funktion `status_class(value)` mappt nur `drift` → `drifted` und `done` → `completed`. Registriert in `app.py` neben den bestehenden `colors_list`/`from_json`-Filtern.

---

## 4. Import-Seite (neu, `/import`)

### 4.1 Konsolidierung

Migration (`/migration`, W5) und Orca Cloud Import (`/import-orca`, W6) werden zu einer Seite `/import` mit zwei Tabs zusammengefasst:

- **Tab 1: "From Printer DB"** — bisher `/migration`, 3-Schritt-Wizard (Scan → Review → Complete) inhaltlich unverändert. Stepper, "Start import scan"-Button, Proposal-Tabelle mit Checkboxen.
- **Tab 2: "From Orca Cloud"** — bisher `/import-orca`, inhaltlich unverändert. Tabelle mit unmatched Presets, Checkboxen, "user-tuned"-Filter, "Import selected"-Button.

Tab-Switching via inline JS (show/hide von Tab-Inhalts-Divs), analog zu M2-Mockup. Aktiver Tab wird in URL-Query persistiert: `/import?tab=printer` oder `/import?tab=orca`.

### 4.2 Clean Cutover

- Route `/migration` → **ENTFERNT** (kein Redirect — interne App, keine externen Links)
- Route `/import-orca` → **ENTFERNT**
- Route `/import` → **NEU** (bedient beide Tabs)
- Template `migration.html` → wird zu Tab-Inhalt in `import.html` (oder `import_printer.html` Include)
- Template `import_orca.html` → wird zu Tab-Inhalt in `import.html` (oder `import_orca.html` Include)
- Nav-Eintrag "Migration" → **ENTFERNT**
- Nav-Eintrag "Orca Cloud Import" → **ENTFERNT**
- Nav-Eintrag "Import" → **NEU** (zwischen "Sync" und "Settings")

### 4.3 Profiles-Seite Änderungen

- **"Sync from Orca"-Button** (`profiles.html:6-8`) → **ENTFERNT**. Ersetzt durch dezenter Link "Sync status →" (`.text-link`) zum Sync Hub (`/sync`).
- **Toast "Synced from Orca Cloud"** → **ENTFERNT** (kein Trigger mehr auf dieser Seite)
- **Profile-Edit Orca-Fragment** (`orca_fragment.html`): "Sync from Orca"-Button → **"Pull from Cloud"** (Label-Änderung, Funktionalität unverändert: `POST /profiles/{pid}/sync-from-orca`, adopt=True). Das Fragment bleibt read-only + Pull-Button. **Begründung:** Kontext-Flow — der Benutzer hat in OrcaSlicer getunt, öffnet den Profile-Edit, um die Cloud-Werte in den Catalog zu übernehmen. Der Pull-Button im Edit-Kontext ist der natürliche Ort für diese Aktion.

### 4.4 Navigation (base.html) — 7 → 6 Einträge

| # | Alt | Neu |
|---|-----|-----|
| 1 | Dashboard → `/` | Dashboard → `/` (unverändert) |
| 2 | Profiles → `/profiles` | Profiles → `/profiles` (unverändert) |
| 3 | Spools → `/spools` | Spools → `/spools` (unverändert) |
| 4 | Sync → `/sync` | Sync → `/sync` (unverändert, wird "Sync Hub") |
| 5 | Migration → `/migration` | **Import → `/import`** (neu, ersetzt Migration + Orca Cloud Import) |
| 6 | Orca Cloud Import → `/import-orca` | **entfernt** |
| 7 | Settings → `/settings` | Settings → `/settings` (unverändert) |

---

## 5. Keine Verhaltensänderungen an Sync-Logik

Die E1/E2/E3-Fixes (adopt-Split, SyncTarget-Erstellung bei Import, read-only drift_status) bleiben exakt wie implementiert. Dieser Spec ändert UI, Routen und Naming. Die Sync-Logik (Job-Handler, Hash-Berechnung, Adopt-Logik) wird nicht angetastet — mit **einer minimalen Ausnahme**: `refresh_preset_drift` erhält einen optionalen `profile_id`-Parameter für den per-Profil-Pull (§6.8). Ohne `profile_id` verhält sich die Funktion identisch zu heute — kein bestehender Pfad ändert sich.

- `handle_push`, `handle_verify`, `handle_generate_preset`, `handle_import_scan`, `handle_github_backup` — unverändert
- `refresh_preset_drift(adopt=False)` in `drift_status()` — unverändert (read-only beim Seitenladen)
- `refresh_preset_drift(adopt=True)` bei Pull-Buttons — unverändert (Baseline-Adopt + nozzle_temperature-Write-back); der neue `profile_id`-Parameter ist optional und defaultet auf None (alle Profile)
- `_enqueue_github_backup` nach `generate_preset` — unverändert (Auto-Trigger bleibt, Sichtbarkeit ist Out-of-Scope §7.1)

**Keine Modell-Migrationen** (AGENTS.md Rule 6). Keine neuen Spalten, keine neuen Tabellen. Alle Daten bleiben in existierenden `Profile`, `SyncTarget`, `Job`, `activity_log`-Strukturen.

**OrcaSlicer bleibt Editor** (AGENTS.md Rule 5). Print-Settings kommen aus Orca Cloud via `SyncTarget.actual_state`, werden read-only angezeigt. Keine Print-Settings-Felder im Profile-Edit-Form.

---

## 6. Technik

### 6.1 Route-Änderungen in `fm/api/ui.py`

| Route | Aktion | Details |
|-------|--------|---------|
| `GET /migration` | **ENTFERNEN** | Funktionalität → `GET /import?tab=printer` |
| `POST /migration/import` | **ENTFERNEN** | → `POST /import/printer/scan` |
| `POST /migration/apply` | **ENTFERNEN** | → `POST /import/printer/apply` |
| `GET /import-orca` | **ENTFERNEN** | Funktionalität → `GET /import?tab=orca` |
| `POST /import-orca/apply` | **ENTFERNEN** | → `POST /import/orca/apply` |
| `GET /import` | **NEU** | Rendert `import.html` mit beiden Tabs, `tab`-Query-Param wählt aktiven Tab |
| `POST /import/printer/scan` | **NEU** | Wie bisher `migration_import` (enqueue import_scan, drain, redirect) |
| `POST /import/printer/apply` | **NEU** | Wie bisher `migration_apply` |
| `POST /import/orca/apply` | **NEU** | Wie bisher `import_orca_apply` |
| `GET /sync` | **ÄNDERN** | Rendert `sync_hub.html` statt `sync.html` (neues Template, gleiche Daten: drift, awaiting_jobs, recent_jobs, github_configured) |
| `GET /sync/progress` | **UNVERÄNDERT** | HTMX-Fragment für Job-Queue-Polling |
| `POST /sync/jobs/{id}/resolve` | **UNVERÄNDERT** | Busy-Dialog-Resolve |
| `POST /profiles/sync-from-orca` | **ENTFERNEN** | Bulk-Pull → ersetzt durch `POST /api/sync/pull` (siehe §6.8) |
| `POST /profiles/{pid}/sync-from-orca` | **BEHALTEN** | Per-Profil-Pull im Orca-Fragment — nur Label ändert sich ("Sync from Orca" → "Pull from Cloud"), Route-Path bleibt unverändert um Fragment-Logik nicht zu brechen |
| `GET /profiles/{pid}/orca-fragment` | **UNVERÄNDERT** | HTMX-Fragment, read-only + Pull-Button |

### 6.2 API-Route-Änderungen in `fm/api/sync.py`

| Route | Aktion | Details |
|-------|--------|---------|
| `POST /sync/push` | **UNVERÄNDERT** | Akzeptiert bereits `profile_ids` im JSON-Body; per-Profil-Push nutzt `{"profile_ids": [id]}` |
| `POST /sync/preset/{profile_id}` | **UNVERÄNDERT** | Generate-Preset per Profil |
| `POST /sync/backup-github` | **UNVERÄNDERT** | GitHub-Backup |
| `GET /status/drift` | **UNVERÄNDERT** | Drift-Matrix |
| `POST /sync/pull/{profile_id}` | **NEU** | Per-Profil-Pull: `refresh_preset_drift(db, adopt=True)` für ein Profil. Siehe §6.8. |
| `POST /sync/pull` | **NEU** | Bulk-Pull: `refresh_preset_drift(db, adopt=True)` für alle. Siehe §6.8. |
| `POST /jobs/{id}/resolve` | **UNVERÄNDERT** | Job-Resolve |
| `GET /jobs` | **UNVERÄNDERT** | Job-Liste |
| `GET /jobs/{id}` | **UNVERÄNDERT** | Einzelner Job |

### 6.3 Template-Liste

| Template | Aktion |
|----------|--------|
| `sync.html` | **ENTFERNEN** → ersetzt durch `sync_hub.html` |
| `sync_hub.html` | **NEU** — Sync Hub Layout (M3), extends `base.html` |
| `migration.html` | **ENTFERNEN** → Inhalt wird Tab in `import.html` |
| `import_orca.html` | **ENTFERNEN** → Inhalt wird Tab in `import.html` |
| `import.html` | **NEU** — Tab-Shell mit zwei Tab-Inhalten |
| `base.html` | **ÄNDERN** — Nav: 7→6 Einträge, "Import" ersetzt Migration + Orca Cloud Import |
| `profiles.html` | **ÄNDERN** — "Sync from Orca"-Button entfernen, "Sync status →"-Link hinzufügen |
| `orca_fragment.html` | **ÄNDERN** — Button-Label "Sync from Orca" → "Pull from Cloud" |
| `dashboard.html` | **UNVERÄNDERT** — Drift-Matrix bleibt (read-only Übersicht) |
| `profile_form.html` | **UNVERÄNDERT** |
| `profile_delete.html` | **UNVERÄNDERT** |
| `settings.html` | **UNVERÄNDERT** |
| `spools.html` | **UNVERÄNDERT** |
| `template_dropdown.html` | **UNVERÄNDERT** |

### 6.4 HTMX-Endpunkte (bleiben unverändert)

| Endpunkt | Verwendung in Sync Hub |
|-----------|----------------------|
| `GET /sync/progress?since=N` | Job-Queue-Polling (alle 3s) |
| `GET /status/drift` | Wird von `sync_page`-Route serverseitig gerendert (nicht clientseitig gefetcht) |
| `POST /sync/jobs/{id}/resolve` | Busy-Dialog-Buttons |

### 6.5 Jinja2-Filter (neu in `app.py`)

```python
_STATUS_CLASS_MAP = {
    "drift": "drifted",
    "done": "completed",
}

def status_class(value: str) -> str:
    """Map internal status values to app.css badge class names."""
    return _STATUS_CLASS_MAP.get(value, value)
```

Registriert neben `colors_list` und `from_json`:
```python
templates.env.filters["status_class"] = status_class
```

Verwendung im Template: `class="status-badge status-{{ d.printer_db.status | status_class }}"`

### 6.6 Tests

| Test-Bereich | Was testen |
|--------------|------------|
| **Route-Tests** | `GET /import` → 200; `GET /import?tab=printer` → 200; `GET /import?tab=orca` → 200; `GET /migration` → 404; `GET /import-orca` → 404; `GET /sync` → 200 (neues Template) |
| **Nav-Tests** | `base.html` hat 6 Nav-Items, nicht 7; "Import" vorhanden, "Migration" und "Orca Cloud Import" absent |
| **Label-Tests** | `GET /sync` HTML enthält "Push to Printer", "Push to Cloud", "Pull from Cloud", "Backup to GitHub"; enthält NICHT "Sync from Orca", "Push all profiles", "Sync now"; `GET /profiles` HTML enthält NICHT "Sync from Orca"; `GET /profiles/{id}/edit` Orca-Fragment enthält "Pull from Cloud", nicht "Sync from Orca" |
| **Per-Profil-Push-Test** | `POST /api/sync/push` mit `{"profile_ids": [1]}` pusht nur Profil 1 (bereits durch existierende `handle_push`-Logik abgedeckt — Test prüft, dass nur 1 Profil gepusht wird) |
| **Per-Profil-Pull-Test** | `POST /api/sync/pull/{profile_id}` → adoptiert Cloud-Baseline für ein Profil, setzt `expected_hash`, schreibt `nozzle_temperature` zurück |
| **Bulk-Pull-Test** | `POST /api/sync/pull` → adoptiert für alle Profile |
| **Bestehende Tests** | Alle 392 existierenden Tests müssen grün bleiben (Sync-Logik unverändert) |


### 6.7 Per-Profil "Push to Printer" — bestehender Endpoint

Das M3-Mockup zeigt per-Zeile "Push to Printer"-Buttons. Die bestehende `POST /api/sync/push`-Route akzeptiert bereits `profile_ids` im JSON-Body und `handle_push` → `_rebuild_printer_db` filtert nach `profile_ids`. **Es ist kein neuer API-Endpoint erforderlich** — der UI-Button sendet `{"profile_ids": [id]}` an die bestehende Route.

**Aber:** Der per-Profil-Push rebuild die gesamte Printer-DB (Cache + nur das eine Profil), ladet sie hoch, und reboots den Printer. Das ist funktional korrekt, aber der Benutzer sollte verstehen, dass ein per-Profil-Push denselben Reboot wie ein Global-Push auslöst. Der Button-Text "Push to Printer" kommuniziert dies nicht explizit — das ist akzeptabel, da der Busy-Dialog bei Bedarf den Reboot bestätigt.

### 6.8 Per-Profil/Bulk "Pull from Cloud" — neue Endpoints

**`POST /api/sync/pull/{profile_id}`** (neu in `fm/api/sync.py`):

```python
@router.post("/sync/pull/{profile_id}")
def sync_pull_profile(profile_id: int, db: DbSession) -> dict:
    """Pull a single profile's preset from Orca Cloud (adopt=True)."""
    refresh_preset_drift(db, adopt=True, profile_id=profile_id)
    db.flush()
    return {"status": "ok", "profile_id": profile_id}
```

**Voraussetzung:** `refresh_preset_drift` muss einen optionalen `profile_id`-Parameter akzeptieren, um nur ein Profil zu adoptieren statt alle. Aktuell adoptiert die Funktion alle Profile. Die Erweiterung ist minimal: Filter auf `profile_id` in der Match-Loop. Dies ist eine **Logik-Erweiterung**, aber keine Verhaltensänderung an bestehenden Pfaden (ohne `profile_id` verhält sich die Funktion identisch).

**`POST /api/sync/pull`** (neu, Bulk — ersetzt `POST /profiles/sync-from-orca`):

```python
@router.post("/sync/pull")
def sync_pull_all(db: DbSession) -> dict:
    """Pull all Orca Cloud presets and adopt (replaces /profiles/sync-from-orca)."""
    refresh_preset_drift(db, adopt=True)
    db.flush()
    return {"status": "ok"}
```
---

## 7. Nicht-Scope

### 7.1 Kategorie C: Auto-Trigger-Sichtbarkeit

Auto-`refresh_preset_drift` bei Seitenladen (V3), Auto-`verify` nach `push` (V5), Auto-`github_backup` nach `generate_preset` (V4) — deren Sichtbarkeit (Lade-Indikatoren, Toasts, Kausalitäts-Hinweise) ist **nicht** Teil dieses Specs. Die Auto-Trigger funktionieren unverändert; ihre UX-Verbesserung ist ein separater Folgespec.

### 7.2 GitHub-Backup-Historie

Das M2-Mockup zeigte eine Backup-Historie-Tabelle. Dies ist **explizit Out-of-Scope**. Die Backup-Karte im Sync Hub zeigt nur Last-Backup-Zeitstempel und den "Backup to GitHub"-Button. Eine Historien-Ansicht ist ein Folgespec.

### 7.3 Dashboard-Drift-Tabelle

Die Dashboard-Drift-Tabelle (`dashboard.html:29-34`) bleibt unverändert. Sie ist eine read-only Übersicht und löst ebenfalls `drift_status()` (mit `refresh_preset_drift(adopt=False)`) aus. Die Trennung Dashboard="sehen" / Sync Hub="handeln" wird durch die Aktions-Buttons im Sync Hub klar — das Dashboard braucht keine Änderung.

---

## 8. Offene Fragen

### Q1: Soll `POST /profiles/{pid}/sync-from-orca` (Orca-Fragment) auf `POST /api/sync/pull/{profile_id}` migriert werden?

**Empfehlung: Ja, in einer Folgerunde.** In diesem Spec bleibt der Route-Path unverändert, um die HTMX-Fragment-Logik nicht zu brechen (der Button returnt ein HTML-Fragment, kein JSON). Eine Migration auf den API-Endpoint würde erfordern, dass der API-Endpoint HTML returnt oder der Client einen Re-fetch des Fragments macht. Das ist eine Refactor-Aufgabe, die von diesem Spec (UI/Routing/Naming) getrennt bleiben sollte. Vorerst: nur Label-Änderung im Template.

### Q2: Soll der per-Profil "Push to Printer" den Reboot überspringen können (no_reboot-Option)?

**Empfehlung: Nein.** Der per-Profil-Push nutzt denselben `handle_push`-Pfad wie der Global-Push. Die Iron Rules (Version ≥ 9876543210 + reboot mandatory) gelten unverändert. Der Busy-Dialog bietet bei Bedarf die Wait/Force-reboot/No-reboot-Optionen. Einen "Quick Push ohne Reboot" per Profil anzubieten würde die Iron Rules untergraben. Der Button sendet `{"profile_ids": [id]}` ohne `no_reboot`-Flag.

### Q3: Tab-Zustand in URL oder Session?

**Empfehlung: URL-Query-Param (`/import?tab=printer`).** Session-basierte Tab-State ist fragil (Session-Expiry, Multi-Tab-Browsing). URL-Query ist bookmarkbar, teilbar, und stateless. Der `tab`-Param wird im Template gelesen und setzt die `active`-Klasse. Default: `printer` (Migration ist der häufigere Import-Flow).
