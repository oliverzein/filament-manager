# Phase-0-Audit — Filament-Manager Rework

**Datum:** 2026-09-04
**Branch:** `feat/category-c-auto-trigger-visibility`
**Status:** Entscheidungsvorlage — KEIN Rework-Code
**Test-Stand:** 486 Tests grün (`scripts/test.sh`), keine Code-Änderungen in dieser Phase

---

## Kontext: Rework-Ziel

Der geplante Rework eliminiert den internen Profile-Store (`profiles`-Tabelle als
Profile-Quelle). Orca Cloud wird zur **einzigen Profile-Quelle**. Neue Profile
werden in OrcaSlicer Desktop erstellt, nicht in der App. Die App behält:

1. **Spool-Inventar** (unverändert — `spools`-Tabelle, RFID-Tags, Restmenge)
2. **Push-to-Printer** (Printer-DB-Einträge aus Cloud-Presets live gelesen)
3. **Minimalen lokalen Cache** (vendor, name, type) — gelesen aus Orca Cloud

Push-to-Printer muss die benötigten Felder **live aus Orca Cloud lesen** (temp-Cache
nur für die Dauer der Operation). Keine Profil-Modifikation in der App.

Dieser Audit beantwortet drei offene Fragen, bevor Rework-Code geschrieben wird.

---

## Frage 1 — Feld-Abdeckung: Cloud-Preset vs. Printer-DB-Eintrag

### Methodik

**Live-Pull:** `OrcaCloudClient.sync_pull(cursor=0)` mit Credentials aus
`.local/settings.json` (`orca_refresh_token` + `orca_user_id`), geladen wie
`_default_orca_client()` in `fm/service/sync_handlers.py:93`. READ-ONLY —
kein `sync_push`, kein `sync_delete`. Diagnose-Skript:
`.local/phase0_audit_pull.py` (gitignored, nicht committed).

**Ergebnis:** 16 upserts total (10 filament, 4 print, 2 printer), 11 deletes.
**10 Filament-Presets** in Orca Cloud.

### Live-Daten: Preset-Inventar

| # | Cloud-Resource-ID | Name | filament_id | inherits | from | Vollständige Felder? |
|---|---|---|---|---|---|---|
| 1 | `64ebeda3…` | eSUN PETG Basic Optimized | `Pcc54342b78005b` | `""` | — | ✓ voll |
| 2 | `71e3e825…` | CAILAB PLA Silk | `Pfae533fc102a2` | `""` | — | ✓ voll |
| 3 | `4afddf33…` | Creality Hyper PLA Optimized | `P959e9ac23c0d80` | `""` | — | ✓ voll |
| 4 | `0cfe86bc…` | Hyper Stardust Optimized | *(absent)* | `Hyper Stardust @K2-all` | — | ✗ vererbt |
| 5 | `8a9c4997…` | Cailab PLA+ Bio | *(absent)* | `""` | — | ✓ voll, kein fid |
| 6 | `08c63940…` | Overture TPU Optimized | *(absent)* | `""` | — | ✓ voll, kein fid |
| 7 | `3ab10a0c…` | EN-PLA+ Optimzed | *(absent)* | `EN-PLA+ @K2-all` | — | ✗ vererbt |
| 8 | `ba0716c6…` | Jayo PLA+ | `Pbb4c36bbcf74e` | `""` | `User` | ✓ voll |
| 9 | `2e4254f1…` | Geetech PLA | `Pb94705fa043b8` | `""` | `User` | ✓ voll |
| 10 | `7a6f6eaf…` | Sunlu PLA+ Optimized | `Pbbf5010` | `""` | — | ✓ voll |

**Zwei Preset-Typen:**

- **Vollständige Presets** (8/10): Tragen alle Kernfelder explizit
  (nozzle_temperature, filament_density, filament_vendor, filament_type, etc.).
  Erzeugt von der App (`generate_preset`) oder OrcaSlicer Desktop mit
  `from: User`.

- **Vererbte Presets** (2/10): `inherits` zeigt auf ein System-Preset
  (z.B. `Hyper Stardust @K2-all`). Nur Override-Felder gespeichert
  (flow_ratio, pressure_advance, max_volumetric_speed, retraction_length).
  Kernfelder wie nozzle_temperature, filament_density, filament_vendor,
  filament_type **fehlen** — sie kommen vom vererbten System-Preset.

### Feld-Abdeckungstabelle (Live-Pull, 10 Filament-Presets)

