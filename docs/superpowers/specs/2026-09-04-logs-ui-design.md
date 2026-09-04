# Logs Page — UI/UX Design Specification

**Date:** 2026-09-04
**Status:** Design complete — ready for implementation
**Repo:** `/home/oliverzein/Dokumente/Daten/Development/3D Printing/filament-manager`
**Route:** `GET /logs` (full page), `GET /logs/table` (HTMX partial)

---

## 1. Purpose

A dedicated `/logs` page that consolidates two data sources into a single
unified, filterable, searchable timeline:

| Source | Table | What it captures | Status vocabulary |
|--------|-------|------------------|-------------------|
| Background jobs | `jobs` | `push`, `verify`, `github_backup` — queued, running, done, error, awaiting_input | `queued`, `running`, `done`, `error`, `awaiting_input` |
| Direct UI actions | `activity_log` | `printer_db_remove`, `cache_sync`, `verify` (warn), `github_backup` (ok), job-completion echoes | `ok`, `warn`, `error` |

**Deduplication rule:** When a job completes (ok/error/warn), the job
service writes an `ActivityLog` row with `detail.job_id` pointing back to
the job. The unified view **shows the Job row** for job lifecycle states
(queued, running, awaiting_input) and **shows the ActivityLog row** for
the final outcome, linking them via `detail.job_id`. Completed jobs that
have a matching activity_log entry are **excluded** from the job-source
query to avoid duplicates. The activity_log row carries the richer
result/error payload, so it is the canonical display for finished work.

**Action name mapping** (human-readable labels for the Action column):

| Raw kind/action | Display label |
|-----------------|---------------|
| `push` | Push to printer |
| `verify` | Verify printer DB |
| `github_backup` | GitHub backup |
| `printer_db_remove` | Remove printer DB entry |
| `cache_sync` | Orca Cloud cache sync |

---

## 2. Data Model (Backend Query Specification)

The backend assembles a unified list of `LogEntry` dicts. Each entry is
normalized to a common shape regardless of source:

```python
# Pseudocode for the unified query (implementation in ui.py)
{
    "id": "job-12" | "act-34",          # unique row key for HTMX targeting
    "source": "job" | "action",          # which table it came from
    "source_id": 12 | None,              # Job.id for jobs, None for direct actions
    "timestamp": datetime,               # Job.created_at or ActivityLog.ts
    "action": "Push to printer",         # human-readable label
    "action_raw": "push",                # raw kind/action for search matching
    "status": "done" | "running" | ...,  # normalized status
    "status_class": "done",              # CSS class suffix via status_class filter
    "duration": "2.4s" | "running…" | "instant",
    "detail": {...},                     # parsed JSON payload/result for expansion
    "has_detail": True | False,
}
```

**Status normalization** (activity_log outcomes → display status):

| ActivityLog outcome | Display status | CSS class |
|---------------------|---------------|-----------|
| `ok` | `done` | `status-done` |
| `warn` | `awaiting_input` | `status-awaiting_input` |
| `error` | `error` | `status-error` |

**Duration calculation:**
- Job with `finished_at`: `(finished_at - created_at)` formatted as `Xs` or `Xm Ys`
- Job still running/queued: `running…`
- ActivityLog (instant actions): `instant`

**Query strategy (single page load, no pagination in v1 — last 100 entries):**

```sql
-- Jobs that are NOT yet final (queued, running, awaiting_input)
SELECT * FROM jobs WHERE status IN ('queued', 'running', 'awaiting_input')
UNION ALL
-- Jobs that ARE final but have no matching activity_log entry
-- (edge case: job completed before activity_log existed or logging failed)
SELECT * FROM jobs WHERE status IN ('done', 'error')
  AND id NOT IN (SELECT json_extract(detail, '$.job_id') FROM activity_log
                 WHERE json_extract(detail, '$.job_id') IS NOT NULL)
UNION ALL
-- All activity_log entries
SELECT * FROM activity_log
ORDER BY timestamp DESC
LIMIT 100;
```

In practice this is two SQLAlchemy queries (jobs + activity_log) merged
and sorted in Python, since SQLite UNION of differently-shaped tables is
awkward. The merge logic lives in a `_unified_log_entries(db, filters)`
helper in `ui.py`.

---

## 3. Sidebar Navigation

Add a "Logs" nav item in `base.html` between Sync and Settings:

```html
<!-- Insert between the /sync and /settings nav items (line ~33 in base.html) -->
<a href="/logs" class="nav-item {% if request.url.path.startswith('/logs') %}active{% endif %}">
  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h14a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Zm2 4v2h10V8H7Zm0 4v2h10v-2H7Zm0 4v2h7v-2H7Z"/></svg><span>Logs</span>
</a>
```

