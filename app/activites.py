"""Activités extérieures : formation, réunion à l'extérieur, visite client… déclarées par l'employé.

- L'employé déclare une activité (journée entière ou plage horaire, sur un ou plusieurs jours), avant ou après coup
  dans la limite du délai paramétré ; son responsable (toute sa hiérarchie) ou un agent RH peut aussi la déclarer.
- Options (Ressources humaines → Activités extérieures) : validation obligatoire des déclarations des employés
  (circuit paramétrable, comme les arrêts maladie), effet immédiat quand c'est le responsable qui déclare ; une
  saisie RH est toujours validée d'office.
- Une activité validée est recopiée dans PostgreSQL (table pointage_activites) : journée sans pointage en
  « Activité extérieure », pas de retard ni de pointage partiel pendant l'activité, durée créditée selon le
  paramètre (plage déclarée ou forfait par jour).
"""
import json
import re
from datetime import date, datetime, time, timedelta
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from . import arrets, pointage
from .models import OUTING_CREDIT, OutingAction, OutingRequest, OutingSettings, User, utcnow

ACTIONS = {"declare": "Déclarée", "saisi_rh": "Saisie par les RH (validée d'office)",
           "saisi_responsable": "Déclarée par le responsable", "valide": "Validée", "refuse": "Refusée",
           "annule": "Annulée"}
SOURCES = {"employe": "Employé", "responsable": "Responsable", "rh": "RH"}
EVENTS = {"a_valider": "Activité à valider", "valide": "Activité validée", "refuse": "Activité refusée"}
MAX_DAYS = 366
_TIME = re.compile(r"^([01]?\d|2[0-3])[:hH]([0-5]\d)$")


class OutingError(Exception):
    pass


# --------------------------------------------------------------------------- paramètres


def get_settings(db) -> OutingSettings:
    row = db.scalars(select(OutingSettings).order_by(OutingSettings.id)).first()
    if row is None:
        row = OutingSettings()
        db.add(row)
        db.commit()
    return row


def kinds(s: OutingSettings) -> list[str]:
    return pointage.multi((s.kinds or "").split(",")) or ["Formation", "Réunion extérieure", "Autre"]


def parse_time(value: str) -> Optional[str]:
    match = _TIME.match((value or "").strip())
    return f"{int(match.group(1)):02d}:{match.group(2)}" if match else None


def get_workflow(db) -> list[dict]:
    get_settings(db)
    return arrets.get_workflow(db, OutingSettings)


def save_workflow(db, steps: list[dict], author: str) -> None:
    get_settings(db)
    try:
        arrets.save_workflow(db, steps, author, OutingSettings)
    except arrets.SickLeaveError as exc:
        raise OutingError(str(exc)) from None


def notify_enabled(db) -> bool:
    return arrets.notify_enabled(db, OutingSettings)


def set_notify(db, enabled: bool, author: str) -> None:
    get_settings(db)
    arrets.set_notify(db, enabled, author, OutingSettings)


# Droits et étapes : mêmes règles que les arrêts maladie (même structure de demande).
step_label, leave_steps, can_act, can_see, is_hr, get_user, timeline = (
    arrets.step_label, arrets.leave_steps, arrets.can_act, arrets.can_see, arrets.is_hr, arrets.get_user, arrets.timeline)


def visible_requests(db, user: Optional[User], role: Optional[str], username: str, limit: int = 300) -> list:
    rows = db.scalars(select(OutingRequest).order_by(OutingRequest.created_at.desc(), OutingRequest.id.desc())
                      .limit(2000)).all()
    return [x for x in rows if can_see(x, user, role, username)][:limit]


def pending_for(db, user: Optional[User], role: Optional[str], username: str) -> list:
    rows = db.scalars(select(OutingRequest).where(OutingRequest.status == "en_attente")
                      .order_by(OutingRequest.created_at)).all()
    return [x for x in rows if can_act(x, user, role, username)]