| Cloud-Feld | Abdeckung | Sample-Werte | Hinweis |
|---|---|---|---|
| `name` | 10/10 (100%) | "eSUN PETG Basic Optimized", … | immer vorhanden |
| `type` | 10/10 (100%) | `"filament"` | Orca Cloud Metadaten, nicht filament_type |
| `version` | 10/10 (100%) | "2.4.0.3", "2.3.2.74", "2.3.2.75" | |
| `inherits` | 10/10 (100%) | `""`, `"Hyper Stardust @K2-all"` | leer = standalone, nicht-leer = vererbt |
| `filament_settings_id` | 10/10 (100%) | `"eSUN PETG Basic Optimized"` | |
| `filament_max_volumetric_speed` | 10/10 (100%) | 10, 20, 19, 16 | |
| `filament_flow_ratio` | 9/10 (90%) | 1.02, 1.0227, 0.98, 0.92 | 1 vererbt ohne |
| `filament_retraction_length` | 9/10 (90%) | nil, 0.8, 0.6, 0.4, 1.2 | |
| `filament_retraction_speed` | 9/10 (90%) | nil, 30 | meist nil |
| `nozzle_temperature` | 8/10 (80%) | 240, 230, 220, 222, 235 | **2 vererbte fehlen** |
| `nozzle_temperature_range_low` | 8/10 (80%) | 230, 200, 210, 210 | **2 vererbte fehlen** |
| `nozzle_temperature_range_high` | 8/10 (80%) | 250, 240, 240, 230 | **2 vererbte fehlen** |
| `nozzle_temperature_initial_layer` | 8/10 (80%) | 240, 230, 220, 225 | |
| `filament_density` | 8/10 (80%) | 1.27, 1.24, 1.25, 1.18 | **2 vererbte fehlen** |
| `filament_vendor` | 8/10 (80%) | "eSUN", "CAILAB", "Creality" | **2 vererbte fehlen** |
| `filament_type` | 8/10 (80%) | "PETG", "PLA", "TPU" | **2 vererbte fehlen** |
| `filament_diameter` | 8/10 (80%) | 1.75 | **2 vererbte fehlen** |
| `default_filament_colour` | 8/10 (80%) | `""` (leer) | **2 vererbte fehlen** |
| `pressure_advance` | 8/10 (80%) | 0.02, 0.032, 0.065, 0.04 | |
| `enable_pressure_advance` | 8/10 (80%) | 0, 1 | |
| `filament_id` | 6/10 (60%) | `Pcc54342b78005b`, … | 2 vererbte + 2 ohne fid |
| `from` | 2/10 (20%) | `"User"` | nur App-generierte Presets |
| `compatible_printers` | 6/10 (60%) | `"Creality K2 0.4 nozzle"` | |
| `base_id` | 4/10 (40%) | `4OC4zOZaAUUsyu27` | nur vererbte Presets |

### Spezialfelder — mit Live-Evidenz beantwortet

#### Drying-Felder (jeder Key mit 'dry')

**Ergebnis: KEIN Cloud-Preset hat ein Feld mit 'dry' im Namen.**

```
DRYING FIELDS (any key containing 'dry')
  NONE FOUND — no cloud preset has any key containing 'dry'
```

Orca Cloud speichert keine Trocknungs-Parameter (`dryingTemp`, `dryingTime`).
Diese Felder existieren nur im lokalen Profile-Store und im Printer-DB-Eintrag
(`base.dryingTemp`, `base.dryingTime`). **Das ist ein harter Gap.**

#### Colors / default_filament_colour

**Ergebnis: 8/10 Presets haben `default_filament_colour`, alle mit Wert `""` (leer).**

```
default_filament_colour: 8/10 (80%)
  eSUN PETG Basic Optimized: ""
  CAILAB PLA Silk: ""
  Creality Hyper PLA Optimized: ""
  ...
```

Orca Cloud speert `default_filament_colour` als Flat-String (lokal als Array).
In der Live-Daten sind **alle Werte leer** (`""`). Die App hat beim
Preset-Generate bewusst `[""]` gesetzt (nicht-hex Farbnamen → leer, um
weiße Swatch-Icons zu vermeiden). **Keine echten Farbwerte in der Cloud.**

#### Retraction

`filament_retraction_length`: 9/10 (90%), Werte: nil, 0.8, 0.6, 0.4, 1.2
`filament_retraction_speed`: 9/10 (90%), Werte: nil, 30 (meist nil)

Retraction-Daten sind teilweise vorhanden, aber meist `nil` (Template-Default).

#### Flow

`filament_flow_ratio`: 9/10 (90%), Werte: 1.02, 1.0227, 0.98, 0.92

#### PA (Pressure Advance)

`pressure_advance`: 8/10 (80%), Werte: 0.02, 0.032, 0.065, 0.04
`enable_pressure_advance`: 8/10 (80%), Werte: 0, 1

### Printer-DB-Eintrag: exakte Feldliste

Quelle: `_profile_to_values` (`sync_handlers.py:209`) + `build_entry`
(`dbcodec.py:114`) + `_entry_hash_fields` (`sync_handlers.py:250`).

**Base-Felder** (in `entry["base"]`):

