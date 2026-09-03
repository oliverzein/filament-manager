# Sync-/Migration-Workflow-Analyse — Filament Manager

**Datum:** 2026-09-02
**Ziel:** Präziser Überblick über alle Sync-/Migration-Workflows, ihre UI-Entry-Points, Datenflüsse und Verwirrungspunkte.

---

## (a) Workflow-Inventar-Tabelle

| # | Code-Name | UI-Name (exakte Beschriftung) | Job-Handler? | Entry-Point(s) | Richtung |
|---|-----------|-------------------------------|--------------|----------------|----------|
| W1 | `push` | **"Push all profiles"** (Sync-Seite) | Ja (`handle_push`) | Sync-Seite → `POST /api/sync/push` | Catalog → K2 Printer DB |
| W2 | `verify` | *(kein Button — auto nach push)* | Ja (`handle_verify`) | Auto-enqueue nach W1 | Printer DB → Catalog (read-only check) |
| W3 | `generate_preset` | *(Icon-Button ohne Textlabel, Upload-Icon)* in Drift-Tabelle | Ja (`handle_generate_preset`) | Sync-Seite Drift-Tabelle → `POST /api/sync/preset/{id}` | Catalog → Orca Cloud |
| W4 | `refresh_preset_drift` | **"Sync from Orca"** (3 Orte!) + **"Sync now"** (Import-Seite) | Nein — synchron | 3 UI-Buttons + auto in `drift_status()` | Orca Cloud → Catalog (drift update) |
| W5 | `import_scan` | **"Start import scan"** (Migration-Seite) | Ja (`handle_import_scan`) | Migration-Seite → `POST /migration/import` → `POST /migration/apply` | Printer DB → Catalog (neue Profile) |
| W6 | *(kein Job — synchron)* | **"Import selected"** (Orca Cloud Import-Seite) | Nein | `/import-orca` → `POST /import-orca/apply` | Orca Cloud → Catalog (neue Profile) |
| W7 | `github_backup` | **"Backup to GitHub"** (Sync-Seite, konditional) | Ja (`handle_github_backup`) | Sync-Seite → `POST /api/sync/backup-github` + auto nach W3 | Orca Cloud → GitHub |
| W8 | *(Teil von W4)* | *(kein eigener Button)* | Nein | Auto-Aufruf in `drift_status()` | Orca Cloud → SyncTarget-Status |
| W9 | `profile_delete_form` + `sync_delete` | **"Also delete the OrcaSlicer Cloud preset"** (Delete-Seite) | Nein — synchron | Profile Delete → `POST /profiles/{pid}/delete?delete_cloud=1` | Catalog → Orca Cloud (delete) |

### Im Spec beschrieben, seit 2026-08 durch Cloud Sync abgelöst — bewusst nicht gebaut

| Feature | Spec-Referenz | Status |
|---------|---------------|--------|
| `temp_sync` (Desktop-Presets → K2 DB) | Spec §4 "Temp sync" | **Entfernt (2026-08)** — ersetzt durch Orca Cloud Transport. Temp-Write-back läuft heute über `refresh_preset_drift(adopt=True)` mit `cloud_id`-Matching |
| `report_presets` (Companion Agent) | Spec §5 | **Entfernt (2026-08)** — konzeptionell ersetzt durch Orca Cloud Sync Pull (`OrcaCloudClient.sync_pull`) |
| `apply_preset` (Companion Agent) | Spec §5 | **Entfernt (2026-08)** — konzeptionell ersetzt durch `generate_preset` → Orca Cloud Push → OrcaSlicer-eigener Sync |
| Companion Agent (`fm-agent`) | Spec §5 | **Entfernt (2026-08)** — ersetzt durch Orca Cloud Sync. Kein Agent-CLI, keine `/api/agent/` Endpoints |

**Registrierte Job-Handler** (`sync_handlers.py:1017-1021`):
```
push, verify, generate_preset, import_scan, github_backup
```

---

## (b) Datenfluss je Workflow

### W1: Printer-DB Push (`push`)
```
Catalog (alle Profile mit printer_db_id)
  → _rebuild_printer_db (cache DB + catalog merge)
  → run_push (SSH/SCP upload + version bump + reboot)
  → SyncTarget(printer_db).expected_hash = hash(entry)
  → SyncTarget(printer_db).status = "pending"
  → AUTO-ENQUEUE: verify job (W2)
  → Bei "busy": _awaiting_input → UI-Busy-Dialog
```
**Trigger:** Manuell ("Push all profiles" Button)
**Auto-Nebenwirkung:** Enqueued immer einen `verify` Job (`sync_handlers.py:534`)

### W2: Verify (`verify`)
```
SSH pull fresh printer DB
  → Version floor check (>= version_override, z.B. 9876543210)
  → Per Profile: presence check + hash compare
  → SyncTarget(printer_db).status = "in_sync" | "error"
  → Cloud-rollback detection → status = "error" + activity_log warn
```
**Trigger:** Auto nach W1. Kein manueller Button.

### W3: Generate Preset (`generate_preset`)
```
Profile → build_standalone_preset (template + catalog fields)
  → OrcaCloudClient.sync_push (push to Orca Cloud)
    → 409 conflict → retry mit server updated_time → oder status="drift"
  → SyncTarget(orca_preset).cloud_id = UUIDv5(user_id, filament_id)
  → SyncTarget(orca_preset).expected_hash = hash(preset_fields)
  → SyncTarget(orca_preset).status = "in_sync"
  → AUTO-ENQUEUE: github_backup job (W7) via _enqueue_github_backup
```
**Trigger:** Manuell (Icon-Button in Drift-Tabelle, per Profil)
**Auto-Nebenwirkung:** Enqueued `github_backup` wenn GitHub konfiguriert (`sync_handlers.py:716`)