def pending_by_day(db, du: date, au: date) -> dict:
    """Activités déclarées mais pas encore validées : {(clé employé, jour): libellé} (étiquette du suivi)."""
    out = {}
    rows = db.scalars(select(OutingRequest).where(
        OutingRequest.status == "en_attente", OutingRequest.start_date <= datetime.combine(au, time()),
        OutingRequest.end_date >= datetime.combine(du, time()))).all()
    for r in rows:
        day = max(r.start_date.date(), du)
        while day <= min(r.end_date.date(), au):
            out[(r.emp_key, day)] = f"{r.kind} ({r.hours_label}) en attente de validation"
            day += timedelta(days=1)
    return out


# --------------------------------------------------------------------------- PostgreSQL


def ensure_table(engine: Engine, m: pointage.Mapping) -> None:
    with engine.begin() as c:
        c.execute(text(pointage.outing_table_sql(m)))


def sync_pg(engine: Engine, m: pointage.Mapping, req: OutingRequest, s: OutingSettings) -> None:
    """Recopie une activité validée dans PostgreSQL (mode de crédit en vigueur), la retire sinon."""
    S = pointage.qi(m.objs)
    ensure_table(engine, m)
    with engine.begin() as c:
        c.execute(text(f"DELETE FROM {S}.pointage_activites WHERE id = :i"), {"i": req.id})
        if req.status == "valide":
            c.execute(text(
                f"INSERT INTO {S}.pointage_activites (id, emp_key, matricule, du, au, debut, fin, type, mode, forfait) "
                f"VALUES (:i, :k, :m, :d, :a, CAST(:deb AS time), CAST(:fin AS time), :t, :mode, CAST(:f AS interval))"),
                {"i": req.id, "k": req.emp_key, "m": req.matricule, "d": req.start_date.date(), "a": req.end_date.date(),
                 "deb": None if req.full_day else req.start_time, "fin": None if req.full_day else req.end_time,
                 "t": req.kind, "mode": s.credit_mode if s.credit_mode in OUTING_CREDIT else "plage",
                 "f": s.forfait or "08:00"})


# --------------------------------------------------------------------------- déclaration et décisions


def _log(db, req: OutingRequest, actor: str, action: str, label: str = "", comment: str = "") -> None:
    db.add(OutingAction(request_id=req.id, actor=actor, action=action, step_label=label, comment=comment or ""))


def source_for(engine: Engine, m: pointage.Mapping, user: Optional[User], role: Optional[str], username: str,
               emp_key: str) -> Optional[str]:
    """Qui déclare pour cet employé : « rh », « employe » (lui-même), « responsable » (dans sa hiérarchie), sinon None."""
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