| Feld | Quelle heute (Catalog-Spalte) | In Cloud? | Gap? | Fallback |
|---|---|---|---|---|
| `id` | `profile.printer_db_id` (99xxx) | nein (App-assigned) | nein — App generiert | — |
| `brand` | `profile.brand` | `filament_vendor` (8/10) | **ja, 2/10 vererbt** | aus `inherits` auflösen oder aus `name` parsen |
| `name` | `profile.name` | `name` (10/10) | nein | — |
| `meterialType` | `profile.type` | `filament_type` (8/10) | **ja, 2/10 vererbt** | aus `inherits` auflösen oder Default "PLA" |
| `minTemp` | `profile.min_temp` | `nozzle_temperature_range_low` (8/10) | **ja, 2/10 vererbt** | aus `inherits` auflösen oder `nozzle_temperature` |
| `maxTemp` | `profile.max_temp` | `nozzle_temperature_range_high` (8/10) | **ja, 2/10 vererbt** | aus `inherits` auflösen oder `nozzle_temperature` |
| `density` | `profile.density` | `filament_density` (8/10) | **ja, 2/10 vererbt** | aus `inherits` auflösen oder 0 (wie heute bei Creality) |
| `dryingTemp` | `profile.drying_temp` | **NICHT VORHANDEN** (0/10) | **ja — harter Gap** | 0 (Default, wie `build_entry` heute) |
| `dryingTime` | `profile.drying_time` | **NICHT VORHANDEN** (0/10) | **ja — harter Gap** | 0 (Default, wie `build_entry` heute) |
| `colors` | `profile.colors` (JSON-Array) | `default_filament_colour` (8/10, alle `""`) | **ja — keine echten Farben** | `[""]` (wie heute) |

**kvParam-Felder** (in `entry["kvParam"]`):

| Feld | Quelle heute (Catalog-Spalte) | In Cloud? | Gap? | Fallback |
|---|---|---|---|---|
| `nozzle_temperature` | `profile.nozzle_temperature` | `nozzle_temperature` (8/10) | **ja, 2/10 vererbt** | `maxTemp` (wie `build_entry` heute: `values.get("nozzle_temperature", values["maxTemp"])`) |
| `nozzle_temperature_range_low` | `profile.min_temp` | `nozzle_temperature_range_low` (8/10) | **ja, 2/10 vererbt** | `minTemp` |
| `nozzle_temperature_range_high` | `profile.max_temp` | `nozzle_temperature_range_high` (8/10) | **ja, 2/10 vererbt** | `maxTemp` |
| `filament_type` | `profile.type` | `filament_type` (8/10) | **ja, 2/10 vererbt** | aus `inherits` auflösen |
| `filament_vendor` | `profile.brand` | `filament_vendor` (8/10) | **ja, 2/10 vererbt** | aus `inherits` auflösen oder aus `name` parsen |
| `filament_density` | `profile.density` | `filament_density` (8/10) | **ja, 2/10 vererbt** | aus `inherits` auflösen oder 0 |
| `filament_flow_ratio` | `profile.flow_ratio` | `filament_flow_ratio` (9/10) | **ja, 1/10 vererbt** | nil (Template-Default) |
| `pressure_advance` | `profile.pa` | `pressure_advance` (8/10) | **ja, 2/10 vererbt** | nil |
| `filament_max_volumetric_speed` | `profile.max_volumetric` | `filament_max_volumetric_speed` (10/10) | nein | — |

**Hash-Felder** (`_entry_hash_fields`, für Drift-Detection):

| Hash-Feld | Base/kvParam | In Cloud? | Gap? |
|---|---|---|---|
| `id` | base | nein (App) | nein |
| `brand` | base | `filament_vendor` (8/10) | ja, vererbt |
| `name` | base | `name` (10/10) | nein |
| `type` | base.meterialType | `filament_type` (8/10) | ja, vererbt |
| `minTemp` | base | `nozzle_temperature_range_low` (8/10) | ja, vererbt |
| `maxTemp` | base | `nozzle_temperature_range_high` (8/10) | ja, vererbt |
| `nozzle_temperature` | kvParam | `nozzle_temperature` (8/10) | ja, vererbt |
| `density` | base | `filament_density` (8/10) | ja, vererbt |
| `dryingTemp` | base | **NICHT VORHANDEN** | **ja — harter Gap** |
| `dryingTime` | base | **NICHT VORHANDEN** | **ja — harter Gap** |
| `pa` | kvParam.pressure_advance | `pressure_advance` (8/10) | ja, vererbt |
| `flow_ratio` | kvParam.filament_flow_ratio | `filament_flow_ratio` (9/10) | ja, vererbt |
| `max_volumetric` | kvParam | `filament_max_volumetric_speed` (10/10) | nein |

### Vererbungs-Problem: detaillierte Analyse

Zwei der zehn Cloud-Presets sind **vererbte Presets** (`inherits` nicht leer):

1. **Hyper Stardust Optimized** — erbt von `Hyper Stardust @K2-all`
   - Hat nur: `filament_flow_ratio`, `filament_max_volumetric_speed`,
     `pressure_advance`, `enable_pressure_advance`, `base_id`
   - Fehlt: nozzle_temperature, filament_density, filament_vendor,
     filament_type, filament_diameter, default_filament_colour, filament_id

2. **EN-PLA+ Optimzed** — erbt von `EN-PLA+ @K2-all`
   - Hat nur: `filament_retraction_length`, `filament_max_volumetric_speed`,
     diverse retraction-Felder (alle nil), `base_id`
   - Fehlt: nozzle_temperature, filament_density, filament_vendor,
     filament_type, filament_diameter, default_filament_colour, filament_id

**Ursache:** Diese Presets wurden in OrcaSlicer Desktop als "abgeleitete
Varianten" erstellt — sie überschreiben nur ausgewählte Felder eines
System-Presets. Die Kernwerte liegen im System-Preset, nicht in der Cloud.