The SVG is a clipboard/list-with-lines icon — a rounded rectangle with
three horizontal lines, consistent with the existing icon style
(single-path, 24×24 viewBox, `fill: currentColor`).

---

## 4. Page Template: `logs.html`

Full page template extending `base.html`. Follows the same structure as
`sync_hub.html` and `dashboard.html`: page-header with eyebrow + h1 +
description, then a filter bar panel, then the log table panel.

```html
{% extends "base.html" %}
{% block title %}Logs — Filament Manager{% endblock %}
{% block content %}
<section id="logs-page">
  <header class="page-header">
    <div>
      <p class="eyebrow">Audit</p>
      <h1>Logs</h1>
      <p>Unified timeline of background jobs and direct actions — push, verify, backup, cache sync, and printer DB operations.</p>
    </div>
  </header>

  {# ── Filter & search bar ── #}
  <section class="panel logs-filter-panel">
    <form class="logs-filter-bar"
          hx-get="/logs/table"
          hx-target="#logs-table-container"
          hx-swap="innerHTML"
          hx-trigger="change, submit"
          hx-include="this">
      {# Status filter #}
      <label class="filter-select">
        <span>Status</span>
        <select name="status" id="log-status-filter">
          <option value="">All statuses</option>
          <option value="done">Success / Done</option>
          <option value="running">Active / Running</option>
          <option value="awaiting_input">Warning / Awaiting Input</option>
          <option value="error">Error</option>
          <option value="queued">Queued</option>
        </select>
      </label>

      {# Source filter #}
      <label class="filter-select">
        <span>Source</span>
        <select name="source" id="log-source-filter">
          <option value="">All sources</option>
          <option value="job">Background jobs</option>
          <option value="action">Direct actions</option>
        </select>
      </label>

      {# Search input with debounce #}
      <label class="filter-search">
        <span>Search</span>
        <input type="search"
               name="q"
               id="log-search"
               placeholder="Search by ID, action, or error…"
               hx-get="/logs/table"
               hx-target="#logs-table-container"
               hx-swap="innerHTML"
               hx-trigger="input changed delay:300ms"
               hx-include="closest form">
      </label>

      {# Sort direction #}
      <label class="filter-select">
        <span>Sort</span>
        <select name="sort" id="log-sort">
          <option value="desc">Newest first</option>
          <option value="asc">Oldest first</option>
        </select>
      </label>

      {# Auto-refresh toggle + manual refresh #}
      <div class="filter-actions">
        <label class="filter-toggle">
          <input type="checkbox" name="auto_refresh" id="log-auto-refresh"
                 onchange="toggleLogAutoRefresh(this.checked)">
          Auto-refresh
        </label>
        <button type="button"
                class="icon-button"
                title="Refresh now"
                aria-label="Refresh logs"
                hx-get="/logs/table"
                hx-target="#logs-table-container"
                hx-swap="innerHTML"
                hx-include="closest form"
                hx-vals='{"_manual": "1"}'>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17.7 6.3A8 8 0 0 0 4.3 10H1l4 4 4-4H6.3a6 6 0 0 1 10-2.3L18 9.4l1.4-1.4-1.7-1.7ZM19 10l-4 4h2.7a6 6 0 0 1-10 2.3L6 14.6 4.6 16l1.7 1.7A8 8 0 0 0 19.7 14H23l-4-4Z"/></svg>
        </button>
      </div>
    </form>
  </section>

  {# ── Log table (HTMX partial target) ── #}
  <section class="panel logs-table-panel">
    <div class="panel-heading">
      <div><p class="eyebrow">Timeline</p><h2>Activity log</h2></div>
      <span class="result-count" id="log-result-count">{{ entries | length }} entries</span>
    </div>
    <div id="logs-table-container">
      {% include "_logs_table.html" %}
    </div>
  </section>
</section>

<script>
// Auto-refresh: when toggled on, poll the table partial every 5s.
// Reuses the existing filter form values via hx-include.
var _logRefreshTimer = null;
function toggleLogAutoRefresh(on) {
  if (_logRefreshTimer) { clearInterval(_logRefreshTimer); _logRefreshTimer = null; }
  if (on) {
    _logRefreshTimer = setInterval(function() {
      var btn = document.querySelector('.filter-actions .icon-button');
      if (btn) btn.click();  // triggers the HTMX refresh
    }, 5000);
  }
}
// Clean up on page unload
window.addEventListener('beforeunload', function() {
  if (_logRefreshTimer) clearInterval(_logRefreshTimer);
});
</script>
{% endblock %}
```

### HTMX interaction notes

- The filter `<form>` itself has `hx-trigger="change, submit"` — changing
  any `<select>` fires a table refresh immediately. The search input has
  its own `hx-trigger="input changed delay:300ms"` for debounced search.
- Both the form and the search input use `hx-include="this"` / `hx-include="closest form"`
  to serialize all filter values on every request, so the backend always
  receives the full filter state.