### W4: "Sync from Orca" / Preset Drift Refresh (`refresh_preset_drift`)
```
OrcaCloudClient.sync_pull (alle Cloud-Presets)
  → Match per cloud_id (exact) oder name-matching (legacy → speichert cloud_id)
  → Prüft deletes aus Cloud
  → Kein Match / delete → status = "drift"
  → Match gefunden:
    adopt=False (read-only, Default — Seitenladen/drift_status):
      - target.status = "in_sync" if cloud_hash == expected_hash else "drift"
      - target.actual_state = cloud preset JSON (wird aktualisiert)
      - target.last_synced_at = now (wird aktualisiert)
      - expected_hash UNVERÄNDERT
      - profile.nozzle_temperature UNVERÄNDERT
    adopt=True (expliziter "Sync from Orca"-Klick):
      - target.expected_hash = cloud hash (adoptiert Cloud als neue Baseline)
      - target.status = "in_sync"
      - profile.nozzle_temperature = cloud nozzle_temperature[0] (write-back)
      - target.actual_state + last_synced_at wie oben
```
**Trigger:**
1. Manuell — "Sync from Orca" Button auf Profiles-Seite (`POST /profiles/sync-from-orca`) → **adopt=True**
2. Manuell — "Sync from Orca" Button im Profile-Edit-Formular (`POST /profiles/{pid}/sync-from-orca`) → **adopt=True**
3. Manuell — "Sync now" Button auf Import-Orca-Seite (`POST /profiles/sync-from-orca`, bulk-Endpoint) → **adopt=True**
4. **Automatisch** — bei jedem Aufruf von `drift_status()` (`fm/api/sync.py:146`), d.h. bei jedem Laden der Sync-Seite, des Dashboards → **adopt=False (read-only)**

**Fix-Notiz (E3, 2026-09-02):** Bisher machte `refresh_preset_drift` bei jedem Aufruf (auch beim Seitenladen) Baseline-Adopt + nozzle_temperature-Write-back. Jetzt ist `adopt` ein keyword-only Parameter (`adopt: bool = False`); `drift_status()` nutzt den Default (read-only), die sync-from-orca-Routen in `ui.py` übergeben `adopt=True`.

### W5: Migration Wizard (`import_scan`)
```
SSH pull printer DB
  → find_custom_entries (alle 99xxx Einträge)
  → _entry_to_proposal (base/kvParam → Profile-Felder)
  → UI: Proposal-Tabelle mit Checkboxen
  → User select → POST /migration/apply
  → Create/update Profile + SyncTarget(printer_db, status="pending")
```
**Trigger:** Manuell ("Start import scan" Button)
**Einmalig:** Design-Spec sagt "one-time" — aber keine Sperre gegen Wiederholung

### W6: Import from Orca Cloud (neue Profile)
```
OrcaCloudClient.sync_pull (alle Cloud-Presets)
  → _unmatched_orca_presets (filter: filament_id nicht in existierenden Profilen)
  → UI: Tabelle mit Checkboxen, "user-tuned" Filter
  → User select → POST /import-orca/apply
  → Create Profile (NO printer_db_id, NO push to printer)
  → Create SyncTarget(kind="orca_preset"):
      - cloud_id = preset._path (Orca Cloud resource UUID)
      - expected_hash = canonical_hash(name, filament_id, filament_vendor, filament_type, nozzle_temperature)
      - actual_state = preset JSON
      - status = "pending"
      - last_synced_at = now
```
**Trigger:** Manuell ("Import selected" Button)
**Fix-Notiz (E2, 2026-09-02):** Bisher wurden Profile ohne SyncTarget erstellt — sie waren für das Sync-System unsichtbar. Jetzt wird analog zu `migration_apply` (printer_db) ein `SyncTarget(orca_preset)` angelegt. Da `expected_hash` aus denselben Feldern wie `refresh_preset_drift`'s Cloud-Hash berechnet wird, zeigt die Drift-Matrix nach Import + Seitenladen `in_sync` (Cloud-Hash == expected_hash).

### W7: GitHub Backup (`github_backup`)
```
OrcaCloudClient.sync_pull (alle Cloud-Presets, cursor=0)
  → _cloud_presets_to_local
  → Write each as presets/{name}.json + index.json
  → GitHubBackupClient.backup (atomic commit)
  → activity_log
```
**Trigger:**
1. Manuell — "Backup to GitHub" Button auf Sync-Seite (nur wenn `github_configured`)
2. **Automatisch** nach W3 (`handle_generate_preset` → `_enqueue_github_backup`)

### W8: Preset Drift Detection (auto, Teil von W4)
```
= refresh_preset_drift(db, adopt=False) — read-only Modus
  Wird bei jedem drift_status() Aufruf ausgeführt
  → Updated SyncTarget(orca_preset) Status-Badges in Drift-Tabelle
  → Vergleicht Cloud-Hash vs. expected_hash → in_sync / drift
  → Mutiert NICHT expected_hash oder Profile.nozzle_temperature
```
**Kein separater Workflow** — ist die automatische Ausführung von W4 im read-only Modus (`adopt=False`).

### W9: Profile Delete mit Cloud-Cleanup
```
perform_delete (catalog delete)
  → if delete_cloud: OrcaCloudClient.sync_delete(cloud_ids)
  → fail-soft: Cloud-Fehler blockiert nicht den Catalog-Delete
  → activity_log "orca_cloud_delete"
```
**Trigger:** Manuell (Delete-Seite, Checkbox "Also delete the OrcaSlicer Cloud preset")

