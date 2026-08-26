# fm/printer/transport.py
"""Printer transport — SSH/SCP/WS boundary behind an injectable Protocol.

Lifted from skill/cfs.py lines 316-477. Adaptations per the plan:
  * die(code, msg) process exits -> raising FmError(code, msg)
  * config dict -> Config dataclass
  * transport functions become SshTransport methods (subprocess/WS calls intact)
  * check_printer_busy returns None (idle) or a dict with filename/progress/layer

A module-level check_printer_busy(transport) operates on the PrinterTransport
Protocol so callers (pipeline, API) can inject a fake in tests.
"""
import json
import subprocess
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

import websocket

from fm.config import Config
from fm.errors import FmError

EXIT_SSH = 2
EXIT_WS = 5
EXIT_REBOOT = 6

BUSY_STATES = {1, 2}  # 1=printing, 2=paused — only these block reboot
# Known states: 0=idle, 1=printing, 2=paused, 3=completed?, 4=cancelled


@runtime_checkable
class PrinterTransport(Protocol):
    """Injectable printer boundary: SSH run, SCP push/pull, WS request."""
    def run(self, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess: ...
    def scp_push(self, local: Path) -> None: ...
    def scp_pull(self, local: Path) -> None: ...
    def ws_request(self, method: str, params: dict) -> dict: ...


class SshTransport:
    """Real SSH/SCP/WS transport against a Creality K2 printer."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _ssh_base_cmd(self):
        return [
            "sshpass", "-p", self.cfg.ssh_password,
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            f"{self.cfg.ssh_user}@{self.cfg.printer_ip}",
        ]

    def run(self, cmd, timeout=30):
        full = self._ssh_base_cmd() + [cmd]
        try:
            result = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise FmError(EXIT_SSH, f"SSH timeout ({timeout}s): {cmd}")
        if result.returncode == 255 and "Permission denied" in result.stderr:
            raise FmError(EXIT_SSH, "SSH auth failed. Check config.")
        if result.returncode != 0 and result.returncode != 255:
            raise FmError(EXIT_SSH, f"SSH error (rc={result.returncode}): {result.stderr}")
        return result

    def scp_pull(self, local):
        remote = f"{self.cfg.ssh_user}@{self.cfg.printer_ip}:{self.cfg.db_remote_path}"
        cmd = ["sshpass", "-p", self.cfg.ssh_password, "scp",
               "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
               remote, str(local)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise FmError(EXIT_SSH, "SCP pull timeout")
        if result.returncode != 0:
            raise FmError(EXIT_SSH, f"SCP pull failed: {result.stderr}")

    def scp_push(self, local):
        remote = f"{self.cfg.ssh_user}@{self.cfg.printer_ip}:{self.cfg.db_remote_path}"
        cmd = ["sshpass", "-p", self.cfg.ssh_password, "scp",
               "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
               str(local), remote]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise FmError(EXIT_SSH, "SCP push timeout")
        if result.returncode != 0:
            raise FmError(EXIT_SSH, f"SCP push failed: {result.stderr}")

    def ws_request(self, method, params):
        uri = f"ws://{self.cfg.printer_ip}:{self.cfg.ws_port}"
        payload = json.dumps({"method": method, "params": params})
        try:
            ws = websocket.create_connection(uri, timeout=10)
            ws.send(payload)
            raw = ws.recv()
            ws.close()
        except Exception as e:
            raise FmError(EXIT_WS, f"WS connection failed ({uri}): {e}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise FmError(EXIT_WS, f"WS response not parseable: {e}")

    def get_printer_status(self):
        """Query printer status via WS. Returns full status dict."""
        return self.ws_request("get", {})

    def check_printer_busy(self):
        """None when idle, else a dict with filename/progress/layer."""
        return check_printer_busy(self)

    def wait_for_reboot(self, timeout=300):
        deadline = time.time() + timeout
        time.sleep(10)  # initial boot wait
        while time.time() < deadline:
            try:
                result = subprocess.run(
                    self._ssh_base_cmd() + ["echo ok"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and "ok" in result.stdout:
                    time.sleep(5)  # services settle
                    return True
            except subprocess.TimeoutExpired:
                pass
            time.sleep(5)
        raise FmError(EXIT_REBOOT, f"Printer did not come back within {timeout}s")

    def ssh_backup(self):
        remote_path = self.cfg.db_remote_path
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_path = f"{remote_path}.bak.{ts}"
        self.run(f"cp {remote_path} {backup_path}")
        # rotate: keep last 5
        self.run(
            f"ls -t {remote_path}.bak.* 2>/dev/null | tail -n +6 | xargs -r rm")

    def ssh_reboot(self):
        # sync filesystem buffers before reboot to prevent flash corruption on /mnt/UDISK
        # K2 uses procd (OpenWrt) — reboot is hard, so we sync explicitly first
        try:
            subprocess.run(
                self._ssh_base_cmd() + ["sync; sync; sleep 2; reboot"],
                capture_output=True, text=True, timeout=15,
            )
        except (subprocess.TimeoutExpired, Exception):
            pass  # expected — connection drops during reboot


def check_printer_busy(transport: PrinterTransport):
    """Check if printer is currently printing via the transport Protocol.

    Returns None (idle) or a dict with filename/progress/layer.

    Busy = state in BUSY_STATES (printing or paused). Stale
    printFileName/printProgress after cancel/complete does NOT count as busy.
    """
    status = transport.ws_request("get", {})
    state = status.get("state", 0)
    if state not in BUSY_STATES:
        return None
    return {
        "filename": status.get("printFileName", ""),
        "progress": status.get("printProgress", 0),
        "layer": status.get("layer", 0),
    }
