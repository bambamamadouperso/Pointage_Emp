"""Gestion des connexions aux bases sources (MariaDB) et cibles (PostgreSQL)."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..crypto import encrypt
from ..database import get_db
from ..joblog import write_log
from ..models import DEFAULT_PORTS, KIND_LABELS, Connection, SyncJob
from ..sync import list_tables, test_connection
from ..web import flash, redirect, render, require_login

router = APIRouter(prefix="/connections", dependencies=[Depends(require_login)])


@router.get("")
def list_connections(request: Request, db: Session = Depends(get_db)):
    conns = db.scalars(select(Connection).order_by(Connection.kind, Connection.name)).all()
    return render(request, "connections.html", connections=conns)


@router.get("/new")
def new_connection(request: Request, kind: str = "mariadb"):
    conn = Connection(kind=kind if kind in KIND_LABELS else "mariadb", host="", database="", username="")
    conn.port = DEFAULT_PORTS.get(conn.kind, 3306)
    return render(request, "connection_form.html", conn=conn, kinds=KIND_LABELS)


@router.get("/{conn_id}/edit")
def edit_connection(conn_id: int, request: Request, db: Session = Depends(get_db)):
    conn = db.get(Connection, conn_id)
    if conn is None:
        flash(request, "Connexion introuvable.", "err")
        return redirect("/connections")
    return render(request, "connection_form.html", conn=conn, kinds=KIND_LABELS)


@router.post("/save")
def save_connection(
    request: Request,
    conn_id: int = Form(0),
    name: str = Form(...),
    kind: str = Form(...),
    host: str = Form(...),
    port: int = Form(...),
    database: str = Form(...),
    username: str = Form(...),
    password: str = Form(""),
    action: str = Form("save"),
    db: Session = Depends(get_db),
):
    existing = db.get(Connection, conn_id) if conn_id else None
    if conn_id and existing is None:
        flash(request, "Connexion introuvable.", "err")
        return redirect("/connections")
    if kind not in KIND_LABELS:
        flash(request, "Type de base invalide.", "err")
        return redirect("/connections")
    values = dict(
        name=name.strip(), kind=kind, host=host.strip(), port=port,
        database=database.strip(), username=username.strip(),
    )
    password_enc = encrypt(password) if password or not existing else existing.password_enc

    if action == "test":
        # Test avec les valeurs saisies, sans rien enregistrer.
        probe = Connection(id=conn_id or None, password_enc=password_enc, **values)
        try:
            version = test_connection(probe)
            flash(request, f"Connexion réussie : {version}", "ok")
        except Exception as exc:
            flash(request, f"Échec de connexion : {exc}", "err")
        return render(request, "connection_form.html", conn=probe, kinds=KIND_LABELS, keep_password=password)

    conn = existing or Connection()
    for key, value in values.items():
        setattr(conn, key, value)
    conn.password_enc = password_enc
    if not conn_id:
        db.add(conn)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, f"Une connexion nommée « {name} » existe déjà.", "err")
        return render(request, "connection_form.html", conn=conn, kinds=KIND_LABELS)
    write_log("INFO", f"Connexion « {conn.name} » {'modifiée' if conn_id else 'créée'}.")
    flash(request, f"Connexion « {conn.name} » enregistrée.", "ok")
    return redirect("/connections")


@router.post("/{conn_id}/test")
def test_saved_connection(conn_id: int, request: Request, db: Session = Depends(get_db)):
    conn = db.get(Connection, conn_id)
    if conn is None:
        flash(request, "Connexion introuvable.", "err")
    else:
        try:
            version = test_connection(conn)
            count = len(list_tables(conn))
            flash(request, f"« {conn.name} » : connexion réussie ({version}) — {count} table(s).", "ok")
        except Exception as exc:
            flash(request, f"« {conn.name} » : échec de connexion : {exc}", "err")
    return redirect("/connections")


@router.post("/{conn_id}/delete")
def delete_connection(conn_id: int, request: Request, db: Session = Depends(get_db)):
    conn = db.get(Connection, conn_id)
    if conn is None:
        return redirect("/connections")
    used = db.scalars(
        select(SyncJob.name).where(or_(SyncJob.source_id == conn_id, SyncJob.target_id == conn_id))
    ).all()
    if used:
        flash(request, f"Connexion utilisée par : {', '.join(used)}. Supprimez d'abord ces jobs.", "err")
        return redirect("/connections")
    db.delete(conn)
    db.commit()
    write_log("INFO", f"Connexion « {conn.name} » supprimée.")
    flash(request, f"Connexion « {conn.name} » supprimée.", "ok")
    return redirect("/connections")