---

## (c) UI-Map — Welche Seite hat welche Sync-Elemente

### Navigation (`base.html:23-44`) — 6 Einträge (Sync Hub Redesign)
```
Dashboard    → /
Profiles     → /profiles
Spools       → /spools
Sync         → /sync
Import       → /import
Settings     → /settings
```
**Änderung (Sync Hub Redesign):** Navigation von 7 auf 6 Einträge reduziert. `Migration` (`/migration`) und `Orca Cloud Import` (`/import-orca`) entfernt; zusammengeführt als `Import` (`/import`) mit Tabs "From Printer DB" und "From Orca Cloud".

### Dashboard (`/`, `dashboard.html`)
| Element | Typ | Workflow |
|---------|-----|----------|
| "Fully synced" Stat-Card | Anzeige | W2+W4 (drift matrix) |
| "Sync drift" Tabelle (Printer + Orca Spalten) | Anzeige | W8 (auto `refresh_preset_drift(adopt=False)` bei Render — read-only) |
| "Recent activity" Tabelle | Anzeige | Alle (activity_log) |
| "Manage sync →" Link | Navigation → /sync | — |

### Profiles (`/profiles`, `profiles.html`) — **GEFIXT (Sync Hub Redesign)**
| Element | Typ | Workflow | Code-Ref |
|---------|-----|----------|----------|
| "New profile" Button | Navigation | — | `profiles.html:5` |
| **"Sync status →"** Link | Navigation → `/sync` | — | `profiles.html:5` |
| Edit/Delete Icon-Buttons | Navigation | — | `profiles.html:23` |
| Material/Brand Filter + "Apply filters" | Form GET | — | `profiles.html:9-13` |

**Änderung (Sync Hub Redesign):** "Sync from Orca" Button entfernt. Bulk-Pull in den Sync Hub verschoben. Stattdessen "Sync status →" Link zur Sync-Seite.

### Profile Edit (`/profiles/{pid}/edit`, `profile_form.html`)
| Element | Typ | Workflow | Code-Ref |
|---------|-----|----------|----------|
| Orca-Fragment (Section 03 "Print settings") | HTMX-Load → `/profiles/{pid}/orca-fragment` | W4 (read) | `profile_form.html:47` |
| **"Pull from Cloud"** Button (im Orca-Fragment; Label geändert, Route unverändert) | HTMX POST → `/profiles/{pid}/sync-from-orca` | W4 (adopt=True) | `orca_fragment.html` |
| "Preset source" Template-Dropdown | HTMX | W3 (template selection) | `profile_form.html:24-39` |

### Sync Hub (`/sync`, `sync_hub.html`) — M3 Two-Column Layout (Sync Hub Redesign)

**Layout:** Linke Spalte = Drift-Tabelle + Busy-Dialog; rechte Spalte = drei Target-Cards (Printer DB, Cloud presets, GitHub backup).

**Linke Spalte — Drift-Tabelle:**
| Element | Typ | Workflow | Code-Ref |
|---------|-----|----------|----------|
| Drift-Tabelle: "Printer DB" + "Orca preset" Status-Spalten | Anzeige | W2+W8 (read-only drift check) | `sync_hub.html:46-52` |
| **"Push to Printer"** Button (per Profil, nur wenn `printer_db_id`) | HTMX POST → `/api/sync/push` (profile_ids) | W1 (per-Profil) | `sync_hub.html:56` |
| **"Push to Cloud"** Button (per Profil) | HTMX POST → `/api/sync/preset/{id}` | W3 | `sync_hub.html:60` |
| **"Pull from Cloud"** Button (per Profil) | HTMX POST → `/api/sync/pull/{id}` | W4 (adopt=True, per-Profil) | `sync_hub.html:61` |
| Busy-Dialog: **"Force reboot"** / **"Wait"** / **"No reboot"** | HTMX POST → `/sync/jobs/{id}/resolve` | W1 (resolve) | `sync_hub.html:75-87` |

**Rechte Spalte — Target-Cards:**
| Element | Typ | Workflow | Code-Ref |
|---------|-----|----------|----------|
| Printer DB Card: Summary "N in sync, M drifted" + **"Push to Printer"** Button (bulk) | HTMX POST → `/api/sync/push` | W1 (bulk) | `sync_hub.html:98-106` |
| Cloud presets Card: Summary + **"Pull from Cloud (all)"** Button | HTMX POST → `/api/sync/pull` | W4 (adopt=True, bulk) | `sync_hub.html:108-116` |
| GitHub backup Card: **"Backup to GitHub"** Button (konditional) | HTMX POST → `/api/sync/backup-github` | W7 | `sync_hub.html:118-126` |

**Unterer Bereich:**
| Element | Typ | Workflow | Code-Ref |
|---------|-----|----------|----------|
| "Recent jobs" Queue (auto-poll alle 3s) | Anzeige | Alle | `sync_hub.html:130` |

**Änderung (Sync Hub Redesign):** "Push all profiles" → "Push to Printer" (Printer DB Card). Icon-Button ersetzt durch Text-Button "Push to Cloud". Neu: per-Profil "Push to Printer" und "Pull from Cloud" Buttons in Drift-Tabelle. Bulk "Pull from Cloud (all)" in Cloud presets Card. "Sync from Orca" Bulk von Profiles-Seite hierher verschoben.

