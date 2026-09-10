# Filament Manager — Rework Spec

**Datum:** 2026-09-04
**Status:** Approved (planning)
**Repo:** `/home/oliverzein/Dokumente/Daten/Development/3D Printing/filament-manager`
**Branch:** `feat/category-c-auto-trigger-visibility`
**Vorgänger:**
- `docs/rework/phase-0-audit.md` — Live-evidentierte Entscheidungen (F1=B, F2=D, F3=B), commit `ccb3a60`
- Original-Spec `docs/superpowers/specs/2026-08-26-filament-manager-design.md` (archiviert unter `docs/archive/superpowers/specs/`)

Dieses Dokument ist der verbindliche Referenz-Entwurf für den mehrphasigen Rework.
Jede technische Aussage ist durch den Phase-0-Audit belegt; wo der Audit keine
Evidenz liefert, ist der Punkt in §13 als "Offen" markiert.

---

## 1. Zweck

Rework der App-Architektur: **Orca Cloud ist die EINZIGE Profil-Quelle.** Es gibt
keinen internen Profile-Store mehr, keine Profil-Modifikation in der App. Neue
Profile entstehen ausschließlich in OrcaSlicer Desktop — nie in der App.

Die App behält genau drei Funktionen:

1. **Spool-Inventar** — unverändert (`spools`-Tabelle, RFID-Tags, Restmenge).
2. **Push-to-Printer** — Printer-DB-Einträge werden aus live aus der Cloud
   gelesenen Feldern gebaut. Ein Temp-Cache hält die gelesenen Werte nur für die
   Dauer der Operation.
3. **Minimaler lokaler Cache** — `vendor`, `name`, `type` für das
   Spool-Inventar, gelesen aus Orca Cloud.

**Non-goals:**

- Multi-Printer-Support.
- Multi-User/Auth über den bestehenden Agent-Token hinaus.
- Drying-Verwaltung — `dryingTemp`/`dryingTime` bleiben dauerhaft auf Default 0
  (Audit: kein Cloud-Preset hat ein `dry`-Feld, 0/10; CFS RFID matcht auf ID,
  nicht auf drying).
- Farbverwaltung — `colors`/`default_filament_colour` bleibt dauerhaft leer.
  Farbe lebt auf dem Spool via `spools.color_override` (Audit: alle
  Cloud-Farbwerte sind `""`, 8/10 vorhanden, alle leer).

---

## 2. Architektur

**Kernsatz:** Catalog-as-mirror entfällt. Damit entfällt die gesamte
Drift-/Adopt-/Write-back-Maschinerie — die Bugklasse, die in dieser Session
behandelt wurde (E3 read-only-vs-adopt, Duplicate-Preset, nozzle-Write-back).

```
OrcaSlicer Desktop
      │ (benutzerseitige Pflege, einzige Edit-Quelle)
      ▼
Orca Cloud
      │ (Sync: Pull Subset — READ-ONLY)
      ▼
Filament Manager Cache  (Unraid Container)
      │ (Push: Live-Read + Build)
      ▼
K2 Printer DB
```

### Komponenten

| Komponente | Läuft auf | Tech | Rolle |
|---|---|---|---|
| Web-App | Unraid Docker | Python 3.12, FastAPI, SQLAlchemy + SQLite, Jinja2 + HTMX | Cache, Spool-Inventar, Push-Orchestrierung |
| Orca Cloud | OrcaSlicer-Backend | Supabase-auth, Sync-API | Quelle (einzige Profil-Quelle) |
| K2 Printer DB | Creality K2 | material_database.json | Ziel (Push) |

Die App liest aus Orca Cloud ausschließlich via `OrcaCloudClient.sync_pull`
(READ-ONLY). `sync_push` und `sync_delete` werden nicht mehr aufgerufen.

---

## 3. Datenmodell (Zielzustand)

### Entfällt

- **`profiles`-Tabelle komplett.** Sie war der interne Profile-Store und die
  Quelle der Drift-Bugklasse. Statt sie zum Cache zu degradieren, erfolgt ein
  sauberer Cut (Audit Frage 4: neue Cache-Tabelle, keine Altlasten).

### Neu: `profile_cache`

