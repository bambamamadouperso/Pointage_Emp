"""Planification des jobs de synchronisation (APScheduler)."""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete, select, update

from .config import settings
from .database import SessionLocal
from .joblog import write_log
from .models import JobRun, LogEntry, SyncJob, utcnow
from . import sync
from .sync import cancel_overdue, run_job

logger = logging.getLogger("scheduler")

scheduler = BackgroundScheduler(
    # Le chien de garde a son propre fil : il doit tourner même si tous les autres sont bloqués.
    executors={"default": ThreadPoolExecutor(settings.max_workers), "watchdog": ThreadPoolExecutor(1)},
    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
)


def _aps_id(job_id: int) -> str:
    return f"sync-{job_id}"


def schedule_job(job: SyncJob) -> None:
    """Ajoute, met à jour ou retire la planification d'un job selon son état."""
    if not scheduler.running:
        return
    aps_id = _aps_id(job.id)
    if not job.enabled:
        unschedule_job(job.id)
        return
    scheduler.add_job(
        run_job,
        IntervalTrigger(seconds=max(job.interval_seconds, 10)),
        id=aps_id,
        name=job.name,
        args=[job.id, "schedule"],
        replace_existing=True,
    )


def unschedule_job(job_id: int) -> None:
    if scheduler.running and scheduler.get_job(_aps_id(job_id)):
        scheduler.remove_job(_aps_id(job_id))


def run_now(job_id: int, trigger: str = "manual", **options) -> None:
    """Lance immédiatement un job en arrière-plan (options : mapping_id, reset, recreate)."""
    if scheduler.running:
        scheduler.add_job(run_job, args=[job_id, trigger], kwargs=options,
                          id=f"{trigger}-{job_id}-{utcnow().timestamp()}")
    else:  # planificateur désactivé (tests) : exécution synchrone
        run_job(job_id, trigger, **options)


def _run_retry(job_id: int) -> None:
    """Relance automatique : ignorée si le job a été désactivé entre-temps."""
    with SessionLocal() as db:
        job = db.get(SyncJob, job_id)
        if job is None or not job.enabled:
            return
    run_job(job_id, "retry")


def schedule_retry(job_id: int, delay_minutes: int) -> None:
    if not scheduler.running:
        return
    when = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes, seconds=0 if delay_minutes else 5)
    scheduler.add_job(_run_retry, DateTrigger(run_date=when), args=[job_id], id=f"retry-{job_id}",
                      replace_existing=True, misfire_grace_time=None)


def suspend(job_id: int) -> None:
    unschedule_job(job_id)
    if scheduler.running and scheduler.get_job(f"retry-{job_id}"):
        scheduler.remove_job(f"retry-{job_id}")


def next_run_time(job_id: int) -> Optional[datetime]:
    if not scheduler.running:
        return None
    aps_job = scheduler.get_job(_aps_id(job_id))
    if aps_job is None or aps_job.next_run_time is None:
        return None
    # Toutes les dates de l'application sont stockées en UTC naïf.
    return aps_job.next_run_time.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def purge_old_logs() -> None:
    limit = utcnow() - timedelta(days=settings.log_retention_days)
    with SessionLocal() as db:
        logs = db.execute(delete(LogEntry).where(LogEntry.created_at < limit)).rowcount
        runs = db.execute(
            delete(JobRun).where(JobRun.started_at < limit, JobRun.status != "running")
        ).rowcount
        db.commit()
    if logs or runs:
        write_log("INFO", f"Purge : {logs} log(s) et {runs} exécution(s) de plus de "
                          f"{settings.log_retention_days} jours supprimés.")


def notify_after_sync(job_id: int) -> None:
    """Mails de confirmation de badge : traités en arrière-plan après une synchronisation réussie."""
    from . import mails

    scheduler.add_job(mails.process, id="mails-badge", replace_existing=True, max_instances=1,
                      misfire_grace_time=600)


def start() -> None:
    if scheduler.running:
        return
    with SessionLocal() as db:
        # Les exécutions restées « running » ont été interrompues par un arrêt de l'application.
        n = db.execute(
            update(JobRun)
            .where(JobRun.status == "running")
            .values(status="error", finished_at=utcnow(), message="Interrompu (arrêt de l'application).")
        ).rowcount
        db.commit()
        jobs = db.scalars(select(SyncJob)).all()
    if n:
        write_log("WARNING", f"{n} exécution(s) interrompue(s) lors du dernier arrêt.")
    sync.schedule_retry, sync.suspend_job = schedule_retry, suspend
    sync.after_success = notify_after_sync
    scheduler.start()
    for job in jobs:
        schedule_job(job)
    scheduler.add_job(cancel_overdue, IntervalTrigger(minutes=1),
                      id="watchdog", executor="watchdog", replace_existing=True)
    scheduler.add_job(purge_old_logs, IntervalTrigger(hours=6), id="purge-logs", replace_existing=True,
                      next_run_time=datetime.now())
    from . import digests  # résumés par mail aux responsables (quotidien / hebdomadaire)

    scheduler.add_job(digests.run_due, IntervalTrigger(minutes=10), id="digests", replace_existing=True,
                      next_run_time=datetime.now() + timedelta(minutes=1))
    write_log("INFO", f"Planificateur démarré ({sum(j.enabled for j in jobs)} job(s) actif(s)).")


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