### Import page — Tab "From Printer DB" (`/import?tab=printer`, `import_printer.html`)
| Element | Typ | Workflow | Code-Ref |
|---------|-----|----------|----------|
| **"Start import scan"** Button | Form POST → `/import/printer/scan` | W5 | `import_printer.html:5` |
| Proposal-Tabelle mit Checkboxen | Form POST → `/import/printer/apply` | W5 | `import_printer.html:6` |
| 3-Schritt-Stepper (Scan → Review → Complete) | Anzeige | W5 | `import_printer.html:4` |
| **"← Back to scan"** Button (Step 2) | Link → /import?tab=printer&step=scan | W5 (Navigation) | `import_printer.html:6` |
| **"Back to review"** Link (Step 1, nur wenn import_job existiert) | Link → /import?tab=printer | W5 (Navigation) | `import_printer.html:5` |

**Änderung (Sync Hub Redesign):** Ehemals eigene `/migration` Seite. Jetzt Tab "From Printer DB" auf `/import`. Routes: `/import/printer/scan`, `/import/printer/apply`. Wizard-State via URL-Query (`?step=scan`) — Step 1 auch bei vorhandenem import_scan-Job erreichbar; ohne Param bleibt Review beim letzten Scan.

### Import page — Tab "From Orca Cloud" (`/import?tab=orca`, `import_orca_tab.html`)
| Element | Typ | Workflow | Code-Ref |
|---------|-----|----------|----------|
| Empty State: "Connect Orca Cloud in Settings, then Pull from Cloud in Sync Hub" | Anzeige | W6 (config hint) | `import_orca_tab.html:16-22` |
| "All presets matched" Empty State | Anzeige | W6 | `import_orca_tab.html:23-30` |
| "Only user-tuned presets" Filter | JS-Filter | W6 | `import_orca_tab.html:41-46` |
| **"Import selected"** Button | Form POST → `/import/orca/apply` | W6 | `import_orca_tab.html:48` |

**Änderung (Sync Hub Redesign):** Ehemals eigene `/import-orca` Seite. Jetzt Tab "From Orca Cloud" auf `/import`. "Sync now" Button entfernt (Bulk-Pull im Sync Hub als "Pull from Cloud (all)"). Route: `/import/orca/apply`.

### Settings (`/settings`, `settings.html`)
| Element | Typ | Workflow | Code-Ref |
|---------|-----|----------|----------|
| "Printer connection" Section (IP, SSH, DB path) | Form | W1+W2+W5 (config) | `settings.html:10-15` |
| "Test connection" Button | JS fetch → `/api/settings/test-connection` | W1 (config test) | `settings.html:15` |
| "Orca Cloud" Section: Email/Password + **"Connect"** Button | JS fetch → `/api/settings/orca-connect` | W3+W4+W6+W7+W9 (config) | `settings.html:16-28` |
| **"Disconnect"** Button (wenn connected) | JS fetch → `/api/settings/orca-disconnect` | W3+W4 (config) | `settings.html:20` |
| "GitHub backup" Section (Token, Repo, Branch) | Form | W7 (config) | `settings.html:35-39` |
| "Test connection" Button (GitHub) | JS fetch → `/api/settings/github-test` | W7 (config test) | `settings.html:39` |

### Profile Delete (`/profiles/{pid}/delete`, `profile_delete.html`)
| Element | Typ | Workflow | Code-Ref |
|---------|-----|----------|----------|
| **"Also delete the OrcaSlicer Cloud preset"** Checkbox | Form POST | W9 | `profile_delete.html` |

---

## (d) Verwirrungs-Punkte aus Benutzersicht

### V1: "Sync" wird für mindestens 4 verschiedene Dinge verwendet — **GEFIXT (Sync Hub Redesign)**

| Button | Was es tut | Richtung | Seite |
|--------|-----------|----------|-------|
| **"Sync from Orca"** (Profiles-Seite) | Pullt Cloud-Presets, adoptiert Cloud-Baseline, **schreibt nozzle_temperature zurück** (adopt=True) | Cloud → Catalog | `/profiles` |
| **"Sync from Orca"** (Profile-Edit, Orca-Fragment) | Gleiche Funktion (adopt=True), aber für ein Profil im Kontext des Edit-Forms | Cloud → Catalog | `/profiles/{id}/edit` |
| **"Sync now"** (Import-Orca-Seite, empty state) | Gleiche Funktion (adopt=True), bulk-Endpoint `/profiles/sync-from-orca` | Cloud → Catalog | `/import-orca` |
| **"Push all profiles"** (Sync-Seite) | Pusht Catalog → Printer DB | Catalog → Printer | `/sync` |
| Drift-Tabelle Icon-Button (Upload-Icon) | Pusht Catalog → Orca Cloud (generate_preset) | Catalog → Cloud | `/sync` |

**Problem:** Der Begriff "Sync" wird für Pull (Cloud→Catalog) und Push (Catalog→Printer/Cloud) verwendet, ohne dass die Richtung aus dem Button-Text ersichtlich ist. "Sync from Orca" vs. "Push all profiles" — aber das Drift-Tabellen-Icon hat gar keinen Text.

**Code-Refs:** `profiles.html:7`, `orca_fragment.html` (Sync from Orca button), `import_orca.html:26`, `sync.html:6` (Push all profiles), `sync.html:9` (icon button)

**Fix (Sync Hub Redesign, 2026-09-02):** Alle Buttons haben jetzt richtungsdeutliche Verben: "Push to Printer", "Push to Cloud", "Pull from Cloud". Das Icon-Button-Problem (V7) ist ebenfalls behoben. "Sync from Orca" als Label existiert nicht mehr im Sync-Kontext.

### V2: Drei "Sync from Orca" Buttons an drei verschiedenen Orten — **GEFIXT**