Lokaler Read-Only-Cache der Cloud-Presets. Nie manuell editierbar.

| Spalte | Typ | Hinweis |
|---|---|---|
| `cloud_resource_id` | TEXT PK | Orca Cloud Resource-UUID |
| `name` | TEXT | aus Cloud `name` (10/10) |
| `vendor` | TEXT | aus Cloud `filament_vendor` (8/10; vererbte via §5) |
| `type` | TEXT | aus Cloud `filament_type` (8/10; vererbte via §5) |
| `filament_id` | TEXT, nullable | vererbte Presets haben keins (Audit: 6/10) |
| `is_inherited` | BOOL | `inherits` ≠ `""` |
| `deleted_from_cloud` | BOOL | Soft-Delete: True wenn im Tombstone-Feed gelöscht |
| `deleted_at` | DATETIME, nullable | Zeitstempel des Soft-Deletes |
| `last_seen_at` | DATETIME | letzter erfolgreicher Cache-Sync |

**Soft-Delete-Regel:** Wird ein Cloud-Preset gelöscht, wird der Eintrag in `profile_cache` **nicht physisch gelöscht**, sondern auf `deleted_from_cloud = True` gesetzt. Dadurch bleiben Spool-Verknüpfungen intakt (Spools zeigen weiterhin Name und Vendor, erhalten aber ein Orphan-Badge), und Orphan-Einträge in der Printer-DB behalten ihre Klartext-Metadaten.

### `spools`

- `profile_id` (FK → profiles) **wird ersetzt** durch `cloud_resource_id`
  (Referenz auf `profile_cache`). Neue Spools referenzieren direkt den
  Cloud-Eintrag.
- Spools behalten auch bei Löschung des Presets in Orca Cloud ihren Verweis auf
  den `profile_cache`-Eintrag (`deleted_from_cloud = True`), was die Anzeige
  sichert und ein sauberes Orphan-Badge im Spool-Inventar ermöglicht.
- Unverändert bleiben: `color_override`, `remaining_pct`, `location`,
  `rfid_tag`, `notes`.

### `sync_targets` (Persistentes Printer-DB-Mapping)

- **Neue Spalte `printer_db_id`** (Entscheidung F2/D). Lookup-Schlüssel für den
  Push ist `cloud_resource_id` (über `cloud_id`), nicht mehr `profile_id`.
- Dient als persistentes Mapping: `cloud_id` → `printer_db_id` + `expected_hash` + `status` + `last_synced_at`.
- `kind` bleibt während der Migration (`printer_db` vs. `orca_preset`) und wird
  nach Migration auf `printer_db` fokussiert.
- `expected_hash` / `status`-Semantik verbleibt für den Push-Verify-Zyklus
  (siehe §6: Verify vergleicht Hash aus Cloud-Werten).
- `profile_id`-FK wird obsolet (keine `profiles`-Tabelle mehr).
### Unverändert

- `jobs`, `activity_log`.
- Settings-Store inkl. Orca-Token-Rotation (`save_orca_token`).
- GitHub-Backup-Config.

### Migration

Siehe §8 (One-time, Task-Serie im Rework).

---

## 4. Cloud-Lese-Subset (Cache-Sync)

### Gelesene Felder pro Preset

`name`, `filament_vendor`, `filament_type`, `filament_id`, `inherits`,
Resource-`id`, `updated_time`. (Vollständige Feld-Abdeckungstabelle im Audit;
dieses Subset reicht für Cache-Anzeige + Vererbungs-Auflösung + Push-Build.)

### Trigger

- **On-demand:** Button / Endpoint (Cache-Sync).
- **Non-blocking beim Dashboard-Load:** Muster ist die Background-Drain- /
  Action-Modal-Infrastruktur aus dieser Session. **KEIN blocking Call im
  Render-Pfad** (V8/Lektion: `refresh_preset_drift` blockierte ehemals jedes
  Seitenladen — explizit verworfen).

### Cache-Update

