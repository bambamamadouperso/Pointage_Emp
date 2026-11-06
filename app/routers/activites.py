"""Activités extérieures (formation, réunion…) : déclaration, validation, justificatif, paramètres."""
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import activites, arrets, auth, pointage
from ..database import get_db
from ..errors import friendly
from ..models import OUTING_CREDIT, SICK_STATUS, OutingRequest, utcnow
from ..sync import make_engine
from ..web import flash, redirect, render, require_login
from .suivi import load_config

router = APIRouter(prefix="/activites", dependencies=[Depends(require_login)])
admin_router = APIRouter(prefix="/admin/activites", dependencies=[Depends(require_login)])


def _who(request: Request, db: Session):
    username = request.session.get("user", "")
    return username, activites.get_user(db, username), request.session.get("role")


def _notify(request: Request, req: OutingRequest, username: str, comment: str = "") -> None:
    event = {"en_attente": "a_valider", "valide": "valide", "refuse": "refuse"}.get(req.status)
    if event:
        activites.start_notify(req.id, event, str(request.base_url), username, comment)


def _summary(req: OutingRequest) -> str:
    period = f"du {req.start_date:%d/%m/%Y} au {req.end_date:%d/%m/%Y}" if req.days > 1 else f"le {req.start_date:%d/%m/%Y}"
    return f"{req.kind} {period}, {req.hours_label}"


@router.get("")
def index(request: Request, db: Session = Depends(get_db)):
    username, user, role = _who(request, db)
    s = activites.get_settings(db)
    statut = request.query_params.get("statut", "")
    rows = activites.visible_requests(db, user, role, username)
    if statut in SICK_STATUS:
        rows = [x for x in rows if x.status == statut]
    return render(request, "activites.html", requests=rows, pending=activites.pending_for(db, user, role, username),
                  hr=activites.is_hr(user, role, username), me=user, statut=statut, statuses=SICK_STATUS,
                  kinds=activites.kinds(s), s=s, sources=activites.SOURCES,
                  workflow=[activites.step_label(x) for x in activites.get_workflow(db)],
                  can_act=lambda x: activites.can_act(x, user, role, username))


@router.post("")
async def create(request: Request, db: Session = Depends(get_db), type: str = Form(""), du: str = Form(""),
                 au: str = Form(""), journee: bool = Form(False), debut: str = Form(""), fin: str = Form(""),
                 lieu: str = Form(""), objet: str = Form(""), matricule: str = Form(""), commentaire: str = Form(""),
                 justificatif: Optional[UploadFile] = File(None)):
    username, user, role = _who(request, db)
    matricule = matricule.split(" — ")[0].strip() or (user.emp_matricule if user and user.emp_matricule else "")
    if not matricule:
        flash(request, "Votre compte n'est rattaché à aucun employé : demandez à un administrateur d'indiquer "
                       "votre matricule, ou adressez-vous aux RH.", "err")
        return redirect("/activites")
    cfg, mapping = load_config(db)
    if mapping is None:
        flash(request, "Le module de pointage n'est pas configuré.", "err")
        return redirect("/activites")
    file = None
    if justificatif is not None and justificatif.filename:
        file = (justificatif.filename, await justificatif.read(arrets.MAX_FILE_BYTES + 1))
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
    try:
        req = activites.create(db, engine, mapping, matricule=matricule, kind=type, start=pointage.parse_day(du),
                               end=pointage.parse_day(au or du), full_day=journee, start_time=debut, end_time=fin,
                               place=lieu, subject=objet, comment=commentaire, file=file, user=user, role=role,
                               username=username)
    except activites.OutingError as exc:
        flash(request, str(exc), "err")
        return redirect("/activites")
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        flash(request, f"Enregistrement impossible : {friendly(exc)}", "err")
        return redirect("/activites")
    finally:
        engine.dispose()
    _notify(request, req, username)
    if activites.informs_manager(req):
        activites.start_notify(req.id, "information", str(request.base_url), username)
    auth.audit(request, f"Activité extérieure déclarée ({activites.SOURCES[req.source].lower()})",
               f"{req.matricule} {req.name}", _summary(req))
    if req.status == "valide":
        flash(request, f"Activité enregistrée : {_summary(req)}."
                       + (" Votre responsable en est informé par e-mail." if activites.informs_manager(req) else ""), "ok")
    else:
        flash(request, "Activité déclarée : elle est transmise pour validation "
                       f"({activites.step_label(activites.leave_steps(req)[0])}).", "ok")
    return redirect(f"/activites/{req.id}")