Drei Buttons mit identischem oder ähnlichem Label, die alle `refresh_preset_drift(adopt=True)` aufrufen, aber unterschiedliche UX-Kontexte haben:

1. **Profiles-Seite** (`profiles.html:7`): `POST /profiles/sync-from-orca` — global, redirect mit Toast
2. **Profile-Edit Orca-Fragment** (`orca_fragment.html`): `POST /profiles/{pid}/sync-from-orca` — per-Profil, return HTMX-Fragment
3. **Import-Orca-Seite empty state** (`import_orca.html:25`): `POST /profiles/sync-from-orca` — bulk-Endpoint, redirect

**Fix-Notiz (E1, 2026-09-02):** Der dritte Button war ein Bug — er rief `sync-from-orca` mit hardcoded `pid=1` auf (404 wenn Profil 1 nicht existiert). Jetzt postet er an den bulk-Endpoint `/profiles/sync-from-orca` (ohne PID).

**Fix (Sync Hub Redesign, 2026-09-02):** "Sync from Orca" Button von der Profiles-Seite entfernt. Bulk-Pull in den Sync Hub verschoben ("Pull from Cloud (all)" in der Cloud presets Card). Der Profile-Edit Orca-Fragment Button bleibt als per-Profil "Pull from Cloud" in der Drift-Tabelle.

### V3: Automatischer `refresh_preset_drift` bei jedem Seitenladen

`drift_status()` (`fm/api/sync.py:146`) ruft `refresh_preset_drift(db)` auf — das macht einen **Live-API-Call zu Orca Cloud** bei jedem Laden der Sync-Seite, des Dashboards, und bei jedem HTMX-Poll der Drift-Tabelle.

**Verhalten (nach E3-Fix):**
- Der Aufruf erfolgt mit `adopt=False` (read-only) — es werden **keine** Catalog-Daten mutiert
- `expected_hash` und `Profile.nozzle_temperature` bleiben unangetastet
- Status = Vergleich Cloud-Hash vs. `expected_hash` → `in_sync` / `drift`
- `actual_state` und `last_synced_at` werden aktualisiert (Badge-Frische)

**Verbleibendes Problem:**
- ~~Der Benutzer weiß nicht, dass beim bloßen Öffnen der Sync-Seite ein Cloud-API-Call stattfindet~~
- ~~Keine Ladeanzeige oder Status-Feedback für diesen Auto-Call~~

**Fix-Notiz (Kategorie C, 2026-09-03):** Unter beiden Drift-Tabellen (Sync Hub `/sync` und Dashboard `/`) wurde ein statischer Hinweis "Status refresh includes a live check against Orca Cloud." ergänzt. Ein echter Lade-Indikator wurde bewusst nicht gebaut (Cloud-Sync ist schnell).

**Fix-Notiz (E3, 2026-09-02):** Bisher schrieb die Funktion bei jedem Seitenladen `nozzle_temperature` aus Cloud zurück (stiller Datenfluss). Das ist behoben — `drift_status()` nutzt jetzt `adopt=False` (read-only).

**Code-Ref:** `fm/api/sync.py:146` (`refresh_preset_drift(db)` in `drift_status()`, default `adopt=False`)

### V4: Automatischer GitHub-Backup nach Generate Preset — unsichtbar — **GEFIXT (Kategorie C, 2026-09-03)**

`handle_generate_preset` (`sync_handlers.py:716`) ruft `_enqueue_github_backup(session)` auf. Das bedeutet: Jeder "Generate Preset" Klick (Icon in Drift-Tabelle) löst **automatisch** auch einen GitHub-Backup aus, wenn GitHub konfiguriert ist.

**Problem:** 
- ~~Keine UI-Anzeige, dass ein GitHub-Backup getriggert wurde~~
- ~~Der Backup-Job erscheint zwar in der "Recent jobs" Queue, aber ohne Erklärung des Kausalzusammenhangs~~
- ~~Benutzer könnte verwirrt sein, warum nach einem Preset-Push plötzlich ein `github_backup` Job in der Queue auftaucht~~

**Fix-Notiz (Kategorie C, 2026-09-03):** `handle_generate_preset` gibt jetzt `"github_backup_scheduled": bool` zurück (True wenn GitHub konfiguriert und Job enqueued). Der "Push to Cloud" Toast zeigt entsprechend "Preset pushed — GitHub backup scheduled" (statt "Preset pushed to Orca Cloud"). Die bestehende Target-Note auf der GitHub-Card ("Also triggered automatically after every successful preset push") bleibt als statische Erklärung.

**Code-Ref:** `sync_handlers.py:716` (`_enqueue_github_backup(session)` in `handle_generate_preset`)

### V5: Automatischer Verify nach Push — unsichtbar — **GEFIXT (Kategorie C, 2026-09-03)**

`handle_push` (`sync_handlers.py:534`) enqueued automatisch einen `verify` Job nach erfolgreichem Push.

**Problem:**
- ~~Der Benutzer sieht "Push complete: N profiles" Toast, aber nicht, dass automatisch ein Verify-Job folgt~~
- ~~Der Verify-Job taucht in der Queue auf, aber der Kausalzusammenhang ist nicht ersichtlich~~
- Der SyncTarget-Status geht erst nach Verify von "pending" → "in_sync" — in der Zwischenzeit zeigt die Drift-Tabelle "pending", was wie ein Fehler aussehen kann

**Fix-Notiz (Kategorie C, 2026-09-03):** Push-Toasts enden jetzt mit "— verifying..." (per-Profil: "Pushed to printer — verifying...", bulk: "Push complete: N profiles, M skipped — verifying..."). Der Verify-Job erscheint in der auto-pollenden "Recent jobs" Queue.

