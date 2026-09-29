"""Arrêts maladie : déclaration (employé ou RH), validation selon le circuit, justificatif, paramétrage."""
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import arrets, auth, pointage
from ..database import get_db
from ..errors import friendly
from ..models import SICK_STATUS, SickLeave, User
from ..sync import make_engine
from ..web import flash, redirect, render, require_login
from .suivi import load_config

router = APIRouter(prefix="/arrets", dependencies=[Depends(require_login)])
admin_router = APIRouter(prefix="/admin/arrets", dependencies=[Depends(require_login)])


def _who(request: Request, db: Session):
    username = request.session.get("user", "")
    return username, arrets.get_user(db, username), request.session.get("role")


@router.get("")
def index(request: Request, db: Session = Depends(get_db)):
    username, user, role = _who(request, db)
    statut = request.query_params.get("statut", "")
    leaves = arrets.visible_leaves(db, user, role, username)
    if statut in SICK_STATUS:
        leaves = [x for x in leaves if x.status == statut]
    return render(request, "arrets.html", leaves=leaves, pending=arrets.pending_for(db, user, role, username),
                  hr=arrets.is_hr(user, role, username), me=user, statut=statut, statuses=SICK_STATUS,
                  workflow=[arrets.step_label(s) for s in arrets.get_workflow(db)],
                  can_act=lambda x: arrets.can_act(x, user, role, username))


@router.post("")
async def declare(request: Request, db: Session = Depends(get_db), du: str = Form(""), au: str = Form(""),
                  matricule: str = Form(""), commentaire: str = Form(""),
                  justificatif: Optional[UploadFile] = File(None)):
    username, user, role = _who(request, db)
    hr = arrets.is_hr(user, role, username)
    if not hr:
        # Un employé ne déclare que pour lui-même (compte rattaché à son matricule).
        if not (user and user.emp_matricule):
            flash(request, "Votre compte n'est rattaché à aucun employé : demandez à un administrateur d'indiquer "
                           "votre matricule, ou adressez-vous aux RH.", "err")
            return redirect("/arrets")
        matricule = user.emp_matricule
    cfg, mapping = load_config(db)
    if mapping is None:
        flash(request, "Le module de pointage n'est pas configuré.", "err")
        return redirect("/arrets")
    file = None
    if justificatif is not None and justificatif.filename:
        data = await justificatif.read(arrets.MAX_FILE_BYTES + 1)
        file = (justificatif.filename, data)
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
    try:
        leave = arrets.declare(db, engine, mapping, matricule=matricule, start=pointage.parse_day(du),
                               end=pointage.parse_day(au), comment=commentaire, file=file, actor=username, by_hr=hr)
    except arrets.SickLeaveError as exc:
        flash(request, str(exc), "err")
        return redirect("/arrets")
    except Exception as exc:  # noqa: BLE001
        flash(request, f"Enregistrement impossible : {friendly(exc)}", "err")
        return redirect("/arrets")
    finally:
        engine.dispose()
    auth.audit(request, "Arrêt maladie " + ("saisi (RH)" if hr else "déclaré"),
               f"{leave.matricule} {leave.name}", f"du {leave.start_date:%d/%m/%Y} au {leave.end_date:%d/%m/%Y}")
    if leave.status == "valide":
        flash(request, f"Arrêt maladie de {leave.name} enregistré et validé : du {leave.start_date:%d/%m/%Y} "
                       f"au {leave.end_date:%d/%m/%Y}.", "ok")
    else:
        flash(request, "Arrêt maladie déclaré : il est transmis pour validation "
                       f"({arrets.step_label(arrets.leave_steps(leave)[0])}).", "ok")
    return redirect(f"/arrets/{leave.id}")


def _load(db: Session, leave_id: int, request: Request) -> Optional[SickLeave]:
    username, user, role = _who(request, db)
    leave = db.get(SickLeave, leave_id)
    if leave is None or not arrets.can_see(leave, user, role, username):
        return None
    return leave


@router.get("/{leave_id}")
def detail(request: Request, leave_id: int, db: Session = Depends(get_db)):
    leave = _load(db, leave_id, request)
    if leave is None:
        flash(request, "Arrêt introuvable ou non accessible.", "err")
        return redirect("/arrets")
    username, user, role = _who(request, db)
    hr = arrets.is_hr(user, role, username)
    can_cancel = leave.status not in ("refuse", "annule") and (hr or (leave.status == "en_attente"
                                                                       and leave.created_by == username))
    return render(request, "arret_detail.html", leave=leave, history=arrets.history(db, leave), actions=arrets.ACTIONS,
                  timeline=arrets.timeline(leave), can_act=arrets.can_act(leave, user, role, username),
                  can_cancel=can_cancel, working_days=arrets.working_days(leave.start_date.date(), leave.end_date.date()))


