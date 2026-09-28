"""Espace d'administration (rôle admin) : paramètres horaires historisés, source des pointages, jours fériés,
utilisateurs et rôles, journal d'audit, traitements planifiés."""
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import auth, pointage, scheduler
from ..database import get_db
from ..errors import friendly
from ..models import ROLES, AuditEntry, Connection, PointageConfig, SyncJob, User, utcnow
from ..sync import is_running, make_engine
from ..web import flash, redirect, render, require_login

router = APIRouter(prefix="/admin", dependencies=[Depends(require_login)])


def _config(db: Session) -> PointageConfig:
    cfg = db.query(PointageConfig).order_by(PointageConfig.id).first()
    if cfg is None:
        cfg = PointageConfig(data="{}")
        db.add(cfg)
        db.commit()
    return cfg


def _mapping_ready(db: Session):
    from .suivi import load_config

    cfg = _config(db)
    _, mapping = load_config(db)  # réinstalle les calculs s'ils datent d'une version précédente
    return cfg, mapping


# --------------------------------------------------------------------------- accueil


@router.get("")
def index(request: Request, db: Session = Depends(get_db)):
    cfg = _config(db)
    jobs = db.scalars(select(SyncJob).order_by(SyncJob.name)).all()
    return render(
        request, "admin/index.html", cfg=cfg, mapping=pointage.Mapping.from_json(cfg.data), jobs=jobs,
        running={j.id: is_running(j.id) for j in jobs}, next_runs={j.id: scheduler.next_run_time(j.id) for j in jobs},
        users=db.scalar(select(func.count(User.id))), audits=db.scalar(select(func.count(AuditEntry.id))),
    )


# --------------------------------------------------------------------------- source des pointages

_MAPPING_FIELDS = [f for f in pointage.Mapping.__dataclass_fields__]


@router.get("/pointage")
def pointage_config(request: Request, db: Session = Depends(get_db)):
    cfg = _config(db)
    params = request.query_params
    exploring = "conn_id" in params
    mapping = pointage.Mapping.from_json(cfg.data)
    conn_id = cfg.conn_id
    if exploring:  # rechargement du formulaire (choix d'une connexion, d'un schéma ou d'une table)
        mapping = pointage.Mapping(**{k: params.get(k, "").strip() for k in _MAPPING_FIELDS})
        conn_id = int(params["conn_id"]) if params["conn_id"].isdigit() else None
    connections = db.scalars(select(Connection).where(Connection.kind == "postgresql").order_by(Connection.name)).all()
    conn = db.get(Connection, conn_id) if conn_id else (connections[0] if connections else None)
    schemas, tables, punch_cols, emp_cols, service_cols, error, installed = [], [], {}, {}, {}, None, False
    person_cols, hier_cols, diag, stale = {}, {}, None, False
    if conn is not None:
        engine = make_engine(conn)
        try:
            schemas = pointage.list_schemas(engine)
            if mapping.schema not in schemas and schemas:
                mapping.schema = "public" if "public" in schemas else schemas[0]
            tables = pointage.list_tables(engine, mapping.schema)
            names = [t for t in tables]
            mapping.punch_table = mapping.punch_table or pointage.guess("punch_table", names)
            mapping.emp_table = mapping.emp_table or pointage.guess("emp_table", names, (mapping.punch_table,))
            punch_cols = pointage.column_types(engine, mapping.schema, mapping.punch_table)
            emp_cols = pointage.column_types(engine, mapping.schema, mapping.emp_table)
            service_cols = pointage.column_types(engine, mapping.schema, mapping.service_table)
            person_cols = pointage.column_types(engine, mapping.schema, mapping.person_table)
            hier_cols = pointage.column_types(engine, mapping.schema, mapping.hier_table)
            # Colonnes des tables facultatives : proposées dès que la table est choisie.
            for key, cols in (("person_key_col", person_cols), ("person_nom_col", person_cols),
                              ("person_prenom_col", person_cols), ("hier_emp_col", hier_cols),
                              ("hier_manager_col", hier_cols)):
                if cols and not getattr(mapping, key):
                    setattr(mapping, key, pointage.guess(key, list(cols)))
            if person_cols and not mapping.emp_person_col:
                mapping.emp_person_col = pointage.guess("emp_person_col", list(emp_cols))
            pc, ec = list(punch_cols), list(emp_cols)
            if not mapping.punch_emp_col:
                mapping.punch_emp_col = pointage.guess("punch_emp_col", pc)
            if not mapping.punch_ts_col:
                mapping.punch_ts_col = pointage.guess("punch_ts_col", pc, (mapping.punch_emp_col,))
            for key in ("emp_key_col", "emp_nom_col", "emp_prenom_col", "emp_service_col"):
                if not getattr(mapping, key) and not exploring:
                    setattr(mapping, key, pointage.guess(key, ec))
            if not mapping.emp_matricule_col and not exploring:
                mapping.emp_matricule_col = pointage.guess("emp_matricule_col", ec)
            if not exploring and cfg.installed_at is None and not mapping.hier_table:
                # Première configuration : hiérarchie souvent dans la table des employés (colonne « responsable »).
                boss = pointage.guess("hier_manager_col", ec)
                if boss:
                    mapping.hier_table, mapping.hier_manager_col = mapping.emp_table, boss
                    mapping.hier_emp_col = mapping.emp_key_col
                    hier_cols = emp_cols
            installed = cfg.installed_at is not None and pointage.is_installed(engine, mapping)
            if installed and not exploring:
                diag = pointage.diagnostics(engine, mapping)
                last = diag["dernier_pointage"]
                stale = last is not None and (date.today() - last.date()).days > 3
        except Exception as exc:
            error = friendly(exc)
        finally:
            engine.dispose()
    return render(
        request, "admin/pointage.html", cfg=cfg, m=mapping, conn=conn, connections=connections, schemas=schemas,
        tables=tables, punch_cols=punch_cols, emp_cols=emp_cols, service_cols=service_cols, error=error,
        person_cols=person_cols, hier_cols=hier_cols, diag=diag, stale=stale,
        installed=installed, exploring=exploring,
    )


