# fm/service/jobs.py
"""Job queue + activity log service.

Jobs move through statuses: queued -> running -> (done | error | awaiting_input).
``run_pending`` drains the queue by dispatching each job to a registered handler
in ``HANDLERS``. Each handler runs inside a savepoint; on exception the
savepoint is rolled back (discarding the handler's partial DB writes) before
the failure is recorded as a job ``error`` result plus an
``ActivityLog(outcome="error")`` row. Handler exceptions, unknown kinds, and
non-serializable results never propagate to the caller, so the session's
auto-commit-on-clean-exit policy persists the recorded error state rather
than rolling back partial work.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Config
from ..db import SessionLocal
from ..models import ActivityLog, Job


log = logging.getLogger(__name__)

# Handlers registered by later tasks. A handler receives the live session, the
# resolved Config, and the decoded job payload, and returns a result dict that
# becomes the job's `result` on success.
HANDLERS: dict[str, Callable[[Session, Config, dict], dict]] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(session: Session, kind: str, payload: dict) -> Job:
    """Create a queued job for ``kind`` carrying ``payload``."""
    job = Job(kind=kind, payload=json.dumps(payload), status="queued")
    session.add(job)
    session.flush()
    return job


def claim_next(
    session: Session,
    exclude: set[int] | None = None,
    kinds: set[str] | None = None,
) -> Job | None:
    """Atomically claim the oldest queued job (FIFO): queued -> running.

    ``exclude`` is an optional set of job IDs to skip — used by
    ``run_pending`` to leap over jobs whose kind has no server-side
    handler.  ``kinds`` restricts the claim to a set of job kinds.

    Returns ``None`` when no queued jobs remain.
    """
    stmt = (
        select(Job)
        .where(Job.status == "queued")
        .order_by(Job.id)
        .limit(1)
    )
    if exclude:
        stmt = stmt.where(~Job.id.in_(exclude))
    if kinds:
        stmt = stmt.where(Job.kind.in_(kinds))
    job = session.scalars(stmt).first()
    if job is None:
        return None
    job.status = "running"
    session.flush()
    return job


def complete(job: Job, result: dict) -> None:
    """Mark ``job`` done and store its success ``result``."""
    job.status = "done"
    job.result = json.dumps(result)
    job.finished_at = _now()


def fail(job: Job, error: dict) -> None:
    """Mark ``job`` errored and store the ``error`` detail."""
    job.status = "error"
    job.result = json.dumps(error)
    job.finished_at = _now()


def await_input(job: Job, options: list[str], *, extra: dict | None = None) -> None:
    """Park ``job`` awaiting user input from ``options``.

    ``extra`` carries additional context (e.g. ``{"busy": {...}}`` from
    the busy-check) that is merged into the stored result so the UI can
    display it.
    """
    stored: dict = {"options": options}
    if extra:
        stored.update(extra)
    job.status = "awaiting_input"
    job.result = json.dumps(stored)


def log_activity(
    session: Session, action: str, outcome: str, detail: dict
) -> ActivityLog:
    """Append an ``ActivityLog`` row.

    ``outcome`` is one of ok|warn|error to match the column's domain.
    """
    entry = ActivityLog(
        ts=_now(), action=action, outcome=outcome, detail=json.dumps(detail)
    )
    session.add(entry)
    return entry


def run_pending(session: Session, config: Config) -> None:
    """Drain the queue: claim and run each queued job through ``HANDLERS``.

    Each handler runs inside a savepoint (``session.begin_nested()``). On
    exception the savepoint is rolled back, discarding the handler's partial
    DB writes, before ``fail()`` + ``ActivityLog(error)`` record the failure
    — so the job status transition and error log survive while partial
    handler state does not. Serialization failures (non-JSON-serializable
    handler results) are contained the same way. Nothing escapes to the
    caller, so the surrounding session commits the recorded state cleanly.

    A ``session.commit()`` is issued after each job state transition
    (claim → running, complete, fail, await_input) so that concurrent
    connections — e.g. a polling request from the push-modal overlay —
    can observe intermediate states while the drain is still in progress.

    Jobs whose ``kind`` is not in ``HANDLERS`` are left ``queued``
    without error so registered kinds queued behind them still process.
    """
    skipped: set[int] = set()
    while True:
        job = claim_next(session, exclude=skipped)
        if job is None:
            return
        # Commit the "running" state so concurrent polling connections
        # can observe the transition while the handler executes.
        session.commit()
        handler = HANDLERS.get(job.kind)
        if handler is None:
            # No server-side handler for this kind — leave queued.
            job.status = "queued"
            session.flush()
            log.debug("job %s: no handler for kind %r; leaving queued",
                       job.id, job.kind)
            skipped.add(job.id)
            continue
        payload = json.loads(job.payload) if job.payload else {}
        try:
            nested = session.begin_nested()
            try:
                result = handler(session, config, payload)
            except Exception:
                nested.rollback()
                raise
        except Exception as exc:  # noqa: BLE001 - record, never propagate
            detail = {"error": str(exc), "job_id": job.id, "kind": job.kind}
            fail(job, detail)
            log_activity(session, job.kind, "error", detail)
            session.commit()
            log.exception("job %s (%s) failed", job.id, job.kind)
            continue
        # Handler can request to park the job awaiting user input (e.g. busy
        # printer).  The sentinel result {"_awaiting_input": True, ...} tells
        # run_pending to call await_input instead of complete.
        if isinstance(result, dict) and result.get("_awaiting_input"):
            options = result.get("options", [])
            await_input(job, options, extra={"busy": result.get("busy")} if result.get("busy") else None)
            log_activity(
                session, job.kind, "warn",
                {"job_id": job.id, "busy": result.get("busy")},
            )
            session.commit()
            continue
        # Release the savepoint (commit it) once the handler result is known
        # good; serialization of the result is contained below.
        try:
            complete(job, result)
            log_activity(
                session, job.kind, "ok", {"job_id": job.id, "result": result}
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 - e.g. non-serializable result
            nested.rollback()
            detail = {"error": str(exc), "job_id": job.id, "kind": job.kind}
            fail(job, detail)
            log_activity(session, job.kind, "error", detail)
            session.commit()
            log.exception("job %s (%s) result recording failed", job.id, job.kind)


def drain_background(config: Config) -> None:
    """Drain the job queue in a background daemon thread.

    Opens its own ``SessionLocal`` session so job state transitions are
    committed independently of the calling request's session — this lets
    polling endpoints observe intermediate states (queued → running →
    done/error/awaiting_input) while the drain is still in progress.

    Tests monkeypatch this to run synchronously (see ``_sync_drain`` in
    test fixtures) so assertions can check the final job state immediately.
    """
    t = threading.Thread(target=_drain_worker, args=(config,), daemon=True)
    t.start()


def _drain_worker(config: Config) -> None:
    with SessionLocal() as session:
        run_pending(session, config)