@router.post("/{leave_id}/decision")
def decision(request: Request, leave_id: int, decision: str = Form(...), commentaire: str = Form(""),
             db: Session = Depends(get_db)):
    leave = _load(db, leave_id, request)
    if leave is None:
        return redirect("/arrets")
    username, user, role = _who(request, db)
    cfg, mapping = load_config(db)
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS) if mapping else None
    try:
        arrets.decide(db, engine, mapping, leave, approve=decision == "valider", comment=commentaire,
                      user=user, role=role, username=username)
    except arrets.SickLeaveError as exc:
        flash(request, str(exc), "err")
        return redirect(f"/arrets/{leave_id}")
    except Exception as exc:  # noqa: BLE001
        flash(request, f"Enregistrement impossible : {friendly(exc)}", "err")
        return redirect(f"/arrets/{leave_id}")
    finally:
        if engine is not None:
            engine.dispose()
    auth.audit(request, "Arrêt maladie " + ("validé" if decision == "valider" else "refusé"),
               f"{leave.matricule} {leave.name}", commentaire)
    if leave.status == "valide":
        flash(request, "Arrêt validé : ses jours apparaissent désormais en « Arrêt maladie ».", "ok")
    elif leave.status == "refuse":
        flash(request, "Arrêt refusé.", "warn")
    else:
        flash(request, "Étape validée : l'arrêt passe à l'étape suivante "
                       f"({arrets.step_label(arrets.leave_steps(leave)[leave.step])}).", "ok")
    return redirect(f"/arrets/{leave_id}")


@router.post("/{leave_id}/annuler")
def cancel(request: Request, leave_id: int, commentaire: str = Form(""), db: Session = Depends(get_db)):
    leave = _load(db, leave_id, request)
    if leave is None:
        return redirect("/arrets")
    username, user, role = _who(request, db)
    cfg, mapping = load_config(db)
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS) if mapping else None
    try:
        arrets.cancel(db, engine, mapping, leave, comment=commentaire, user=user, role=role, username=username)
    except arrets.SickLeaveError as exc:
        flash(request, str(exc), "err")
        return redirect(f"/arrets/{leave_id}")
    finally:
        if engine is not None:
            engine.dispose()
    auth.audit(request, "Arrêt maladie annulé", f"{leave.matricule} {leave.name}", commentaire)
    flash(request, "Arrêt annulé.", "ok")
    return redirect(f"/arrets/{leave_id}")


@router.get("/{leave_id}/justificatif")
def attachment(request: Request, leave_id: int, db: Session = Depends(get_db)):
    leave = _load(db, leave_id, request)
    data = arrets.file_bytes(leave) if leave is not None else None
    if data is None:
        return Response("Justificatif introuvable ou non accessible.", status_code=404,
                        media_type="text/plain; charset=utf-8")
    safe = leave.file_name.encode("ascii", "ignore").decode() or "justificatif"
    return Response(data, media_type=leave.file_type or "application/octet-stream", headers={
        "Content-Disposition": f'inline; filename="{safe}"', "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox", "Cache-Control": "private, no-store"})


# --------------------------------------------------------------------------- paramètres (administrateur)


@admin_router.get("")
def workflow_page(request: Request, db: Session = Depends(get_db)):
    users = db.scalars(select(User).where(User.active.is_(True)).order_by(User.username)).all()
    return render(request, "admin/arrets.html", steps=arrets.get_workflow(db), types=arrets.STEP_TYPES, users=users,
                  hr_users=[u for u in users if u.sick_leave_hr])


@admin_router.post("")
async def workflow_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    types, names = form.getlist("type"), form.getlist("user")
    steps = [{"type": t, "user": names[i] if i < len(names) else ""} for i, t in enumerate(types) if t]
    try:
        arrets.save_workflow(db, steps, request.session.get("user", ""))
    except arrets.SickLeaveError as exc:
        flash(request, str(exc), "err")
        return redirect("/admin/arrets")
    labels = " → ".join(arrets.step_label(s) for s in arrets.get_workflow(db)) or "aucune étape (validé d'office)"
    auth.audit(request, "Circuit des arrêts maladie modifié", labels)
    flash(request, f"Circuit enregistré : {labels}. Il s'applique aux prochaines déclarations.", "ok")
    return redirect("/admin/arrets")