- Upsert nach `cloud_resource_id`.
- Presets im `deletes`-Tombstone-Feed des `sync_pull` → **Soft-Delete**: `deleted_from_cloud = True` und `deleted_at = _now()` setzen. Kein physischer DELETE! Die referenzierenden Spools und Printer-DB-Mappings behalten ihren Cache-Eintrag und bekommen im UI ein Orphan-Badge (§7).
- Cloud nicht erreichbar → Cache **gefroren anzeigen** + Hinweis-Badge. **Kein
  Fehler-Nunzius im Dashboard.**

---

## 5. Vererbungs-Auflösung (Entscheidung F1/B)

OrcaSlicer Desktop legt neue Presets stets vererbt an (`inherits` ≠ `""`). Zwei
der zehn Live-Presets sind vererbt (`Hyper Stardust @K2-all`,
`EN-PLA+ @K2-all`) und tragen nur Override-Felder — Kernfelder
(nozzle_temperature, filament_density, filament_vendor, filament_type,
filament_id) fehlen und kommen vom vererbten System-Preset.

**Vererbung in OrcaSlicer ist inhärent rekursiv (Multi-Level):**
`Cloud-Preset -> K2-System-Preset (@K2-all) -> fdm_filament_<type> -> fdm_filament_common`.
Beide Live-Presets sind bereits 3 Ebenen tief; Kernfelder (`nozzle_temperature`,
`filament_density`) stammen erst aus den tieferen Basisschichten. Ein
1-Ebenen-Limit würde beide Presets brechen.

**Auflösung vor JEDER Verwendung** (Push UND Cache-Anzeige von Kernfeldern):

1. Rekursives Flattening via `fm/presets/flattener.py:flatten_preset` (bereits im
   Projekt vorhanden, löst Ketten über beliebig viele Ebenen auf).
2. Base-Templates aus der Kette liefern Standardwerte, Overrides des Cloud-Presets
   gewinnen.

**Bundling-Pflicht in Phase 1:**
Im Docker-Container (`python:3.12-slim`) existiert `/opt/orca-slicer` nicht. Die
benötigten K2-System-Presets (`*@K2-all.json` und deren Eltern-Hierarchie) müssen
in Phase 1 fest nach `fm/presets/templates/` gebündelt werden (aus
`/opt/orca-slicer/resources/profiles/Creality/filament/` bzw. dem Skill-Repo
`~/Dokumente/Daten/Development/skills/Creality-custom-filament` — nur lesen/übernehmen,
nie modifizieren per AGENTS.md Working Rule 3).

**Phase 1 beginnt zwingend mit einem Verifikationstest:**
"Hyper Stardust @K2-all" und "EN-PLA+ @K2-all" müssen über `flatten_preset`
vollständig auflösen und alle Pflichtwerte (`nozzle_temperature`, `density`,
`vendor`, `type`) liefern.

**Auflösung schlägt fehl** (Name in Kette nicht auflösbar) → Preset ist **nicht pushbar**,
UI-Hinweis. Keine geratenen Defaults — F1/C (Fallback-Werte, Raterei) wurde
explizit verworfen.

---

## 6. Push-to-Printer (Reshaped)

### Flow

```
Cloud-Live-Read (sync_pull, Temp-Cache)
  → Vererbungs-Merge (§5)
  → Entry-Build (build_entry / _profile_to_values-Logik;
                 Quelle jetzt Cloud-Preset statt Catalog)
  → Rebuild
  → bestehende Pipeline UNVERÄNDERT:
      MIN_DB_ENTRIES → WebSocket-Busy-Check → Backup (Rotation 5)
      → Version-Bump (≥ 9876543210) → SCP → Reboot → Verify
```

Iron Rules (AGENTS.md) unverändert.

### `printer_db_id`-Lookup

- Lookup `sync_targets.printer_db_id` via `cloud_resource_id`.
- Fehlt der Eintrag → `next_free_id` (99xxx, auto-inkrement ab `id_range_start`).
- Creality presets are pushed normally with 99xxx IDs (stock RFID block
  removed — custom/optimized Creality profiles can now be tagged via
  RFID like any other brand).

### Type-Unboxing & Adapter (`_cloud_preset_to_entry_values`)

Orca-Cloud-Presets und geflattete Templates liefern Werte häufig als 1-elementige Listen von Strings (z. B. `"filament_density": ["1.24"]`, `"nozzle_temperature": ["220"]`). `fm/printer/dbcodec.py:validate_entry()` und `build_entry()` verlangen jedoch strikte Typen (`int`, `float`, `str`).