def create(db, engine: Engine, m: pointage.Mapping, *, matricule: str, kind: str, start: date, end: date,
           full_day: bool, start_time: str, end_time: str, place: str, subject: str, comment: str,
           file: Optional[tuple[str, bytes]], user: Optional[User], role: Optional[str], username: str,
           today: Optional[date] = None) -> OutingRequest:
    s = get_settings(db)
    today = today or date.today()
    if kind not in kinds(s):
        raise OutingError("Choisissez le type d'activité dans la liste.")
    if start is None or end is None:
        raise OutingError("Indiquez les dates de début et de fin.")
    if end < start:
        raise OutingError("La date de fin doit être postérieure ou égale à la date de début.")
    if (end - start).days >= MAX_DAYS:
        raise OutingError(f"Une activité ne peut pas dépasser {MAX_DAYS} jours : faites plusieurs déclarations.")
    if not full_day:
        start_time, end_time = parse_time(start_time), parse_time(end_time)
        if not (start_time and end_time):
            raise OutingError("Indiquez l'heure de début et l'heure de fin (HH:MM), ou cochez « journée entière ».")
        if end_time <= start_time:
            raise OutingError("L'heure de fin doit suivre l'heure de début.")
    found = pointage.resolve_employee(engine, m, matricule or "")
    if found is None:
        raise OutingError(f"Aucun employé trouvé pour le matricule « {matricule} ».")
    emp_key, name = found
    source = source_for(engine, m, user, role, username, emp_key)
    if source is None:
        raise OutingError("Vous ne pouvez déclarer une activité que pour vous-même ou pour une personne de votre équipe.")
    if source != "rh" and start < today - timedelta(days=s.retro_days or 0):
        raise OutingError(f"Une activité ne peut être déclarée qu'au plus {s.retro_days or 0} jour(s) après coup : "
                          f"adressez-vous aux RH pour une date antérieure.")
    for other in db.scalars(select(OutingRequest).where(
            OutingRequest.emp_key == emp_key, OutingRequest.status.in_(("en_attente", "valide")),
            OutingRequest.start_date <= datetime.combine(end, time()),
            OutingRequest.end_date >= datetime.combine(start, time()))):
        if full_day or other.full_day or (start_time < other.end_time and other.start_time < end_time):
            raise OutingError(f"Une activité couvre déjà cette période ({other.kind}, du {other.start_date:%d/%m/%Y} "
                              f"au {other.end_date:%d/%m/%Y}, {other.hours_label}, {other.status_label.lower()}).")
    stored = ("", "", "")
    if file is not None:
        try:
            stored = arrets.store_file(*file)
        except arrets.SickLeaveError as exc:
            raise OutingError(str(exc)) from None
    req = OutingRequest(emp_key=emp_key, name=name, kind=kind, full_day=full_day,
                        start_time="" if full_day else start_time, end_time="" if full_day else end_time,
                        start_date=datetime.combine(start, time()), end_date=datetime.combine(end, time()),
                        place=(place or "").strip()[:255], subject=(subject or "").strip()[:255],
                        comment=(comment or "").strip()[:2000], source=source, created_by=username,
                        file_name=stored[0], file_path=stored[1], file_type=stored[2])
    req.matricule = pointage.employee_matricule(engine, m, emp_key) or (matricule or "").strip()
    manager = pointage.manager_of(engine, m, emp_key)
    if manager:
        req.manager_matricule, req.manager_name = manager
    direct = source == "rh" or (source == "responsable" and s.manager_direct) or \
        (source == "employe" and not s.validation_required)
    steps = [] if direct else get_workflow(db)
    req.workflow, req.status = json.dumps(steps), ("valide" if not steps else "en_attente")
    if req.status == "valide":
        req.decided_at = utcnow()
    db.add(req)
    db.flush()
    _log(db, req, username, {"rh": "saisi_rh", "responsable": "saisi_responsable"}.get(source, "declare"),
         comment=req.comment)
    if req.status == "valide" and source == "employe":
        _log(db, req, "système", "valide", "Validation non requise" if not s.validation_required
             else "Aucune étape de validation paramétrée")
    db.commit()
    if req.status == "valide":
        sync_pg(engine, m, req, s)
    return req


def decide(db, engine: Engine, m: pointage.Mapping, req: OutingRequest, *, approve: bool, comment: str,
           user: Optional[User], role: Optional[str], username: str) -> OutingRequest:
    if not can_act(req, user, role, username):
        raise OutingError("Vous ne pouvez pas valider cette étape de la déclaration.")
    steps = leave_steps(req)
    label = step_label(steps[req.step])
    if not approve:
        if not (comment or "").strip():
            raise OutingError("Indiquez le motif du refus.")
        req.status, req.decided_at = "refuse", utcnow()
        _log(db, req, username, "refuse", label, comment)
        db.commit()
        return req
    _log(db, req, username, "valide", label, comment)
    req.step += 1
    if req.step >= len(steps):
        req.status, req.decided_at = "valide", utcnow()
    db.commit()
    if req.status == "valide":
        sync_pg(engine, m, req, get_settings(db))
    return req


def cancel(db, engine: Optional[Engine], m: pointage.Mapping, req: OutingRequest, *, comment: str,
           user: Optional[User], role: Optional[str], username: str) -> OutingRequest:
    hr = is_hr(user, role, username)
    if req.status in ("refuse", "annule"):
        raise OutingError("Cette déclaration est déjà close.")
    if req.status == "valide" and not hr:
        raise OutingError("Une activité validée ne peut être annulée que par les RH.")
    if req.status == "en_attente" and not (hr or req.created_by == username):
        raise OutingError("Seul l'auteur de la déclaration ou les RH peuvent l'annuler.")
    was_valid = req.status == "valide"
    req.status, req.decided_at = "annule", utcnow()
    _log(db, req, username, "annule", comment=comment)
    db.commit()
    if was_valid and engine is not None:
        sync_pg(engine, m, req, get_settings(db))
    return req


