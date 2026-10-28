"""Demandes de congé de maternité et de période d'allaitement faites par les employées.

- L'employée (compte rattaché à son matricule) fait sa demande : elle suit le circuit de validation défini dans
  les paramètres (Ressources humaines → Maternité et allaitement), étape par étape, comme les arrêts maladie
  (mêmes types d'étapes : responsable N+1, agent RH habilité, utilisateur désigné).
- Un agent RH habilité (ou un administrateur) fait la demande pour une employée : validée d'office.
- Une demande validée est recopiée dans PostgreSQL (table pointage_maternite) et s'applique au suivi : statut
  « Congé maternité » ou heures d'allaitement. Annulée, la période en est retirée.
"""
import json
from datetime import date, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.engine import Engine

from . import arrets, pointage
from .models import MaternityRequest, MaternityRequestAction, MaternitySettings, User, utcnow

KINDS = pointage.MATERNITY_KINDS
ACTIONS = {"declare": "Demandée", "saisi_rh": "Saisie par les RH (validée d'office)", "valide": "Validée",
           "refuse": "Refusée", "annule": "Annulée"}
EVENTS = {"a_valider": "Demande à valider", "valide": "Demande validée", "refuse": "Demande refusée"}
MAX_DAYS = 366


class MaternityError(Exception):
    pass


# --------------------------------------------------------------------------- circuit (paramètres)


def get_workflow(db) -> list[dict]:
    return arrets.get_workflow(db, MaternitySettings)


def save_workflow(db, steps: list[dict], author: str) -> None:
    try:
        arrets.save_workflow(db, steps, author, MaternitySettings)
    except arrets.SickLeaveError as exc:
        raise MaternityError(str(exc)) from None


def notify_enabled(db) -> bool:
    return arrets.notify_enabled(db, MaternitySettings)


def set_notify(db, enabled: bool, author: str) -> None:
    arrets.set_notify(db, enabled, author, MaternitySettings)


# Droits et étapes : mêmes règles que les arrêts maladie (même structure de demande).
step_label, leave_steps, can_act, can_see, is_hr, get_user, timeline = (
    arrets.step_label, arrets.leave_steps, arrets.can_act, arrets.can_see, arrets.is_hr, arrets.get_user, arrets.timeline)


def visible_requests(db, user: Optional[User], role: Optional[str], username: str, limit: int = 300) -> list:
    rows = db.scalars(select(MaternityRequest).order_by(MaternityRequest.created_at.desc(),
                                                        MaternityRequest.id.desc()).limit(2000)).all()
    return [x for x in rows if can_see(x, user, role, username)][:limit]


def pending_for(db, user: Optional[User], role: Optional[str], username: str) -> list:
    rows = db.scalars(select(MaternityRequest).where(MaternityRequest.status == "en_attente")
                      .order_by(MaternityRequest.created_at)).all()
    return [x for x in rows if can_act(x, user, role, username)]


def hours_label(req: MaternityRequest) -> str:
    if req.kind != "allaitement":
        return ""
    return f"{int(req.hours[:2])} h par jour" if req.hours else "heures par défaut"


# --------------------------------------------------------------------------- PostgreSQL


def _apply(engine: Engine, m: pointage.Mapping, req: MaternityRequest) -> None:
    """Demande validée : période recopiée dans pointage_maternite (lève MaternityError en cas de chevauchement)."""
    try:
        _, _, req.pg_id = pointage.maternity_add(
            engine, m, ref=req.matricule or req.emp_key, kind=req.kind, du=req.start_date.date(), au=req.end_date.date(),
            heures=req.hours or None, comment=f"Demande n°{req.id}" + (f" — {req.comment}" if req.comment else ""),
            author=req.created_by)
    except pointage.PointageError as exc:
        raise MaternityError(str(exc)) from None


def _remove(engine: Engine, m: pointage.Mapping, req: MaternityRequest) -> None:
    if req.pg_id is not None:
        pointage.maternity_delete(engine, m, req.pg_id)
        req.pg_id = None


# --------------------------------------------------------------------------- demande et décisions


def _log(db, req: MaternityRequest, actor: str, action: str, label: str = "", comment: str = "") -> None:
    db.add(MaternityRequestAction(request_id=req.id, actor=actor, action=action, step_label=label, comment=comment or ""))