Vor dem Aufruf von `build_entry` übersetzt eine Adapter-Funktion `_cloud_preset_to_entry_values(preset: dict, entry_id: str) -> dict`:
- **Unboxing:** Listen mit 1 Element werden zum Skalar entpackt (`val[0] if isinstance(val, list) else val`).
- **Sanitizing:** Anführungszeichen/Whitespace werden bereinigt.
- **Casting:** `minTemp`, `maxTemp`, `nozzle_temperature` → `int`; `density`, `flowRatio`, `pa`, `maxVolumetric` → `float`.
- **Defaults:** `dryingTemp = 0`, `dryingTime = 0`, `color = None` (wird zu `colors = [""]`).
- **Naming / Iron Rule 2:** Preset-Name wird wie in OrcaSlicer gepflegt übernommen. Fehlt der Vendor im Namen, erzeugt `validate_entry` wie heute ein Warning, blockiert den Push aber nicht.

### Payload & API Contract

`POST /api/sync/push` und `/sync/push-modal` akzeptieren `cloud_resource_ids: list[str]` (Orca Cloud UUIDs) anstelle der obsoleten Integer-`profile_ids`. Wird keine Liste übergeben, werden alle pushbaren Presets gepusht.
### Gaps (entschieden)

- `dryingTemp` / `dryingTime` = 0 (Audit: harter Gap, Default 0 akzeptiert).
- `colors` = leer (Audit: keine echten Farbwerte in der Cloud, akzeptiert).

### Orphan-Regel (Entscheidung F3/B)

`_rebuild_printer_db` **verliert das Remove-Verhalten**: 99xxx-Einträge ohne
Cloud-Preset bleiben erhalten, werden im UI als Orphan geflaggt. Entfernung nur
über eine explizite Aktion mit Double-Confirm (Rule 5, typed token).

Begründung (Audit): Im Rework ist OrcaSlicer die Quelle — ein Klick dort kann
ein Preset löschen, ohne dass der Benutzer die Auswirkung auf RFID-Tags
versteht. Auto-Remove (F3/A) wurde verworfen; Tombstone-only (F3/C) wurde
wegen Cursor-Komplexität ohne Mehrwert für Single-User verworfen.

### Verify (Reshaped)

Verify bleibt. Hash-Vergleich arbeitet mit Cloud-gelesenen Werten, nicht mit
`Profile`-Spalten. `SyncTarget.expected_hash` wird aus dem Cloud-Preset
berechnet. Cloud-rollback detection (Version-Floor ≥ 9876543210) bleibt wie
heute.

---

## 7. UI-Seiten (Zielzustand)

UI-Arbeit verwendet die **designer role** (omp designer agent) gemäß AGENTS.md.

- **Dashboard:** Stats aus Cache + Spools; Cache-Sync non-blocking;
  Cloud-Offline-Badge.
- **Spools:** unverändert + Orphan-Badge (referenziertes Cloud-Preset gelöscht).
- **Printer/Sync-Seite (umbenannt/reshaped):**
  - **Preset- & Status-Tabelle (ersetzt Drift-Tabelle):**
    - Listet alle Presets aus `profile_cache` (Vendor, Name, Type, Vererbungs-Status).
    - Zeigt zugeordnete `printer_db_id` (z. B. `99001` oder `Not pushed`).
    - Status-Badges: `In Sync`, `Pending Verify`, `Orphan` (in Cloud gelöscht).
    - Aktionen pro Zeile: `Push to Printer` (Einzel-Push via `cloud_resource_id`), bei Orphans `Remove from Printer` (Double-Confirm mit typed token).
  - Target-Cards (rechts): Printer DB ("X in sync, Y pending/unlinked"), Orca Cloud Status ("X cached, Y orphans"), Backup to GitHub.
  - Push-Buttons — Action-Modal-Infrastruktur dieser Session wird wiederverwendet (Polling, Busy-Choices im Modal, Hub-Refresh).
  - Cache-Sync-Button + zuletzt-synchronisiert-Anzeige.
