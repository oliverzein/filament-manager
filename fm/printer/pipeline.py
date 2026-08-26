# fm/printer/pipeline.py
"""Push pipeline + drift primitives — lifted from skill/cfs.py push_local_db.

Adaptations per the plan:
  * die(code, msg) process exits -> raising FmError(code, msg)
  * config dict -> Config dataclass
  * subprocess/WS calls -> injected transport (PrinterTransport Protocol)
  * push_local_db body restructured to call transport.run / scp_push /
    check_printer_busy (the module-level Protocol function from transport.py)

Push order (Global Constraint 1, load-bearing):
  MIN_DB_ENTRIES check -> WS busy-check -> backup on printer -> version
  auto-increment (>= 9876543210) -> SCP upload -> sleep 3 -> sync;sync;reboot
  -> wait online -> verify.

Busy -> returns {"status": "busy", job=<details>} WITHOUT touching the printer.
"""
import hashlib
import json
import tempfile
import time
from pathlib import Path

from fm.config import Config
from fm.errors import FmError
from fm.printer.dbcodec import MIN_DB_ENTRIES, bump_version, load_db, save_db
from fm.printer.transport import PrinterTransport, check_printer_busy

EXIT_VALIDATE = 4
EXIT_REBOOT = 6
EXIT_BUSY = 10


def canonical_hash(fields: dict) -> str:
    """sha256 of canonical JSON (sort_keys=True) of identity+parameter fields only.

    The caller is responsible for passing only the identity + parameter fields
    (brand, name, type, temps, density, ...); this function canonicalises and
    hashes whatever it receives so drift between two field sets is detectable.
    """
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _verify_version(db, expected):
    """Check version >= expected (cloud sync sets ~epoch, which is < override).
    Auto-increment may push version above override, so exact match would false-fail."""
    actual = db.get("result", {}).get("version") if isinstance(db, dict) else None
    if actual is None:
        return False
    try:
        return int(actual) >= int(expected)
    except (ValueError, TypeError):
        return str(actual) == str(expected)


def verify_printer(config: Config, transport: PrinterTransport,
                   expected_ids: list[str]) -> dict:
    """Pull DB fresh from printer (ignoring cache) and check version floor + entry
    presence. Lifts cfs.py verify_version floor check + verify_entry presence.

    Returns dict(version_ok: bool, present: list[str], missing: list[str]).
    """
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        local = Path(f.name)
    try:
        transport.scp_pull(local)
        db = load_db(local)
    finally:
        local.unlink(missing_ok=True)
    version_ok = _verify_version(db, config.version_override)
    ids = {e.get("base", {}).get("id")
           for e in db.get("result", {}).get("list", [])}
    present = [i for i in expected_ids if i in ids]
    missing = [i for i in expected_ids if i not in ids]
    return {"version_ok": version_ok, "present": present, "missing": missing}


def run_push(config: Config, local_db_path: Path, transport: PrinterTransport,
             *, force_reboot=False, no_reboot=False, force_push=False,
             sleep=time.sleep) -> dict:
    """Push local DB to printer: MIN_DB check, busy check, backup, version bump,
    SCP upload, reboot. Order enforced per Global Constraint 1.

    Returns {"status": "ok"|"busy"|"no_reboot", "job": dict|None}. Busy returns
    WITHOUT touching the printer (no backup, no upload, no reboot).
    """
    db = load_db(local_db_path)

    # Sanity check — refuse to push a suspiciously small DB (prevents corruption overwrite)
    count = len(db.get("result", {}).get("list", []))
    if count < MIN_DB_ENTRIES and not force_push:
        raise FmError(EXIT_VALIDATE,
            f"REFUSING to push: DB has only {count} entries (expected >= {MIN_DB_ENTRIES}). "
            f"This looks corrupt. Use force_push to override (DANGEROUS — overwrites printer DB).")

    # Busy check BEFORE upload — no point uploading if we can't reboot to protect it
    if not no_reboot:
        job = check_printer_busy(transport)
        if job and not force_reboot:
            return {"status": "busy", "job": job}

    # Backup remote DB before overwriting (rotation: keep last 5)
    remote_path = config.db_remote_path
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_path = f"{remote_path}.bak.{ts}"
    transport.run(f"cp {remote_path} {backup_path}")
    # rotate: keep last 5
    transport.run(
        f"ls -t {remote_path}.bak.* 2>/dev/null | tail -n +6 | xargs -r rm")

    # Version bump + upload
    bump_version(db, config.version_override)
    save_db(local_db_path, db)
    transport.scp_push(local_db_path)

    # Wait for filesystem flush on printer before reboot
    # SCP returns when transfer is complete, but printer OS may still write to flash
    sleep(3)

    if no_reboot:
        return {"status": "no_reboot", "job": None}

    # Reboot + wait
    # sync filesystem buffers before reboot to prevent flash corruption on /mnt/UDISK
    # K2 uses procd (OpenWrt) — reboot is hard, so we sync explicitly first
    try:
        transport.run("sync; sync; sleep 2; reboot", timeout=15)
    except FmError:
        pass  # expected — connection drops during reboot
    _wait_for_reboot(transport, sleep=sleep)
    return {"status": "ok", "job": None}


def _wait_for_reboot(transport: PrinterTransport, *, timeout=300, sleep=time.sleep):
    """Poll SSH until the printer is back online (lifted from SshTransport.wait_for_reboot)."""
    deadline = time.time() + timeout
    sleep(10)  # initial boot wait
    while time.time() < deadline:
        try:
            result = transport.run("echo ok", timeout=5)
            if result.returncode == 0 and "ok" in result.stdout:
                sleep(5)  # services settle
                return True
        except FmError:
            pass
        sleep(5)
    raise FmError(EXIT_REBOOT, f"Printer did not come back within {timeout}s")