def history(db, req: OutingRequest) -> list[OutingAction]:
    return db.scalars(select(OutingAction).where(OutingAction.request_id == req.id)
                      .order_by(OutingAction.at, OutingAction.id)).all()


# --------------------------------------------------------------------------- notifications par e-mail


def build_notification(req: OutingRequest, event: str, base_url: str, company: str = "", comment: str = "") -> tuple:
    period = f"du {req.start_date:%d/%m/%Y} au {req.end_date:%d/%m/%Y}" if req.days > 1 else f"le {req.start_date:%d/%m/%Y}"
    rows = [("Employé", f"{req.name} ({req.matricule})"), ("Activité", req.kind + (f" — {req.subject}" if req.subject else "")),
            ("Date", f"{period} · {req.hours_label}")]
    if req.place:
        rows.append(("Lieu", req.place))
    link = f"{base_url.rstrip('/')}/activites/{req.id}" if base_url else ""
    if event == "a_valider":
        steps = leave_steps(req)
        rows.append(("Étape à valider", step_label(steps[req.step]) if req.step < len(steps) else "—"))
        rows.append(("Déclarée par", req.created_by))
        title, intro, button = (f"Activité extérieure à valider — {req.name}",
                                "Une activité extérieure attend votre validation dans l'application Suivi de présence.",
                                "Examiner la déclaration")
    elif event == "valide":
        title, intro, button = (f"Activité extérieure validée — {req.name}",
                                "L'activité a été validée : elle est prise en compte dans le suivi des pointages.",
                                "Voir la déclaration")
    else:
        if comment:
            rows.append(("Motif du refus", comment[:500]))
        title, intro, button = (f"Activité extérieure refusée — {req.name}",
                                "L'activité a été refusée. Rapprochez-vous de votre responsable ou du service RH.",
                                "Voir la déclaration")
    text_body = f"{title}\n\n{intro}\n\n" + "\n".join(f"{k} : {v}" for k, v in rows) + (f"\n\n{link}" if link else "")
    return title, arrets._mail_html(title, intro, req, rows, link, button, company, eyebrow="Activité extérieure",
                                    accent="#4338ca"), text_body


def notify(request_id: int, event: str, base_url: str = "", actor: str = "", comment: str = "") -> dict:
    """Mails d'un événement (valideurs de l'étape en cours, puis l'employé). Ne lève jamais d'exception."""
    from .database import SessionLocal
    from .joblog import write_log
    from .mails import get_settings as mail_settings_of

    try:
        with SessionLocal() as db:
            if not notify_enabled(db) or event not in EVENTS:
                return {"status": "disabled"}
            req = db.get(OutingRequest, request_id)
            ms = mail_settings_of(db)
            if req is None or not ms.smtp_host or not ms.from_email:
                return {"status": "unconfigured"}
            users = arrets.step_recipients(db, req) if event == "a_valider" else arrets.outcome_recipients(db, req)
            users = [u for u in users if u.username != actor]
            recipients = arrets.addresses(db, users)
            if not recipients:
                return {"status": "no_recipient", "sent": 0}
            subject, body, text_body = build_notification(req, event, base_url, ms.company, comment)
            messages, production = arrets.build_messages(ms, recipients, subject, body, text_body, "Activités extérieures")
            if messages is None:
                return {"status": "no_test_recipient", "sent": 0}
        arrets.send_messages(ms, messages)
        write_log("INFO", f"Activité extérieure n°{request_id} : {len(messages)} mail(s) « {EVENTS[event].lower()} » "
                          f"envoyé(s)" + ("" if production else " (mode test)") + ".")
        return {"status": "sent", "sent": len(messages)}
    except Exception as exc:  # noqa: BLE001
        try:
            write_log("ERROR", f"Activité extérieure n°{request_id} : notification impossible ({exc.__class__.__name__}: {exc}).")
        except Exception:  # noqa: BLE001
            pass
        return {"status": "error", "error": str(exc)}


def start_notify(request_id: int, event: str, base_url: str = "", actor: str = "", comment: str = "") -> None:
    import threading

    threading.Thread(target=notify, args=(request_id, event, base_url, actor, comment), daemon=True).start()