def create(db, engine: Engine, m: pointage.Mapping, *, matricule: str, kind: str, start: date, end: date,
           hours: str, comment: str, file: Optional[tuple[str, bytes]], actor: str, by_hr: bool) -> MaternityRequest:
    if kind not in KINDS:
        raise MaternityError("Choisissez le type de demande : congé de maternité ou période d'allaitement.")
    if start is None or end is None:
        raise MaternityError("Indiquez les dates de début et de fin.")
    if end < start:
        raise MaternityError("La date de fin doit être postérieure ou égale à la date de début.")
    if (end - start).days >= MAX_DAYS:
        raise MaternityError(f"Une période ne peut pas dépasser {MAX_DAYS} jours : faites plusieurs demandes.")
    hours = (hours or "").strip() if kind == "allaitement" else ""
    if hours and hours not in pointage.ALLAITEMENT_HOURS:
        raise MaternityError("Heures d'allaitement : 1 h, 2 h ou 3 h par jour.")
    found = pointage.resolve_employee(engine, m, matricule or "")
    if found is None:
        raise MaternityError(f"Aucun employé trouvé pour le matricule « {matricule} ».")
    emp_key, name = found
    overlap = db.scalars(select(MaternityRequest).where(
        MaternityRequest.emp_key == emp_key, MaternityRequest.kind == kind,
        MaternityRequest.status.in_(("en_attente", "valide")),
        MaternityRequest.start_date <= datetime.combine(end, datetime.min.time()),
        MaternityRequest.end_date >= datetime.combine(start, datetime.min.time()))).first()
    if overlap is not None:
        raise MaternityError(f"Une demande de {KINDS[kind].lower()} couvre déjà une partie de cette période (du "
                             f"{overlap.start_date:%d/%m/%Y} au {overlap.end_date:%d/%m/%Y}, {overlap.status_label.lower()}).")
    stored = ("", "", "")
    if file is not None:
        try:
            stored = arrets.store_file(*file)
        except arrets.SickLeaveError as exc:
            raise MaternityError(str(exc).replace("Justificatif", "Certificat").replace("justificatif", "certificat")) from None
    req = MaternityRequest(emp_key=emp_key, name=name, kind=kind, hours=hours,
                           start_date=datetime.combine(start, datetime.min.time()),
                           end_date=datetime.combine(end, datetime.min.time()), comment=(comment or "").strip()[:2000],
                           source="rh" if by_hr else "employe", created_by=actor,
                           file_name=stored[0], file_path=stored[1], file_type=stored[2])
    req.matricule = pointage.employee_matricule(engine, m, emp_key) or (matricule or "").strip()
    manager = pointage.manager_of(engine, m, emp_key)
    if manager:
        req.manager_matricule, req.manager_name = manager
    steps = [] if by_hr else get_workflow(db)
    req.workflow = json.dumps(steps)
    req.status = "en_attente"
    db.add(req)
    db.flush()
    _log(db, req, actor, "saisi_rh" if by_hr else "declare", comment=req.comment)
    if not steps:
        try:
            _apply(engine, m, req)  # validée d'office ; période déjà enregistrée → rien n'est conservé
        except MaternityError:
            db.rollback()
            raise
        req.status, req.decided_at = "valide", utcnow()
        if not by_hr:
            _log(db, req, "système", "valide", "Aucune étape de validation paramétrée")
    db.commit()
    return req


def decide(db, engine: Engine, m: pointage.Mapping, req: MaternityRequest, *, approve: bool, comment: str,
           user: Optional[User], role: Optional[str], username: str) -> MaternityRequest:
    if not can_act(req, user, role, username):
        raise MaternityError("Vous ne pouvez pas valider cette étape de la demande.")
    steps = leave_steps(req)
    label = step_label(steps[req.step])
    if not approve:
        if not (comment or "").strip():
            raise MaternityError("Indiquez le motif du refus.")
        req.status, req.decided_at = "refuse", utcnow()
        _log(db, req, username, "refuse", label, comment)
        db.commit()
        return req
    if req.step + 1 >= len(steps):
        try:
            _apply(engine, m, req)  # dernière étape : la période s'applique au suivi
        except MaternityError:
            db.rollback()
            raise
        req.status, req.decided_at = "valide", utcnow()
    req.step += 1
    _log(db, req, username, "valide", label, comment)
    db.commit()
    return req


def cancel(db, engine: Engine, m: pointage.Mapping, req: MaternityRequest, *, comment: str, user: Optional[User],
           role: Optional[str], username: str) -> MaternityRequest:
    hr = is_hr(user, role, username)
    if req.status in ("refuse", "annule"):
        raise MaternityError("Cette demande est déjà close.")
    if req.status == "valide" and not hr:
        raise MaternityError("Une demande validée ne peut être annulée que par les RH.")
    if req.status == "en_attente" and not (hr or req.created_by == username):
        raise MaternityError("Seul l'auteur de la demande ou les RH peuvent l'annuler.")
    if req.status == "valide" and engine is not None:
        _remove(engine, m, req)
    req.status, req.decided_at = "annule", utcnow()
    _log(db, req, username, "annule", comment=comment)
    db.commit()
    return req