- **Gelöscht:**
  - Profiles-Seite (CRUD).
  - Profile-Edit + Orca-Fragment.
  - Import-Seite (beide Tabs "From Printer DB" / "From Orca Cloud").
  - Preset-Generierung inkl. Builder.

Richtungs-Button-Namen ("Push to Printer" etc.) und Feedback-Konventionen
(Modal, Toast, `hx-confirm`) bleiben exakt wie etabliert. htmx bleibt auf
`htmx.org@1.9.12` gepinnt (unversionierte URL lieferte 2.x, wo `hx-on::`-Handler
still sterben).

---

## 8. Migration (One-time, im Rework als Task-Serie)

Zur Vermeidung von Datenverlust muss die Migration in einer strikten SQL-Reihenfolge ablaufen, damit die Zuordnung von `cloud_id` (aus `kind='orca_preset'`) und `printer_db_id` (aus `profiles`) nicht durch vorzeitiges Löschen verloren geht:

1. **`sync_targets.printer_db_id` befüllen und `cloud_id` auf `printer_db` übertragen:**
   ```sql
   ALTER TABLE sync_targets ADD COLUMN printer_db_id VARCHAR(16);
   UPDATE sync_targets
   SET cloud_id = (
       SELECT o.cloud_id FROM sync_targets o
       WHERE o.profile_id = sync_targets.profile_id AND o.kind = 'orca_preset'
   ),
   printer_db_id = (
       SELECT p.printer_db_id FROM profiles p
       WHERE p.id = sync_targets.profile_id
   )
   WHERE kind = 'printer_db';
   ```
   Die 6 existierenden Einträge erhalten stabil ihre ID:

   | Name | printer_db_id | cloud_resource_id |
   |---|---|---|
   | Sunlu PLA+ Optimized | 99001 | `7a6f6eaf…` |
   | eSUN PETG Basic Optimized | 99002 | `64ebeda3…` |
   | CAILAB PLA Silk | 99003 | `71e3e825…` |
   | Cailab PLA+ Bio | 99004 | `8a9c4997…` |
   | Overture TPU Optimized | 99005 | `08c63940…` |
   | Geetech PLA | 99006 | `2e4254f1…` |

2. **`spools.profile_id` → `cloud_resource_id` migrieren:**
   ```sql
   ALTER TABLE spools ADD COLUMN cloud_resource_id VARCHAR(64);
   UPDATE spools
   SET cloud_resource_id = (
       SELECT st.cloud_id FROM sync_targets st
       WHERE st.profile_id = spools.profile_id AND st.cloud_id IS NOT NULL
   );
   ```
   15 Spool-Rows referenzieren aktuell Profile (1–10). Keine RFID-Tags aktuell gesetzt (alle `rfid_tag` NULL) — dennoch bleibt `printer_db_id`-Stabilität für zukünftige Tags erhalten.

3. **`profile_cache` initial befüllen** — aus `sync_pull` (inkl. `deleted_from_cloud = False`).

4. **Bereinigung erst NACH Live-Verifikation:**
   Erst nachdem die neue Pipeline verifiziert ist, werden `kind='orca_preset'`-Zeilen gelöscht (`DELETE FROM sync_targets WHERE kind = 'orca_preset'`) und alte Spalten/Tabellen gedroppt.
**Rule 5 (Double-Confirm, typed token) gilt für jeden destruktiven
Migrationsschritt.**

---

## 9. Was stirbt / was überlebt

Handler-Checkliste (Audit, Quelle `register_handlers()` — 6 Handler):

| Handler | Rework-Entscheidung | Begründung |
|---|---|---|
| `push` | **KEEP + RESHAPE** | Quelle = Cloud-Upsets, nicht `select(Profile)`. Iron Rules unverändert. |
| `verify` | **KEEP + RESHAPE** | Hash aus Cloud-Werten, nicht aus `Profile`-Spalten. |
| `generate_preset` | **DELETE** | Keine Profil-Modifikation in der App; Presets entstehen in OrcaSlicer Desktop. `sync_push` entfällt. |
| `pull` | **DELETE** | Adopt-Logik (Cloud → Profile-Store write-back) obsolet — kein Profile-Store. |
| `import_scan` | **DELETE** | Migration-Wizard war einmalig; Orca Cloud ist jetzt die Quelle. |
| `github_backup` | **KEEP** | Liest aus Orca Cloud, schreibt nach GitHub — unabhängig vom Profile-Store. |

