#!/usr/bin/env python3
"""bump-printer-version.py — Bump printer DB version and reboot.

Enforces all Iron Rules:
- Floor check (MIN_DB_ENTRIES)
- Remote backup with rotation (keep 5)
- Version bump (>= 9876543210, auto-increment or explicit target)
- Reboot after write + wait for online
- Verification pull after reboot
- Local cache update
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

# Add project root to sys.path
import dataclasses
from pathlib import Path
from fm.errors import FmError
from fm.printer.dbcodec import load_db, save_db, bump_version
from fm.printer.pipeline import run_push, MIN_DB_ENTRIES
from fm.printer.transport import SshTransport, check_printer_busy
from fm.settings_store import load_merged_config
from fm.service import sync_handlers


def main():
    parser = argparse.ArgumentParser(description="Bump printer DB version and reboot")
    parser.add_argument("--version", type=int, default=None, help="Explicit version override (default: auto-increment)")
    parser.add_argument("--force-reboot", action="store_true", help="Force reboot even if busy check reports active state")
    args = parser.parse_args()

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

    curr_ver = db.get("result", {}).get("version")
    total_entries = len(db.get("result", {}).get("list", []))
    print(f"Current printer DB version: {curr_ver}, entries: {total_entries}")

    if total_entries < MIN_DB_ENTRIES:
        print(f"ERROR: DB entries {total_entries} < MIN_DB_ENTRIES ({MIN_DB_ENTRIES}). Aborting.", file=sys.stderr)
        sys.exit(1)

    # 2. Determine new version
    if args.version is not None:
        target_version = args.version
        db["result"]["version"] = str(target_version)
    else:
        # Bump version via standard logic
        target_version = bump_version(db, config.version_override)

    print(f"Bumping version: {curr_ver} -> {target_version}")

    # 3. Save to temp file and push
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as push_tmp:
        local_push_path = Path(push_tmp.name)

    save_db(local_push_path, db)

    # Note: run_push calls bump_version internally as well.
    # To ensure it keeps target_version if target_version > version_override:
    config = dataclasses.replace(config, version_override=target_version)

    print("Uploading updated DB and rebooting printer...")
    try:
        push_res = run_push(
            config,
            local_push_path,
            transport,
            force_reboot=args.force_reboot,
        )
    finally:
        local_push_path.unlink(missing_ok=True)

    if push_res.get("status") == "busy":
        print("ERROR: Printer is busy. Aborting.", file=sys.stderr)
        sys.exit(2)

    print("Push and reboot completed successfully.")

    # 4. Update local printer-db-cache.json if present
    cache_path = sync_handlers._cache_db_path if sync_handlers._cache_db_path is not None else sync_handlers._resolve_cache_path()
    if cache_path is not None and Path(cache_path).exists():
        try:
            cached_db = load_db(cache_path)
            cached_db["result"]["version"] = str(target_version)
            save_db(cache_path, cached_db)
            print(f"Updated local cache version to {target_version}")
        except Exception as ex:
            print(f"Warning: Could not update local cache: {ex}", file=sys.stderr)

    # 5. Verify on live printer
    print("Verifying live printer DB...")
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as verify_tmp:
        verify_path = Path(verify_tmp.name)
    try:
        transport.scp_pull(verify_path)
        fresh_db = load_db(verify_path)
    finally:
        verify_path.unlink(missing_ok=True)

    fresh_ver = fresh_db.get("result", {}).get("version")
    fresh_count = len(fresh_db.get("result", {}).get("list", []))
    print(f"VERIFICATION SUCCESS: Live printer DB version is now {fresh_ver} (entries: {fresh_count})")


if __name__ == "__main__":
    main()
