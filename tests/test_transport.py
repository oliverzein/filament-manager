# tests/test_transport.py
# Task 4: Printer transport (SSH/SCP/WS behind a Protocol).
# Lifted from skill/cfs.py lines 316-477; die() -> FmError, config dict -> Config,
# transport functions become SshTransport methods + a Protocol-based check_printer_busy.
import subprocess

import pytest
from fm.errors import FmError
from fm.printer import transport as pt
from fm.printer.transport import PrinterTransport, SshTransport

from conftest import FakeTransport


# --- Protocol satisfaction ---

def test_protocol_satisfied():
    t = FakeTransport()
    # FakeTransport must satisfy the PrinterTransport protocol
    assert isinstance(t, PrinterTransport)
    # module-level check_printer_busy operates on any transport; must not raise
    assert pt.check_printer_busy(t) is None


def test_fake_transport_records_commands():
    t = FakeTransport()
    t.run("echo hi")
    t.scp_push("/tmp/a")
    t.scp_pull("/tmp/b")
    assert t.commands == ["echo hi", "scp-push:/tmp/a", "scp-pull:/tmp/b"]


# --- Busy detection (semantics lifted from cfs.check_printer_busy) ---

@pytest.fixture
def fake_busy_ws():
    """WS status dict simulating a printing printer (state=1)."""
    return {"state": 1, "printFileName": "benchy.gcode",
            "printProgress": 42, "layer": 17, "TotalLayer": 100}


def test_busy_detection_parses_job(fake_busy_ws):
    class _T:
        def ws_request(self, method, params):
            return fake_busy_ws
    info = pt.check_printer_busy(_T())
    assert info == {"filename": "benchy.gcode", "progress": 42, "layer": 17}


def test_idle_returns_none():
    class _T:
        def ws_request(self, method, params):
            return {"state": 0, "printFileName": "", "printProgress": 0, "layer": 0}
    assert pt.check_printer_busy(_T()) is None


def test_paused_state_is_busy():
    # state=2 (paused) blocks reboot per BUSY_STATES
    class _T:
        def ws_request(self, method, params):
            return {"state": 2, "printFileName": "x.gcode", "printProgress": 5, "layer": 1}
    assert pt.check_printer_busy(_T()) is not None


def test_completed_state_not_busy():
    # stale printFileName/progress after cancel/complete does NOT count as busy
    class _T:
        def ws_request(self, method, params):
            return {"state": 3, "printFileName": "x.gcode", "printProgress": 100, "layer": 99}
    assert pt.check_printer_busy(_T()) is None


def test_cancelled_state_not_busy():
    class _T:
        def ws_request(self, method, params):
            return {"state": 4, "printFileName": "x.gcode", "printProgress": 50, "layer": 40}
    assert pt.check_printer_busy(_T()) is None


def test_busy_via_fake_transport():
    t = FakeTransport(busy={"filename": "x.gcode", "progress": "50%", "layer": 12})
    info = pt.check_printer_busy(t)
    assert info == {"filename": "x.gcode", "progress": "50%", "layer": 12}


# --- SshTransport.run (lifted from ssh_cmd) ---

def test_ssh_run_success(cfg, monkeypatch):
    monkeypatch.setattr(pt.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess([], 0, "out", ""))
    t = SshTransport(cfg)
    assert t.run("echo hi").returncode == 0


def test_ssh_run_timeout_raises(cfg, monkeypatch):
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired([], 30)
    monkeypatch.setattr(pt.subprocess, "run", _boom)
    t = SshTransport(cfg)
    with pytest.raises(FmError) as exc:
        t.run("echo hi")
    assert exc.value.code == pt.EXIT_SSH


def test_ssh_run_permission_denied_raises(cfg, monkeypatch):
    monkeypatch.setattr(pt.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess([], 255, "", "Permission denied"))
    t = SshTransport(cfg)
    with pytest.raises(FmError) as exc:
        t.run("echo hi")
    assert exc.value.code == pt.EXIT_SSH