- The manual refresh button also includes the form, preserving active
  filters.
- Auto-refresh uses a JS `setInterval` that programmatically clicks the
  refresh button, reusing the same HTMX request path. This avoids a
  second `hx-trigger="every 5s"` element that would conflict with filter
  changes.

---

## 5. Partial Template: `_logs_table.html`

This partial is rendered server-side and swapped into `#logs-table-container`
by every HTMX request. It receives `entries` (list of normalized LogEntry
dicts) from the template context.

```html
{# Partial: unified log table. Included by logs.html and rendered by
   GET /logs/table for HTMX filter/search/sort/refresh requests. #}
{% if entries %}
<div class="table-wrap">
  <table class="logs-table">
    <thead>
      <tr>
        <th class="col-time">Time</th>
        <th class="col-source">Source</th>
        <th class="col-action">Action</th>
        <th class="col-duration">Duration</th>
        <th class="col-status">Status</th>
        <th class="col-toggle" aria-label="Expand"></th>
      </tr>
    </thead>
    <tbody>
      {% for e in entries %}
      <tr class="log-row log-row-{{ e.source }} status-{{ e.status_class }}"
          id="log-row-{{ e.id }}"
          onclick="toggleLogDetail('{{ e.id }}')"
          role="button"
          tabindex="0"
          aria-expanded="false"
          aria-label="Toggle details for {{ e.action }}">
        <td class="col-time">
          <span class="cell-main">{{ e.timestamp_fmt }}</span>
          <span class="cell-sub">{{ e.timestamp_rel }}</span>
        </td>
        <td class="col-source">
          {% if e.source == 'job' %}
            <span class="badge badge-purple">Job #{{ e.source_id }}</span>
          {% else %}
            <span class="badge badge-muted">Action</span>
          {% endif %}
        </td>
        <td class="col-action">
          <span class="cell-main">{{ e.action }}</span>
        </td>
        <td class="col-duration">
          {% if e.duration == 'running' %}
            <span class="duration-running">
              <span class="spinner spinner-sm"></span> running…
            </span>
          {% elif e.duration == 'queued' %}
            <span class="muted">queued</span>
          {% else %}
            <span class="cell-sub">{{ e.duration }}</span>
          {% endif %}
        </td>
        <td class="col-status">
          <span class="status-badge status-{{ e.status_class }}">{{ e.status_label }}</span>
        </td>
        <td class="col-toggle">
          {% if e.has_detail %}
          <span class="detail-chevron" aria-hidden="true">▸</span>
          {% else %}
          <span class="muted">—</span>
          {% endif %}
        </td>
      </tr>
      {% if e.has_detail %}
      <tr class="log-detail-row hidden" id="log-detail-{{ e.id }}">
        <td colspan="6">
          <div class="log-detail-content">
            <div class="log-detail-meta">
              {% if e.source == 'job' %}
              <span class="log-meta-item"><strong>Job ID:</strong> #{{ e.source_id }}</span>
              <span class="log-meta-item"><strong>Kind:</strong> {{ e.action_raw }}</span>
              <span class="log-meta-item"><strong>Created:</strong> {{ e.timestamp_fmt }}</span>
              {% if e.finished_at_fmt %}
              <span class="log-meta-item"><strong>Finished:</strong> {{ e.finished_at_fmt }}</span>
              {% endif %}
              {% else %}
              <span class="log-meta-item"><strong>Log ID:</strong> #{{ e.source_id }}</span>
              <span class="log-meta-item"><strong>Action:</strong> {{ e.action_raw }}</span>
              <span class="log-meta-item"><strong>Recorded:</strong> {{ e.timestamp_fmt }}</span>
              {% endif %}
            </div>
            {% if e.error_msg %}
            <div class="log-detail-error">
              <strong>Error:</strong>
              <code>{{ e.error_msg }}</code>
            </div>
            {% endif %}
            {% if e.busy_detail %}
            <div class="log-detail-busy">
              <strong>Printer busy:</strong>
              <code>{{ e.busy_detail }}</code>
            </div>
            {% endif %}
            <div class="log-detail-json">
              <strong>Raw payload / result:</strong>
              <pre><code>{{ e.detail_json }}</code></pre>
            </div>
          </div>
        </td>
      </tr>
      {% endif %}
      {% endfor %}
    </tbody>
  </table>
</div>
<div class="table-footer">
  <span>Showing <b>{{ entries | length }}</b> of <b>{{ total_count }}</b> entries</span>
  {% if truncated %}
  <span class="muted">· results truncated to last 100</span>
  {% endif %}
</div>
{% else %}
<div class="empty-state">
  <span>◇</span>
  <h3>{% if has_filters %}No matching log entries{% else %}No log entries yet{% endif %}</h3>
  <p>{% if has_filters %}Try adjusting your filters or search query.{% else %}Background jobs and direct actions will appear here as they occur.{% endif %}</p>
  {% if has_filters %}
  <button class="button button-secondary"
          onclick="document.getElementById('logs-page').querySelector('form').reset(); htmx.trigger(document.getElementById('logs-page').querySelector('form'), 'submit')">
    Clear filters
  </button>
  {% endif %}
</div>
{% endif %}
```