def _load(db: Session, request_id: int, request: Request) -> Optional[OutingRequest]:
    username, user, role = _who(request, db)
    req = db.get(OutingRequest, request_id)
    if req is None or not activites.can_see(req, user, role, username):
        return None
    return req


@router.get("/{request_id}")
def detail(request: Request, request_id: int, db: Session = Depends(get_db)):
    req = _load(db, request_id, request)
    if req is None:
        flash(request, "Déclaration introuvable ou non accessible.", "err")
        return redirect("/activites")
    username, user, role = _who(request, db)
    hr = activites.is_hr(user, role, username)
    can_cancel = req.status not in ("refuse", "annule") and (hr or (req.status == "en_attente"
                                                                     and req.created_by == username))
    return render(request, "activite_detail.html", req=req, history=activites.history(db, req),
                  actions=activites.ACTIONS, sources=activites.SOURCES, timeline=activites.timeline(req),
                  can_act=activites.can_act(req, user, role, username), can_cancel=can_cancel)


@router.post("/{request_id}/decision")
def decision(request: Request, request_id: int, decision: str = Form(...), commentaire: str = Form(""),
             db: Session = Depends(get_db)):
    req = _load(db, request_id, request)
    if req is None:
        return redirect("/activites")
    username, user, role = _who(request, db)
    cfg, mapping = load_config(db)
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS) if mapping else None
    try:
        if engine is None:
            raise activites.OutingError("Le module de pointage n'est pas configuré.")
        activites.decide(db, engine, mapping, req, approve=decision == "valider", comment=commentaire,
                         user=user, role=role, username=username)
    except activites.OutingError as exc:
        flash(request, str(exc), "err")
        return redirect(f"/activites/{request_id}")
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        flash(request, f"Enregistrement impossible : {friendly(exc)}", "err")
        return redirect(f"/activites/{request_id}")
    finally:
        if engine is not None:
            engine.dispose()
    _notify(request, req, username, commentaire)
    auth.audit(request, "Activité extérieure " + ("validée" if decision == "valider" else "refusée"),
               f"{req.matricule} {req.name}", commentaire)
    if req.status == "valide":
        flash(request, "Activité validée : elle est prise en compte dans le suivi des pointages.", "ok")
    elif req.status == "refuse":
        flash(request, "Activité refusée.", "warn")
    else:
        flash(request, "Étape validée : la déclaration passe à l'étape suivante "
                       f"({activites.step_label(activites.leave_steps(req)[req.step])}).", "ok")
    return redirect(f"/activites/{request_id}")


@router.post("/{request_id}/annuler")
def cancel(request: Request, request_id: int, commentaire: str = Form(""), db: Session = Depends(get_db)):
    req = _load(db, request_id, request)
    if req is None:
        return redirect("/activites")
    username, user, role = _who(request, db)
    cfg, mapping = load_config(db)
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS) if mapping else None
    try:
        activites.cancel(db, engine, mapping, req, comment=commentaire, user=user, role=role, username=username)
    except activites.OutingError as exc:
        flash(request, str(exc), "err")
        return redirect(f"/activites/{request_id}")
    finally:
        if engine is not None:
            engine.dispose()
    auth.audit(request, "Activité extérieure annulée", f"{req.matricule} {req.name}", commentaire)
    flash(request, "Déclaration annulée.", "ok")
    return redirect(f"/activites/{request_id}")


