"""Gestion des jobs de synchronisation et de leurs tables."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import scheduler
from ..database import get_db
from ..joblog import write_log
from ..models import MODE_FULL, MODE_INCREMENTAL, MODE_LABELS, Connection, JobRun, SyncJob, TableMapping
from ..sync import is_running, list_columns, list_tables
from ..web import back_url, flash, redirect, render, require_login

router = APIRouter(prefix="/jobs", dependencies=[Depends(require_login)])

UNITS = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}


def _split_interval(seconds: int) -> tuple[int, str]:
    for unit in ("days", "hours", "minutes"):
        if seconds % UNITS[unit] == 0:
            return seconds // UNITS[unit], unit
    return seconds, "seconds"


def _connections(db: Session, kind: str):
    return db.scalars(select(Connection).where(Connection.kind == kind).order_by(Connection.name)).all()


def _get_job(db: Session, job_id: int):
    return db.get(SyncJob, job_id)


@router.get("")
def list_jobs(request: Request, db: Session = Depends(get_db)):
    jobs = db.scalars(select(SyncJob).order_by(SyncJob.name)).all()
    return render(
        request,
        "jobs.html",
        jobs=jobs,
        next_runs={j.id: scheduler.next_run_time(j.id) for j in jobs},
        running={j.id: is_running(j.id) for j in jobs},
    )


def _form(request: Request, db: Session, job: SyncJob):
    value, unit = _split_interval(job.interval_seconds or 3600)
    return render(
        request,
        "job_form.html",
        job=job,
        sources=_connections(db, "mariadb"),
        targets=_connections(db, "postgresql"),
        interval_value=value,
        interval_unit=unit,
    )


@router.get("/new")
def new_job(request: Request, db: Session = Depends(get_db)):
    return _form(request, db, SyncJob(interval_seconds=3600, target_schema="public", enabled=True))


@router.get("/{job_id}/edit")
def edit_job(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = _get_job(db, job_id)
    if job is None:
        return redirect("/jobs")
    return _form(request, db, job)


@router.post("/save")
def save_job(
    request: Request,
    job_id: int = Form(0),
    name: str = Form(...),
    source_id: int = Form(...),
    target_id: int = Form(...),
    target_schema: str = Form("public"),
    interval_value: int = Form(...),
    interval_unit: str = Form("minutes"),
    enabled: bool = Form(False),
    db: Session = Depends(get_db),
):
    job = _get_job(db, job_id) if job_id else SyncJob()
    if job is None:
        return redirect("/jobs")
    source, target = db.get(Connection, source_id), db.get(Connection, target_id)
    if source is None or source.kind != "mariadb" or target is None or target.kind != "postgresql":
        flash(request, "Choisissez une source MariaDB et une cible PostgreSQL.", "err")
        return redirect(f"/jobs/{job_id}/edit" if job_id else "/jobs/new")
    seconds = max(interval_value, 1) * UNITS.get(interval_unit, 60)
    if seconds < 10:
        flash(request, "L'intervalle minimum est de 10 secondes.", "err")
        return redirect(f"/jobs/{job_id}/edit" if job_id else "/jobs/new")

    job.name, job.source_id, job.target_id = name.strip(), source_id, target_id
    job.target_schema = target_schema.strip() or "public"
    job.interval_seconds, job.enabled = seconds, enabled
    if not job_id:
        db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, f"Un job nommé « {name} » existe déjà.", "err")
        return redirect(f"/jobs/{job_id}/edit" if job_id else "/jobs/new")
    db.refresh(job)
    scheduler.schedule_job(job)
    write_log("INFO", f"Job « {job.name} » {'modifié' if job_id else 'créé'} "
                      f"(intervalle {job.interval_label}, {'actif' if job.enabled else 'inactif'}).", job_id=job.id)
    flash(request, f"Job « {job.name} » enregistré.", "ok")
    return redirect(f"/jobs/{job.id}")


@router.get("/{job_id}")
def job_detail(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = _get_job(db, job_id)
    if job is None:
        flash(request, "Job introuvable.", "err")
        return redirect("/jobs")
    runs = db.scalars(
        select(JobRun).where(JobRun.job_id == job_id).order_by(JobRun.id.desc()).limit(20)
    ).all()
    source_tables, source_error = [], None
    try:
        source_tables = list_tables(job.source)
    except Exception as exc:
        source_error = str(exc)
    mapped = {m.source_table for m in job.tables}
    return render(
        request,
        "job_detail.html",
        job=job,
        runs=runs,
        next_run=scheduler.next_run_time(job.id),
        running=is_running(job.id),
        source_tables=source_tables,
        unmapped=[t for t in source_tables if t not in mapped],
        source_error=source_error,
        modes=MODE_LABELS,
    )


@router.get("/{job_id}/columns")
def source_columns(job_id: int, table: str, db: Session = Depends(get_db)):
    """Colonnes d'une table source (utilisé par le formulaire d'ajout de table)."""
    job = _get_job(db, job_id)
    if job is None:
        return JSONResponse({"error": "Job introuvable"}, status_code=404)
    try:
        return {"columns": list_columns(job.source, table)}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@router.post("/{job_id}/run")
def run_job_now(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = _get_job(db, job_id)
    if job is None:
        return redirect("/jobs")
    if is_running(job_id):
        flash(request, f"Le job « {job.name} » est déjà en cours d'exécution.", "warn")
    else:
        scheduler.run_now(job_id)
        flash(request, f"Exécution du job « {job.name} » lancée.", "ok")
    return redirect(back_url(request, f"/jobs/{job_id}"))


@router.post("/{job_id}/toggle")
def toggle_job(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = _get_job(db, job_id)
    if job is None:
        return redirect("/jobs")
    job.enabled = not job.enabled
    db.commit()
    scheduler.schedule_job(job)
    state = "activé" if job.enabled else "désactivé"
    write_log("INFO", f"Job « {job.name} » {state}.", job_id=job.id)
    flash(request, f"Job « {job.name} » {state}.", "ok")
    return redirect(back_url(request, f"/jobs/{job_id}"))


@router.post("/{job_id}/delete")
def delete_job(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = _get_job(db, job_id)
    if job is None:
        return redirect("/jobs")
    if is_running(job_id):
        flash(request, "Impossible de supprimer un job en cours d'exécution.", "err")
        return redirect(f"/jobs/{job_id}")
    scheduler.unschedule_job(job_id)
    name = job.name
    db.delete(job)
    db.commit()
    write_log("INFO", f"Job « {name} » supprimé.")
    flash(request, f"Job « {name} » supprimé.", "ok")
    return redirect("/jobs")


# --------------------------------------------------------------------------- tables


@router.post("/{job_id}/tables")
def add_table(
    job_id: int,
    request: Request,
    source_table: str = Form(...),
    target_table: str = Form(""),
    mode: str = Form(MODE_FULL),
    incremental_column: str = Form(""),
    key_columns: str = Form(""),
    db: Session = Depends(get_db),
):
    job = _get_job(db, job_id)
    if job is None:
        return redirect("/jobs")
    source_table = source_table.strip()
    if mode not in MODE_LABELS:
        mode = MODE_FULL
    if mode == MODE_INCREMENTAL and not incremental_column.strip():
        flash(request, "Le mode incrémental nécessite une colonne de suivi (ex. id ou updated_at).", "err")
        return redirect(f"/jobs/{job_id}")
    db.add(
        TableMapping(
            job_id=job.id,
            source_table=source_table,
            target_table=(target_table.strip() or source_table),
            mode=mode,
            incremental_column=incremental_column.strip() or None,
            key_columns=key_columns.strip() or None,
        )
    )
    db.commit()
    write_log("INFO", f"Table « {source_table} » ajoutée au job ({MODE_LABELS[mode]}).", job_id=job.id)
    flash(request, f"Table « {source_table} » ajoutée.", "ok")
    return redirect(f"/jobs/{job_id}")


@router.post("/{job_id}/tables/add-all")
def add_all_tables(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = _get_job(db, job_id)
    if job is None:
        return redirect("/jobs")
    try:
        tables = list_tables(job.source)
    except Exception as exc:
        flash(request, f"Impossible de lister les tables source : {exc}", "err")
        return redirect(f"/jobs/{job_id}")
    mapped = {m.source_table for m in job.tables}
    added = [t for t in tables if t not in mapped]
    for t in added:
        db.add(TableMapping(job_id=job.id, source_table=t, target_table=t, mode=MODE_FULL))
    db.commit()
    if added:
        write_log("INFO", f"{len(added)} table(s) ajoutée(s) au job en mode complet.", job_id=job.id)
    flash(request, f"{len(added)} table(s) ajoutée(s).", "ok")
    return redirect(f"/jobs/{job_id}")


def _get_mapping(db: Session, job_id: int, mapping_id: int):
    m = db.get(TableMapping, mapping_id)
    return m if m is not None and m.job_id == job_id else None


@router.post("/{job_id}/tables/{mapping_id}/update")
def update_table(
    job_id: int,
    mapping_id: int,
    request: Request,
    target_table: str = Form(...),
    mode: str = Form(MODE_FULL),
    incremental_column: str = Form(""),
    key_columns: str = Form(""),
    db: Session = Depends(get_db),
):
    m = _get_mapping(db, job_id, mapping_id)
    if m is None:
        return redirect(f"/jobs/{job_id}")
    if mode == MODE_INCREMENTAL and not incremental_column.strip():
        flash(request, "Le mode incrémental nécessite une colonne de suivi.", "err")
        return redirect(f"/jobs/{job_id}")
    if incremental_column.strip() != (m.incremental_column or "") or mode != m.mode:
        m.last_value = None  # la colonne a changé : on repart de zéro
    m.target_table = target_table.strip() or m.source_table
    m.mode = mode if mode in MODE_LABELS else MODE_FULL
    m.incremental_column = incremental_column.strip() or None
    m.key_columns = key_columns.strip() or None
    db.commit()
    write_log("INFO", f"Table « {m.source_table} » modifiée.", job_id=job_id)
    flash(request, f"Table « {m.source_table} » modifiée.", "ok")
    return redirect(f"/jobs/{job_id}")


@router.post("/{job_id}/tables/{mapping_id}/toggle")
def toggle_table(job_id: int, mapping_id: int, request: Request, db: Session = Depends(get_db)):
    m = _get_mapping(db, job_id, mapping_id)
    if m is not None:
        m.enabled = not m.enabled
        db.commit()
        flash(request, f"Table « {m.source_table} » {'activée' if m.enabled else 'désactivée'}.", "ok")
    return redirect(f"/jobs/{job_id}")


@router.post("/{job_id}/tables/{mapping_id}/reset")
def reset_table(job_id: int, mapping_id: int, request: Request, db: Session = Depends(get_db)):
    m = _get_mapping(db, job_id, mapping_id)
    if m is not None:
        m.last_value = None
        db.commit()
        write_log("INFO", f"Curseur incrémental de « {m.source_table} » réinitialisé.", job_id=job_id)
        flash(request, f"Curseur de « {m.source_table} » réinitialisé : tout sera relu au prochain passage.", "ok")
    return redirect(f"/jobs/{job_id}")


@router.post("/{job_id}/tables/{mapping_id}/delete")
def delete_table(job_id: int, mapping_id: int, request: Request, db: Session = Depends(get_db)):
    m = _get_mapping(db, job_id, mapping_id)
    if m is not None:
        db.delete(m)
        db.commit()
        write_log("INFO", f"Table « {m.source_table} » retirée du job.", job_id=job_id)
        flash(request, f"Table « {m.source_table} » retirée (la table cible n'est pas supprimée).", "ok")
    return redirect(f"/jobs/{job_id}")