def test_ssh_run_tolerates_rc255(cfg, monkeypatch):
    # rc=255 without "Permission denied" is a transient SSH error -> tolerated, returned
    monkeypatch.setattr(pt.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess([], 255, "", "host unreachable"))
    t = SshTransport(cfg)
    r = t.run("echo hi")
    assert r.returncode == 255


# --- SshTransport.scp_push / scp_pull ---

def test_scp_pull_failure_raises(cfg, monkeypatch):
    monkeypatch.setattr(pt.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess([], 1, "", "nope"))
    t = SshTransport(cfg)
    with pytest.raises(FmError) as exc:
        t.scp_pull("/tmp/local.json")
    assert exc.value.code == pt.EXIT_SSH


def test_scp_push_timeout_raises(cfg, monkeypatch):
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired([], 60)
    monkeypatch.setattr(pt.subprocess, "run", _boom)
    t = SshTransport(cfg)
    with pytest.raises(FmError) as exc:
        t.scp_push("/tmp/local.json")
    assert exc.value.code == pt.EXIT_SSH


# --- SshTransport.ws_request ---

def test_ws_request_failure_raises(cfg, monkeypatch):
    class _Boom:
        def create_connection(self, uri, timeout):
            raise OSError("refused")
    monkeypatch.setattr(pt, "websocket", _Boom())
    t = SshTransport(cfg)
    with pytest.raises(FmError) as exc:
        t.ws_request("get", {})
    assert exc.value.code == pt.EXIT_WS


# --- SshTransport.ssh_backup (lifted from cfs.ssh_backup) ---

def test_ssh_backup_runs_cp_and_rotate(cfg, monkeypatch):
    t = SshTransport(cfg)
    cmds = []
    monkeypatch.setattr(t, "run", lambda cmd, timeout=30: cmds.append(cmd))
    t.ssh_backup()
    assert any(c.startswith("cp ") and ".bak." in c for c in cmds)
    assert any("tail -n +6" in c and "xargs -r rm" in c for c in cmds)


# --- SshTransport.ssh_reboot (fixed sequence) ---

def test_ssh_reboot_fixed_sequence(cfg, monkeypatch):
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["timeout"] = kw.get("timeout")
        # connection drops during reboot -> TimeoutExpired is expected
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 15))

    monkeypatch.setattr(pt.subprocess, "run", fake_run)
    t = SshTransport(cfg)
    t.ssh_reboot()  # must not raise despite the drop
    assert captured["cmd"][-1] == "sync; sync; sleep 2; reboot"
    assert captured["timeout"] == 15


# --- SshTransport.wait_for_reboot ---

def test_wait_for_reboot_success(cfg, monkeypatch):
    monkeypatch.setattr(pt.time, "sleep", lambda s: None)
    monkeypatch.setattr(pt.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess([], 0, "ok", ""))
    t = SshTransport(cfg)
    assert t.wait_for_reboot(timeout=300) is True


def test_wait_for_reboot_raises_on_timeout(cfg, monkeypatch):
    # time advances past the deadline after one loop iteration
    times = iter([1000.0, 1000.0, 2000.0])
    monkeypatch.setattr(pt.time, "time", lambda: next(times))
    monkeypatch.setattr(pt.time, "sleep", lambda s: None)
    monkeypatch.setattr(pt.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess([], 1, "", ""))
    t = SshTransport(cfg)
    with pytest.raises(FmError) as exc:
        t.wait_for_reboot(timeout=300)
    assert exc.value.code == pt.EXIT_REBOOT


# --- SshTransport.check_printer_busy delegates to the protocol function ---

def test_ssh_check_printer_busy_delegates(cfg, monkeypatch):
    t = SshTransport(cfg)
    monkeypatch.setattr(t, "ws_request",
                        lambda method, params: {"state": 1, "printFileName": "j.gcode",
                                                 "printProgress": 7, "layer": 3})
    info = t.check_printer_busy()
    assert info == {"filename": "j.gcode", "progress": 7, "layer": 3}
