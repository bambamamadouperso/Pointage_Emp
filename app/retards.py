"""Justification des retards.

- L'employé en retard justifie sa journée : motif (liste paramétrée), commentaire et pièce jointe (facultative ou
  obligatoire selon le paramètre), dans le délai paramétré.
- La justification est validée par le responsable (N+1) ; le circuit est paramétrable (Ressources humaines →
  Justification des retards). Un responsable qui justifie pour son équipe, ou un agent RH, la valide d'office.
- Le retard reste un retard dans les calculs : le suivi journalier affiche sous « Retard » « Justifié » (vert) ou
  « Justification non approuvée » (en attente), ou « Justification refusée ».
"""
import json
from datetime import date, datetime, time, timedelta
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from . import arrets, pointage
from .models import LATE_REASONS_DEFAULT, LateAction, LateJustification, LateSettings, User, utcnow

ACTIONS = {"declare": "Justification envoyée", "saisi_rh": "Saisie par les RH (validée d'office)",
           "saisi_responsable": "Saisie par le responsable (validée d'office)", "valide": "Validée",
           "refuse": "Refusée", "annule": "Annulée"}
SOURCES = {"employe": "Employé", "responsable": "Responsable", "rh": "RH"}
EVENTS = {"a_valider": "Retard à justifier : validation", "valide": "Justification de retard validée",
          "refuse": "Justification de retard refusée"}
DEFAULT_WORKFLOW = [{"type": "responsable"}]
# Libellés sous le statut « Retard » (écran et exports).
MARKS = {"valide": "Justifié", "en_attente": "Justification non approuvée", "refuse": "Justification refusée"}


class LateError(Exception):
    pass


# --------------------------------------------------------------------------- paramètres


def get_settings(db) -> LateSettings:
    row = db.scalars(select(LateSettings).order_by(LateSettings.id)).first()
    if row is None:
        row = LateSettings(workflow=json.dumps(DEFAULT_WORKFLOW), notify=True)
        db.add(row)
        db.commit()
    return row


def reasons(s: LateSettings) -> list[str]:
    return pointage.multi((s.reasons or "").split(",")) or pointage.multi(LATE_REASONS_DEFAULT.split(","))


def get_workflow(db) -> list[dict]:
    s = get_settings(db)
    if not s.workflow:
        return [dict(x) for x in DEFAULT_WORKFLOW]
    return arrets.get_workflow(db, LateSettings)


def save_workflow(db, steps: list[dict], author: str) -> None:
    get_settings(db)
    try:
        arrets.save_workflow(db, steps, author, LateSettings)
    except arrets.SickLeaveError as exc:
        raise LateError(str(exc)) from None


def notify_enabled(db) -> bool:
    get_settings(db)
    return arrets.notify_enabled(db, LateSettings)


def set_notify(db, enabled: bool, author: str) -> None:
    get_settings(db)
    arrets.set_notify(db, enabled, author, LateSettings)


# Droits et étapes : mêmes règles que les arrêts maladie (même structure de demande).
step_label, leave_steps, can_act, can_see, is_hr, get_user, timeline = (
    arrets.step_label, arrets.leave_steps, arrets.can_act, arrets.can_see, arrets.is_hr, arrets.get_user, arrets.timeline)


def visible_requests(db, user: Optional[User], role: Optional[str], username: str, limit: int = 300) -> list:
    rows = db.scalars(select(LateJustification).order_by(LateJustification.day.desc(), LateJustification.id.desc())
                      .limit(2000)).all()
    return [x for x in rows if can_see(x, user, role, username)][:limit]


def pending_for(db, user: Optional[User], role: Optional[str], username: str) -> list:
    rows = db.scalars(select(LateJustification).where(LateJustification.status == "en_attente")
                      .order_by(LateJustification.created_at)).all()
    return [x for x in rows if can_act(x, user, role, username)]


