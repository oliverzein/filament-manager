#!/usr/bin/env python3
"""remove-printer-entry.py — Remove a custom 99xxx entry from printer DB.

Enforces all Iron Rules:
- Custom 99xxx ID validation (stock protected)
- MIN_DB_ENTRIES floor check
- Remote backup before write with rotation (keep 5)
- Version bump (>= 9876543210, auto-increment)
- Reboot after write + wait for online
- Verification after reboot
- Local cache + SyncTarget cleanup
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fm.config import Config
from fm.errors import FmError
from fm.printer.dbcodec import (
    find_entry,
    load_db,
    remove_entry,
    save_db,
    _is_custom_id,
)
from fm.printer.pipeline import run_push, MIN_DB_ENTRIES
from fm.printer.transport import SshTransport, check_printer_busy
from fm.settings_store import load_merged_config
from fm.service import sync_handlers


def main():
    parser = argparse.ArgumentParser(description="Remove custom 99xxx entry from printer DB")
    parser.add_argument("entry_id", help="Entry ID to remove (e.g. 99008)")
    parser.add_argument("--force-reboot", action="store_true", help="Force reboot even if busy check reports active state")
    parser.add_argument("--no-reboot", action="store_true", help="Do not reboot printer after upload (DANGEROUS)")
    args = parser.parse_args()

    entry_id = str(args.entry_id).strip()
    if not _is_custom_id(entry_id):
        print(f"ERROR: {entry_id} is not a valid custom ID (must be 99xxx). Stock entries are protected.", file=sys.stderr)
        sys.exit(1)

    config = load_merged_config()
    if config is None:
        print("ERROR: Failed to load printer configuration from settings.json or env.", file=sys.stderr)
        sys.exit(1)

    transport = SshTransport(config)

    # 1. Pull live DB directly from printer
    print(f"Connecting to printer at {config.printer_ip}...")
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp_file:
        tmp_path = Path(tmp_file.name)

    try:
        transport.scp_pull(tmp_path)
        db = load_db(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    # 2. Check entry exists
    entry = find_entry(db, entry_id)
    if entry is None:
        print(f"Entry {entry_id} not found in printer DB.", file=sys.stderr)
        sys.exit(1)

    base = entry.get("base", {})
    print(f"Found entry {entry_id}: {base.get('brand')} {base.get('name')} ({base.get('meterialType')})")

    # 3. Remove entry from DB
    remove_entry(db, entry_id)
    total_entries = len(db.get("result", {}).get("list", []))
    print(f"Removed {entry_id}. Remaining entries: {total_entries}")

    if total_entries < MIN_DB_ENTRIES:
        print(f"ERROR: Remaining entries {total_entries} < MIN_DB_ENTRIES ({MIN_DB_ENTRIES}). Aborting push.", file=sys.stderr)
        sys.exit(1)

    # 4. Save to temp file for run_push
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as push_tmp:
        local_push_path = Path(push_tmp.name)

    save_db(local_push_path, db)

    # 5. Push to printer with run_push
    print("Pushing updated DB to printer...")
    try:
        push_res = run_push(
            config,
            local_push_path,
            transport,
            force_reboot=args.force_reboot,
            no_reboot=args.no_reboot,
        )
    finally:
        local_push_path.unlink(missing_ok=True)

    if push_res.get("status") == "busy":
        print("ERROR: Printer is reported as busy. Use --force-reboot if safe to proceed.", file=sys.stderr)
        print(f"Busy details: {push_res.get('job')}", file=sys.stderr)
        sys.exit(2)

    print("Push and reboot completed successfully.")

    # 6. Update local printer-db-cache.json if present
    cache_path = sync_handlers._cache_db_path if sync_handlers._cache_db_path is not None else sync_handlers._resolve_cache_path()
    if cache_path is not None and Path(cache_path).exists():
        try:
            cached_db = load_db(cache_path)
            if find_entry(cached_db, entry_id) is not None:
                remove_entry(cached_db, entry_id)
                save_db(cache_path, cached_db)
                print(f"Updated local cache: {cache_path}")
        except Exception as ex:
            print(f"Warning: Could not update local cache: {ex}", file=sys.stderr)

    # 7. Clean up SyncTarget in local SQLite DB if present
    try:
        import sqlite3
        db_url = os.environ.get("FM_DATABASE_URL", "sqlite:///.local/filaments.db")
        if db_url.startswith("sqlite:///"):
            sqlite_path = Path(db_url[len("sqlite:///"):])
            if sqlite_path.exists():
                conn = sqlite3.connect(str(sqlite_path))
                cur = conn.cursor()
                cur.execute("DELETE FROM sync_targets WHERE printer_db_id = ?", (entry_id,))
                deleted = cur.rowcount
                conn.commit()
                conn.close()
                if deleted:
                    print(f"Removed {deleted} SyncTarget row(s) for {entry_id} from {sqlite_path}")
    except Exception as ex:
        print(f"Warning: Could not clean up SyncTarget in SQLite: {ex}", file=sys.stderr)

    # 8. Verify on live printer
    print("Verifying live printer DB...")
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as verify_tmp:
        verify_path = Path(verify_tmp.name)
    try:
        transport.scp_pull(verify_path)
        fresh_db = load_db(verify_path)
    finally:
        verify_path.unlink(missing_ok=True)

    verified_entry = find_entry(fresh_db, entry_id)
    if verified_entry is not None:
        print(f"VERIFICATION FAILED: Entry {entry_id} is still in live printer DB!", file=sys.stderr)
        sys.exit(1)

    fresh_count = len(fresh_db.get("result", {}).get("list", []))
    fresh_ver = fresh_db.get("result", {}).get("version")
    print(f"VERIFICATION SUCCESS: Entry {entry_id} successfully removed.")
    print(f"Live printer DB version: {fresh_ver}, total entries: {fresh_count}")


if __name__ == "__main__":
    main()
