"""Justification des retards : justification par l'employé, validation par le N+1, pièce jointe, paramètres."""
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import arrets, auth, pointage, retards
from ..database import get_db
from ..errors import friendly
from ..models import LateJustification, utcnow
from ..sync import make_engine
from ..web import flash, redirect, render, require_login
from .suivi import load_config

router = APIRouter(prefix="/retards", dependencies=[Depends(require_login)])
admin_router = APIRouter(prefix="/admin/retards", dependencies=[Depends(require_login)])

FILTERS = {"en_attente": "Non approuvées", "valide": "Justifiés", "refuse": "Refusées", "annule": "Annulées"}


def _who(request: Request, db: Session):
    username = request.session.get("user", "")
    return username, retards.get_user(db, username), request.session.get("role")


def _notify(request: Request, req: LateJustification, username: str, comment: str = "") -> None:
    event = {"en_attente": "a_valider", "valide": "valide", "refuse": "refuse"}.get(req.status)
    if event:
        retards.start_notify(req.id, event, str(request.base_url), username, comment)


@router.get("")
def index(request: Request, db: Session = Depends(get_db)):
    username, user, role = _who(request, db)
    s = retards.get_settings(db)
    statut = request.query_params.get("statut", "")
    rows = retards.visible_requests(db, user, role, username)
    if statut in FILTERS:
        rows = [x for x in rows if x.status == statut]
    mine, error = [], None
    cfg, mapping = load_config(db)
    if mapping is not None and user and user.emp_matricule:
        engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
        try:
            mine = retards.my_late_days(db, engine, mapping, user)
        except Exception as exc:  # noqa: BLE001
            error = friendly(exc)
        finally:
            engine.dispose()
    return render(request, "retards.html", requests=rows, pending=retards.pending_for(db, user, role, username),
                  hr=retards.is_hr(user, role, username), me=user, statut=statut, statuses=FILTERS, s=s,
                  reasons=retards.reasons(s), mine=mine, error=error, sources=retards.SOURCES,
                  workflow=[retards.step_label(x) for x in retards.get_workflow(db)],
                  jour=request.query_params.get("jour", ""), matricule=request.query_params.get("matricule", ""),
                  can_act=lambda x: retards.can_act(x, user, role, username))


@router.post("")
async def create(request: Request, db: Session = Depends(get_db), jour: str = Form(""), motif: str = Form(""),
                 matricule: str = Form(""), commentaire: str = Form(""),
                 justificatif: Optional[UploadFile] = File(None)):
    username, user, role = _who(request, db)
    matricule = matricule.split(" — ")[0].strip() or (user.emp_matricule if user and user.emp_matricule else "")
    back = "/retards"
    if not matricule:
        flash(request, "Votre compte n'est rattaché à aucun employé : demandez à un administrateur d'indiquer "
                       "votre matricule, ou adressez-vous aux RH.", "err")
        return redirect(back)
    cfg, mapping = load_config(db)
    if mapping is None:
        flash(request, "Le module de pointage n'est pas configuré.", "err")
        return redirect(back)
    file = None
    if justificatif is not None and justificatif.filename:
        file = (justificatif.filename, await justificatif.read(arrets.MAX_FILE_BYTES + 1))
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
    try:
        req = retards.create(db, engine, mapping, matricule=matricule, day=pointage.parse_day(jour), reason=motif,
                             comment=commentaire, file=file, user=user, role=role, username=username)
    except retards.LateError as exc:
        flash(request, str(exc), "err")
        return redirect(back)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        flash(request, f"Enregistrement impossible : {friendly(exc)}", "err")
        return redirect(back)
    finally:
        engine.dispose()
    _notify(request, req, username)
    auth.audit(request, f"Retard justifié ({retards.SOURCES[req.source].lower()})", f"{req.matricule} {req.name}",
               f"{req.day:%d/%m/%Y} · {req.reason}")
    if req.status == "valide":
        flash(request, f"Retard du {req.day:%d/%m/%Y} justifié.", "ok")
    else:
        steps = retards.leave_steps(req)
        who = req.manager_name if steps[0].get("type") == "responsable" and req.manager_name else retards.step_label(steps[0])
        flash(request, f"Justification envoyée : elle attend l'approbation de {who}.", "ok")
    return redirect(f"/retards/{req.id}")


def _load(db: Session, request_id: int, request: Request) -> Optional[LateJustification]:
    username, user, role = _who(request, db)
    req = db.get(LateJustification, request_id)
    if req is None or not retards.can_see(req, user, role, username):
        return None
    return req


@router.get("/{request_id}")
def detail(request: Request, request_id: int, db: Session = Depends(get_db)):
    req = _load(db, request_id, request)
    if req is None:
        flash(request, "Justification introuvable ou non accessible.", "err")
        return redirect("/retards")
    username, user, role = _who(request, db)
    hr = retards.is_hr(user, role, username)
    can_cancel = req.status not in ("refuse", "annule") and (hr or (req.status == "en_attente"
                                                                     and req.created_by == username))
    return render(request, "retard_detail.html", req=req, history=retards.history(db, req),
                  actions=retards.ACTIONS, sources=retards.SOURCES, timeline=retards.timeline(req),
                  can_act=retards.can_act(req, user, role, username), can_cancel=can_cancel)


