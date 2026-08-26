# tests/test_jobs.py
import json

import pytest
from sqlalchemy import select

from fm.models import ActivityLog, Job
from fm.service import jobs


def _last_activity(session) -> ActivityLog:
    return session.scalars(
        select(ActivityLog).order_by(ActivityLog.id.desc())
    ).first()


def _last_job(session) -> Job:
    return session.scalars(select(Job).order_by(Job.id.desc())).first()


def test_enqueue_creates_queued_job(inmemory_session):
    s = inmemory_session
    job = jobs.enqueue(s, "push", {"profile_id": 7})
    s.flush()
    assert job.status == "queued"
    assert job.kind == "push"
    assert json.loads(job.payload) == {"profile_id": 7}
    assert job.id is not None


def test_claim_next_is_fifo_and_marks_running(inmemory_session):
    s = inmemory_session
    j1 = jobs.enqueue(s, "noop", {})
    j2 = jobs.enqueue(s, "noop", {})
    s.flush()
    claimed = jobs.claim_next(s)
    assert claimed.id == j1.id
    assert claimed.status == "running"
    # second claim picks the next queued job, not j1 (now running)
    claimed2 = jobs.claim_next(s)
    assert claimed2.id == j2.id
    assert claimed2.status == "running"


def test_claim_next_returns_none_when_empty(inmemory_session):
    s = inmemory_session
    assert jobs.claim_next(s) is None


def test_claim_next_skips_non_queued(inmemory_session):
    s = inmemory_session
    j = jobs.enqueue(s, "noop", {})
    s.flush()
    jobs.claim_next(s)  # now running
    assert jobs.claim_next(s) is None  # j is running, not queued
    assert j.status == "running"


def test_complete_marks_done_with_result(inmemory_session):
    s = inmemory_session
    j = jobs.enqueue(s, "noop", {})
    s.flush()
    jobs.claim_next(s)
    jobs.complete(j, {"ok": True})
    s.flush()
    assert j.status == "done"
    assert json.loads(j.result) == {"ok": True}
    assert j.finished_at is not None


def test_fail_marks_error_with_detail(inmemory_session):
    s = inmemory_session
    j = jobs.enqueue(s, "push", {})
    s.flush()
    jobs.fail(j, {"error": "boom"})
    s.flush()
    assert j.status == "error"
    assert json.loads(j.result) == {"error": "boom"}
    assert j.finished_at is not None


def test_await_input_parks_with_options(inmemory_session):
    s = inmemory_session
    j = jobs.enqueue(s, "push", {})
    s.flush()
    jobs.await_input(j, ["retry", "abort"])
    s.flush()
    assert j.status == "awaiting_input"
    assert json.loads(j.result) == {"options": ["retry", "abort"]}
    assert j.finished_at is None


def test_log_activity_records_row(inmemory_session):
    s = inmemory_session
    jobs.log_activity(s, "push", "ok", {"job_id": 1})
    s.flush()
    entry = _last_activity(s)
    assert entry.action == "push"
    assert entry.outcome == "ok"
    assert json.loads(entry.detail) == {"job_id": 1}
    assert entry.ts is not None


def test_handler_registry_runs_and_logs_ok(inmemory_session, cfg):
    s = inmemory_session
    jobs.HANDLERS["noop"] = lambda sess, conf, payload: {"ok": True}
    try:
        j = jobs.enqueue(s, "noop", {"x": 1})
        s.flush()
        jobs.run_pending(s, cfg)
        s.flush()
        assert j.status == "done"
        assert json.loads(j.result) == {"ok": True}
        assert _last_activity(s).outcome == "ok"
    finally:
        jobs.HANDLERS.pop("noop", None)


def test_handler_receives_session_config_payload(inmemory_session, cfg):
    s = inmemory_session
    seen = {}
    def handler(sess, conf, payload):
        seen["sess"] = sess
        seen["conf"] = conf
        seen["payload"] = payload
        return {"echo": payload}
    jobs.HANDLERS["echo"] = handler
    try:
        j = jobs.enqueue(s, "echo", {"a": 1})
        s.flush()
        jobs.run_pending(s, cfg)
        s.flush()
        assert seen["sess"] is s
        assert seen["conf"] is cfg
        assert seen["payload"] == {"a": 1}
        assert json.loads(j.result) == {"echo": {"a": 1}}
    finally:
        jobs.HANDLERS.pop("echo", None)


def test_unknown_kind_fails_and_logs_error(inmemory_session, cfg):
    s = inmemory_session
    j = jobs.enqueue(s, "mystery", {})
    s.flush()
    # must not raise
    jobs.run_pending(s, cfg)
    s.flush()
    assert j.status == "error"
    result = json.loads(j.result)
    assert "unknown kind" in result["error"]
    entry = _last_activity(s)
    assert entry.outcome == "error"
    assert entry.action == "mystery"


def test_handler_exception_becomes_error_result_and_log(inmemory_session, cfg):
    s = inmemory_session
    def boom(sess, conf, payload):
        raise RuntimeError("kaboom")
    jobs.HANDLERS["boom"] = boom
    try:
        j = jobs.enqueue(s, "boom", {})
        s.flush()
        # must not propagate
        jobs.run_pending(s, cfg)
        s.flush()
        assert j.status == "error"
        result = json.loads(j.result)
        assert "kaboom" in result["error"]
        entry = _last_activity(s)
        assert entry.outcome == "error"
        assert entry.action == "boom"
    finally:
        jobs.HANDLERS.pop("boom", None)


def test_run_pending_drains_all_queued_in_order(inmemory_session, cfg):
    s = inmemory_session
    order: list[str] = []
    def handler(sess, conf, payload):
        order.append(payload["tag"])
        return {"tag": payload["tag"]}
    jobs.HANDLERS["tag"] = handler
    try:
        jobs.enqueue(s, "tag", {"tag": "a"})
        jobs.enqueue(s, "tag", {"tag": "b"})
        jobs.enqueue(s, "tag", {"tag": "c"})
        s.flush()
        jobs.run_pending(s, cfg)
        s.flush()
        assert order == ["a", "b", "c"]
        # all jobs done, none left queued
        queued = s.scalars(
            select(Job).where(Job.status == "queued")
        ).all()
        assert queued == []
    finally:
        jobs.HANDLERS.pop("tag", None)


def test_run_pending_no_handlers_is_noop(inmemory_session, cfg):
    s = inmemory_session
    # empty queue -> returns immediately, no activity logged
    jobs.run_pending(s, cfg)
    assert _last_activity(s) is None


def test_run_pending_persists_error_state_across_commit(tmp_path, cfg):
    """Error state recorded by run_pending survives the session auto-commit,
    and a handler that raises does not roll back the failure recording."""
    from fm.db import init_engine, SessionLocal
    init_engine(f"sqlite:///{tmp_path}/persist.db")
    jobs.HANDLERS["boom"] = lambda sess, conf, payload: (_ for _ in ()).throw(
        RuntimeError("fail")
    )
    try:
        with SessionLocal() as s:
            j = jobs.enqueue(s, "boom", {})
            s.flush()
            jobs.run_pending(s, cfg)
        # new session reads committed state
        with SessionLocal() as s:
            job = s.scalar(select(Job).where(Job.kind == "boom"))
            assert job.status == "error"
            assert "fail" in json.loads(job.result)["error"]
            entry = s.scalar(
                select(ActivityLog).where(ActivityLog.action == "boom")
            )
            assert entry is not None and entry.outcome == "error"
    finally:
        jobs.HANDLERS.pop("boom", None)
