# Filament Manager — User Manual

A web app to track 3D-printing filament spool inventory and keep your
Creality K2 printer database in sync with Orca Cloud filament presets.
Orca Cloud is the sole source of filament presets — presets are created
and tuned in OrcaSlicer Desktop, synced to Orca Cloud, and the Manager
caches them read-only for spool tracking and printer DB push.

## Table of Contents

1. [Getting Started](#getting-started)
2. [Dashboard](#dashboard)
3. [Spools](#spools)
   - [Add a Spool](#add-a-spool)
   - [Edit a Spool](#edit-a-spool)
   - [Duplicate a Spool](#duplicate-a-spool)
   - [Delete a Spool](#delete-a-spool)
   - [Update Remaining Material](#update-remaining-material)
   - [Filter Spools](#filter-spools)
   - [Orphan Spools](#orphan-spools)
4. [Sync Hub](#sync-hub)
   - [Push to Printer](#push-to-printer)
   - [Orphan Printer DB Entry Removal](#orphan-printer-db-entry-removal)
   - [GitHub Backup](#github-backup)
   - [Printer Busy — Resolution Options](#printer-busy--resolution-options)
5. [Settings](#settings)
6. [Complete Workflow Examples](#complete-workflow-examples)

---

## Getting Started

The Filament Manager runs as a web app on your local network. Open it in
your browser at the address provided by your setup (e.g.
`http://192.168.0.31:8404`).

Before using sync features, configure the printer connection under
**Settings**. To sync filament presets, connect your Orca Cloud account
there as well. Nothing needs to be installed on a PC — the app talks to
the printer over SSH and to Orca Cloud directly.

**Presets are created in OrcaSlicer Desktop.** Open OrcaSlicer, create or
edit a filament preset, and sync it to Orca Cloud via File > Sync Presets.
The Manager pulls the updated presets into its cache automatically.

Only **filament** presets are cached — Orca Cloud also stores print
process and printer presets, but those are ignored by the Manager.

---

## Dashboard

The dashboard (`/`) is the landing page. It shows:

- **Profile count** — total cached preset entries.
- **Spools in stock** — total spools across all presets.
- **Fully synced** — presets where the printer DB is in sync.
- **Spools by profile** — which presets have stock.
- **Sync drift** — per-preset status for printer DB. The printer DB
  column shows live status from the printer: **New** (never pushed),
  **In Sync** (entry matches cache), **Modified** (entry differs — hover
  for detail), **Missing** (ID set but entry absent).
- **Recent activity** — timeline of sync actions and their outcomes.

Quick actions: click **＋ Add spool** to jump to the spools page.

---

## Spools

Spools are physical filament rolls in your inventory. Each spool
references a cached Orca Cloud preset via `cloud_resource_id` and tracks
color, remaining material, and location.

### Add a Spool

> A cached preset must exist first. If the filament is not yet in Orca
> Cloud, create it in OrcaSlicer Desktop and sync to Orca Cloud, then
> let the Manager pull it into the cache.

1. Go to **Spools** (`/spools`).
2. Click **＋ Add spool**.
3. Fill in the inline form:

   | Field | Description |
   |---|---|
   | Profile | Select from the dropdown (all non-deleted cached presets) |
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

- **Profile** — filter by a specific cached preset.
- **Location** — filter by location text.

Click **Apply filters** to update the list.

### Orphan Spools

If a preset is deleted from Orca Cloud (e.g. you removed it in
OrcaSlicer and synced), the Manager tombstones it in the cache. Spools
linked to tombstoned presets show an **Orphan** badge in the inventory
table. The spool retains its `cloud_resource_id` so you don't lose
tracking history. You can reassign an orphan spool to a different cached
preset via the edit form, or delete it if no longer needed.

> **Re-uploaded presets:** if you delete a preset in OrcaSlicer and then
> re-create it, Orca Cloud assigns it a new UUID. The Manager
> automatically matches the new UUID to your existing spools and sync
> state by name, so you don't lose any tracking history or push status.

---

## Sync Hub

The Sync Hub (`/sync`) is the central sync management page. It shows a
**Preset & Status table** with one row per cached Orca Cloud preset,
plus target cards for the printer DB, Orca Cloud, and GitHub backup.

### Preset & Status Table

Each row shows:

- **Preset** — vendor + name (if the name already starts with the vendor,
  only the name is shown to avoid duplication), type (with "inherited"
  badge for inherited presets).
- **Printer DB** — the 99xxx ID if pushed, or "Not pushed".
- **Status** — `in_sync`, `pending`, or `orphan`.
- **Actions** — **Push to Printer** (for non-orphan presets),
  **Remove** (for orphan presets with a printer DB ID).

### Push to Printer

Pushes cached Orca Cloud presets to the Creality K2 printer's filament
database. This writes custom entries (99xxx IDs) to the printer.

1. Go to **Sync**.
2. Click **Push to Printer** — either the bulk button in the Printer DB
   card (pushes all cached presets) or the per-preset button in the
   Preset & Status table (pushes only that preset).
3. An overlay modal appears instantly with a spinner showing
   "Pushing to printer…". The modal shows live progress as the
   operation runs:
   - **Pushing** — the app rebuilds the printer DB from cloud presets,
     uploads it via SSH, and reboots the printer.
   - **Verifying** — after the push, a verify job runs automatically to
     confirm entries survived. The modal shows "Verifying…" while this
     runs.
   - **Push complete** — the modal shows a summary (N presets pushed,
     M skipped) and the Sync Hub refreshes automatically. No manual page
     reload is needed.
4. Click **Close** to dismiss the modal.

> **Important:** The push reboots your printer. Do not start a push
> while a print is running. If the printer is busy, the modal shows
> the three resolution options inside the overlay (see
> [below](#printer-busy--resolution-options)).

### Orphan Printer DB Entry Removal

When a preset is deleted from Orca Cloud, its printer DB entry (if
previously pushed) becomes an orphan — a 99xxx entry on the printer
with no matching cloud preset. Orphan entries persist on the printer
until explicitly removed.

1. In the Sync Hub's Preset & Status table, find the orphan row (status
   badge shows "orphan").
2. Click **Remove**.
3. A confirmation dialog appears. Type the printer DB entry ID (the
   99xxx number) to confirm removal.
4. The entry is removed from the local printer DB cache and the
   associated sync target is deleted. The entry will be physically
   removed from the printer on the next push.

### GitHub Backup

Commits all Orca Cloud presets as JSON files plus an `index.json` to a
configured GitHub repository. This provides an off-cloud backup of your
preset library.

1. Configure a GitHub token and repository under **Settings**.
2. Go to **Sync**.
3. Click **Backup to GitHub** in the GitHub backup card.
4. A modal shows progress. When complete, all presets are committed to
   the repo.

### Printer Busy — Resolution Options

If the printer is printing or paused when you trigger a push, the
operation pauses and the push modal shows three choice buttons inside
the overlay:

| Option | What happens |
|---|---|
| **Force reboot** | Reboots the printer immediately. **This kills the active print.** |
| **Wait** | Retries the push — if the printer finishes, the push proceeds; if still busy, the choices reappear in the modal. |
| **No reboot** | Uploads the DB but skips the reboot. **Dangerous** — cloud sync may overwrite within ~12 minutes. Reboot manually later. |

After choosing an option, the modal continues showing live progress
until the operation completes. The Sync Hub then refreshes
automatically.

> **Fallback:** If you navigate away from the Sync page while a job is
> awaiting input, a page-level busy dialog appears when you return to
> the Sync page, showing the same three options.

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

### GitHub Backup

| Field | Description |
|---|---|
| GitHub token | Personal access token with repo access |
| Repository | `owner/repo` format (e.g. `myuser/filament-backup`) |

Click **Test** to verify the token and repository access before saving.

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

### Workflow 1: Add a Spool for an Existing Preset

1. **Ensure the preset is in Orca Cloud:**
   - Open OrcaSlicer Desktop, create or edit a filament preset
   - Sync to Orca Cloud via File > Sync Presets
   - The Manager pulls it into the cache on the next sync

2. **Add the spool:**
   - Spools → ＋ Add spool
   - Select the preset from the dropdown, pick a color, enter location
   - Click **Add spool**

3. **(Optional) Push to the printer:**
   - Sync → find the preset in the Preset & Status table
   - Click **Push to Printer**
   - Printer reboots; verify runs automatically

4. **(Manual) Write the NFC tag:**
   - Use your RFID writer to program the spool's NFC tag with the
     custom ID assigned by the app (visible in the Sync Hub).

### Workflow 2: First-Time Setup

1. Settings → configure printer connection → **Save settings**
2. Settings → connect your Orca Cloud account
3. Open OrcaSlicer Desktop → create/tune filament presets → sync to
   Orca Cloud via File > Sync Presets
4. Sync → **Push to Printer** to push all cached presets to the printer
5. Spools → ＋ Add spool → select a preset for each physical spool

### Workflow 3: Remove a Deleted Preset's Printer DB Entry

1. In OrcaSlicer Desktop, delete the preset and sync to Orca Cloud
2. Go to **Sync** — the preset shows an **orphan** status badge
3. Click **Remove** on the orphan row
4. Type the 99xxx entry ID to confirm removal
5. Click **Push to Printer** to push the updated DB (without the orphan
   entry) to the printer