**Lösungsansatz für Push-to-Printer:** Die App muss die Vererbungskette
auflösen. Orca Cloud liefert nur das Override-Preset; die Basisklasse
(System-Preset wie `Hyper Stardust @K2-all`) ist in den lokalen
Template-Dateien (`fm/presets/templates/`) gespeichert. Die App kann
`inherits` → Template-Lookup → Merge durchführen. Alternativ kann der
Benutzer angewiesen werden, nur "standalone" Presets in OrcaSlicer zu
erstellen (keine vererbten).

### Entscheidung benötigt — Frage 1

**A)** Nur standalone-Presets unterstützen. Vererbte Presets (`inherits` ≠ `""`)
werden beim Push-to-Printer übersprungen und im UI als "inherited — not
pushable" markiert. Benutzer muss in OrcaSlicer ein standalone-Preset erstellen.
— *Einfach, aber 2/10 existierende Presets würden nicht pushbar sein.*

**B)** Vererbungskette auflösen: `inherits`-Feld → Lookup in
`fm/presets/templates/` → Merge Override + Base. Vollständige Felder für
Push-to-Printer. — *Komplexer, aber alle Presets pushbar. Template-Index
muss um System-Preset-Namen erweitert werden.*

**C)** Hybride: Vererbte Presets pushen mit Fallback-Werten (nozzle_temperature
aus `nozzle_temperature` falls vorhanden, sonst 0; density=0; type aus
`inherits`-Name raten). Drift-Detection ignoriert Felder, die bei vererbten
Presets fehlen. — *Schnell, aber ungenau — Drift-Meldungen bei jedem Push.*

**Empfehlung: B.** Die Vererbungskette ist lösbar — die App hat bereits
`fm/presets/template_index.py` mit allen System-Presets. Der `inherits`-Wert
entspricht dem System-Preset-Namen. Ein Merge-Step vor `build_entry` liefert
alle benötigten Felder. Das ist die einzige Option, die alle 10 existierenden
Cloud-Presets korrekt verarbeitet, ohne Datenverlust oder unzuverlässige
Raterei.

**Drying-Gap:** Unabhängig von A/B/C: `dryingTemp` und `dryingTime` existieren
**nicht in Orca Cloud** (0/10 Presets). `build_entry` defaultet heute bereits
auf 0 (`values.get("dryingTemp", 0)`). Der Rework übernimmt diesen Default.
Trocknungs-Parameter werden im Printer-DB-Eintrag auf 0 gesetzt — die K2-Firmware
zeigt sie, aber sie sind nicht funktionskritisch (CFS RFID app matcht auf ID,
nicht auf drying). **Keine Aktion nötig — Default 0 ist akzeptiert.**

**Colors-Gap:** `default_filament_colour` ist in 8/10 Presets vorhanden, aber
**alle Werte sind `""`** (leer). Die App hat bewusst leere Farben gesetzt
(nicht-hex → leer, vermeidet weiße Swatch-Icons). `build_entry` setzt
`b["colors"] = [values["color"]]` nur wenn `color` in values. Ohne echte
Farben wird `colors` nicht gesetzt — der Template-Default bleibt. **Keine
Aktion nötig — leere Farben sind akzeptiert, wie heute.**

---

## Frage 2 — printer_db_id Persistenz-Strategie

### Heutige Mechanik

**ID-Zuweisung** (`_rebuild_printer_db`, `sync_handlers.py:463-471`):
1. Wenn `profile.printer_db_id` gesetzt → existierenden Eintrag entfernen,
   neu insertieren mit gleicher ID.
2. Wenn `profile.printer_db_id = None` → `next_free_id(db, config.id_range_start)`
   → auto-inkrement ab 99001, erste freie ID.

**Stock-ID-Schutz** (`_is_stock_rfid`, `sync_handlers.py:246`):
`STOCK_RFID_BRANDS = {"creality"}` — Creality-Profile werden vom Push
ausgeschlossen (ihre RFID-Tags matchen bereits Stock-01xxx-Einträge).

**`find_custom_entries`** (`dbcodec.py:43`): Filtert alle Einträge mit
`id.startswith("99")` — nur Custom-Einträge, keine Stock-Einträge.

**RFID-Beziehung:** Das CFS RFID-System auf dem Drucker matcht
Spool-Tag → Printer-DB-Eintrags-ID. Die Tag-ID ist "1" + Entry-ID
(für Stock) oder direkt die 99xxx-Nummer (für Custom). Die ID muss
**stabil** bleiben — ändert sie sich, matcht der Tag nicht mehr.

**filament_id-Zirkularität:** Heute ist
`filament_id = "P" + md5(f"{name} {db_id}")[:13]` (`builder.py:22`).
`db_id` ist `printer_db_id` (z.B. "99001"). Die filament_id hängt also von
der printer_db_id ab. Im Rework gibt es keinen Profile-Store mehr —
`printer_db_id` muss aus der Cloud-Resource abgeleitet werden, nicht aus
der filament_id (die selbst von der printer_db_id abhinge → Zirkelschluss).

### Existierende Einträge