@router.post("/pointage")
async def pointage_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    cfg = _config(db)
    conn_id = str(form.get("conn_id", ""))
    conn = db.get(Connection, int(conn_id)) if conn_id.isdigit() else None
    if conn is None or conn.kind != "postgresql":
        flash(request, "Choisissez la base PostgreSQL qui contient les pointages.", "err")
        return redirect("/admin/pointage")
    mapping = pointage.Mapping(**{k: str(form.get(k, "")).strip() for k in _MAPPING_FIELDS})
    before = cfg.data
    engine = make_engine(conn)
    try:
        pointage.install(engine, mapping, request.session.get("user", ""))
    except pointage.PointageError as exc:
        flash(request, str(exc), "err")
        return redirect("/admin/pointage?" + _query(conn.id, mapping))
    except Exception as exc:
        flash(request, f"Installation impossible : {friendly(exc)}", "err")
        return redirect("/admin/pointage?" + _query(conn.id, mapping))
    finally:
        engine.dispose()
    cfg.conn_id, cfg.data, cfg.installed_at = conn.id, mapping.to_json(), utcnow()
    cfg.sql_version = pointage.SQL_VERSION
    db.commit()
    auth.audit(request, "Source des pointages configurée", f"{conn.name} · {mapping.schema}",
               f"avant : {before}\naprès : {cfg.data}")
    flash(request, f"Configuration enregistrée : fonction {mapping.objs}.f_pointage_journalier et vue "
                   f"{mapping.objs}.v_pointage_journalier installées dans {conn.database}.", "ok")
    return redirect("/admin/pointage")


def _query(conn_id: int, m: pointage.Mapping) -> str:
    from urllib.parse import urlencode

    return urlencode({"conn_id": conn_id, **{k: getattr(m, k) for k in _MAPPING_FIELDS}})


# --------------------------------------------------------------------------- paramètres horaires


@router.get("/parametres")
def params_page(request: Request, db: Session = Depends(get_db)):
    cfg, mapping = _mapping_ready(db)
    context = dict(mapping=mapping, params=pointage.PARAMS, weekdays=pointage.WEEKDAYS, today=date.today(),
                   values=dict(pointage.PARAM_DEFAULTS), history=[], holidays=[], error=None, upcoming=[])
    if mapping is not None:
        engine = make_engine(cfg.conn)
        try:
            context["values"] = pointage.params_at(engine, mapping, date.today())
            context["history"] = pointage.params_history(engine, mapping)
            context["holidays"] = pointage.holidays(engine, mapping)
            context["upcoming"] = [h for h in context["history"] if h.date_effet > date.today()]
        except Exception as exc:
            context["error"] = friendly(exc)
        finally:
            engine.dispose()
    return render(request, "admin/parametres.html", **context)


