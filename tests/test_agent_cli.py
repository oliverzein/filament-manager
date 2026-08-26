# tests/test_agent_cli.py
"""End-to-end tests for the fm-agent companion CLI (Task 12).

Exercises ``poll`` against a stub HTTP server (an in-process
``ThreadingHTTPServer`` implementing the Task 11 agent API contract) using a
temp OrcaSlicer user tree. ``watch`` is a thin loop over ``poll_once`` and is
covered by a single bounded-iteration test.
"""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest

from fm.agent import cli as agent_cli
from fm.presets.matching import temp_sync_targets


# ---------------------------------------------------------------------------
# Stub HTTP API (Task 11 contract)
# ---------------------------------------------------------------------------

class _StubState:
    """In-memory agent API state shared with the stub HTTP handler."""

    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.results: list[dict] = []
        self._next_id = 1

    def enqueue(self, kind: str, payload: dict) -> int:
        jid = self._next_id
        self._next_id += 1
        self.jobs.append({
            "job_id": jid, "kind": kind, "status": "queued",
            "payload": payload, "result": None,
        })
        return jid


class _StubHandler(http.server.BaseHTTPRequestHandler):
    # Set as a class attribute by the fixture.
    state: _StubState

    def log_message(self, *args: object) -> None:  # silence test noise
        pass

    def _send(self, code: int, body: dict) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        if self.path != "/api/agent/jobs":
            self._send(404, {"detail": "not found"})
            return
        job = None
        for j in self.state.jobs:
            if j["status"] == "queued":
                j["status"] = "running"
                job = j
                break
        if job is None:
            self._send(200, {"jobs": []})
            return
        self._send(200, {"jobs": [{
            "job_id": job["job_id"], "kind": job["kind"],
            "status": job["status"], "payload": job["payload"],
            "result": job["result"],
        }]})

    def do_POST(self) -> None:  # noqa: N802 — http.server API
        if self.path != "/api/agent/results":
            self._send(404, {"detail": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or "{}")
        self.state.results.append(body)
        for j in self.state.jobs:
            if j["job_id"] == body["job_id"]:
                j["status"] = body["status"]
                j["result"] = body["result"]
                break
        self._send(200, {
            "job_id": body["job_id"], "kind": "?", "status": body["status"],
            "payload": None, "result": body["result"],
        })


@pytest.fixture
def stub_api():
    """Start an in-process stub agent API on an ephemeral port."""
    state = _StubState()
    handler = type("_BoundStubHandler", (_StubHandler,), {"state": state})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, base
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# Temp OrcaSlicer tree fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_orca_tree(tmp_path, monkeypatch):
    """A single-UUID OrcaSlicer user tree; OrcaSlicer is treated as not running."""
    monkeypatch.setenv("FM_ORCASLICER_CONFIG_DIR", str(tmp_path))
    filament_dir = tmp_path / "user" / "uuid-1" / "filament"
    filament_dir.mkdir(parents=True)
    # Default: OrcaSlicer not running so apply_preset can proceed.
    monkeypatch.setattr(agent_cli, "is_orcaslicer_running", lambda: False)
    return tmp_path, filament_dir


@pytest.fixture
def tmp_orca_tree_with_two_uuid_dirs(tmp_path, monkeypatch):
    """Two UUID user dirs, each with a filament/ subdir; only the first is scanned."""
    monkeypatch.setenv("FM_ORCASLICER_CONFIG_DIR", str(tmp_path))
    first = tmp_path / "user" / "uuid-1" / "filament"
    second = tmp_path / "user" / "uuid-2" / "filament"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    monkeypatch.setattr(agent_cli, "is_orcaslicer_running", lambda: False)
    return tmp_path, first, second


# ---------------------------------------------------------------------------
# apply_preset
# ---------------------------------------------------------------------------

def test_apply_preset_writes_files_and_reports_done(stub_api, tmp_orca_tree):
    state, base = stub_api
    _tree, filament_dir = tmp_orca_tree
    state.enqueue("apply_preset", {
        "name": "Generic PLA",
        "preset": {"filament_id": "GF99", "type": "filament"},
        "info": "sync_info = \n",
    })

    rc = agent_cli.main(["poll", "--base-url", base, "--token", "t"])

    assert rc == 0
    # Preset JSON + .info written into the first user filament dir.
    preset_file = filament_dir / "Generic PLA.json"
    info_file = filament_dir / "Generic PLA.info"
    assert preset_file.exists()
    assert info_file.exists()
    assert json.loads(preset_file.read_text())["filament_id"] == "GF99"
    assert info_file.read_text() == "sync_info = \n"
    # Done result posted back.
    assert len(state.results) == 1
    assert state.results[0]["job_id"] == 1
    assert state.results[0]["status"] == "done"
    assert state.results[0]["result"]["name"] == "Generic PLA"
    # Job marked done in stub state.
    assert state.jobs[0]["status"] == "done"


def test_refuses_while_orcaslicer_running(stub_api, tmp_orca_tree, monkeypatch):
    state, base = stub_api
    _tree, filament_dir = tmp_orca_tree
    monkeypatch.setattr(agent_cli, "is_orcaslicer_running", lambda: True)
    state.enqueue("apply_preset", {
        "name": "Generic PLA",
        "preset": {"filament_id": "GF99"},
        "info": "",
    })

    rc = agent_cli.main(["poll", "--base-url", base, "--token", "t"])

    assert rc == 0
    # No files written.
    assert not (filament_dir / "Generic PLA.json").exists()
    # Error result posted back.
    assert state.results[0]["status"] == "error"
    assert state.results[0]["result"]["error"] == "OrcaSlicer running"
    assert state.jobs[0]["status"] == "error"


def test_apply_preset_missing_user_dir_reports_error(stub_api, tmp_path, monkeypatch):
    """No user filament dir -> error result, no crash."""
    state, base = stub_api
    # Config dir exists but has no user/<uuid>/filament/ tree.
    monkeypatch.setenv("FM_ORCASLICER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(agent_cli, "is_orcaslicer_running", lambda: False)
    state.enqueue("apply_preset", {"name": "X", "preset": {"type": "filament"}, "info": ""})

    rc = agent_cli.main(["poll", "--base-url", base, "--token", "t"])

    assert rc == 0
    assert state.results[0]["status"] == "error"
    assert "user filament directory" in state.results[0]["result"]["error"]


# ---------------------------------------------------------------------------
# report_presets
# ---------------------------------------------------------------------------

def test_report_scans_first_user_dir_only(tmp_orca_tree_with_two_uuid_dirs, stub_api):
    state, base = stub_api
    _tree, first, second = tmp_orca_tree_with_two_uuid_dirs
    (first / "AAA.json").write_text(json.dumps({
        "name": "AAA", "type": "filament", "filament_type": ["PLA"],
        "filament_vendor": "Sunlu", "nozzle_temperature": ["210"],
        "nozzle_temperature_initial_layer": ["210"], "inherits": "",
    }))
    (second / "BBB.json").write_text(json.dumps({
        "name": "BBB", "type": "filament", "filament_type": ["PETG"],
        "nozzle_temperature": ["240"],
    }))

    state.enqueue("report_presets", {})
    rc = agent_cli.main(["poll", "--base-url", base, "--token", "t"])

    assert rc == 0
    assert state.results[0]["status"] == "done"
    presets = state.results[0]["result"]["presets"]
    # Only the first user UUID dir is scanned.
    assert [p["name"] for p in presets] == ["AAA"]
    entry = presets[0]
    # Human-facing fields per the brief.
    assert entry["filament_type"] == ["PLA"]
    assert entry["filament_vendor"] == "Sunlu"
    assert entry["nozzle_temperature"] == ["210"]
    assert entry["nozzle_temperature_initial_layer"] == ["210"]
    assert entry["inherits"] == ""
    assert entry["from"] == "user"
    # Internal keys that temp_sync_targets needs.
    assert entry["_system"] is False
    assert entry["_path"].endswith("AAA.json")


def test_report_result_consumable_by_temp_sync(tmp_orca_tree_with_two_uuid_dirs, stub_api):
    """The reported presets must drive temp_sync_targets end-to-end."""
    state, base = stub_api
    _tree, first, _second = tmp_orca_tree_with_two_uuid_dirs
    (first / "Sunlu PLA+.json").write_text(json.dumps({
        "name": "Sunlu PLA+", "type": "filament", "filament_type": ["PLA"],
        "filament_vendor": "Sunlu", "nozzle_temperature": ["220"],
        "nozzle_temperature_initial_layer": ["220"], "inherits": "",
    }))

    state.enqueue("report_presets", {})
    agent_cli.main(["poll", "--base-url", base, "--token", "t"])
    presets = state.results[0]["result"]["presets"]

    profiles_state = [{
        "id": 1, "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
        "nozzle_temperature": 215, "printer_db_id": "99001",
    }]
    updates, skipped = temp_sync_targets(profiles_state, presets)
    assert skipped == []
    assert len(updates) == 1
    assert updates[0]["profile_id"] == 1
    assert updates[0]["orca_temp"] == 220
    assert updates[0]["db_temp"] == 215


def test_report_empty_when_no_user_dir(stub_api, tmp_path, monkeypatch):
    state, base = stub_api
    monkeypatch.setenv("FM_ORCASLICER_CONFIG_DIR", str(tmp_path))
    state.enqueue("report_presets", {})

    rc = agent_cli.main(["poll", "--base-url", base, "--token", "t"])

    assert rc == 0
    assert state.results[0]["status"] == "done"
    assert state.results[0]["result"]["presets"] == []


def test_report_skips_non_filament_json(tmp_orca_tree, stub_api):
    state, base = stub_api
    _tree, filament_dir = tmp_orca_tree
    (filament_dir / "Good.json").write_text(json.dumps({
        "name": "Good", "type": "filament", "filament_type": ["PLA"],
    }))
    (filament_dir / "Bad.json").write_text(json.dumps({"name": "Bad", "type": "printer"}))
    state.enqueue("report_presets", {})

    agent_cli.main(["poll", "--base-url", base, "--token", "t"])

    presets = state.results[0]["result"]["presets"]
    assert [p["name"] for p in presets] == ["Good"]


# ---------------------------------------------------------------------------
# poll plumbing
# ---------------------------------------------------------------------------

def test_poll_no_jobs_is_noop(stub_api, tmp_orca_tree):
    state, base = stub_api
    rc = agent_cli.main(["poll", "--base-url", base, "--token", "t"])
    assert rc == 0
    assert state.results == []


def test_poll_processes_one_job_per_call(stub_api, tmp_orca_tree):
    """GET /api/agent/jobs claims at most one job; a second poll gets the next."""
    state, base = stub_api
    _tree, filament_dir = tmp_orca_tree
    state.enqueue("apply_preset", {"name": "A", "preset": {"type": "filament"}, "info": ""})
    state.enqueue("apply_preset", {"name": "B", "preset": {"type": "filament"}, "info": ""})

    agent_cli.main(["poll", "--base-url", base, "--token", "t"])
    assert len(state.results) == 1
    assert state.results[0]["result"]["name"] == "A"

    agent_cli.main(["poll", "--base-url", base, "--token", "t"])
    assert len(state.results) == 2
    assert state.results[1]["result"]["name"] == "B"


def test_poll_unknown_kind_posts_error(stub_api, tmp_orca_tree):
    state, base = stub_api
    state.enqueue("mystery_kind", {})
    agent_cli.main(["poll", "--base-url", base, "--token", "t"])
    assert state.results[0]["status"] == "error"
    assert "mystery_kind" in state.results[0]["result"]["error"]


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def test_load_config_requires_base_url_and_token(monkeypatch, tmp_path):
    """Missing credentials raise SystemExit rather than crashing."""
    monkeypatch.setattr(agent_cli, "AGENT_CONFIG_FILE", tmp_path / "nope.json")
    with pytest.raises(SystemExit):
        agent_cli.load_config(None, None)


def test_load_config_file_overrides_default(monkeypatch, tmp_path):
    cfg = tmp_path / "agent.json"
    cfg.write_text(json.dumps({"base_url": "http://from-file", "token": "filetok"}))
    monkeypatch.setattr(agent_cli, "AGENT_CONFIG_FILE", cfg)
    base, token = agent_cli.load_config(None, None)
    assert base == "http://from-file"
    assert token == "filetok"


def test_load_config_cli_args_override_file(monkeypatch, tmp_path):
    cfg = tmp_path / "agent.json"
    cfg.write_text(json.dumps({"base_url": "http://from-file", "token": "filetok"}))
    monkeypatch.setattr(agent_cli, "AGENT_CONFIG_FILE", cfg)
    base, token = agent_cli.load_config("http://cli", "clitok")
    assert base == "http://cli"
    assert token == "clitok"


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------

def test_watch_loops_until_no_jobs(stub_api, tmp_orca_tree, monkeypatch):
    """watch polls repeatedly; bounded here by stopping the loop after a few
    iterations once all jobs are processed."""
    state, base = stub_api
    _tree, filament_dir = tmp_orca_tree
    state.enqueue("apply_preset", {"name": "W", "preset": {"type": "filament"}, "info": ""})

    iterations = {"n": 0}

    def fake_sleep(_s: float) -> None:
        iterations["n"] += 1
        if iterations["n"] >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(agent_cli.time, "sleep", fake_sleep)

    # watch runs until KeyboardInterrupt breaks the loop.
    with pytest.raises(KeyboardInterrupt):
        agent_cli.main(["watch", "--base-url", base, "--token", "t", "--interval", "0"])

    # The single job was processed on the first iteration.
    assert len(state.results) == 1
    assert state.results[0]["result"]["name"] == "W"
    assert (filament_dir / "W.json").exists()