| Profile-ID | Name | printer_db_id | filament_id | Cloud-Resource-ID |
|---|---|---|---|---|
| 1 | Sunlu PLA+ Optimized | 99001 | `Pbbf5010` | `7a6f6eaf…` |
| 2 | eSUN PETG Basic Optimized | 99002 | `Pcc54342b78005b` | `64ebeda3…` |
| 3 | CAILAB PLA Silk | 99003 | `Pfae533fc102a2` | `71e3e825…` |
| 4 | Cailab PLA+ Bio | 99004 | `00001` | `8a9c4997…` |
| 5 | Overture TPU Optimized | 99005 | `GFOT008` | `08c63940…` |
| 9 | Geetech PLA | 99006 | `Pb94705fa043b8` | `2e4254f1…` |

6 Profile mit `printer_db_id`, 4 ohne (Creality-Profile + Jayo PLA+).

### Optionen

**A) Lokale Mapping-Tabelle (`cloud_resource_id → printer_db_id`)**

Neue Tabelle `cloud_id_map` (cloud_resource_id TEXT PK, printer_db_id TEXT).
Bei Push: Lookup → existierende ID oder `next_free_id`. Migration: Existing
`SyncTarget.cloud_id` + `Profile.printer_db_id` → Mapping-Tabelle befüllen.

- *Vorteil:* Explizit, nachvollziehbar, keine Kollisionen, IDs bleiben stabil.
- *Nachteil:* Neue Tabelle (aber `sync_targets` hat bereits `cloud_id` +
  `profile_id` — die Mapping-Info existiert bereits implizit).
- *Migration:* 6 Einträge aus `sync_targets` (kind=orca_preset, cloud_id
  gesetzt) + `profiles.printer_db_id` → neue Tabelle. Direkt.

**B) Deterministische Ableitung aus Cloud-UUID (Hash → 99xxx, Kollisions-Probing)**

`printer_db_id = 99001 + (int(sha256(cloud_resource_id)[:4], 16) % 9999)`.
Bei Kollision: lineares Sondieren.

- *Vorteil:* Keine Tabelle, deterministisch — gleiche Cloud-ID → gleiche
  printer_db_id.
- *Nachteil:* **Kollisionen möglich** (10 Presets → gering, aber nicht null).
  **Bricht existierende IDs** — 99001-99006 würden neu berechnet, nicht
  erhalten. RFID-Tags würden invalidiert. Migration extrem fragil.

**C) filament_id-Hash (aktuelle Formel, evaluiert)**

`filament_id = "P" + md5(name + db_id)` — **Zirkelschluss**: `db_id` ist
die gesuchte `printer_db_id`, `filament_id` hängt davon ab. Ohne
Profile-Store gibt es keinen `db_id` als Input. **Funktioniert nicht.**

Zudem haben 4/10 Cloud-Presets gar kein `filament_id`-Feld (vererbte +
zwei ohne fid). **Nicht praktikabel.**

**D) SyncTarget.cloud_id als Mapping (Empfehlung)**

Die `sync_targets`-Tabelle hat bereits `cloud_id` (Orca Cloud Resource UUID)
und `profile_id`. Im Rwork wird `profile_id` durch `cloud_resource_id` als
primärer Key ersetzt. `printer_db_id` wird in `sync_targets` (kind=
`printer_db`) gespeichert, verknüpft über `cloud_id`.

Konkret: Die `sync_targets`-Tabelle mit `kind=printer_db` speichert
`cloud_id` (die Orca Cloud Resource UUID des Presets) und ein neues Feld
`printer_db_id` (oder `Profile.printer_db_id` wird beibehalten, aber der
Lookup erfolgt über `cloud_id` statt `profile_id`).

- *Vorteil:* **Keine neue Tabelle.** `sync_targets` existiert bereits mit
  `cloud_id`. Die Information "welches Cloud-Preset hat welche printer_db_id"
  ist bereits dort gespeichert (kind=printer_db, cloud_id=Resource-UUID).
- *Nachteil:* `sync_targets.profile_id` FK wird im Rework obsolet (keine
  `profiles`-Tabelle mehr als Profile-Quelle). Schema-Migration nötig.
- *Migration:* Für die 6 existierenden Einträge: `sync_targets` (kind=
  orca_preset) hat bereits `cloud_id`. `profiles.printer_db_id` kann in
  ein neues `sync_targets.printer_db_id`-Feld oder eine separate Spalte
  migriert werden. Die 6 Cloud-IDs sind bekannt und stabil.

### Entscheidung benötigt — Frage 2

**A)** Neue Mapping-Tabelle `cloud_id_map` (cloud_resource_id → printer_db_id).
**B)** Hash-Ableitung aus Cloud-UUID (bricht existierende IDs — abgelehnt).
**C)** filament_id-Hash (Zirkelschluss — nicht funktionsfähig).
**D)** `sync_targets` erweitern: `cloud_id` als primärer Lookup-Key für
`printer_db_id`, kein `profile_id`-FK mehr nötig.

**Empfehlung: D.** `sync_targets` hat bereits `cloud_id` (für 10/10 Presets
gesetzt, siehe Live-Daten). Die Tabelle muss nur um `printer_db_id` erweitert
werden (oder das Feld aus `profiles` migriert werden). Keine neue Tabelle,
keine Kollisionen, existierende IDs bleiben stabil. Die 6 existierenden
99001-99006 Einträge werden 1:1 migriert: `sync_targets.cloud_id` →
`printer_db_id` Mapping bleibt erhalten.