### Expandable row interaction

The detail row uses the same `hidden` class toggle pattern as the spool
edit row (`.spool-edit-row.hidden` in `spools.html`). A small JS function
toggles visibility:

```javascript
function toggleLogDetail(id) {
  var row = document.getElementById('log-row-' + id);
  var detail = document.getElementById('log-detail-' + id);
  if (!detail) return;
  var isHidden = detail.classList.toggle('hidden');
  row.setAttribute('aria-expanded', !isHidden);
  var chevron = row.querySelector('.detail-chevron');
  if (chevron) chevron.textContent = isHidden ? '▸' : '▾';
}
// Keyboard support: Enter/Space toggles detail
document.addEventListener('keydown', function(e) {
  if ((e.key === 'Enter' || e.key === ' ') && document.activeElement && document.activeElement.classList.contains('log-row')) {
    e.preventDefault();
    document.activeElement.click();
  }
});
```

This script lives in `_logs_table.html` inside a `<script>` tag at the
bottom (HTMX swaps it with the partial, so it re-binds on each refresh).
Alternatively, it can live in `logs.html` and use event delegation on
`#logs-table-container` to survive partial swaps without re-binding.

**Preferred approach:** event delegation in `logs.html` (survives swaps):

```javascript
// In logs.html <script> block — event delegation survives HTMX swaps
document.getElementById('logs-table-container').addEventListener('click', function(e) {
  var row = e.target.closest('.log-row');
  if (!row) return;
  var id = row.id.replace('log-row-', '');
  var detail = document.getElementById('log-detail-' + id);
  if (!detail) return;
  var isHidden = detail.classList.toggle('hidden');
  row.setAttribute('aria-expanded', !isHidden);
  var chevron = row.querySelector('.detail-chevron');
  if (chevron) chevron.textContent = isHidden ? '▸' : '▾';
});
document.getElementById('logs-table-container').addEventListener('keydown', function(e) {
  if ((e.key === 'Enter' || e.key === ' ') && e.target.classList.contains('log-row')) {
    e.preventDefault();
    e.target.click();
  }
});
```

---

## 6. CSS Additions

All new CSS is appended to `fm/api/static/app.css`. It reuses existing
variables (`--surface`, `--border`, `--text-muted`, `--accent`, etc.) and
component classes (`.panel`, `.status-badge`, `.badge`, `.empty-state`,
`.table-wrap`, `.table-footer`, `.filter-bar`, `.filter-toggle`,
`.spinner-sm`).

