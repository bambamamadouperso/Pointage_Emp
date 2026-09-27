"""Tableau de bord, historique des exécutions et consultation des logs."""
import csv
import io
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import scheduler
from ..database import get_db
from ..joblog import LEVELS
from ..models import Connection, JobRun, LogEntry, SyncJob, utcnow
from ..sync import is_running
from ..web import fmt_dt, redirect, render, require_login

router = APIRouter(dependencies=[Depends(require_login)])

PAGE_SIZE = 100


def _as_int(value: str) -> Optional[int]:
    """Les filtres vides arrivent sous forme de chaîne vide."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    since = utcnow() - timedelta(hours=24)
    jobs = db.scalars(select(SyncJob).order_by(SyncJob.name)).all()
    counts = dict(
        db.execute(
            select(JobRun.status, func.count()).where(JobRun.started_at >= since).group_by(JobRun.status)
        ).all()
    )
    rows_24h = db.scalar(select(func.coalesce(func.sum(JobRun.rows_written), 0)).where(JobRun.started_at >= since))
    stats = {
        "connections": db.scalar(select(func.count()).select_from(Connection)),
        "jobs": len(jobs),
        "jobs_enabled": sum(1 for j in jobs if j.enabled),
        "runs_ok": counts.get("success", 0),
        "runs_partial": counts.get("partial", 0),
        "runs_err": counts.get("error", 0),
        "running": sum(1 for j in jobs if is_running(j.id)),
        "rows_24h": rows_24h,
    }
    recent_runs = db.scalars(select(JobRun).order_by(JobRun.id.desc()).limit(10)).all()
    recent_errors = db.scalars(
        select(LogEntry).where(LogEntry.level == "ERROR").order_by(LogEntry.id.desc()).limit(8)
    ).all()
    return render(
        request,
        "dashboard.html",
        stats=stats,
        jobs=jobs,
        next_runs={j.id: scheduler.next_run_time(j.id) for j in jobs},
        running={j.id: is_running(j.id) for j in jobs},
        recent_runs=recent_runs,
        recent_errors=recent_errors,
        scheduler_running=scheduler.scheduler.running,
    )


@router.get("/runs")
def runs(request: Request, job_id: str = "", status: str = "", page: int = 1, db: Session = Depends(get_db)):
    job_id = _as_int(job_id)
    q = select(JobRun)
    if job_id:
        q = q.where(JobRun.job_id == job_id)
    if status:
        q = q.where(JobRun.status == status)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    page = max(page, 1)
    items = db.scalars(q.order_by(JobRun.id.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    return render(
        request,
        "runs.html",
        runs=items,
        jobs=db.scalars(select(SyncJob).order_by(SyncJob.name)).all(),
        filters={"job_id": job_id, "status": status},
        page=page,
        pages=max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
        total=total,
    )


@router.get("/runs/{run_id}")
def run_detail(run_id: int, request: Request, db: Session = Depends(get_db)):
    run = db.get(JobRun, run_id)
    if run is None:
        return redirect("/runs")
    logs = db.scalars(select(LogEntry).where(LogEntry.run_id == run_id).order_by(LogEntry.id)).all()
    return render(request, "run_detail.html", run=run, logs=logs)


def _log_query(job_id: Optional[int], level: str, q: str, table: str):
    query = select(LogEntry)
    if job_id:
        query = query.where(LogEntry.job_id == job_id)
    if level:
        # Niveau minimum : WARNING inclut ERROR, etc.
        allowed = LEVELS[LEVELS.index(level):] if level in LEVELS else [level]
        query = query.where(LogEntry.level.in_(allowed))
    if q:
        query = query.where(LogEntry.message.ilike(f"%{q}%"))
    if table:
        query = query.where(LogEntry.table_name == table)
    return query


@router.get("/logs")
def logs(
    request: Request,
    job_id: str = "",
    level: str = "",
    q: str = "",
    table: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
):
    job_id = _as_int(job_id)
    query = _log_query(job_id, level, q, table)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    page = max(page, 1)
    items = db.scalars(query.order_by(LogEntry.id.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    return render(
        request,
        "logs.html",
        logs=items,
        jobs=db.scalars(select(SyncJob).order_by(SyncJob.name)).all(),
        levels=LEVELS,
        filters={"job_id": job_id, "level": level, "q": q, "table": table},
        page=page,
        pages=max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
        total=total,
    )


@router.get("/logs/export.csv")
def export_logs(job_id: str = "", level: str = "", q: str = "", table: str = "",
                db: Session = Depends(get_db)):
    query = _log_query(_as_int(job_id), level, q, table).order_by(LogEntry.id.desc()).limit(50000)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["date", "niveau", "job", "execution", "table", "message"])
    for log in db.scalars(query):
        writer.writerow([fmt_dt(log.created_at), log.level, log.job.name if log.job else "",
                         log.run_id or "", log.table_name or "", log.message])
    return Response(
        "\ufeff" + buf.getvalue(),  # BOM pour une ouverture correcte dans Excel
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=logs.csv"},
    )
