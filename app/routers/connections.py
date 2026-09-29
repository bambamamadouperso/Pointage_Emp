"""Gestion des connexions : sources (MariaDB, Google Sheets) et cibles (PostgreSQL)."""
import json
import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import dbadmin, gsheet, smartsheet
from ..crypto import decrypt, encrypt
from ..database import get_db
from ..errors import friendly
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
    return _form(request, conn)


def _form(request: Request, conn: Connection, **extra):
    return render(request, "connection_form.html", conn=conn, kinds=KIND_LABELS, ss_hosts=smartsheet.HOSTS,
                  sheet_auths=gsheet.AUTH_LABELS, sa_email=gsheet.service_account_email(conn)
                  if conn.is_gsheet and conn.password_enc else "", **extra)


@router.get("/{conn_id}/edit")
def edit_connection(conn_id: int, request: Request, db: Session = Depends(get_db)):
    conn = db.get(Connection, conn_id)
    if conn is None:
        flash(request, "Connexion introuvable.", "err")
        return redirect("/connections")
    return _form(request, conn)


@router.post("/save")
def save_connection(
    request: Request,
    conn_id: int = Form(0),
    name: str = Form(""),
    kind: str = Form(...),
    host: str = Form(""),
    port: int = Form(0),
    database: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    sheet_link: str = Form(""),
    sheet_auth: str = Form(gsheet.AUTH_PUBLIC),
    sa_json: str = Form(""),
    ss_host: str = Form(smartsheet.DEFAULT_HOST),
    ss_sheets: str = Form(""),
    ss_token: str = Form(""),
    options: str = Form(""),
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

    error = None
    if kind == "gsheet":
        # Google Sheets : database = identifiant du classeur, username = mode d'accès,
        # mot de passe = clé JSON du compte de service.
        host, port, database = "docs.google.com", 443, gsheet.parse_spreadsheet_id(sheet_link)
        username = sheet_auth if sheet_auth in gsheet.AUTH_LABELS else gsheet.AUTH_PUBLIC
        password = ""
        if username == gsheet.AUTH_SERVICE_ACCOUNT:
            if sa_json.strip():
                try:
                    info = json.loads(sa_json)
                    if not info.get("client_email") or not info.get("private_key"):
                        raise ValueError
                    password = sa_json.strip()
                except (ValueError, AttributeError):
                    error = "Clé JSON invalide : collez le contenu complet du fichier .json du compte de service."
            elif not (existing and existing.is_gsheet and existing.username == gsheet.AUTH_SERVICE_ACCOUNT):
                error = "Collez la clé JSON du compte de service."
        if not database:
            error = "Indiquez le lien ou l'identifiant du classeur Google Sheets."
    elif kind == "smartsheet":
        # Smartsheet : host = serveur de l'API, database = feuille(s), mot de passe = jeton d'accès API.
        host = ss_host if ss_host in smartsheet.HOSTS else smartsheet.DEFAULT_HOST
        port, username, password = 443, "token", ss_token.strip()
        database = ", ".join(r.strip() for r in re.split(r"[,;\n]+", ss_sheets) if r.strip())
        if not password and not (existing and existing.is_smartsheet and existing.password_enc):
            error = "Collez le jeton d'accès API Smartsheet."
        elif not database and action != "test":
            error = ("Indiquez la ou les feuilles à lire (identifiant ou nom). « Tester la connexion » sans feuille "
                     "liste les feuilles accessibles avec le jeton.")
    elif not (host.strip() and port and database.strip() and username.strip()):
        error = "Hôte, port, base de données et utilisateur sont obligatoires."

    if action != "test" and not name.strip():
        error = "Donnez un nom à la connexion (ex. pointeuse-hfsql)."
    values = dict(
        name=name.strip(), kind=kind, host=host.strip(), port=port,
        database=database.strip(), username=username.strip(),
        options=(options.strip() or None) if kind == "hfsql" else None,
    )
    if kind == "gsheet" and username == gsheet.AUTH_PUBLIC:
        password_enc = ""
    else:
        password_enc = encrypt(password) if password or not existing else existing.password_enc
    if error:
        flash(request, error, "err")
        return _form(request, Connection(id=conn_id or None, password_enc=password_enc, **values),
                     keep_password=password)

    if action == "test":
        # Test avec les valeurs saisies, sans rien enregistrer.
        probe = Connection(id=conn_id or None, password_enc=password_enc, **values)
        try:
            if probe.is_odbc:
                from ..hfsql import reset_pool

                reset_pool()  # paramètres peut-être modifiés : on teste une connexion neuve
            version = test_connection(probe)
            flash(request, f"Connexion réussie : {version}", "ok")
        except Exception as exc:
            flash(request, f"Échec de connexion : {friendly(exc)}", "err")
        return _form(request, probe, keep_password=password)

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
        return _form(request, conn)
    write_log("INFO", f"Connexion « {conn.name} » {'modifiée' if conn_id else 'créée'}.")
    flash(request, f"Connexion « {conn.name} » enregistrée.", "ok")
    return redirect("/connections")


def _server_password(db: Session, conn_id: int, kind: str, password: str) -> str:
    """Mot de passe saisi, ou à défaut celui déjà enregistré pour cette connexion."""
    if password or not conn_id:
        return password
    existing = db.get(Connection, conn_id)
    return decrypt(existing.password_enc) if existing is not None and existing.kind == kind else ""


@router.post("/databases")
def server_databases(
    conn_id: int = Form(0),
    kind: str = Form(...),
    host: str = Form(""),
    port: int = Form(0),
    database: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    """Liste les bases du serveur saisi dans le formulaire (JSON)."""
    if kind not in ("postgresql", "mariadb") or not host.strip() or not username.strip():
        return JSONResponse({"error": "Renseignez d'abord l'hôte, le port, l'utilisateur et le mot de passe."},
                            status_code=400)
    try:
        names = dbadmin.list_databases(kind, host.strip(), port or DEFAULT_PORTS[kind], username.strip(),
                                       _server_password(db, conn_id, kind, password), database.strip())
    except dbadmin.DbAdminError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"databases": names}


@router.post("/create-database")
def server_create_database(
    request: Request,
    conn_id: int = Form(0),
    host: str = Form(""),
    port: int = Form(0),
    username: str = Form(""),
    password: str = Form(""),
    new_database: str = Form(""),
    db: Session = Depends(get_db),
):
    """Crée une base PostgreSQL sur le serveur saisi dans le formulaire (JSON)."""
    if not host.strip() or not username.strip():
        return JSONResponse({"error": "Renseignez d'abord l'hôte, le port, l'utilisateur et le mot de passe."},
                            status_code=400)
    name = new_database.strip()
    try:
        dbadmin.create_database(host.strip(), port or 5432, username.strip(),
                                _server_password(db, conn_id, "postgresql", password), name)
    except dbadmin.DbAdminError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    write_log("INFO", f"Base PostgreSQL « {name} » créée sur {host.strip()} par {request.session.get('user')}.")
    return {"ok": True, "database": name, "message": f"Base « {name} » créée."}


@router.post("/{conn_id}/test")
def test_saved_connection(conn_id: int, request: Request, db: Session = Depends(get_db)):
    conn = db.get(Connection, conn_id)
    if conn is None:
        flash(request, "Connexion introuvable.", "err")
    else:
        try:
            version = test_connection(conn)
            if conn.is_sheet:
                flash(request, f"« {conn.name} » : {version}.", "ok")
            else:
                count = len(list_tables(conn))
                flash(request, f"« {conn.name} » : connexion réussie ({version}) — {count} table(s).", "ok")
        except Exception as exc:
            flash(request, f"« {conn.name} » : échec de connexion : {friendly(exc)}", "err")
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