**Templates (`fm/presets/templates/` + `template_index.py`) BLEIBEN** — der
Vererbungs-Merge (§5) braucht sie. Nur `build_standalone_preset` /
Builder-Code (`fm/presets/builder.py`) stirbt.

**Drift-/Adopt-Maschinerie stirbt komplett:** `refresh_preset_drift`,
`_discover_cloud_resource`, `_entry_to_proposal`, `fm/presets/matching.py`
(Profile ↔ Cloud), Profile-CRUD (`fm/api/catalog.py`), Profile-Edit +
Orca-Fragment, Orca-Import-Seite, Profile-Delete mit Cloud-Cleanup.

**Überlebt (unverändert):** Spool-Inventar, Printer-DB-Codec (`build_entry`,
`load_db`, `save_db`, `find_entry`, `remove_entry`, `next_free_id`,
`bump_version`), Push-Pipeline (`run_push`, `verify_printer`), Transport
(SSH/SCP/WS), Orca Cloud Client (`sync_pull` bleibt READ-ONLY), Settings-Store,
GitHub Backup, Job-Queue.

---

## 10. Fehlerbehandlung

- **Cloud nicht erreichbar:** Cache-Anzeige gefroren + Badge; Push verweigert
  mit klarer Meldung.
- **Auth abgelaufen:** bestehende Token-Rotation (`save_orca_token`).
- **Verify-Fail:** wie heute (Cloud-rollback detection, Version-Floor).
- **Orphan-Push-Versuch:** verweigert mit Hinweis (Preset nicht pushbar, da
  Cloud-Preset gelöscht / Vererbung nicht auflösbar).

---

## 11. Test-Strategie

TDD (writing-plans-Konvention). SSH-Boundary gemockt wie heute.

Neue Testkerne:

- **Merge-Kette** inkl. Verifikationstest §5 ("Hyper Stardust @K2-all",
  "EN-PLA+ @K2-all" lösen über `flatten_preset` vollständig auf).
- **Type-Unboxing & Adapter** (`_cloud_preset_to_entry_values` unboxed Listen, castet Typen, setzt Defaults).
- **ID-Mapping-Konsistenz** (`cloud_resource_id` → `printer_db_id`).
- **Rebuild-ohne-Remove** (Orphan-Regel F3/B).
- **Migration-Scripts** (strikte Sequenz aus §8).
- **Cache-Sync** (Upsert, Soft-Delete bei Tombstones, Offline-Verhalten).

**Phasenweise Test-Bereinigung:**
Über 200 der bestehenden 486 Tests betreffen Features, die im Rework gelöscht werden (`test_api_catalog.py`, `test_builder.py`, `test_matching.py`, `test_ui_delete.py`, `test_ui_template_picker.py` etc.). Die Dekommissionierung dieser Tests muss phasenweise mit den entsprechenden Code-Schnitten erfolgen, damit die Test-Suite bei jedem Zwischenschritt messbar grün bleibt.

---

## 12. Deployment

Unverändert: `scripts/release.sh` / `deploy-unraid.sh`, `FM_*`-Env-Vars.
Versions-Sprung nach Rework: **Major (2.0.0)**. Docs-Sync-Konvention (README +
Manuals) gilt.

---

## 13. Offene Punkte

- **Exakte Cache-Feldliste finalisieren** — ob `filament_id` in den Cache muss
  (vererbte Presets haben keins; aktuell im §3-Schema als nullable
  vorgesehen).
- **UI-Details Orphan-Verwaltung** — designer role (Badge-Platzierung,
  Entfernen-Flow-Details).
- **Multi-Level-Vererbung (GEKLÄRT / ENTSCHIEDEN):** Rekursives Flattening via
  `fm/presets/flattener.py:flatten_preset` ist verbindlich. Keine
  YAGNI-Beschränkung auf 1 Ebene, da reale Live-Presets 3 Ebenen tief sind.