### V6: Migration vs. Import-Orca — zwei Import-Workflows mit ähnlichem Zweck — **GEFIXT**

| Feature | Migration (`/migration`) | Import-Orca (`/import-orca`) |
|---------|--------------------------|------------------------------|
| Quelle | Printer DB (99xxx Einträge) | Orca Cloud (alle Presets) |
| Ziel | Neue Profile + SyncTarget(printer_db) | Neue Profile + SyncTarget(orca_preset) |
| Mechanismus | Job (`import_scan`) | Synchron (`_pull_orca_presets`) |
| Stepper | 3-Schritt-Wizard | 2-Schritt (Scan → Select) |
| Nach Import | Profile haben `printer_db_id` | Profile haben **kein** `printer_db_id`, haben `orca_preset` Target |

**Fix-Notiz (E2, 2026-09-02):** Bisher erstellte Import-Orca Profile ohne SyncTarget — sie waren in der Drift-Matrix als `pending` sichtbar, aber für das Sync-System unsichtbar (kein `orca_preset`-Target, `generate_preset`/`push` griffen nicht). Jetzt wird ein `SyncTarget(kind="orca_preset")` mit `cloud_id`, `expected_hash`, `actual_state` angelegt. Da `expected_hash` aus denselben Feldern wie `refresh_preset_drift`'s Cloud-Hash berechnet wird, zeigt die Drift-Matrix nach Import `in_sync`.

**Fix (Sync Hub Redesign, 2026-09-02):** Migration (`/migration`) und Import-Orca (`/import-orca`) zu einer einzigen `/import` Seite mit Tabs "From Printer DB" und "From Orca Cloud" zusammengeführt. Beide alten Routes geben 404.

### V7: Drift-Tabelle Icon-Button hat kein Text-Label — **GEFIXT**

Der "Generate Preset" Button in der Drift-Tabelle (`sync.html:9`) ist ein reiner Icon-Button (Upload-Pfeil-Icon) mit nur einem `aria-label`. Es gibt keinen sichtbaren Text, der erklärt, was der Button tut.

**Problem:** 
- Benutzer kann nicht erkennen, dass dies "Generate Preset / Push to Orca Cloud" bedeutet
- Kein Unterschied zu einem eventuellen "Push to Printer" Icon erkennbar (wenn es eines gäbe)
- Das `aria-label` sagt "Generate preset for {name}" — aber das ist nur für Screen-Reader sichtbar

**Code-Ref:** `sync.html:9` — `class="icon-button btn-preset"` mit SVG-Icon

**Fix (Sync Hub Redesign, 2026-09-02):** Icon-Button ersetzt durch Text-Button **"Push to Cloud"** in der Drift-Tabelle (`sync_hub.html:60`). Zusätzlich per-Profil "Push to Printer" und "Pull from Cloud" Buttons mit Text-Labels.

### V8: Sync-Seite vs. Dashboard — doppelte Drift-Anzeige — **GEFIXT (Kategorie C, 2026-09-03, per Entscheidung)**

Sowohl das Dashboard (`dashboard.html:29-34`) als auch die Sync-Seite (`sync.html:9`) zeigen eine Drift-Tabelle mit Printer DB + Orca preset Status-Spalten. Beide rufen `drift_status()` auf, was `refresh_preset_drift(adopt=False)` triggert (read-only — mutiert keine Catalog-Daten).

**Problem:** 
- ~~Zwei Orte zeigen dieselben Informationen — aber das Dashboard ist eine Übersicht und die Sync-Seite ist die Aktionsseite~~
- ~~Beide lösen Cloud-API-Calls aus beim Laden~~
- ~~Keine klare Trennung: Dashboard = "sehen", Sync = "handeln" — aber beide zeigen identische Tabellen~~

**Fix-Notiz (Kategorie C, 2026-09-03):** Per Produkt-Entscheidung (2026-09-03) bleibt der automatische Cloud-Call beim Dashboard-Laden bewusst (Badge-Frische). Die Sichtbarkeit wird durch den statischen Live-Check-Hinweis (C3a) sichergestellt. Die doppelte Drift-Anzeige wird akzeptiert: Dashboard = Übersicht, Sync Hub = Aktionsseite.

### V9: Import-Orca-Seite: "Sync now" Button mit hardcoded PID — **GEFIXT**

`import_orca.html:25`: `<form method="post" action="/profiles/sync-from-orca">`

**Fix (E1, 2026-09-02):** Der Button postet jetzt an den bulk-Endpoint `POST /profiles/sync-from-orca` (ohne PID), der `refresh_preset_drift(adopt=True)` ausführt und auf `/profiles?orca_synced=1` redirectet. Bisher war die Action `/profiles/1/sync-from-orca` mit erfundener PID 1 (404 wenn Profil 1 nicht existiert).

**Code-Ref:** `import_orca.html:25`, `ui.py:183` (`profiles_sync_from_orca` bulk-Route)

### V10: Orca-Fragment im Profile-Edit zeigt "From Orca Cloud" — Datenrichtung nicht erkennbar — **GEFIXT (2026-09-03)**

Das Orca-Fragment (`orca_fragment.html`) zeigt Section "03 Print settings — From Orca Cloud". Die Daten kommen aus `SyncTarget.actual_state`.

~~**Problem:** Das Label "From Orca Cloud" impliziert, die Werte wurden aus der Cloud geholt. Aber nach einem "Generate Preset" (W3) stehen dort die Werte, die **vom Catalog zur Cloud gepusht** wurden. Die Richtung ist im UI nicht erkennbar.~~