@router.post("/parametres")
async def params_save(request: Request, db: Session = Depends(get_db)):
    cfg, mapping = _mapping_ready(db)
    if mapping is None:
        return redirect("/admin/pointage")
    form = await request.form()
    effective = pointage.parse_day(str(form.get("date_effet", "")))
    if effective is None:
        flash(request, "Indiquez la date d'effet.", "err")
        return redirect("/admin/parametres")
    try:
        values = {}
        for key, _, kind, _ in pointage.PARAMS:
            raw = ",".join(form.getlist("jours_ouvres")) if kind == "days" else str(form.get(key, ""))
            values[key] = pointage.validate_param(key, raw)
        problem = pointage.check_consistency(values)
        if problem:
            raise pointage.PointageError(problem)
    except pointage.PointageError as exc:
        flash(request, str(exc), "err")
        return redirect("/admin/parametres")
    engine = make_engine(cfg.conn)
    try:
        changes = pointage.save_params(engine, mapping, values, effective, request.session.get("user", ""))
    except Exception as exc:
        flash(request, f"Enregistrement impossible : {friendly(exc)}", "err")
        return redirect("/admin/parametres")
    finally:
        engine.dispose()
    if not changes:
        flash(request, "Aucun paramètre modifié.", "info")
        return redirect("/admin/parametres")
    details = "\n".join(f"{pointage.PARAM_LABELS[k]} : {old} → {new}" for k, (old, new) in changes.items())
    auth.audit(request, "Paramètres horaires modifiés", f"date d'effet {effective:%d/%m/%Y}", details)
    when = "à partir d'aujourd'hui" if effective == date.today() else f"à partir du {effective:%d/%m/%Y}"
    flash(request, f"{len(changes)} paramètre(s) modifié(s), appliqué(s) {when}. Les jours antérieurs gardent "
                   f"les valeurs de l'époque.", "ok")
    return redirect("/admin/parametres")


@router.post("/feries")
def holiday_add(request: Request, jour: str = Form(...), libelle: str = Form(""), db: Session = Depends(get_db)):
    cfg, mapping = _mapping_ready(db)
    day = pointage.parse_day(jour)
    if mapping is None or day is None:
        flash(request, "Date invalide.", "err")
        return redirect("/admin/parametres")
    engine = make_engine(cfg.conn)
    try:
        pointage.add_holiday(engine, mapping, day, libelle.strip(), request.session.get("user", ""))
    finally:
        engine.dispose()
    auth.audit(request, "Jour férié ajouté", f"{day:%d/%m/%Y}", libelle.strip())
    flash(request, f"Jour férié du {day:%d/%m/%Y} enregistré : personne n'y sera compté absent.", "ok")
    return redirect("/admin/parametres#feries")


@router.post("/feries/delete")
def holiday_delete(request: Request, jour: str = Form(...), db: Session = Depends(get_db)):
    cfg, mapping = _mapping_ready(db)
    day = pointage.parse_day(jour)
    if mapping is None or day is None:
        return redirect("/admin/parametres")
    engine = make_engine(cfg.conn)
    try:
        pointage.delete_holiday(engine, mapping, day)
    finally:
        engine.dispose()
    auth.audit(request, "Jour férié supprimé", f"{day:%d/%m/%Y}")
    flash(request, f"Jour férié du {day:%d/%m/%Y} supprimé.", "ok")
    return redirect("/admin/parametres#feries")


# --------------------------------------------------------------------------- utilisateurs


@router.get("/users")
def users(request: Request, db: Session = Depends(get_db)):
    return render(request, "admin/users.html", users=db.scalars(select(User).order_by(User.username)).all(),
                  roles=ROLES, rescue=auth.settings.admin_username)


@router.post("/users/save")
def user_save(
    request: Request,
    user_id: int = Form(0),
    username: str = Form(""),
    full_name: str = Form(""),
    role: str = Form("lecteur"),
    active: bool = Form(False),
    password: str = Form(""),
    emp_matricule: str = Form(""),
    scope: str = Form("tous"),
    db: Session = Depends(get_db),
):
    me = request.session.get("user", "")
    if role not in ROLES:
        flash(request, "Rôle inconnu.", "err")
        return redirect("/admin/users")
    scope = scope if scope in ("tous", "equipe") else "tous"
    if scope == "equipe" and not emp_matricule.strip():
        flash(request, "Pour limiter un compte à son équipe, indiquez le matricule de l'employé correspondant.", "err")
        return redirect("/admin/users")
    user = db.get(User, user_id) if user_id else User()
    if user is None:
        return redirect("/admin/users")
    if not user_id:
        username = username.strip()
        if not username or auth.is_rescue_admin(username):
            flash(request, "Identifiant vide ou réservé au compte de secours.", "err")
            return redirect("/admin/users")
        problem = auth.password_problem(password)
        if problem:
            flash(request, problem, "err")
            return redirect("/admin/users")
        user.username, user.password_hash = username, auth.hash_password(password)
    elif user.username == me and (role != user.role or not active):
        flash(request, "Vous ne pouvez pas modifier votre propre rôle ni désactiver votre compte.", "err")
        return redirect("/admin/users")
    def describe(u: User) -> str:
        return (f"rôle {u.role}, {'actif' if u.active else 'inactif'}, matricule {u.emp_matricule or '—'}, "
                f"périmètre {'équipe' if u.scope == 'equipe' else 'tous'}")

    before = describe(user) if user_id else "création"
    user.full_name, user.role, user.active = full_name.strip(), role, active if user_id else True
    user.emp_matricule, user.scope = emp_matricule.strip() or None, scope
    if not user_id:
        db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, f"L'identifiant « {username} » existe déjà.", "err")
        return redirect("/admin/users")
    auth.audit(request, "Utilisateur créé" if not user_id else "Utilisateur modifié", user.username,
               f"{before} → {describe(user)}")
    flash(request, f"Utilisateur « {user.username} » enregistré.", "ok")
    return redirect("/admin/users")


