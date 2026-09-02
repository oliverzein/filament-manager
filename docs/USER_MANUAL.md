# Filament Manager — User Manual

A web app to manage 3D-printing filament profiles, track spool inventory,
and keep your Creality K2 printer database and Orca Cloud filament
presets in sync. OrcaSlicer remains the sole editor of print settings;
the Manager displays them read-only after sync.

## Table of Contents

1. [Getting Started](#getting-started)
2. [Dashboard](#dashboard)
3. [Profiles](#profiles)
   - [Create a New Profile (New Brand/Material)](#create-a-new-profile-new-brandmaterial)
   - [Preset Source (Template Picker)](#preset-source-template-picker)
   - [Edit a Profile](#edit-a-profile)
   - [Sync Print Settings from Orca Cloud](#sync-print-settings-from-orca-cloud)
4. [Spools](#spools)
   - [Add a Spool](#add-a-spool)
   - [Edit a Spool](#edit-a-spool)
   - [Duplicate a Spool](#duplicate-a-spool)
   - [Delete a Spool](#delete-a-spool)
   - [Update Remaining Material](#update-remaining-material)
   - [Filter Spools](#filter-spool)
5. [Sync](#sync)
   - [Push to Printer](#push-to-printer)
   - [Generate Orca Cloud Preset](#generate-orca-cloud-preset)
   - [Drift Status](#drift-status)
   - [Printer Busy — Resolution Options](#printer-busy--resolution-options)
6. [Import from Orca Cloud](#import-from-orca-cloud)
7. [Migration (One-Time)](#migration-one-time)
8. [Settings](#settings)
9. [Complete Workflow Examples](#complete-workflow-examples)

---

## Getting Started

The Filament Manager runs as a web app on your local network. Open it in
your browser at the address provided by your setup (e.g.
`http://192.168.0.31:8404`).

Before using sync features, configure the printer connection under
**Settings**. To sync filament presets, connect your Orca Cloud account
there as well. Nothing needs to be installed on a PC — the app talks to
the printer over SSH and to Orca Cloud directly.

---

## Dashboard

The dashboard (`/`) is the landing page. It shows:

- **Profile count** — total catalog entries.
- **Spools in stock** — total spools across all profiles.
- **Fully synced** — profiles where both the printer DB and Orca Cloud
  preset are in sync.
- **Spools by profile** — which profiles have stock.
- **Sync drift** — per-profile status for printer DB and Orca Cloud
  preset. The printer DB column shows live status from the printer:
  **New** (never pushed), **In Sync** (entry matches catalog),
  **Modified** (entry differs — hover for detail), **Missing** (ID set
  but entry absent), **Stock** (Creality profile).
- **Recent activity** — timeline of sync actions and their outcomes.

Quick actions: click **＋ New profile** to jump to profile creation.

---

## Profiles

Profiles are material definitions (brand, type, temperatures, density,
drying info, colors, notes). They are the catalog entries that spools
reference and that get synced to the printer and Orca Cloud.

### Create a New Profile (New Brand/Material)

Use this workflow when you have a filament from a brand or material not
yet in your catalog.

1. Go to **Profiles** (`/profiles`) or click **＋ New profile** on the
   dashboard.
2. Click **＋ New profile**.
3. Fill in the form:

   | Field | Description | Example |
   |---|---|---|
   | Brand | Manufacturer name | Polymaker |
   | Name | Product name — **must contain the brand name** | PolyLite Galaxy |
   | Type | Material type (PLA, PETG, ABS, TPU, PA, PC, PLA-Silk, Other) | PLA |
   | Preset source | OrcaSlicer base template used when generating a preset (see [below](#preset-source-template-picker)) | Generic PLA defaults |
   | Colors | Comma-separated hex color values | #8f42ff, #442188 |
   | Minimum temp | Safe minimum nozzle temperature (°C) | 200 |
   | Maximum temp | Safe maximum nozzle temperature (°C) | 230 |
   | Density | Material density (g/cm³) | 1.24 |
   | Drying temp | Recommended drying temperature (°C) | 65 |
   | Drying time | Recommended drying time (hours) | 6 |
   | Notes | Optional handling or print notes | Prints best at 210°C |

4. Click **Create profile**.

The profile is saved to the catalog. It does **not** automatically go to
the printer or Orca Cloud — use the [Sync](#sync) page for that.

> **Rule:** The profile name must contain the vendor/brand name (e.g.
> "Sunlu PLA+", not just "PLA+"). This prevents matching conflicts in
> OrcaSlicer.

### Preset Source (Template Picker)

The **Preset source** picker in the profile form chooses which OrcaSlicer
base tuning is used when the app generates a preset for this profile. It
has three parts:

1. **Filter by vendor** — an optional dropdown (default **All vendors**)
   that narrows the template list. It is not saved with the profile.
2. **Template** — the dropdown that matters. The default option is
   **Use Generic \<type\> defaults (recommended)**, which uses the
   generic tuning for the selected material type. Below it, templates are
   grouped by vendor (optgroups). Pick a specific template to override
   the generic defaults.
3. **Template preview** — a live panel showing the base values for the
   selected template: **Nozzle** (°C), **Flow ratio**, **Fan** (min–max
   %), and **Max volumetric** (mm³/s). A note reminds you that profile
   settings may override these base values.

Changing the **Type** field resets the picker to the generic default for
the new type and announces a status message. The brand you enter and the
template's vendor are independent — a house brand may use a Generic
preset, and vice versa.

The chosen template only takes effect the next time you generate a
preset (see [Generate Orca Cloud Preset](#generate-orca-cloud-preset)).

### Edit a Profile

1. Go to **Profiles**.
2. Click the edit (pencil) icon on the profile row.
3. Update any identity or temperature fields.
4. Click **Save changes**.

The print settings section (nozzle temperature, flow ratio, pressure
advance, retraction, fan speeds, plate temps) is **read-only** — it
comes from Orca Cloud via the [Sync from Orca](#sync-print-settings-from-orca-cloud)
button.

### Sync Print Settings from Orca Cloud

After a preset exists in Orca Cloud (either generated by the app or
synced from OrcaSlicer), you can pull the tuned print settings back into
the profile page for read-only display:

1. Open the profile's edit page.
2. In the **Print settings** section, click **Sync from Orca**.
3. The app pulls the preset from Orca Cloud and populates the read-only
   fields. A toast confirms "Synced from Orca Cloud".
4. The section shows 12 print settings:
   - Nozzle temperature
   - Initial layer temperature
   - Pressure advance (shows "Disabled" if PA is off)
   - Flow ratio
   - Max volumetric speed
   - Retraction length / speed
   - Fan min / max speed
   - Cool / engineering / hot plate temp

The section header notes the template the settings came from
(`template: <name>`), or "Generic \<type\> defaults" when no override is
set. A timestamp shows when the settings were last synced.

You can also sync **all** profiles at once: on the **Profiles** page,
click **Sync from Orca** in the page header.

---

## Spools

Spools are physical filament rolls in your inventory. Each spool
references a profile and tracks color, remaining material, and location.

### Add a Spool

> A profile must exist first. If the brand/material is not in the
> catalog, [create a profile](#create-a-new-profile-new-brandmaterial)
> before adding a spool.

1. Go to **Spools** (`/spools`).
2. Click **＋ Add spool**.
3. Fill in the inline form:

   | Field | Description |
   |---|---|
   | Profile | Select from the dropdown (all catalog profiles) |
   | Color | Dropdown of 30 common filament colors with live swatch preview, or use the custom color picker |
   | Location | Where the spool is stored (e.g. "Dry box A", "Shelf 2") |
   | Remaining | Percentage of material remaining (defaults to 100%) |

4. Click **Add spool**.

### Edit a Spool

1. Go to **Spools**.
2. Click the edit (pencil) icon on the spool row. An inline edit form
   expands.
3. Change profile, color, remaining, location, or notes.
4. Click **Save**. The form collapses and the table updates.

### Duplicate a Spool

1. Go to **Spools**.
2. Click the duplicate (⧉) icon on the spool row.
3. A new spool is created with the same profile, color, location, and
   notes. **Remaining is reset to 100%**.

Use this when you buy a new spool of the same filament — it copies all
fields so you only need to adjust the remaining percentage if needed.

### Delete a Spool

1. Go to **Spools**.
2. Click the delete (trash) icon on the spool row.
3. The spool is removed immediately.

### Update Remaining Material

Each spool row has a slider. Drag it to set the remaining percentage.
The value saves automatically via HTMX — no button needed. The
percentage display updates inline.

### Filter Spools

Use the filter bar at the top of the spools page:

- **Profile** — filter by a specific profile.
- **Location** — filter by location text.

Click **Apply filters** to update the list.

---

## Sync

The Sync page (`/sync`) keeps your catalog, printer DB, and Orca Cloud
presets aligned.

### Push to Printer

Pushes all catalog profiles to the Creality K2 printer's filament
database. This writes custom entries (99xxx IDs) to the printer.

1. Go to **Sync**.
2. Click **Push all profiles**.
3. The app rebuilds the printer DB from your catalog and uploads it.
   This involves:
   - Checking if the printer is busy (printing/paused).
   - Backing up the current printer DB.
   - Uploading the new DB.
   - Rebooting the printer to protect against cloud-sync overwrite.
   - Waiting for the printer to come back online.
4. After the push, a **verify** job runs automatically to confirm
   entries survived.

> **Important:** The push reboots your printer. Do not start a push
> while a print is running. If the printer is busy, the job parks as
> awaiting_input and a dialog appears with options (see
> [below](#printer-busy--resolution-options)).

### Generate Orca Cloud Preset

Creates a standalone OrcaSlicer filament preset from a catalog profile
and pushes it directly to Orca Cloud.

1. Go to **Sync**.
2. In the drift table, find the profile you want a preset for.
3. Click the generate preset button (sliders icon) on that row.
4. The app builds the preset from the catalog profile and the selected
   [preset source](#preset-source-template-picker) template, then pushes
   it to Orca Cloud. A toast confirms "Preset pushed to Orca Cloud".

The preset appears in OrcaSlicer once it syncs from Orca Cloud. Open
OrcaSlicer, tune the print settings as needed, then use
[Sync from Orca](#sync-print-settings-from-orca-cloud) on the profile
page to pull the tuned values back for read-only display.

> Changing a profile's preset source has no effect until the next
generate-preset run.

### Drift Status

The drift table on the Sync page shows per-profile status for two sync
targets:

**Printer DB** (live — pulled from the printer on each page load, with
cache-file fallback):

| Status | Meaning |
|---|---|
| New | Profile has never been pushed to the printer (no printer DB ID) |
| In Sync | Printer DB entry matches the catalog profile |
| Modified | Printer DB entry differs from the catalog — hover the badge for a detail tooltip (e.g. "Nozzle temp: 240 → 250") |
| Missing | Profile has a printer DB ID but the entry is absent from the printer |
| Stock | Creality stock filament (RFID-tagged, not managed by the app) |

If the printer is unreachable and no cache file exists, the column
falls back to the last known sync status (pending / in_sync / error).

**Orca Cloud preset** (sync bookkeeping):

| Status | Meaning |
|---|---|
| in_sync | Orca Cloud preset matches the catalog |
| pending | A preset was generated but not yet verified |
| drift | The preset diverged from the catalog |
| error | Verification failed |

### Printer Busy — Resolution Options

If the printer is printing or paused when you trigger a push, the job
parks as `awaiting_input` and a dialog appears on the Sync page with
three options per job:

| Option | What happens |
|---|---|
| **Force reboot** | Reboots the printer immediately. **This kills the active print.** |
| **Wait** | Retries the push — if the printer finishes, the push proceeds; if still busy, the dialog reappears. |
| **No reboot** | Uploads the DB but skips the reboot. **Dangerous** — cloud sync may overwrite within ~12 minutes. Reboot manually later. |

---

## Import from Orca Cloud

The **Orca Cloud Import** page (`/import-orca`) brings standard Orca
Cloud filament presets into your catalog as profiles. This is useful for
filaments that are already on the printer (standard filaments) and don't
need a custom printer DB entry.

1. Go to **Orca Cloud Import** in the navigation.
2. The page shows Orca Cloud presets that are not yet linked to a
   catalog profile (matched presets — those whose filament ID already
   maps to a profile — are hidden). Three states:
   - **No presets found** — your Orca Cloud account is not connected or
     has no presets yet. Connect it under **Settings**, then click
     **Sync now** to pull presets.
   - **All presets matched** — every Orca Cloud preset with a filament
     ID is already linked to a catalog profile.
   - **Select presets to import** — a table lists the unmatched presets.
3. The import table shows for each preset:
   - Checkbox, Name, Brand, Type, Nozzle temp, PA, Flow ratio
   - **Source** — a **User-tuned** badge (no inheritance) or an
     **Inherits** badge (inherited from a system preset)
4. Use the **Only user-tuned presets** filter to show only presets you
   have customized.
5. Select the presets you want to import and click **Import selected**.
6. Profiles are created with:
   - `printer_db_id = None` (no push to printer — standard filaments
     are already there)
   - Nozzle temperature from the preset
   - Temp range derived from the nozzle temperature (±15°C)
7. After import, click **Sync from Orca** on the profile page to
   populate the read-only print settings.

---

## Migration (One-Time)

The Migration page (`/migration`) imports existing custom filament
entries from your printer's database into the catalog. Use this once,
when first deploying the app, to bring in entries you previously created
manually via the skill CLI.

### Step 1 — Scan

1. Go to **Migration**.
2. Click **Start import scan**.
3. The app pulls the printer DB, extracts all custom (99xxx) entries,
   and presents them as proposals.

### Step 2 — Review

A table lists each proposed profile with:

- Printer DB ID, brand, name, type, temperature range, density
- Import checkbox
- Status: entries already in the catalog are marked **exists** and
  pre-checked

Select the entries to import and click **Import selected**.

### Step 3 — Complete

Imported entries become catalog profiles with their `printer_db_id`
preserved (so they stay linked to the printer DB without Orca Cloud
collisions).

Expected first-run entries: 99001 Sunlu PLA+, 99002 eSUN PETG Basic,
99003 CAILAB PLA Silk, 99004 Cailab PLA+ Bio.

---

## Settings

The Settings page (`/settings`) configures the printer connection and
Orca Cloud sync.

### Printer Connection

| Field | Description |
|---|---|
| Printer IP | IP address of your Creality K2 (e.g. 192.168.0.101) |
| SSH user | Usually `root` |
| SSH password | Printer SSH password |
| Remote DB path | Path to the printer's material database (default: `/mnt/UDISK/creality/userdata/box/material_database.json`) |

Click **Test connection** to verify SSH access and that the DB path
exists before saving.

### Orca Cloud

Connect your Orca Cloud account to sync filament presets:

| Field | Description |
|---|---|
| Email | Your Orca Cloud account email |
| Password | Your Orca Cloud password |

Click **Connect**. The app authenticates against Orca Cloud and stores
the token locally. When connected, the section shows
**✓ Connected — user ID: \<email\>** with a **Disconnect** button to
remove the stored credentials.

### Advanced

| Field | Default | Description |
|---|---|---|
| WebSocket port | 9999 | Printer status port (busy check) |
| Version override | 9876543210 | DB version floor (must be ≥ this value) |
| ID range start | 99001 | First custom ID for printer DB entries |

Click **Save settings** to persist. Settings are stored in
`settings.json` and take precedence over environment variables.

---

## Complete Workflow Examples

### Workflow 1: Add a Spool of a New Brand

1. **Create the profile:**
   - Profiles → ＋ New profile
   - Enter brand, name (include brand), type, temps, density, drying
   - Pick a [preset source](#preset-source-template-picker) (or leave the
     generic default)
   - Click **Create profile**

2. **Generate an Orca Cloud preset:**
   - Sync → click the generate preset button (sliders icon) for the new
     profile
   - The starter preset is pushed to Orca Cloud
   - Open OrcaSlicer and let it sync from Orca Cloud — the preset appears

3. **(Optional) Tune and sync back:**
   - Open OrcaSlicer, adjust print settings in the preset
   - Let OrcaSlicer sync the changes to Orca Cloud
   - Profiles → edit the profile → **Sync from Orca**
   - Read-only print settings populate on the profile page

4. **Push to the printer:**
   - Sync → **Push all profiles**
   - Printer reboots; verify runs automatically

5. **Add the spool:**
   - Spools → ＋ Add spool
   - Select the new profile, pick a color, enter location
   - Click **Add spool**

6. **(Manual) Write the NFC tag:**
   - Use your RFID writer to program the spool's NFC tag with the
     custom ID assigned by the app (visible in the drift table or
     profile details).

### Workflow 2: Import an Existing Orca Cloud Preset

1. Settings → connect your Orca Cloud account
2. Orca Cloud Import → the page lists unmatched presets
   (if empty, click **Sync now** to pull presets first)
3. Select the preset(s) from the table → **Import selected**
4. Profiles → edit the imported profile → **Sync from Orca** to
   populate read-only print settings
5. Spools → ＋ Add spool → select the imported profile

### Workflow 3: First-Time Migration from Printer DB

1. Settings → configure printer connection → **Save settings**
2. Migration → **Start import scan**
3. Review proposals → select entries → **Import selected**
4. Profiles → review imported profiles
5. Sync → **Push all profiles** to verify everything is consistent