@router.post("/{request_id}/decision")
def decision(request: Request, request_id: int, decision: str = Form(...), commentaire: str = Form(""),
             db: Session = Depends(get_db)):
    req = _load(db, request_id, request)
    if req is None:
        return redirect("/retards")
    username, user, role = _who(request, db)
    try:
        retards.decide(db, req, approve=decision == "valider", comment=commentaire, user=user, role=role,
                       username=username)
    except retards.LateError as exc:
        flash(request, str(exc), "err")
        return redirect(f"/retards/{request_id}")
    _notify(request, req, username, commentaire)
    auth.audit(request, "Justification de retard " + ("validée" if decision == "valider" else "refusée"),
               f"{req.matricule} {req.name}", commentaire)
    if req.status == "valide":
        flash(request, "Justification validée : le retard apparaît comme justifié dans le suivi.", "ok")
    elif req.status == "refuse":
        flash(request, "Justification refusée.", "warn")
    else:
        flash(request, "Étape validée : la justification passe à l'étape suivante "
                       f"({retards.step_label(retards.leave_steps(req)[req.step])}).", "ok")
    return redirect(f"/retards/{request_id}")


@router.post("/{request_id}/annuler")
def cancel(request: Request, request_id: int, commentaire: str = Form(""), db: Session = Depends(get_db)):
    req = _load(db, request_id, request)
    if req is None:
        return redirect("/retards")
    username, user, role = _who(request, db)
    try:
        retards.cancel(db, req, comment=commentaire, user=user, role=role, username=username)
    except retards.LateError as exc:
        flash(request, str(exc), "err")
        return redirect(f"/retards/{request_id}")
    auth.audit(request, "Justification de retard annulée", f"{req.matricule} {req.name}", commentaire)
    flash(request, "Justification annulée.", "ok")
    return redirect(f"/retards/{request_id}")


@router.get("/{request_id}/justificatif")
def attachment(request: Request, request_id: int, db: Session = Depends(get_db)):
    req = _load(db, request_id, request)
    data = arrets.file_bytes(req) if req is not None else None
    if data is None:
        return Response("Pièce jointe introuvable ou non accessible.", status_code=404, media_type="text/plain; charset=utf-8")
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
    return render(request, "admin/retards.html", s=retards.get_settings(db), steps=retards.get_workflow(db),
                  types=arrets.STEP_TYPES, users=users, hr_users=[u for u in users if u.sick_leave_hr],
                  notify=retards.notify_enabled(db), mail=mail_settings_of(db))


@admin_router.post("")
async def settings_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    get = lambda key, default="": str(form.get(key, default) or default).strip()  # noqa: E731
    motifs = pointage.multi(get("reasons").split(","))
    try:
        retro = int(get("retro_days", "7"))
    except ValueError:
        retro = -1
    problems = []
    if not motifs:
        problems.append("indiquez au moins un motif")
    if retro < 0:
        problems.append("délai pour justifier : nombre de jours")
    if problems:
        flash(request, "Paramètres non enregistrés : " + " ; ".join(problems) + ".", "err")
        return redirect("/admin/retards")
    s = retards.get_settings(db)
    s.reasons, s.retro_days, s.file_required = ", ".join(motifs), retro, bool(form.get("file_required"))
    s.updated_at, s.updated_by = utcnow(), request.session.get("user", "")
    db.commit()
    auth.audit(request, "Justification des retards : paramètres", "",
               f"motifs {s.reasons} ; pièce jointe {'obligatoire' if s.file_required else 'facultative'} ; "
               f"délai {s.retro_days} j")
    flash(request, "Paramètres de justification des retards enregistrés.", "ok")
    return redirect("/admin/retards")


@admin_router.post("/circuit")
async def workflow_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    types, names = form.getlist("type"), form.getlist("user")
    steps = [{"type": t, "user": names[i] if i < len(names) else ""} for i, t in enumerate(types) if t]
    try:
        retards.save_workflow(db, steps, request.session.get("user", ""))
    except retards.LateError as exc:
        flash(request, str(exc), "err")
        return redirect("/admin/retards#circuit")
    labels = " → ".join(retards.step_label(x) for x in retards.get_workflow(db)) or "aucune étape (validée d'office)"
    auth.audit(request, "Circuit des justifications de retard modifié", labels)
    flash(request, f"Circuit enregistré : {labels}.", "ok")
    return redirect("/admin/retards#circuit")


@admin_router.post("/notifications")
def notifications_save(request: Request, notify: bool = Form(False), db: Session = Depends(get_db)):
    retards.set_notify(db, notify, request.session.get("user", ""))
    auth.audit(request, "Notifications des justifications de retard " + ("activées" if notify else "désactivées"))
    flash(request, "Notifications par e-mail " + ("activées." if notify else "désactivées."), "ok")
    return redirect("/admin/retards#circuit")