def by_day(db, du: date, au: date) -> dict:
    """État de la justification de chaque retard : {(clé employé, jour): statut} (la plus récente non annulée)."""
    rows = db.scalars(select(LateJustification).where(
        LateJustification.status != "annule", LateJustification.day >= datetime.combine(du, time()),
        LateJustification.day <= datetime.combine(au, time())).order_by(LateJustification.id)).all()
    return {(r.emp_key, r.day.date()): r.status for r in rows}


def by_day_ids(db, du: date, au: date) -> dict:
    rows = db.scalars(select(LateJustification).where(
        LateJustification.status != "annule", LateJustification.day >= datetime.combine(du, time()),
        LateJustification.day <= datetime.combine(au, time())).order_by(LateJustification.id)).all()
    return {(r.emp_key, r.day.date()): r for r in rows}


# --------------------------------------------------------------------------- retards dans PostgreSQL


def late_days(engine: Engine, m: pointage.Mapping, emp_key: str, du: date, au: date) -> list[dict]:
    """Journées en retard d'un employé : jour, heure d'arrivée, minutes de retard."""
    S = pointage.qi(m.objs)
    with engine.connect() as c:
        rows = c.execute(text(f"SELECT jour, premier_pointage, retard_min FROM {S}.f_pointage_journalier(:du, :au) "
                              f"WHERE emp_key = :k AND statut = 'RETARD' ORDER BY jour DESC"),
                         {"du": du, "au": au, "k": emp_key}).mappings().all()
    return [{"jour": r["jour"], "arrivee": r["premier_pointage"].strftime("%H:%M") if r["premier_pointage"] else "",
             "retard_min": int(r["retard_min"] or 0)} for r in rows]


def my_late_days(db, engine: Engine, m: pointage.Mapping, user: Optional[User], today: Optional[date] = None) -> list[dict]:
    """Retards de l'employé connecté encore justifiables (dans le délai), avec l'état de leur justification."""
    if not (user and user.emp_matricule):
        return []
    found = pointage.resolve_employee(engine, m, user.emp_matricule)
    if found is None:
        return []
    s = get_settings(db)
    today = today or date.today()
    du = today - timedelta(days=max(s.retro_days or 0, 0))
    days = late_days(engine, m, found[0], du, today)
    known = by_day_ids(db, du, today)
    for d in days:
        d["justification"] = known.get((found[0], d["jour"]))
    return days


# --------------------------------------------------------------------------- justification et décisions


def _log(db, req: LateJustification, actor: str, action: str, label: str = "", comment: str = "") -> None:
    db.add(LateAction(request_id=req.id, actor=actor, action=action, step_label=label, comment=comment or ""))


def source_for(engine: Engine, m: pointage.Mapping, user: Optional[User], role: Optional[str], username: str,
               emp_key: str) -> Optional[str]:
    """Qui justifie pour cet employé : « rh », « employe » (lui-même), « responsable » (sa hiérarchie), sinon None."""
    if is_hr(user, role, username):
        return "rh"
    if not (user and user.emp_matricule):
        return None
    found = pointage.resolve_employee(engine, m, user.emp_matricule)
    if found is None:
        return None
    if found[0] == emp_key:
        return "employe"
    return "responsable" if pointage.in_scope(engine, m, found[0], emp_key) else None