@router.post("/users/{user_id}/password")
def user_password(request: Request, user_id: int, password: str = Form(...), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    problem = auth.password_problem(password)
    if user is None or problem:
        flash(request, problem or "Utilisateur introuvable.", "err")
        return redirect("/admin/users")
    user.password_hash = auth.hash_password(password)
    db.commit()
    auth.audit(request, "Mot de passe réinitialisé", user.username)
    flash(request, f"Mot de passe de « {user.username} » réinitialisé.", "ok")
    return redirect("/admin/users")


@router.post("/users/{user_id}/delete")
def user_delete(request: Request, user_id: int, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        return redirect("/admin/users")
    if user.username == request.session.get("user"):
        flash(request, "Vous ne pouvez pas supprimer votre propre compte.", "err")
        return redirect("/admin/users")
    name = user.username
    db.delete(user)
    db.commit()
    auth.audit(request, "Utilisateur supprimé", name)
    flash(request, f"Utilisateur « {name} » supprimé.", "ok")
    return redirect("/admin/users")


# --------------------------------------------------------------------------- journal d'audit


@router.get("/audit")
def audit_log(request: Request, user: str = "", q: str = "", du: str = "", au: str = "", page: int = 1,
              db: Session = Depends(get_db)):
    size = 50
    stmt = select(AuditEntry)
    if user:
        stmt = stmt.where(AuditEntry.username == user)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(AuditEntry.action.ilike(like), AuditEntry.target.ilike(like),
                              AuditEntry.details.ilike(like)))
    d1, d2 = pointage.parse_day(du), pointage.parse_day(au)
    if d1:
        stmt = stmt.where(AuditEntry.created_at >= datetime.combine(d1, datetime.min.time()))
    if d2:
        stmt = stmt.where(AuditEntry.created_at < datetime.combine(d2 + timedelta(days=1), datetime.min.time()))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    page = max(page, 1)
    entries = db.scalars(stmt.order_by(AuditEntry.id.desc()).offset((page - 1) * size).limit(size)).all()
    usernames = [u for u in db.scalars(select(AuditEntry.username).distinct().order_by(AuditEntry.username)) if u]
    return render(request, "admin/audit.html", entries=entries, total=total, page=page,
                  pages=max((total + size - 1) // size, 1), usernames=usernames,
                  filters={"user": user, "q": q, "du": du, "au": au})


# --------------------------------------------------------------------------- vérifier un employé


@router.get("/verifier")
def check_employee(request: Request, q: str = "", du: str = "", au: str = "", db: Session = Depends(get_db)):
    """Explique, jour par jour, pourquoi un employé apparaît (ou non) absent."""
    cfg, mapping = _mapping_ready(db)
    today = date.today()
    d_au = pointage.parse_day(au) or today
    d_du = pointage.parse_day(du) or (d_au - timedelta(days=13))
    if d_du > d_au:
        d_du, d_au = d_au, d_du
    d_du = max(d_du, d_au - timedelta(days=92))
    context = dict(q=q, du=d_du, au=d_au, mapping=mapping, results=None, others=[], error=None, calendar=[],
                   last=None, hhmm=pointage.hhmm, statuts=pointage.STATUTS)
    if mapping is not None and q.strip():
        engine = make_engine(cfg.conn)
        try:
            context["results"], context["others"] = pointage.inspect_employee(engine, mapping, q, d_du, d_au)
            context["last"] = pointage.last_punch(engine, mapping)
            context["workdays"] = pointage.params_at(engine, mapping, d_au)["jours_ouvres"].split(",")
        except Exception as exc:
            context["error"] = friendly(exc)
        finally:
            engine.dispose()
        days, d = [], d_du
        while d <= min(d_au, today):
            days.append(d)
            d += timedelta(days=1)
        context["calendar"] = days
    return render(request, "admin/verifier.html", **context)