```css
/* ──────────────────────────────────────────────
   Logs page — /logs
   ────────────────────────────────────────────── */

/* Filter bar: extends .filter-bar with a search input and action group */
.logs-filter-panel { margin-bottom: 24px; }
.logs-filter-bar {
  display: flex;
  align-items: flex-end;
  flex-wrap: wrap;
  gap: 16px;
  padding: 20px 28px;
}
.logs-filter-bar .filter-select,
.logs-filter-bar .filter-search { min-width: 160px; }
.logs-filter-bar .filter-search { flex: 1 1 240px; min-width: 200px; }
.logs-filter-bar .filter-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-left: auto;
}
.logs-filter-bar label > span {
  display: block;
  margin-bottom: 7px;
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: 600;
}

/* Log table */
.logs-table { min-width: 720px; }
.logs-table .col-time { white-space: nowrap; width: 180px; }
.logs-table .col-source { width: 100px; }
.logs-table .col-action { min-width: 160px; }
.logs-table .col-duration { width: 100px; white-space: nowrap; }
.logs-table .col-status { width: 140px; }
.logs-table .col-toggle { width: 40px; text-align: center; }

/* Log row: clickable, cursor pointer, focus ring for keyboard */
.log-row { cursor: pointer; }
.log-row:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.log-row:hover { background: rgba(174, 185, 225, .04); }

/* Running rows: subtle left-border accent like a live indicator */
.log-row.status-running { border-left: 3px solid var(--blue); }
.log-row.status-queued { border-left: 3px solid var(--text-muted); }
.log-row.status-error { border-left: 3px solid var(--red); }
.log-row.status-awaiting_input { border-left: 3px solid var(--amber); }

/* Duration cell */
.duration-running {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--blue);
  font-size: 11px;
  font-weight: 600;
}

/* Detail chevron */
.detail-chevron {
  color: var(--text-muted);
  font-size: 14px;
  transition: transform .15s ease;
}
.log-row[aria-expanded="true"] .detail-chevron { transform: rotate(90deg); }

/* Detail row (expanded content) */
.log-detail-row td {
  padding: 0 20px 20px;
  background: var(--surface-alt);
  border-bottom: 1px solid var(--border);
}
.log-detail-content {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px 8px;
}
.log-detail-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 20px;
  font-size: 11px;
  color: var(--text-secondary);
}
.log-detail-meta .log-meta-item strong {
  color: var(--text-muted);
  font-weight: 600;
  margin-right: 4px;
}
.log-detail-error {
  padding: 12px 16px;
  border: 1px solid rgba(255, 90, 101, .25);
  border-radius: var(--radius-input);
  background: rgba(255, 90, 101, .06);
  color: #ff9ba2;
  font-size: 12px;
}
.log-detail-error code,
.log-detail-busy code {
  overflow-wrap: anywhere;
  font-family: monospace;
  font-size: 11px;
}
.log-detail-busy {
  padding: 12px 16px;
  border: 1px solid rgba(253, 181, 42, .25);
  border-radius: var(--radius-input);
  background: rgba(253, 181, 42, .05);
  color: #ffd070;
  font-size: 12px;
}
.log-detail-json strong {
  display: block;
  margin-bottom: 8px;
  color: var(--text-muted);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: .08em;
  text-transform: uppercase;
}
.log-detail-json pre {
  margin: 0;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius-input);
  background: var(--bg);
  overflow-x: auto;
  font-family: monospace;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}
.log-detail-json code { color: #c8d4f5; }

/* Responsive: stack filter bar on narrow screens */
@media (max-width: 760px) {
  .logs-filter-bar {
    flex-direction: column;
    align-items: stretch;
    padding: 16px 20px;
  }
  .logs-filter-bar .filter-select,
  .logs-filter-bar .filter-search { min-width: 0; width: 100%; }
  .logs-filter-bar .filter-actions {
    margin-left: 0;
    justify-content: space-between;
  }
  .logs-table { min-width: 0; }
  .logs-table .col-time { width: auto; }
  .logs-table .col-source,
  .logs-table .col-duration { display: none; }  /* hide secondary cols on mobile */
  .log-detail-meta { flex-direction: column; gap: 6px; }
}
```

---

## 7. Backend Route Specification

Two new routes in `fm/api/ui.py`:

### `GET /logs` — full page

```python
@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, db: DbSession) -> HTMLResponse:
    """Render the unified logs page with default filters (all, newest first)."""
    entries, total_count, truncated = _unified_log_entries(db)
    return _templates(request).TemplateResponse(
        request, "logs.html",
        {"entries": entries, "total_count": total_count, "truncated": truncated,
         "has_filters": False},
    )
```

### `GET /logs/table` — HTMX partial

Accepts query params: `status`, `source`, `q`, `sort` (`desc`|`asc`).

```python
@router.get("/logs/table", response_class=HTMLResponse)
def logs_table_fragment(
    request: Request, db: DbSession,
    status: str = "", source: str = "", q: str = "", sort: str = "desc",
) -> HTMLResponse:
    """HTMX partial: filtered/searched/sorted log table."""
    entries, total_count, truncated = _unified_log_entries(
        db, status=status, source=source, q=q, sort=sort
    )
    has_filters = bool(status or source or q)
    return _templates(request).TemplateResponse(
        request, "_logs_table.html",
        {"entries": entries, "total_count": total_count, "truncated": truncated,
         "has_filters": has_filters},
    )
```

### `_unified_log_entries` helper