def create(db, engine: Engine, m: pointage.Mapping, *, matricule: str, day: Optional[date], reason: str, comment: str,
           file: Optional[tuple[str, bytes]], user: Optional[User], role: Optional[str], username: str,
           today: Optional[date] = None) -> LateJustification:
    s = get_settings(db)
    today = today or date.today()
    if day is None:
        raise LateError("Indiquez le jour du retard.")
    if reason not in reasons(s):
        raise LateError("Choisissez le motif du retard dans la liste.")
    found = pointage.resolve_employee(engine, m, matricule or "")
    if found is None:
        raise LateError(f"Aucun employé trouvé pour le matricule « {matricule} ».")
    emp_key, name = found
    source = source_for(engine, m, user, role, username, emp_key)
    if source is None:
        raise LateError("Vous ne pouvez justifier un retard que pour vous-même ou pour une personne de votre équipe.")
    if source != "rh" and day < today - timedelta(days=s.retro_days or 0):
        raise LateError(f"Un retard ne peut être justifié qu'au plus {s.retro_days or 0} jour(s) après : "
                        f"adressez-vous aux RH pour une date antérieure.")
    late = late_days(engine, m, emp_key, day, day)
    if not late:
        raise LateError(f"Aucun retard enregistré pour {name} le {day:%d/%m/%Y}.")
    other = db.scalars(select(LateJustification).where(
        LateJustification.emp_key == emp_key, LateJustification.day == datetime.combine(day, time()),
        LateJustification.status.in_(("en_attente", "valide")))).first()
    if other is not None:
        raise LateError(f"Ce retard est déjà justifié ({other.status_label.lower()}).")
    stored = ("", "", "")
    if file is not None:
        try:
            stored = arrets.store_file(*file)
        except arrets.SickLeaveError as exc:
            raise LateError(str(exc)) from None
    elif s.file_required and source == "employe":
        raise LateError("Joignez une pièce justificative (PDF, JPG ou PNG).")
    req = LateJustification(emp_key=emp_key, name=name, day=datetime.combine(day, time()), arrival=late[0]["arrivee"],
                            late_min=late[0]["retard_min"], reason=reason, comment=(comment or "").strip()[:2000],
                            source=source, created_by=username,
                            file_name=stored[0], file_path=stored[1], file_type=stored[2])
    req.matricule = pointage.employee_matricule(engine, m, emp_key) or (matricule or "").strip()
    manager = pointage.manager_of(engine, m, emp_key)
    if manager:
        req.manager_matricule, req.manager_name = manager
    steps = [] if source in ("rh", "responsable") else get_workflow(db)
    req.workflow, req.status = json.dumps(steps), ("valide" if not steps else "en_attente")
    if req.status == "valide":
        req.decided_at = utcnow()
    db.add(req)
    db.flush()
    _log(db, req, username, {"rh": "saisi_rh", "responsable": "saisi_responsable"}.get(source, "declare"),
         comment=req.comment)
    if req.status == "valide" and source == "employe":
        _log(db, req, "système", "valide", "Aucune étape de validation paramétrée")
    db.commit()
    return req


def decide(db, req: LateJustification, *, approve: bool, comment: str, user: Optional[User], role: Optional[str],
           username: str) -> LateJustification:
    if not can_act(req, user, role, username):
        raise LateError("Vous ne pouvez pas valider cette étape de la justification.")
    steps = leave_steps(req)
    label = step_label(steps[req.step])
    if not approve:
        if not (comment or "").strip():
            raise LateError("Indiquez le motif du refus.")
        req.status, req.decided_at = "refuse", utcnow()
        _log(db, req, username, "refuse", label, comment)
        db.commit()
        return req
    _log(db, req, username, "valide", label, comment)
    req.step += 1
    if req.step >= len(steps):
        req.status, req.decided_at = "valide", utcnow()
    db.commit()
    return req


def cancel(db, req: LateJustification, *, comment: str, user: Optional[User], role: Optional[str],
           username: str) -> LateJustification:
    hr = is_hr(user, role, username)
    if req.status in ("refuse", "annule"):
        raise LateError("Cette justification est déjà close.")
    if req.status == "valide" and not hr:
        raise LateError("Une justification validée ne peut être annulée que par les RH.")
    if req.status == "en_attente" and not (hr or req.created_by == username):
        raise LateError("Seul l'auteur de la justification ou les RH peuvent l'annuler.")
    req.status, req.decided_at = "annule", utcnow()
    _log(db, req, username, "annule", comment=comment)
    db.commit()
    return req