def entry_deleted(db, entry_id: int, actor: str) -> Optional[MaternityRequest]:
    """Période supprimée par les RH dans la liste des périodes : la demande validée d'origine passe « annulée »."""
    req = db.scalars(select(MaternityRequest).where(MaternityRequest.pg_id == entry_id)).first()
    if req is None:
        return None
    req.pg_id, req.status, req.decided_at = None, "annule", utcnow()
    _log(db, req, actor, "annule", comment="Période supprimée par les RH (Maternité et allaitement)")
    db.commit()
    return req


def history(db, req: MaternityRequest) -> list[MaternityRequestAction]:
    return db.scalars(select(MaternityRequestAction).where(MaternityRequestAction.request_id == req.id)
                      .order_by(MaternityRequestAction.at, MaternityRequestAction.id)).all()


# --------------------------------------------------------------------------- notifications par e-mail


def build_notification(req: MaternityRequest, event: str, base_url: str, company: str = "", comment: str = "") -> tuple:
    kind = KINDS.get(req.kind, req.kind)
    period = f"du {req.start_date:%d/%m/%Y} au {req.end_date:%d/%m/%Y}"
    rows = [("Employée", f"{req.name} ({req.matricule})"), ("Demande", kind + (f" · {hours_label(req)}" if hours_label(req) else "")),
            ("Période", f"{period} · {req.days} jour(s)")]
    link = f"{base_url.rstrip('/')}/maternite/{req.id}" if base_url else ""
    if event == "a_valider":
        steps = leave_steps(req)
        rows.append(("Étape à valider", step_label(steps[req.step]) if req.step < len(steps) else "—"))
        rows.append(("Demandé par", req.created_by))
        if req.comment:
            rows.append(("Commentaire", req.comment[:300]))
        title, intro, button = (f"{kind} à valider — {req.name}",
                                "Une demande attend votre validation dans l'application de pointage.", "Examiner la demande")
    elif event == "valide":
        title, intro, button = (f"{kind} validé(e) — {req.name}",
                                "La demande a été validée : la période s'applique désormais au suivi des pointages.",
                                "Voir la demande")
    else:
        if comment:
            rows.append(("Motif du refus", comment[:500]))
        title, intro, button = (f"{kind} refusé(e) — {req.name}",
                                "La demande a été refusée. Rapprochez-vous de votre responsable ou du service RH.",
                                "Voir la demande")
    text_body = f"{title}\n\n{intro}\n\n" + "\n".join(f"{k} : {v}" for k, v in rows) + (f"\n\n{link}" if link else "")
    html = arrets._mail_html(title, intro, req, rows, link, button, company, eyebrow="Maternité et allaitement",
                             accent="#be185d")
    return title, html, text_body


def notify(request_id: int, event: str, base_url: str = "", actor: str = "", comment: str = "") -> dict:
    """Mails d'un événement (valideurs de l'étape en cours, puis l'employée). Ne lève jamais d'exception."""
    from .database import SessionLocal
    from .joblog import write_log
    from .mails import get_settings

    try:
        with SessionLocal() as db:
            if not notify_enabled(db) or event not in EVENTS:
                return {"status": "disabled"}
            req = db.get(MaternityRequest, request_id)
            s = get_settings(db)
            if req is None or not s.smtp_host or not s.from_email:
                return {"status": "unconfigured"}
            users = arrets.step_recipients(db, req) if event == "a_valider" else arrets.outcome_recipients(db, req)
            users = [u for u in users if u.username != actor]
            recipients = arrets.addresses(db, users)
            if not recipients:
                if users:
                    write_log("WARNING", f"Demande maternité n°{req.id} ({EVENTS[event].lower()}) : aucun destinataire "
                                         f"n'a d'adresse e-mail ({', '.join(u.username for u in users)}).")
                return {"status": "no_recipient", "sent": 0}
            subject, body, text_body = build_notification(req, event, base_url, s.company, comment)
            messages, production = arrets.build_messages(s, recipients, subject, body, text_body, "Maternité et allaitement")
            if messages is None:
                return {"status": "no_test_recipient", "sent": 0}
        arrets.send_messages(s, messages)
        write_log("INFO", f"Demande maternité n°{request_id} : {len(messages)} mail(s) « {EVENTS[event].lower()} » "
                          f"envoyé(s)" + ("" if production else " (mode test)") + ".")
        return {"status": "sent", "sent": len(messages), "to": [e for e, _ in recipients]}
    except Exception as exc:  # noqa: BLE001
        try:
            write_log("ERROR", f"Demande maternité n°{request_id} : notification impossible ({exc.__class__.__name__}: {exc}).")
        except Exception:  # noqa: BLE001
            pass
        return {"status": "error", "error": str(exc)}


def start_notify(request_id: int, event: str, base_url: str = "", actor: str = "", comment: str = "") -> None:
    import threading

    threading.Thread(target=notify, args=(request_id, event, base_url, actor, comment), daemon=True).start()