@router.get("/{request_id}/justificatif")
def attachment(request: Request, request_id: int, db: Session = Depends(get_db)):
    req = _load(db, request_id, request)
    data = arrets.file_bytes(req) if req is not None else None
    if data is None:
        return Response("Justificatif introuvable ou non accessible.", status_code=404, media_type="text/plain; charset=utf-8")
    safe = req.file_name.encode("ascii", "ignore").decode() or "justificatif"
    return Response(data, media_type=req.file_type or "application/octet-stream", headers={
        "Content-Disposition": f'inline; filename="{safe}"', "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox", "Cache-Control": "private, no-store"})


# --------------------------------------------------------------------------- paramètres (RH et administrateurs)


@admin_router.get("")
def settings_page(request: Request, db: Session = Depends(get_db)):
    from sqlalchemy import select

    from ..mails import get_settings as mail_settings_of
    from ..models import User

    users = db.scalars(select(User).where(User.active.is_(True)).order_by(User.username)).all()
    return render(request, "admin/activites.html", s=activites.get_settings(db), credit_modes=OUTING_CREDIT,
                  steps=activites.get_workflow(db), types=arrets.STEP_TYPES, users=users,
                  hr_users=[u for u in users if u.sick_leave_hr], notify=activites.notify_enabled(db),
                  mail=mail_settings_of(db))


@admin_router.post("")
async def settings_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    get = lambda key, default="": str(form.get(key, default) or default).strip()  # noqa: E731
    types = pointage.multi(get("kinds").split(","))
    forfait = activites.parse_time(get("forfait", "08:00"))
    try:
        retro = min(max(int(get("retro_days", "5") or 0), 0), 365)
    except ValueError:
        retro = -1
    problems = []
    if not types:
        problems.append("indiquez au moins un type d'activité")
    if forfait is None:
        problems.append("durée forfaitaire : format HH:MM (ex. 08:00)")
    if retro < 0:
        problems.append("délai de déclaration après coup : nombre de jours")
    if problems:
        flash(request, "Paramètres non enregistrés : " + " ; ".join(problems) + ".", "err")
        return redirect("/admin/activites")
    s = activites.get_settings(db)
    s.kinds, s.forfait, s.retro_days = ", ".join(types), forfait, retro
    s.validation_required = get("mode") == "validation" if get("mode") else bool(form.get("validation_required"))
    s.manager_direct = bool(form.get("manager_direct"))
    s.credit_mode = get("credit_mode") if get("credit_mode") in OUTING_CREDIT else "plage"
    s.updated_at, s.updated_by = utcnow(), request.session.get("user", "")
    db.commit()
    auth.audit(request, "Activités extérieures : paramètres", "",
               f"types {s.kinds} ; {'validation selon le circuit' if s.validation_required else 'information du N+1 par e-mail'} ; "
               f"responsable {'d’office' if s.manager_direct else 'circuit'} ; crédit {s.credit_mode} ({s.forfait}) ; "
               f"après coup {s.retro_days} j")
    flash(request, "Paramètres des activités extérieures enregistrés. Ils s'appliquent aux prochaines déclarations.", "ok")
    return redirect("/admin/activites")


@admin_router.post("/circuit")
async def workflow_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    types, names = form.getlist("type"), form.getlist("user")
    steps = [{"type": t, "user": names[i] if i < len(names) else ""} for i, t in enumerate(types) if t]
    try:
        activites.save_workflow(db, steps, request.session.get("user", ""))
    except activites.OutingError as exc:
        flash(request, str(exc), "err")
        return redirect("/admin/activites#circuit")
    labels = " → ".join(activites.step_label(x) for x in activites.get_workflow(db)) or "aucune étape (validée d'office)"
    auth.audit(request, "Circuit des activités extérieures modifié", labels)
    flash(request, f"Circuit enregistré : {labels}.", "ok")
    return redirect("/admin/activites#circuit")


@admin_router.post("/notifications")
def notifications_save(request: Request, notify: bool = Form(False), db: Session = Depends(get_db)):
    activites.set_notify(db, notify, request.session.get("user", ""))
    auth.audit(request, "Notifications des activités extérieures " + ("activées" if notify else "désactivées"))
    flash(request, "Notifications par e-mail " + ("activées." if notify else "désactivées."), "ok")
    return redirect("/admin/activites#circuit")