**Korrektur der sachlichen Prämisse:** `actual_state` wird **NICHT** von `handle_generate_preset` (W3, Push) geschrieben — der Code zeigt, dass es ausschließlich von `refresh_preset_drift` (W4/W8, Pull-Pfade, `sync_handlers.py:953/899/906`) geschrieben wird (adopt- und read-only-Pfade; auf `None` gesetzt bei Cloud-Delete/kein Match). Das Fragment zeigt also immer den zuletzt **gepullten** Cloud-Status, niemals direkt gepushte Catalog-Werte.

**Refresh-Verhalten:** `actual_state` wird bei jedem Laden der `/`- oder `/sync`-Seite aktualisiert (`drift_status` → `refresh_preset_drift`, read-only). Die Fragment-Route selbst (`/profiles/{pid}/orca-fragment`) führt kein Refresh durch — sie liest nur den bestehenden `SyncTarget`.

**Fix-Notiz (2026-09-03):** Beide Fragment-Routen (`sync_from_orca` und `orca_fragment` in `ui.py`) übergeben jetzt `"target_status": target.status if target else None` an das Template. Im `{% if orca_data %}`-Zweig wird eine statusabhängige Source-Zeile zwischen Heading und Value-Grid gerendert (stilisiert wie `.orca-timestamp`):
- `in_sync` → "Matches the catalog — as pushed or adopted."
- `drift` → "Cloud values differ from the catalog — tuned in OrcaSlicer? Pull from Cloud adopts them."
- `pending` → "No baseline yet — push to Cloud or pull once."
- kein Target / None → keine zusätzliche Zeile (Empty-State-Branch abgedeckt).

### V11: Kein "Push to Printer" per Profil — nur "Push all" — **GEFIXT**

Die Sync-Seite hat nur **"Push all profiles"** — es gibt keinen per-Profil Push-Button für die Printer DB. Der einzige per-Profil Button in der Drift-Tabelle ist der Generate-Preset (Upload) Icon.

**Problem:** Der Benutzer kann nicht gezielt ein Profil zum Printer pushen — es ist alles-oder-nichts. Generate Preset (Cloud) ist per-Profil, aber Printer-Push ist global. Diese Asymmetrie ist verwirrend.

**Fix (Sync Hub Redesign, 2026-09-02):** Per-Profil **"Push to Printer"** Button in der Drift-Tabelle hinzugefügt (`sync_hub.html:56`). Nur sichtbar wenn Profil `printer_db_id` hat. Bulk-Push bleibt als "Push to Printer" in der Printer DB Card.

### V12: Settings-Seite: Orca Cloud "Connect" vs. "Sync from Orca" — zwei verschiedene Dinge

Die Settings-Seite hat einen **"Connect"** Button (`settings.html:27`), der die Orca Cloud Authentifizierung (Login) durchführt. Die Profiles-Seite hat einen **"Sync from Orca"** Button, der Presets pullt.

**Problem:** "Connect" und "Sync" sind beide Cloud-bezogen, aber semantisch verschieden (Auth vs. Data-Pull). Ein Benutzer könnte "Connect" drücken und erwarten, dass Presets synchronisiert werden. Umgekehrt könnte "Sync from Orca" fehlschlagen, wenn nicht vorher "Connect" in Settings gemacht wurde — aber die Fehlermeldung ist nicht kontextuell.

### V13: `refresh_preset_drift` überschreibt Catalog-Daten still — **GEFIXT (teilweise)**

In `refresh_preset_drift` (`sync_handlers.py`): Der Write-back von `nozzle_temperature` passiert jetzt **nur noch im Adopt-Pfad** (`adopt=True`), d.h. bei explizitem "Sync from Orca"-Klick — nicht mehr beim automatischen Seitenladen.

```python
# adopt=True (expliziter Sync from Orca):
profile.nozzle_temperature = int(temps[0])

# adopt=False (Seitenladen/drift_status): kein Write-back
target.status = "in_sync" if actual_hash == target.expected_hash else "drift"
```

**Fix (E3, 2026-09-02):** Bisher wurde `nozzle_temperature` bei jedem Aufruf (auch beim automatischen `drift_status()`-Seitenladen) aus Cloud zurückgeschrieben — ein stiller Catalog-Mutation. Jetzt ist `adopt` ein keyword-only Parameter (`adopt: bool = False`); `drift_status()` nutzt den Default (read-only), die sync-from-orca-Routen übergeben `adopt=True`. Der Write-back passiert nur noch bei explizitem Benutzer-Klick.

**Verbleibendes Problem:** Beim expliziten "Sync from Orca"-Klick wird `nozzle_temperature` noch immer ohne Warnung überschrieben. Das ist aber jetzt eine bewusste Benutzeraktion, kein stiller Side-Effect beim Seitenladen.

### V14: Profile-Delete mit Cloud-Cleanup vs. Generate-Preset — gegenläufige Cloud-Operationen

- **Generate Preset** (W3): Pushed Catalog → Orca Cloud (erstellt/updated Cloud-Preset)
- **Profile Delete** (W9): Löscht Catalog-Profile + optional Cloud-Preset (`sync_delete`)

**Problem:** Beide interagieren mit Orca Cloud, aber an völlig verschiedenen Orten (Sync-Seite Icon vs. Delete-Seite Checkbox). Es gibt keinen zentralen Ort, an dem der Cloud-Status eines Profils verwaltet wird. Der Benutzer muss wissen, dass er beim Löschen explizit die Cloud-Checkbox setzen muss, sonst bleibt der Cloud-Preset orphaned.