```python
# Action label mapping
_LOG_ACTION_LABELS: dict[str, str] = {
    "push": "Push to printer",
    "verify": "Verify printer DB",
    "github_backup": "GitHub backup",
    "printer_db_remove": "Remove printer DB entry",
    "cache_sync": "Orca Cloud cache sync",
}

# ActivityLog outcome → (display status, status_class, status_label)
_ACT_OUTCOME_MAP: dict[str, tuple[str, str, str]] = {
    "ok": ("done", "done", "done"),
    "warn": ("awaiting_input", "awaiting_input", "awaiting input"),
    "error": ("error", "error", "error"),
}

def _unified_log_entries(
    db: Session, *, status: str = "", source: str = "",
    q: str = "", sort: str = "desc",
) -> tuple[list[dict], int, bool]:
    """Build the unified log entry list from jobs + activity_log.

    Returns (entries, total_count, truncated).
    """
    LIMIT = 100

    # Query jobs: non-final + final-without-matching-activity
    job_ids_with_activity = set()
    for row in db.scalars(
        select(ActivityLog.detail).where(
            ActivityLog.action.in_(("push", "verify", "github_backup"))
        )
    ):
        try:
            d = json.loads(row) if isinstance(row, str) else row
            jid = d.get("job_id")
            if jid is not None:
                job_ids_with_activity.add(jid)
        except (json.JSONDecodeError, TypeError):
            pass

    job_query = select(Job).where(
        or_(
            Job.status.in_(("queued", "running", "awaiting_input")),
            and_(
                Job.status.in_(("done", "error")),
                ~Job.id.in_(job_ids_with_activity) if job_ids_with_activity else true(),
            ),
        )
    )
    jobs = list(db.scalars(job_query))

    # Query activity_log
    act_query = select(ActivityLog)
    activities = list(db.scalars(act_query))

    # Normalize both into unified entries
    entries: list[dict] = []
    for job in jobs:
        entries.append(_normalize_job_entry(job))
    for act in activities:
        entries.append(_normalize_activity_entry(act))

    # Deduplicate: if a job and an activity share the same job_id,
    # keep only the activity (it has the richer result).
    seen_job_ids: set[int] = set()
    deduped: list[dict] = []
    for e in entries:
        if e["source"] == "job" and e.get("linked_activity"):
            # Skip — the activity_log entry will represent this
            continue
        if e["source"] == "action" and e.get("source_job_id") is not None:
            seen_job_ids.add(e["source_job_id"])
        deduped.append(e)
    # Remove job entries whose id is in seen_job_ids (already covered by activity)
    entries = [e for e in deduped if not (e["source"] == "job" and e.get("source_id") in seen_job_ids)]

    # Filter
    if status:
        entries = [e for e in entries if e["status"] == status]
    if source:
        entries = [e for e in entries if e["source"] == source]
    if q:
        ql = q.lower()
        entries = [e for e in entries
                   if ql in str(e.get("id", "")).lower()
                   or ql in e.get("action_raw", "").lower()
                   or ql in e.get("action", "").lower()
                   or ql in str(e.get("error_msg", "")).lower()
                   or ql in str(e.get("detail_json", "")).lower()]

    # Sort
    entries.sort(key=lambda e: e["timestamp"], reverse=(sort != "asc"))

    total_count = len(entries)
    truncated = total_count > LIMIT
    entries = entries[:LIMIT]

    return entries, total_count, truncated
```

### Entry normalization helpers

```python
def _normalize_job_entry(job: Job) -> dict:
    detail = json.loads(job.payload) if job.payload else {}
    result = json.loads(job.result) if job.result else None
    error_msg = None
    busy_detail = None
    if result:
        error_msg = result.get("error")
        busy_detail = result.get("busy")
    has_detail = bool(detail or result or error_msg or busy_detail)
    return {
        "id": f"job-{job.id}",
        "source": "job",
        "source_id": job.id,
        "timestamp": job.created_at,
        "timestamp_fmt": _fmt_local(job.created_at),
        "timestamp_rel": _relative_time(job.created_at),
        "action": _LOG_ACTION_LABELS.get(job.kind, job.kind.replace("_", " ").title()),
        "action_raw": job.kind,
        "status": job.status,
        "status_class": status_class(job.status),
        "status_label": job.status.replace("_", " "),
        "duration": _compute_duration(job),
        "detail": {**(detail or {}), **({"result": result} if result else {})},
        "detail_json": json.dumps(
            {"payload": detail, "result": result}, indent=2, default=str
        ) if has_detail else "",
        "error_msg": error_msg,
        "busy_detail": busy_detail,
        "has_detail": has_detail,
        "finished_at_fmt": _fmt_local(job.finished_at) if job.finished_at else None,
    }

def _normalize_activity_entry(act: ActivityLog) -> dict:
    detail = json.loads(act.detail) if act.detail else {}
    job_id = detail.get("job_id")
    error_msg = detail.get("error") or detail.get("result", {}).get("error") if isinstance(detail.get("result"), dict) else detail.get("error")
    busy_detail = detail.get("busy")
    outcome_status, outcome_class, outcome_label = _ACT_OUTCOME_MAP.get(
        act.outcome, (act.outcome, act.outcome, act.outcome)
    )
    has_detail = bool(detail)
    return {
        "id": f"act-{act.id}",
        "source": "action",
        "source_id": act.id,
        "source_job_id": job_id,  # for dedup linking
        "timestamp": act.ts,
        "timestamp_fmt": _fmt_local(act.ts),
        "timestamp_rel": _relative_time(act.ts),
        "action": _LOG_ACTION_LABELS.get(act.action, act.action.replace("_", " ").title()),
        "action_raw": act.action,
        "status": outcome_status,
        "status_class": outcome_class,
        "status_label": outcome_label,
        "duration": "instant",
        "detail": detail,
        "detail_json": json.dumps(detail, indent=2, default=str) if has_detail else "",
        "error_msg": error_msg,
        "busy_detail": busy_detail,
        "has_detail": has_detail,
        "finished_at_fmt": None,
    }

def _relative_time(dt: datetime) -> str:
    """Human-readable relative time: '3m ago', '2h ago', 'just now'."""
    if not dt:
        return ""
    delta = _now() - dt
    secs = int(delta.total_seconds())
    if secs < 60: return "just now"
    if secs < 3600: return f"{secs // 60}m ago"
    if secs < 86400: return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"

def _compute_duration(job: Job) -> str:
    if job.status in ("queued", "running", "awaiting_input"):
        return job.status
    if not job.finished_at:
        return "—"
    delta = (job.finished_at - job.created_at).total_seconds()
    if delta < 1: return "instant"
    if delta < 60: return f"{delta:.1f}s"
    mins = int(delta // 60)
    secs = int(delta % 60)
    return f"{mins}m {secs}s"
```