**Kollisions-Implikationen:** Keine — `printer_db_id` wird nicht neu
berechnet, sondern aus der Mapping-Tabelle gelesen. Neue Presets bekommen
`next_free_id` (wie heute).

**Migration für 99001-99004 (und 99005-99006):** Die existierenden
`sync_targets`-Einträge (kind=orca_preset, cloud_id gesetzt) werden um
`printer_db_id` aus `profiles.printer_db_id` ergänzt. Danach kann
`profiles.printer_db_id` verworfen werden. Die RFID-Tags auf den Spools
bleiben gültig — die IDs ändern sich nicht.

---

## Frage 3 — Orphan-Regel für Printer-DB-Einträge ohne Cloud-Preset

### Heutige Mechanik

`_rebuild_printer_db` (`sync_handlers.py:478-486`):
```python
# Remove orphaned custom entries — 99xxx IDs with no current catalog
# profile.  Deleted profiles would otherwise be re-pushed indefinitely.
current_db_ids = set(session.scalars(
    select(Profile.printer_db_id).where(Profile.printer_db_id.isnot(None))
))
for entry in find_custom_entries(db):
    eid = entry["base"]["id"]
    if eid not in current_db_ids:
        remove_entry(db, eid)
```

**Heute:** Jeder 99xxx-Eintrag, dessen `printer_db_id` nicht mehr im
Profile-Store existiert, wird beim nächsten Push entfernt. Das ist
"clean, destructive" — gelöschte Profile werden vom Drucker gelöscht.

**Nach dem Rework:** Es gibt keinen Profile-Store mehr. Die "Quelle der
Wahrheit" für welche Einträge existieren sollen ist Orca Cloud. Ein
Cloud-Preset, das in OrcaSlicer gelöscht wurde, fehlt im `sync_pull`-
Ergebnis. Beim nächsten Push würde der Eintrag entfernt — auch wenn
ein RFID-Tag auf einer Spool noch darauf verweist.

### Live-Evidenz: Tombstone-Feed

Der `sync_pull` liefert `deletes` — eine Liste von Resource-IDs, die
seit dem letzten Cursor gelöscht wurden. **Live-Daten: 11 Deletes.**

Eine davon (`65c90420-a94e-5e05-b096-e070157391fe`) ist die alte
Cloud-Resource von "eSUN PETG Basic Optimized" (sync_target id=7,
status=drift). Das Preset wurde gelöscht und mit neuer Resource-ID
(`64ebeda3…`) neu erstellt. **Der Tombstone-Feed funktioniert und
unterscheidet zwischen "gelöscht" und "nie existiert".**

### Optionen

**A) Remove-on-push wie heute (clean, destructive)**

Jeder 99xxx-Eintrag, dessen Cloud-Resource nicht im `sync_pull`-Ergebnis
ist, wird beim Push entfernt.

- *Vorteil:* Sauber — der Drucker hat nur Einträge, die in der Cloud
  existieren.
- *Nachteil:* **Datenverlust bei versehentlichem Löschen.** Ein RFID-Tag
  auf einer Spool verweist auf eine ID, die nicht mehr existiert →
  CFS RFID app kann die Spool nicht mehr matchen. Bei wenigen Profilen
  und einem Benutzer ist das Risiko hoch (ein Klick in OrcaSlicer =
  versehentliches Löschen).

**B) Never auto-remove; Orphan-Einträge im UI markiert, Entfernung nur
per expliziter Aktion**

99xxx-Einträge werden nie automatisch entfernt. Einträge ohne
Cloud-Preset werden als "orphan" markiert (UI: Warning-Badge). Der
Benutzer kann sie explizit entfernen ("Remove orphan" Button).

- *Vorteil:* **Kein Datenverlust.** RFID-Tags bleiben gültig, auch wenn
  das Cloud-Preset versehentlich gelöscht wurde. Der Benutzer hat
  Kontrolle.
- *Nachteil:* Orphan-Einträge sammeln sich an, wenn der Benutzer nicht
  aufräumt. Bei wenigen Profilen (aktuell 10) ist das kein Problem.

**C) Remove nur bei Tombstone (proven deletion, nicht nur absence)**

Einträge werden nur entfernt, wenn ihre `cloud_id` im `deletes`-Feed
des `sync_pull` steht. Fehlt ein Preset im `upserts` aber nicht im
`deletes`, wird es nicht entfernt (könnte ein Pull-Fehler sein).

- *Vorteil:* Präzise — nur bewiesene Löschungen führen zum Entfernen.
  Schützt gegen Pull-Fehler und temporäre Cloud-Ausfälle.
- *Nachteil:* Tombstones sind cursor-abhängig. Bei `cursor=0` (full
  resync) sind alle Deletes seit Beginn enthalten — der Feed ist
  kumulativ. Die App muss tracken, welche Tombstones sie bereits
  verarbeitet hat (Cursor-Persistenz, wie heute in `settings.json`).
  **Komplexer als B, ohne zusätzlichen Nutzen für Single-User.**

### Entscheidung benötigt — Frage 3

