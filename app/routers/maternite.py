"""Maternité et allaitement : demandes des employées (circuit de validation), décisions, certificat, paramétrage."""
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import arrets, auth, maternite, pointage
from ..database import get_db
from ..errors import friendly
from ..models import SICK_STATUS, MaternityRequest
from ..sync import make_engine
from ..web import flash, redirect, render, require_login
from .suivi import load_config

router = APIRouter(prefix="/maternite", dependencies=[Depends(require_login)])
admin_router = APIRouter(prefix="/admin/maternite", dependencies=[Depends(require_login)])


def _who(request: Request, db: Session):
    username = request.session.get("user", "")
    return username, maternite.get_user(db, username), request.session.get("role")


def _notify(request: Request, req: MaternityRequest, username: str, comment: str = "") -> None:
    event = {"en_attente": "a_valider", "valide": "valide", "refuse": "refuse"}.get(req.status)
    if event:
        maternite.start_notify(req.id, event, str(request.base_url), username, comment)


def _summary(req: MaternityRequest) -> str:
    extra = maternite.hours_label(req)
    return f"du {req.start_date:%d/%m/%Y} au {req.end_date:%d/%m/%Y}" + (f", {extra}" if extra else "")


@router.get("")
def index(request: Request, db: Session = Depends(get_db)):
    username, user, role = _who(request, db)
    statut = request.query_params.get("statut", "")
    rows = maternite.visible_requests(db, user, role, username)
    if statut in SICK_STATUS:
        rows = [x for x in rows if x.status == statut]
    return render(request, "maternite.html", requests=rows, pending=maternite.pending_for(db, user, role, username),
                  hr=maternite.is_hr(user, role, username), me=user, statut=statut, statuses=SICK_STATUS,
                  kinds=maternite.KINDS, hours=pointage.ALLAITEMENT_HOURS, hours_label=maternite.hours_label,
                  workflow=[maternite.step_label(s) for s in maternite.get_workflow(db)],
                  can_act=lambda x: maternite.can_act(x, user, role, username))


@router.post("")
async def create(request: Request, db: Session = Depends(get_db), type: str = Form("maternite"), du: str = Form(""),
                 au: str = Form(""), heures: str = Form(""), matricule: str = Form(""), commentaire: str = Form(""),
                 certificat: Optional[UploadFile] = File(None)):
    username, user, role = _who(request, db)
    hr = maternite.is_hr(user, role, username)
    if not hr:
        # Une employée ne fait une demande que pour elle-même (compte rattaché à son matricule).
        if not (user and user.emp_matricule):
            flash(request, "Votre compte n'est rattaché à aucun employé : demandez à un administrateur d'indiquer "
                           "votre matricule, ou adressez-vous aux RH.", "err")
            return redirect("/maternite")
        matricule = user.emp_matricule
    cfg, mapping = load_config(db)
    if mapping is None:
        flash(request, "Le module de pointage n'est pas configuré.", "err")
        return redirect("/maternite")
    file = None
    if certificat is not None and certificat.filename:
        file = (certificat.filename, await certificat.read(arrets.MAX_FILE_BYTES + 1))
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
    try:
        req = maternite.create(db, engine, mapping, matricule=matricule.split(" — ")[0].strip(), kind=type,
                               start=pointage.parse_day(du), end=pointage.parse_day(au), hours=heures,
                               comment=commentaire, file=file, actor=username, by_hr=hr)
    except maternite.MaternityError as exc:
        flash(request, str(exc), "err")
        return redirect("/maternite")
    except Exception as exc:  # noqa: BLE001
        flash(request, f"Enregistrement impossible : {friendly(exc)}", "err")
        return redirect("/maternite")
    finally:
        engine.dispose()
    _notify(request, req, username)
    label = maternite.KINDS[req.kind]
    auth.audit(request, f"{label} " + ("saisi(e) (RH)" if hr else "demandé(e)"), f"{req.matricule} {req.name}", _summary(req))
    if req.status == "valide":
        flash(request, f"{label} de {req.name} enregistré(e) et validé(e) : {_summary(req)}.", "ok")
    else:
        flash(request, f"Demande envoyée : elle est transmise pour validation "
                       f"({maternite.step_label(maternite.leave_steps(req)[0])}).", "ok")
    return redirect(f"/maternite/{req.id}")


def _load(db: Session, request_id: int, request: Request) -> Optional[MaternityRequest]:
    username, user, role = _who(request, db)
    req = db.get(MaternityRequest, request_id)
    if req is None or not maternite.can_see(req, user, role, username):
        return None
    return req