---

## Lösungskategorien (Richtungen + Umsetzungs-Status)

### Kategorie A: Begriffe klären und konsistent benennen — **umgesetzt (Sync Hub Redesign)**
- ~~"Sync" durch richtungsdeutliche Begriffe ersetzen: "Pull from Cloud" / "Push to Printer" / "Push to Cloud"~~ — **umgesetzt**
- ~~"Generate Preset" als Text-Label statt Icon-only~~ — **umgesetzt** ("Push to Cloud")
- "Backup to GitHub" beibehalten, aber Auto-Trigger sichtbar machen — *teilweise* (Note im GitHub Card: "Also triggered automatically after every successful preset push")

### Kategorie B: UI-Konsolidierung — weniger Orte, klarere Trennung — **umgesetzt (Sync Hub Redesign)**
- ~~"Sync from Orca" auf einen Ort reduzieren~~ — **umgesetzt** (Bulk-Pull nur noch im Sync Hub)
- ~~Dashboard = reine Übersicht (keine auto Cloud-Calls)~~; Sync-Seite = Aktionsseite — **entschieden (2026-09-03):** Cloud-Call bleibt bewusst (Badge-Frische), Sichtbarkeit über C3a-Hinweis
- ~~Import-Orca und Migration als Tabs auf einer "Data Import" Seite zusammenfassen~~ — **umgesetzt** (`/import` mit Tabs)

### Kategorie C: Auto-Trigger sichtbar machen — **umgesetzt (Kategorie C, 2026-09-03, commit 4a20ae2)**
- ~~Auto `refresh_preset_drift` bei Seitenladen: Lade-Indikator + Hinweis "Checking Cloud sync..."~~ — **umgesetzt:** Statischer Hinweis "Status refresh includes a live check against Orca Cloud." unter beiden Drift-Tabellen (statischer Hinweis statt Lade-Indikator — bewusste Entscheidung)
- ~~Auto `verify` nach `push`: Im Push-Toast anzeigen ("Verify scheduled...")~~ — **umgesetzt:** Push-Toasts enden mit "— verifying..."
- ~~Auto `github_backup` nach `generate_preset`: Im Preset-Toast anzeigen ("Backup scheduled...")~~ — **umgesetzt:** "Preset pushed — GitHub backup scheduled" (wenn Backup enqueued)
- ~~Auto `nozzle_temperature` write-back: Warnung oder Bestätigung, bevor Catalog-Daten überschrieben werden~~ — **behoben (E3):** Write-back passiert nur noch bei explizitem "Sync from Orca"-Klick (adopt=True), nicht mehr beim Seitenladen

### Kategorie D: Per-Profil-Aktionen vereinheitlichen — **umgesetzt (Sync Hub Redesign)**
- ~~Drift-Tabelle: Klare Buttons pro Zeile mit Text-Labels ("Push to Printer" / "Push to Cloud" / "Pull from Cloud")~~ — **umgesetzt**
- ~~"Push all" als Sekundäraktion, nicht als einzige Option~~ — **umgesetzt** (per-Profil Buttons + Bulk in Target-Cards)
- Cloud-Status eines Profils an zentraler Stelle verwalbar (nicht nur beim Delete) — *offen*

### Kategorie E: Bug-Fixes — **alle umgesetzt (2026-09-02)**

| Bug | Fix | Ticket |
|-----|-----|--------|
| Import-Orca "Sync now" hardcoded PID=1 | Form postet jetzt an bulk-Endpoint `/profiles/sync-from-orca` | E1 |
| Import-Orca Profile ohne SyncTarget | `import_orca_apply` legt `SyncTarget(orca_preset)` an (cloud_id, expected_hash, actual_state, pending) | E2 |
| `refresh_preset_drift` als Side-Effect von `drift_status()` mutiert Catalog-Daten | `adopt: bool = False` Parameter; `drift_status()` = read-only, sync-from-orca-Routen = adopt | E3 |

### Kategorie F: Sync Hub Redesign — **umgesetzt (2026-09-02)**

| Maßnahme | Status | Tickets |
|----------|--------|---------|
| Sync-Seite → "Sync Hub" M3 Two-Column Layout (Drift-Tabelle + Target-Cards) | **umgesetzt** | Task 3 |
| Richtungdeutliche Button-Labels ("Push to Printer" / "Push to Cloud" / "Pull from Cloud") | **umgesetzt** | V1, V7 |
| "Sync from Orca" von Profiles-Seite entfernt; Bulk-Pull in Sync Hub | **umgesetzt** | V2 |
| Per-Profil "Push to Printer" Button in Drift-Tabelle | **umgesetzt** | V11 |
| Migration + Import-Orca → `/import` mit Tabs | **umgesetzt** | V6 |
| Icon-Button → Text-Button "Push to Cloud" | **umgesetzt** | V7 |
| Profiles-Seite: "Sync status →" Link statt "Sync from Orca" Button | **umgesetzt** | V2 |
| Task-4 Minors (M1-M7) + Task-5 Minors (M1/M2) | *deferred* | — |

**Test-Stand:** 427 Tests grün (`scripts/test.sh`), inkl. Tests für E2 (SyncTarget-Erstellung + in_sync nach Import), E3 (read-only drift check + adopt-Verhalten), Sync Hub Redesign (Sync Hub UI, Import-Tabs, per-Profil Actions, Route-Removal 404s), Back-Button-Navigation (Scan ↔ Review), und TestAutoTrigger in `tests/test_github_backup.py` (Assertions für `github_backup_scheduled`-Flag).