**A)** Remove-on-push wie heute (destructive).
**B)** Never auto-remove; Orphan-Flag im UI, explizite Entfernung.
**C)** Remove nur bei Tombstone-Feed (proven deletion).

**Empfehlung: B.** Der Benutzer hat wenige Profile (10) und RFID-Tags
auf Spools. Ein versehentliches Löschen in OrcaSlicer sollte nicht zum
Verlust der Printer-DB-Zuordnung führen. Orphan-Einträge im UI zu
markieren ist minimaler Aufwand (ein Status-Badge "orphan — cloud
preset deleted") und gibt dem Benutzer die Kontrolle. Option C ist
technisch präziser, aber die Cursor-Komplexität ist für einen
Single-User mit wenigen Profilen nicht gerechtfertigt.

**Begründung gegen A:** Die heutige Remove-Logik war sicher, weil der
Profile-Store die Quelle der Wahrheit war — ein Profil wurde bewusst
gelöscht (Double-Confirm mit Typed-Token). Im Rework ist OrcaSlicer
die Quelle — ein Klick in OrcaSlicer kann ein Preset löschen, ohne
dass der Benutzer die Auswirkung auf RFID-Tags versteht. Das ist
ein unnötiges Risiko.

---

## What dies / what survives — Handler-Checkliste

Quelle: `register_handlers()` (`sync_handlers.py:1184`), registriert 6 Handler.

| Handler | Funktion | Rework-Entscheidung | Begründung |
|---|---|---|---|
| `push` | `handle_push` — Printer-DB rebuild + SCP push + version bump + reboot | **KEEP + RESHAPE** | Kernfunktion bleibt. Statt aus `profiles`-Tabelle werden Einträge aus Cloud-Presets (live `sync_pull`) gebaut. `_rebuild_printer_db` wird umgeschrieben: Quelle = Cloud-Upsets, nicht `session.scalars(select(Profile))`. Iron Rules (MIN_DB, busy-check, backup, version bump, stock-ID protection) bleiben unverändert. |
| `verify` | `handle_verify` — SSH pull, version floor, hash compare | **KEEP + RESHAPE** | Verify bleibt. Hash-Vergleich muss mit Cloud-gelesenen Werten arbeiten, nicht mit `Profile`-Spalten. `SyncTarget.expected_hash` wird aus Cloud-Preset berechnet. |
| `generate_preset` | `handle_generate_preset` — build preset, push to Orca Cloud | **DELETE** | "Keine Profil-Modifikation in der App" — Presets werden in OrcaSlicer Desktop erstellt. `sync_push` entfällt. Der gesamte Preset-Builder (`fm/presets/builder.py`) und Template-System werden obsolet. |
| `pull` | `handle_pull` — `refresh_preset_drift(adopt=True)` | **DELETE** | Adopt-Logik (Cloud → Profile-Store write-back) entfällt — es gibt keinen Profile-Store mehr. `nozzle_temperature`-Write-back war für den Profile-Store; ohne ihn irrelevant. |
| `import_scan` | `handle_import_scan` — pull printer DB, extract 99xxx entries as proposals | **DELETE** | Migration-Wizard war einmalig für den initialen Import. Nach dem Rework ist Orca Cloud die Quelle — kein Import aus Printer-DB nötig. |
| `github_backup` | `handle_github_backup` — pull all Cloud presets, write to GitHub | **KEEP** (optional) | Backup bleibt unverändert — liest aus Orca Cloud, schreibt nach GitHub. Unabhängig vom Profile-Store. Kann als "nice to have" erhalten bleiben. |

### Was sonst stirbt

| Komponente | Datei | Begründung |
|---|---|---|
| Profile-CRUD | `fm/api/catalog.py`, `fm/api/templates/profile_form.html` | Keine Profile-Erstellung/-Bearbeitung in der App |
| Preset-Builder | `fm/presets/builder.py` | Keine Preset-Generierung |
| Template-System | `fm/presets/template_index.py`, `fm/presets/templates/` | Nur für `generate_preset` |
| Matching | `fm/presets/matching.py` | Nur für Drift-Detection (Profile ↔ Cloud) |
| `refresh_preset_drift` | `fm/service/sync_handlers.py:906` | Adopt-Logik entfällt |
| `_discover_cloud_resource` | `fm/service/sync_handlers.py:606` | Nur für `generate_preset` |
| `_entry_to_proposal` | `fm/service/sync_handlers.py:1104` | Nur für `import_scan` |
| Profile-Delete mit Cloud-Cleanup | `fm/api/ui.py` (delete routes) | Keine Profile zu löschen |
| Orca-Import-Seite | `fm/api/templates/import_orca_tab.html` | Keine Profile aus Cloud zu importieren |

### Was überlebt

| Komponente | Datei | Begründung |
|---|---|---|
| Spool-Inventar | `fm/models.py:Spool`, `fm/api/templates/spools.html` | Unverändert |
| Printer-DB-Codec | `fm/printer/dbcodec.py` | `build_entry`, `load_db`, `save_db`, `find_entry`, `remove_entry`, `next_free_id`, `bump_version` — alle bleiben |
| Push-Pipeline | `fm/printer/pipeline.py` | `run_push`, `verify_printer` — Iron Rules unverändert |
| Transport | `fm/printer/transport.py` | SSH/SCP/WS — unverändert |
| Orca Cloud Client | `fm/orca_cloud.py` | `sync_pull` bleibt (READ-ONLY). `sync_push`, `sync_delete` werden nicht mehr aufgerufen. |
| Settings-Store | `fm/settings_store.py` | Printer-Config + Orca-Token bleiben |
| GitHub Backup | `fm/github_backup.py` | Unverändert |
| Job-Queue | `fm/service/jobs.py` | Unverändert |

---

## Migrations-Notizen

### `profiles`-Tabelle

Die `profiles`-Tabelle wird im Rework **nicht gelöscht**, sondern
**degradiert** zum lokalen Cache (vendor, name, type). Die Spalten
`min_temp`, `max_temp`, `density`, `drying_temp`, `drying_time`, `pa`,
`flow_ratio`, `max_volumetric`, `retraction_length`, `retraction_speed`,
`nozzle_temperature`, `filament_id`, `template_override` werden obsolet —
die Werte kommen live aus Orca Cloud beim Push.

**Migration-Option:** `profiles`-Tabelle beibehalten, aber nur
`id`, `brand` (= vendor), `name`, `type` behalten. Alle anderen Spalten
werden ignoriert (oder in einer Schema-Migration gedroppt). Die
`printer_db_id`-Spalte wird in `sync_targets` migriert (siehe Frage 2).

**Alternative:** `profiles`-Tabelle komplett löschen. Neuer lokaler
Cache: Tabelle `cloud_presets` (cloud_resource_id TEXT PK, vendor TEXT,
name TEXT, type TEXT, printer_db_id TEXT, last_seen_at DATETIME).
Gefüllt bei jedem `sync_pull`. **Sauberer Cut.**

### `sync_targets`-Tabelle

`sync_targets` mit `kind=orca_preset` wird obsolet — es gibt keine
Preset-Drift mehr (Orca Cloud ist die Quelle, nicht ein abgeleitetes Ziel).
`sync_targets` mit `kind=printer_db` bleibt — speichert `expected_hash`
und `cloud_id` für den Push-Verify-Zyklus.

**Migration:** `kind=orca_preset`-Einträge werden gelöscht.
`kind=printer_db`-Einträge bleiben und werden um `printer_db_id` ergänzt
(siehe Frage 2, Option D).

### Die 4 (10) existierenden Cloud-Presets

Alle 10 Cloud-Presets bleiben unverändert in Orca Cloud. Der Rework
liest sie nur, verändert sie nicht. Die Cloud-Resource-IDs bleiben
stabil (UUIDs, von Orca Cloud zugewiesen).

### Existierende Spool-Rows mit Profile-Referenzen

15 Spools in der DB, referenzieren `profile_id` (1-10). Im Rework muss
die Spool-Tabelle auf `cloud_resource_id` (oder eine lokale Cache-ID)
umgestellt werden. Die 15 Spools müssen migriert werden:
`spool.profile_id` → `spool.cloud_resource_id` (über
`sync_targets.cloud_id` Lookup).

**Keine RFID-Tags aktuell gesetzt** — alle `rfid_tag`-Werte sind NULL.
Die RFID-Tag-Programmierung ist manuell und erfolgt außerhalb der App.
Die `printer_db_id`-Stabilität ist dennoch wichtig für zukünftige Tags.

---

## Offene Fragen für den Benutzer

| # | Frage | Empfehlung |
|---|---|---|
| 1 | Vererbte Presets (`inherits` ≠ `""`): Auflösen per Template-Merge (B), überspringen (A), oder Fallback-Werte (C)? | **B** — Template-Merge. Alle 10 Presets pushbar. |
| 2 | `printer_db_id`-Persistenz: Neue Mapping-Tabelle (A), Hash (B), filament_id (C), oder `sync_targets` erweitern (D)? | **D** — `sync_targets` erweitern. Keine neue Tabelle. |
| 3 | Orphan-Regel: Remove-on-push (A), never auto-remove (B), oder Tombstone-only (C)? | **B** — Never auto-remove. RFID-Schutz. |
| 4 | `profiles`-Tabelle: Degradieren zum Cache (nur vendor/name/type) oder komplett löschen und neue `cloud_presets`-Tabelle? | **Neue `cloud_presets`-Tabelle** — sauberer Cut, keine Altlasten. |
| 5 | GitHub-Backup behalten? | **Ja** — unabhängig vom Profile-Store, geringer Aufwand. |
| 6 | Drying- und Colors-Felder: Default 0 bzw. leer akzeptieren? | **Ja** — wie heute, nicht funktionskritisch. |

---

## Anhang: Diagnose-Skript

- **Skript:** `.local/phase0_audit_pull.py` (gitignored, nicht committed)
- **Rohergebnis:** `.local/phase0_audit_pull_result.json` (gitignored)
- **Methode:** `OrcaCloudClient.sync_pull(cursor=0)` — READ-ONLY
- **Datum:** 2026-09-04
- **Token-Rotation:** Beim Pull rotierte der Refresh-Token; neuer Token
  wurde in `.local/settings.json` persistiert (wie `_default_orca_client`
  via `save_orca_token`).