---

## 8. Interaction Specification

### 8.1 Filter behavior

| Control | Trigger | HTMX target | Behavior |
|---------|---------|-------------|----------|
| Status `<select>` | `change` | `#logs-table-container` | Immediate table refresh with status filter |
| Source `<select>` | `change` | `#logs-table-container` | Immediate table refresh with source filter |
| Search `<input>` | `input changed delay:300ms` | `#logs-table-container` | Debounced search across ID, action, error, detail JSON |
| Sort `<select>` | `change` | `#logs-table-container` | Immediate re-sort |
| Auto-refresh checkbox | JS `setInterval` 5s | `#logs-table-container` | Polls current filter state every 5s while checked |
| Manual refresh button | `click` | `#logs-table-container` | One-shot refresh preserving filters |

All controls serialize the **entire form** via `hx-include`, so the
backend always receives the complete filter state. No filter is lost when
another changes.

### 8.2 Row expansion

- Clicking anywhere on a log row toggles its detail row.
- Keyboard: Tab to focus a row, Enter/Space to expand.
- The chevron (`▸` → `▾`) rotates 90° via CSS `transform` when expanded.
- `aria-expanded` attribute updates for screen reader accessibility.
- Only rows with `has_detail = True` have a detail row; others show `—`
  in the toggle column and are not clickable (cursor reverts to default).

### 8.3 Detail row content

The detail row spans all 6 columns (`colspan="6"`) and shows:

1. **Metadata strip** — Job ID / Log ID, kind/action, created/finished
   timestamps. Uses flex-wrap for responsive layout.
2. **Error block** (if `error_msg`) — Red-tinted alert box with the error
   message in monospace. Uses the same red palette as `.alert-danger`.
3. **Busy block** (if `busy_detail`) — Amber-tinted box showing printer
   busy details. Uses the same amber palette as `.alert-warning`.
4. **Raw JSON** — Pretty-printed JSON of the payload and/or result in a
   `<pre><code>` block with dark background (`var(--bg)`), monospace font,
   horizontal scroll, and word-break for long values.

### 8.4 Status badge mapping

| Entry status | CSS class | Color | Dot |
|-------------|-----------|-------|-----|
| `done` | `status-done` | Green `#48d991` | ● |
| `running` | `status-running` | Blue `#77c3ff` | ● |
| `queued` | `status-queued` | Amber `#ffd070` | ● |
| `awaiting_input` | `status-awaiting_input` | Amber `#ffd070` | ● |
| `error` | `status-error` | Red `#ff8d95` | ● |

All use the existing `.status-badge` base class with `::before` dot. No
new badge classes needed — the existing CSS already covers these status
values.

### 8.5 Source badge

| Source | Badge class | Content |
|--------|-------------|---------|
| `job` | `badge-purple` | `Job #12` |
| `action` | `badge-muted` | `Action` |

Both classes already exist in `app.css`.

### 8.6 Empty states

Two variants, both using the existing `.empty-state` class:

1. **No logs at all** (first visit, empty database):
   - Icon: `◇`
   - Heading: "No log entries yet"
   - Body: "Background jobs and direct actions will appear here as they occur."
   - No action button.

2. **No filter matches** (filters/search returned zero results):
   - Icon: `◇`
   - Heading: "No matching log entries"
   - Body: "Try adjusting your filters or search query."
   - Action: "Clear filters" button that resets the form and triggers a
     refresh.

### 8.7 Auto-refresh

- Toggle is a checkbox labeled "Auto-refresh" using the existing
  `.filter-toggle` class (same as the drift-show filter in sync hub).
- When ON: `setInterval` fires every 5 seconds, programmatically clicking
  the manual refresh icon button, which triggers the HTMX request with
  current form values.
- When OFF: interval is cleared.
- The interval is also cleared on `beforeunload` to prevent leaks.
- Auto-refresh does NOT expand rows — expanded state is lost on refresh
  (acceptable for a log view; users expand to inspect, then it collapses
  on next poll). If preservation is desired in a future iteration, the
  expanded row IDs can be tracked and re-expanded client-side after swap
  via `htmx:afterSwap`.

