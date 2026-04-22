from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.models import Job, JobLog, JobStatus


JOB_COLLECT = "collect_content"
JOB_CLEAN = "clean_content"
JOB_AI = "ai_generate"
JOB_MEDIA = "media_generate"
JOB_PUBLISH_WP = "publish_wordpress"
JOB_ROBOT_RUN = "robot_run"
JOB_FACEBOOK_PUBLISH = "publish_facebook"


def enqueue_job(
    db,
    *,
    user_id: str,
    job_type: str,
    payload: dict[str, Any],
    profile_id: str | None = None,
    post_id: str | None = None,
    run_at: datetime | None = None,
    max_attempts: int = 3,
) -> Job:
    job = Job(
        user_id=user_id,
        profile_id=profile_id,
        post_id=post_id,
        type=job_type,
        payload_json=payload,
        status=JobStatus.queued,
        max_attempts=max_attempts,
        run_at=run_at or datetime.utcnow(),
        attempts=0,
    )
    db.add(job)
    db.flush()
    return job


def log_event(
    db,
    *,
    user_id: str,
    stage: str,
    status: str,
    message: str,
    meta: dict[str, Any] | None = None,
    profile_id: str | None = None,
    post_id: str | None = None,
) -> JobLog:
    log = JobLog(
        user_id=user_id,
        profile_id=profile_id,
        post_id=post_id,
        stage=stage,
        status=status,
        message=message,
        meta_json=meta or {},
    )
    db.add(log)
    return log


def schedule_retry(job: Job) -> datetime:
    attempt = job.attempts + 1
    delay_seconds = min(60 * 30, 5 * (2 ** (attempt - 1)))
    return datetime.utcnow() + timedelta(seconds=delay_seconds)


_STALE_LOCK_MINUTES = 3  # libera locks travados após 3 min


def get_due_job(db, *, worker_id: str) -> Job | None:
    now = datetime.utcnow()
    stale_cutoff = now - timedelta(minutes=_STALE_LOCK_MINUTES)

    # Libera jobs presos em running por mais de 3 min (timeout/crash do worker)
    try:
        stuck = db.scalars(
            select(Job).where(
                Job.status == JobStatus.running,
                Job.locked_at <= stale_cutoff,
            )
        ).all()
        for j in stuck:
            j.status = JobStatus.queued
            j.locked_at = None
            j.locked_by = None
            j.updated_at = now
            db.add(j)
        if stuck:
            db.flush()
    except Exception:
        pass  # não bloqueia seleção do próximo job

    job = db.scalar(
        select(Job)
        .where(
            Job.status == JobStatus.queued,
            Job.run_at <= now,
            Job.attempts < Job.max_attempts,
            Job.locked_at.is_(None),
        )
        .order_by(Job.run_at.asc())
        .limit(1)
        # sem FOR UPDATE SKIP LOCKED — incompatível com pg8000 neste contexto
    )
    if not job:
        return None
    job.status = JobStatus.running
    job.locked_at = now
    job.locked_by = worker_id
    job.updated_at = now
    db.add(job)
    return job