def history(db, req: LateJustification) -> list[LateAction]:
    return db.scalars(select(LateAction).where(LateAction.request_id == req.id)
                      .order_by(LateAction.at, LateAction.id)).all()


# --------------------------------------------------------------------------- notifications par e-mail


def build_notification(req: LateJustification, event: str, base_url: str, company: str = "", comment: str = "") -> tuple:
    rows = [("Employé", f"{req.name} ({req.matricule})"), ("Jour", f"{req.day:%d/%m/%Y}"),
            ("Arrivée", f"{req.arrival.replace(':', 'h')} · {req.late_label} de retard" if req.arrival else req.late_label),
            ("Motif", req.reason)]
    if req.comment:
        rows.append(("Commentaire", req.comment[:500]))
    link = f"{base_url.rstrip('/')}/retards/{req.id}" if base_url else ""
    if event == "a_valider":
        steps = leave_steps(req)
        rows.append(("Étape à valider", step_label(steps[req.step]) if req.step < len(steps) else "—"))
        title, intro, button = (f"Justification de retard à valider — {req.name}",
                                "Un retard a été justifié et attend votre validation dans l'application Suivi de présence.",
                                "Examiner la justification")
    elif event == "valide":
        title, intro, button = (f"Retard justifié — {req.name}",
                                "La justification du retard a été validée : il apparaît comme justifié dans le suivi.",
                                "Voir la justification")
    else:
        if comment:
            rows.append(("Motif du refus", comment[:500]))
        title, intro, button = (f"Justification de retard refusée — {req.name}",
                                "La justification du retard a été refusée. Rapprochez-vous de votre responsable.",
                                "Voir la justification")
    text_body = f"{title}\n\n{intro}\n\n" + "\n".join(f"{k} : {v}" for k, v in rows) + (f"\n\n{link}" if link else "")
    return title, arrets._mail_html(title, intro, req, rows, link, button, company, eyebrow="Retard",
                                    accent="#c2410c"), text_body


def notify(request_id: int, event: str, base_url: str = "", actor: str = "", comment: str = "") -> dict:
    """Mails d'un événement (valideurs de l'étape en cours, puis l'employé). Ne lève jamais d'exception."""
    from .database import SessionLocal
    from .joblog import write_log
    from .mails import get_settings as mail_settings_of

    try:
        with SessionLocal() as db:
            if not notify_enabled(db) or event not in EVENTS:
                return {"status": "disabled"}
            req = db.get(LateJustification, request_id)
            ms = mail_settings_of(db)
            if req is None or not ms.smtp_host or not ms.from_email:
                return {"status": "unconfigured"}
            users = arrets.step_recipients(db, req) if event == "a_valider" else arrets.outcome_recipients(db, req)
            users = [u for u in users if u.username != actor]
            recipients = arrets.addresses(db, users)
            if not recipients:
                return {"status": "no_recipient", "sent": 0}
            subject, body, text_body = build_notification(req, event, base_url, ms.company, comment)
            messages, production = arrets.build_messages(ms, recipients, subject, body, text_body, "Retards")
            if messages is None:
                return {"status": "no_test_recipient", "sent": 0}
        arrets.send_messages(ms, messages)
        write_log("INFO", f"Justification de retard n°{request_id} : {len(messages)} mail(s) « {EVENTS[event].lower()} » "
                          f"envoyé(s)" + ("" if production else " (mode test)") + ".")
        return {"status": "sent", "sent": len(messages)}
    except Exception as exc:  # noqa: BLE001
        try:
            write_log("ERROR", f"Justification de retard n°{request_id} : notification impossible "
                               f"({exc.__class__.__name__}: {exc}).")
        except Exception:  # noqa: BLE001
            pass
        return {"status": "error", "error": str(exc)}


def start_notify(request_id: int, event: str, base_url: str = "", actor: str = "", comment: str = "") -> None:
    import threading

    threading.Thread(target=notify, args=(request_id, event, base_url, actor, comment), daemon=True).start()