---

## 9. Responsive Behavior

| Breakpoint | Behavior |
|-----------|----------|
| > 760px | Full table with all 6 columns. Filter bar is a horizontal flex row. |
| ≤ 760px | Filter bar stacks vertically (full-width controls). Table hides Source and Duration columns (secondary info). Detail metadata wraps vertically. `th`/`td` padding reduces to 15px (existing rule). |
| ≤ 480px | No additional changes beyond the 760px rules. Button-group stacking not needed (no button groups in log rows). |

---

## 10. Accessibility

- Log rows are `role="button"` with `tabindex="0"` for keyboard focus.
- `aria-expanded` reflects detail row visibility.
- `aria-label` on each row: "Toggle details for {action}".
- Detail toggle column has `aria-label="Expand"` on the header.
- Search input has a visible label (`<span>Search</span>`) per existing
  form convention.
- Manual refresh button has `aria-label="Refresh logs"`.
- Color is never the sole status indicator — every status badge has a
  text label, and error/running rows have a left-border accent in
  addition to the badge color.
- `prefers-reduced-motion`: existing global rule disables transitions;
  the chevron rotation and spinner animations are covered.

---

## 11. Files to Create / Modify

| File | Action | Description |
|------|--------|-------------|
| `fm/api/templates/logs.html` | **Create** | Full page template (§4) |
| `fm/api/templates/_logs_table.html` | **Create** | HTMX partial template (§5) |
| `fm/api/templates/base.html` | **Modify** | Add Logs nav item between Sync and Settings (§3) |
| `fm/api/static/app.css` | **Modify** | Append logs-page CSS (§6) |
| `fm/api/ui.py` | **Modify** | Add `GET /logs` and `GET /logs/table` routes + `_unified_log_entries` helper + normalization helpers (§7) |

**No new models, no migrations, no new dependencies.** The feature reads
exclusively from existing `jobs` and `activity_log` tables.

---

## 12. Implementation Sequence

1. **CSS** — Append the §6 block to `app.css`. No restart needed.
2. **Templates** — Create `logs.html` and `_logs_table.html`. No restart
   needed.
3. **base.html** — Insert the Logs nav item. No restart needed.
4. **Routes** — Add the two routes and helpers to `ui.py`. **Restart dev
   server** (per AGENTS.md Rule 4: "After changing UI routes, restart the
   dev server").
5. **Verify** — Navigate to `/logs`, test filters, search, sort,
   auto-refresh, row expansion, and empty states against the running app.

---

## 13. Design Decisions Summary

1. **Unified query, not a DB view** — SQLite makes a cross-table UNION
   awkward due to different schemas. Two simple queries merged in Python
   is clearer, testable, and keeps the dedup logic explicit.

2. **ActivityLog is canonical for completed jobs** — When a job finishes,
   the service writes an ActivityLog with `detail.job_id`. The unified
   view shows the Job row only for in-flight states (queued, running,
   awaiting_input) and the ActivityLog row for the final outcome. This
   avoids duplicates and gives the user the richer result payload.

3. **Event delegation over per-row handlers** — The expand/collapse JS
   uses event delegation on `#logs-table-container` so it survives HTMX
   partial swaps without re-binding. This is more robust than embedding
   `<script>` in the partial.

4. **Auto-refresh via JS interval, not `hx-trigger="every 5s"`** — A
   second `hx-trigger="every 5s"` element would fire independently of
   filter changes, potentially showing stale filter state. The JS
   interval clicks the manual refresh button, which always serializes
   the current form. This is simpler and correct.

5. **No pagination in v1** — The last 100 entries covers typical usage.
   The table footer shows "Showing N of M entries" with a truncation
   note. Pagination can be added later if needed via offset/limit query
   params and a "Load more" button.

6. **Left-border accent for status** — A 3px colored left border on
   running/error/queued/awaiting rows provides at-a-glance status
   scanning without relying solely on the badge. This is a subtle
   enhancement over the existing table pattern.

7. **Monospace JSON in detail** — The raw payload/result is shown as
   pretty-printed JSON in a `<pre><code>` block with `word-break:
   break-word` and horizontal scroll. This handles long error traces and
   nested result dicts gracefully.

8. **Reuses all existing CSS primitives** — No new design tokens. The
   spec uses existing `.panel`, `.panel-heading`, `.status-badge`,
   `.badge`, `.empty-state`, `.table-wrap`, `.table-footer`,
   `.filter-toggle`, `.spinner-sm`, `.icon-button`, `.button`,
   `.button-secondary`, `.eyebrow`, `.page-header`, `.result-count`,
   `.cell-main`, `.cell-sub`, `.muted` classes. New CSS is scoped to
   `.logs-*` selectors only.
