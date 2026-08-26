# tests/test_api_agent.py
"""Agent pull API tests — token auth, job claiming, result completion.

Pins the spec behavior from the Task 11 brief:

* ``GET /api/agent/jobs`` requires a Bearer agent token (401 otherwise).
* A poll claims the oldest queued ``apply_preset``/``report_presets`` job
  (queued -> running) and returns it (with payload) in ``{"jobs": [...]}``.
* ``POST /api/agent/results`` completes a running job, logs activity, and
  stores ``report_presets`` results on ``job.result`` — exactly where
  ``handle_temp_sync`` reads them.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from fm.api.app import create_app, Settings
from fm.config import Config
from fm.db import SessionLocal
from fm.models import ActivityLog, Job
from fm.service import jobs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg():
    return Config(
        printer_ip="192.168.0.101", ssh_user="root",
        ssh_password="pw", db_remote_path="/mnt/UDISK/x.json",
        ws_port=9999, version_override=9876543210,
        id_range_start=99001, agent_token="testtoken",
    )


@pytest.fixture
def client(tmp_path, monkeypatch, cfg):
    monkeypatch.setenv("FM_DATABASE_URL", f"sqlite:///{tmp_path}/agent.db")
    with TestClient(create_app(Settings(config=cfg))) as c:
        yield c


AUTH = {"Authorization": "Bearer testtoken"}
BAD_AUTH = {"Authorization": "Bearer wrong"}


def _enqueue(kind: str, payload: dict) -> int:
    """Persist a queued job in a fresh session, return its id."""
    with SessionLocal() as s:
        job = jobs.enqueue(s, kind, payload)
        s.flush()
        return job.id


@pytest.fixture
def seeded_preset_job(client):
    return _enqueue("apply_preset", {
        "profile_id": 1,
        "name": "Generic PLA",
        "preset": {"filament_id": "GF99"},
        "info": "sync_info = \n",
    })


@pytest.fixture
def seeded_report_job(client):
    return _enqueue("report_presets", {})


# ---------------------------------------------------------------------------
# Token auth
# ---------------------------------------------------------------------------

class TestTokenAuth:
    def test_requires_token(self, client):
        assert client.get("/api/agent/jobs").status_code == 401

    def test_wrong_token_rejected(self, client):
        assert client.get("/api/agent/jobs", headers=BAD_AUTH).status_code == 401

    def test_results_requires_token(self, client):
        r = client.post("/api/agent/results", json={
            "job_id": 1, "status": "done", "result": {},
        })
        assert r.status_code == 401

    def test_no_config_means_no_auth(self, tmp_path, monkeypatch):
        """Without a configured agent_token there is nothing to validate -> 401."""
        monkeypatch.setenv("FM_DATABASE_URL", f"sqlite:///{tmp_path}/nc.db")
        with TestClient(create_app(Settings(config=None))) as c:
            assert c.get("/api/agent/jobs", headers=AUTH).status_code == 401


# ---------------------------------------------------------------------------
# Poll / claim
# ---------------------------------------------------------------------------

class TestPoll:
    def test_poll_claims_apply_preset_job(self, client, seeded_preset_job):
        r = client.get("/api/agent/jobs", headers=AUTH)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["jobs"][0]["kind"] == "apply_preset"
        assert data["jobs"][0]["status"] == "running"
        assert data["jobs"][0]["job_id"] == seeded_preset_job
        # Payload is decoded so the agent can act on it.
        assert data["jobs"][0]["payload"]["profile_id"] == 1

    def test_poll_empty_when_no_queued_jobs(self, client):
        r = client.get("/api/agent/jobs", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["jobs"] == []

    def test_poll_claims_oldest_first(self, client):
        first = _enqueue("apply_preset", {"profile_id": 1})
        second = _enqueue("report_presets", {})
        r = client.get("/api/agent/jobs", headers=AUTH)
        assert r.json()["jobs"][0]["job_id"] == first
        # Second poll claims the next oldest.
        r2 = client.get("/api/agent/jobs", headers=AUTH)
        assert r2.json()["jobs"][0]["job_id"] == second
        # Third poll has nothing left.
        r3 = client.get("/api/agent/jobs", headers=AUTH)
        assert r3.json()["jobs"] == []

    def test_poll_does_not_claim_non_agent_kinds(self, client):
        """A queued push job must not be claimed by the agent endpoint."""
        _enqueue("push", {})
        r = client.get("/api/agent/jobs", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["jobs"] == []

    def test_claimed_job_is_running_in_db(self, client, seeded_preset_job):
        client.get("/api/agent/jobs", headers=AUTH)
        with SessionLocal() as s:
            job = s.get(Job, seeded_preset_job)
            assert job.status == "running"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

class TestResults:
    def test_result_completes_job(self, client, seeded_report_job):
        # Agent first claims the job.
        r = client.get("/api/agent/jobs", headers=AUTH)
        assert r.json()["jobs"][0]["job_id"] == seeded_report_job

        presets = [{"name": "Generic PLA", "nozzle_temperature": ["210"]}]
        r2 = client.post("/api/agent/results", headers=AUTH, json={
            "job_id": seeded_report_job,
            "status": "done",
            "result": {"presets": presets},
        })
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["status"] == "done"
        assert body["result"]["presets"] == presets

        # Stored on job.result — where handle_temp_sync reads it.
        with SessionLocal() as s:
            job = s.get(Job, seeded_report_job)
            assert job.status == "done"
            assert json.loads(job.result)["presets"] == presets

    def test_result_logs_activity(self, client, seeded_report_job):
        client.get("/api/agent/jobs", headers=AUTH)
        client.post("/api/agent/results", headers=AUTH, json={
            "job_id": seeded_report_job, "status": "done", "result": {},
        })
        with SessionLocal() as s:
            entries = list(s.query(ActivityLog).filter(
                ActivityLog.action == "report_presets"
            ))
            assert any(e.outcome == "ok" for e in entries)

    def test_error_status_records_failure(self, client, seeded_preset_job):
        client.get("/api/agent/jobs", headers=AUTH)
        r = client.post("/api/agent/results", headers=AUTH, json={
            "job_id": seeded_preset_job,
            "status": "error",
            "result": {"error": "printer refused preset"},
        })
        assert r.status_code == 200
        assert r.json()["status"] == "error"
        with SessionLocal() as s:
            job = s.get(Job, seeded_preset_job)
            assert job.status == "error"
            assert json.loads(job.result)["error"] == "printer refused preset"
            entries = list(s.query(ActivityLog).filter(
                ActivityLog.action == "apply_preset"
            ))
            assert any(e.outcome == "error" for e in entries)

    def test_result_unknown_job_404(self, client):
        r = client.post("/api/agent/results", headers=AUTH, json={
            "job_id": 9999, "status": "done", "result": {},
        })
        assert r.status_code == 404

    def test_result_non_running_job_409(self, client, seeded_report_job):
        # Job is still queued (never claimed) -> cannot complete.
        r = client.post("/api/agent/results", headers=AUTH, json={
            "job_id": seeded_report_job, "status": "done", "result": {},
        })
        assert r.status_code == 409

    def test_result_invalid_status_422(self, client, seeded_report_job):
        client.get("/api/agent/jobs", headers=AUTH)
        r = client.post("/api/agent/results", headers=AUTH, json={
            "job_id": seeded_report_job, "status": "queued", "result": {},
        })
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Storage convention — temp_sync reads report_presets job.result
# ---------------------------------------------------------------------------

class TestReportPresetsStorage:
    def test_report_result_readable_by_temp_sync_query(self, client, seeded_report_job):
        """handle_temp_sync queries the most recent done report_presets job
        and decodes job.result['presets']. Verify that path succeeds after a
        posted result."""
        client.get("/api/agent/jobs", headers=AUTH)
        presets = [{"name": "PLA", "nozzle_temperature": ["205"]}]
        client.post("/api/agent/results", headers=AUTH, json={
            "job_id": seeded_report_job, "status": "done",
            "result": {"presets": presets},
        })
        with SessionLocal() as s:
            from sqlalchemy import select
            report_job = s.scalars(
                select(Job).where(
                    Job.kind == "report_presets", Job.status == "done"
                ).order_by(Job.id.desc())
            ).first()
            assert report_job is not None
            assert json.loads(report_job.result)["presets"] == presets