@router.get("/{request_id}")
def detail(request: Request, request_id: int, db: Session = Depends(get_db)):
    req = _load(db, request_id, request)
    if req is None:
        flash(request, "Demande introuvable ou non accessible.", "err")
        return redirect("/maternite")
    username, user, role = _who(request, db)
    hr = maternite.is_hr(user, role, username)
    can_cancel = req.status not in ("refuse", "annule") and (hr or (req.status == "en_attente"
                                                                     and req.created_by == username))
    return render(request, "maternite_detail.html", req=req, history=maternite.history(db, req),
                  actions=maternite.ACTIONS, kinds=maternite.KINDS, hours_label=maternite.hours_label(req),
                  timeline=maternite.timeline(req), can_act=maternite.can_act(req, user, role, username),
                  can_cancel=can_cancel, working_days=arrets.working_days(req.start_date.date(), req.end_date.date()))


@router.post("/{request_id}/decision")
def decision(request: Request, request_id: int, decision: str = Form(...), commentaire: str = Form(""),
             db: Session = Depends(get_db)):
    req = _load(db, request_id, request)
    if req is None:
        return redirect("/maternite")
    username, user, role = _who(request, db)
    cfg, mapping = load_config(db)
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS) if mapping else None
    try:
        if engine is None and decision == "valider":
            raise maternite.MaternityError("Le module de pointage n'est pas configuré.")
        maternite.decide(db, engine, mapping, req, approve=decision == "valider", comment=commentaire,
                         user=user, role=role, username=username)
    except maternite.MaternityError as exc:
        flash(request, str(exc), "err")
        return redirect(f"/maternite/{request_id}")
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        flash(request, f"Enregistrement impossible : {friendly(exc)}", "err")
        return redirect(f"/maternite/{request_id}")
    finally:
        if engine is not None:
            engine.dispose()
    _notify(request, req, username, commentaire)
    label = maternite.KINDS[req.kind]
    auth.audit(request, f"{label} " + ("validé(e)" if decision == "valider" else "refusé(e)"),
               f"{req.matricule} {req.name}", commentaire)
    if req.status == "valide":
        flash(request, "Demande validée : la période s'applique désormais au suivi des pointages.", "ok")
    elif req.status == "refuse":
        flash(request, "Demande refusée.", "warn")
    else:
        flash(request, "Étape validée : la demande passe à l'étape suivante "
                       f"({maternite.step_label(maternite.leave_steps(req)[req.step])}).", "ok")
    return redirect(f"/maternite/{request_id}")


@router.post("/{request_id}/annuler")
def cancel(request: Request, request_id: int, commentaire: str = Form(""), db: Session = Depends(get_db)):
    req = _load(db, request_id, request)
    if req is None:
        return redirect("/maternite")
    username, user, role = _who(request, db)
    cfg, mapping = load_config(db)
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS) if mapping else None
    try:
        maternite.cancel(db, engine, mapping, req, comment=commentaire, user=user, role=role, username=username)
    except maternite.MaternityError as exc:
        flash(request, str(exc), "err")
        return redirect(f"/maternite/{request_id}")
    finally:
        if engine is not None:
            engine.dispose()
    auth.audit(request, f"{maternite.KINDS[req.kind]} annulé(e)", f"{req.matricule} {req.name}", commentaire)
    flash(request, "Demande annulée.", "ok")
    return redirect(f"/maternite/{request_id}")


@router.get("/{request_id}/certificat")
def attachment(request: Request, request_id: int, db: Session = Depends(get_db)):
    req = _load(db, request_id, request)
    data = arrets.file_bytes(req) if req is not None else None
    if data is None:
        return Response("Certificat introuvable ou non accessible.", status_code=404, media_type="text/plain; charset=utf-8")
    safe = req.file_name.encode("ascii", "ignore").decode() or "certificat"
    return Response(data, media_type=req.file_type or "application/octet-stream", headers={
        "Content-Disposition": f'inline; filename="{safe}"', "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox", "Cache-Control": "private, no-store"})


# --------------------------------------------------------------------------- paramètres (RH et administrateurs)


@admin_router.post("/circuit")
async def workflow_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    types, names = form.getlist("type"), form.getlist("user")
    steps = [{"type": t, "user": names[i] if i < len(names) else ""} for i, t in enumerate(types) if t]
    try:
        maternite.save_workflow(db, steps, request.session.get("user", ""))
    except maternite.MaternityError as exc:
        flash(request, str(exc), "err")
        return redirect("/admin/maternite#circuit")
    labels = " → ".join(maternite.step_label(s) for s in maternite.get_workflow(db)) or "aucune étape (validée d'office)"
    auth.audit(request, "Circuit des demandes de maternité modifié", labels)
    flash(request, f"Circuit enregistré : {labels}. Il s'applique aux prochaines demandes.", "ok")
    return redirect("/admin/maternite#circuit")


@admin_router.post("/notifications")
def notifications_save(request: Request, notify: bool = Form(False), db: Session = Depends(get_db)):
    maternite.set_notify(db, notify, request.session.get("user", ""))
    auth.audit(request, "Notifications des demandes de maternité " + ("activées" if notify else "désactivées"))
    flash(request, "Notifications par e-mail " + ("activées." if notify else "désactivées."), "ok")
    return redirect("/admin/maternite#circuit")
