"""Arrêts maladie : déclaration avec justificatif, circuit de validation paramétrable, statut dans les calculs.

- Un employé (compte rattaché à son matricule) déclare son arrêt : il suit le circuit de validation défini dans
  les paramètres (responsable N+1, agent RH, utilisateur désigné…), étape par étape.
- Un agent RH habilité (ou un administrateur) saisit un arrêt pour n'importe quel employé : validé d'office.
- Un arrêt validé est recopié dans PostgreSQL (table pointage_arrets_maladie) : ses jours ouvrés sans pointage
  prennent le statut « Arrêt maladie ».
"""
import json
import os
import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import bindparam, select, text
from sqlalchemy.engine import Engine

from . import pointage
from .auth import is_rescue_admin
from .models import SickLeave, SickLeaveAction, SickLeaveSettings, User, utcnow

STEP_TYPES = {
    "responsable": "Responsable N+1 de l'employé",
    "rh": "Agent RH habilité",
    "utilisateur": "Utilisateur désigné",
}
DEFAULT_WORKFLOW = [{"type": "responsable"}, {"type": "rh"}]
ALLOWED_FILES = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_DAYS = 366
ACTIONS = {"declare": "Déclaré", "saisi_rh": "Saisi par les RH (validé d'office)", "valide": "Validé",
           "refuse": "Refusé", "annule": "Annulé"}


class SickLeaveError(Exception):
    pass


# --------------------------------------------------------------------------- circuit de validation


def get_workflow(db) -> list[dict]:
    row = db.scalars(select(SickLeaveSettings).order_by(SickLeaveSettings.id)).first()
    if row is None or not row.workflow:
        return [dict(s) for s in DEFAULT_WORKFLOW]
    try:
        steps = json.loads(row.workflow)
    except ValueError:
        return [dict(s) for s in DEFAULT_WORKFLOW]
    return [s for s in steps if isinstance(s, dict) and s.get("type") in STEP_TYPES]


def save_workflow(db, steps: list[dict], author: str) -> None:
    clean = []
    for s in steps:
        kind = s.get("type")
        if kind not in STEP_TYPES:
            continue
        if kind == "utilisateur":
            user = (s.get("user") or "").strip()
            if not user:
                raise SickLeaveError("Choisissez l'utilisateur de chaque étape « Utilisateur désigné ».")
            clean.append({"type": kind, "user": user})
        else:
            clean.append({"type": kind})
    row = db.scalars(select(SickLeaveSettings).order_by(SickLeaveSettings.id)).first()
    if row is None:
        row = SickLeaveSettings()
        db.add(row)
    row.workflow, row.updated_at, row.updated_by = json.dumps(clean), utcnow(), author
    db.commit()


def step_label(step: dict) -> str:
    if step.get("type") == "utilisateur":
        return f"Utilisateur « {step.get('user', '')} »"
    return STEP_TYPES.get(step.get("type"), step.get("type", ""))


def leave_steps(leave: SickLeave) -> list[dict]:
    try:
        return json.loads(leave.workflow or "[]")
    except ValueError:
        return []


# --------------------------------------------------------------------------- droits


def get_user(db, username: str) -> Optional[User]:
    return db.query(User).filter(User.username == username).one_or_none()


def is_hr(user: Optional[User], role: Optional[str], username: str = "") -> bool:
    return role in ("admin", "rh") or is_rescue_admin(username or "") or bool(user and user.sick_leave_hr)


def can_act(leave: SickLeave, user: Optional[User], role: Optional[str], username: str) -> bool:
    """Peut valider ou refuser l'étape en cours de cet arrêt."""
    if leave.status != "en_attente":
        return False
    steps = leave_steps(leave)
    if leave.step >= len(steps):
        return False
    if role == "admin" or is_rescue_admin(username):
        return True
    step = steps[leave.step]
    hr = bool(user and user.sick_leave_hr) or role == "rh"
    if step["type"] == "rh":
        return hr
    if step["type"] == "utilisateur":
        return username == step.get("user")
    if step["type"] == "responsable":
        if leave.manager_matricule and user and user.emp_matricule == leave.manager_matricule:
            return True
        # Responsable sans compte dans l'application : l'étape revient aux RH.
        return hr and not _manager_has_account(leave)
    return False


def _manager_has_account(leave: SickLeave) -> bool:
    from .database import SessionLocal

    if not leave.manager_matricule:
        return False
    with SessionLocal() as db:
        return db.query(User).filter(User.emp_matricule == leave.manager_matricule, User.active.is_(True)).count() > 0


def can_see(leave: SickLeave, user: Optional[User], role: Optional[str], username: str) -> bool:
    if is_hr(user, role, username) or leave.created_by == username:
        return True
    if user and user.emp_matricule and user.emp_matricule in (leave.matricule, leave.manager_matricule):
        return True
    return any(s.get("type") == "utilisateur" and s.get("user") == username for s in leave_steps(leave))


def visible_leaves(db, user: Optional[User], role: Optional[str], username: str, limit: int = 300) -> list[SickLeave]:
    rows = db.scalars(select(SickLeave).order_by(SickLeave.created_at.desc(), SickLeave.id.desc()).limit(2000)).all()
    return [x for x in rows if can_see(x, user, role, username)][:limit]


def pending_for(db, user: Optional[User], role: Optional[str], username: str) -> list[SickLeave]:
    rows = db.scalars(select(SickLeave).where(SickLeave.status == "en_attente").order_by(SickLeave.created_at)).all()
    return [x for x in rows if can_act(x, user, role, username)]


# --------------------------------------------------------------------------- justificatifs


def storage_dir() -> Path:
    base = os.getenv("ARRETS_DIR") or str(Path(__file__).resolve().parent.parent / "data" / "arrets")
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path


def store_file(name: str, data: bytes) -> tuple[str, str, str]:
    """Enregistre le justificatif sous un nom aléatoire ; renvoie (nom d'origine, nom stocké, type)."""
    ext = os.path.splitext(name or "")[1].lower()
    if ext not in ALLOWED_FILES:
        raise SickLeaveError("Justificatif : format accepté PDF, JPG ou PNG.")
    if not data:
        raise SickLeaveError("Le justificatif est vide.")
    if len(data) > MAX_FILE_BYTES:
        raise SickLeaveError("Justificatif trop volumineux (10 Mo maximum).")
    signatures = {".pdf": (b"%PDF",), ".png": (b"\x89PNG",), ".jpg": (b"\xff\xd8",), ".jpeg": (b"\xff\xd8",)}
    if not data.startswith(signatures[ext]):
        raise SickLeaveError("Le contenu du justificatif ne correspond pas à son extension (PDF, JPG ou PNG).")
    stored = f"{uuid.uuid4().hex}{ext}"
    (storage_dir() / stored).write_bytes(data)
    original = re.sub(r"[^\w.\- ()]", "_", os.path.basename(name))[:200] or f"justificatif{ext}"
    return original, stored, ALLOWED_FILES[ext]


def file_bytes(leave: SickLeave) -> Optional[bytes]:
    if not leave.file_path or "/" in leave.file_path or "\\" in leave.file_path or ".." in leave.file_path:
        return None
    path = storage_dir() / leave.file_path
    return path.read_bytes() if path.is_file() else None


# --------------------------------------------------------------------------- PostgreSQL


def ensure_table(engine: Engine, m: pointage.Mapping) -> None:
    with engine.begin() as c:
        c.execute(text(pointage.sick_table_sql(m)))


def sync_pg(engine: Engine, m: pointage.Mapping, leave: SickLeave) -> None:
    """Recopie un arrêt dans PostgreSQL s'il est validé, le retire sinon (annulation)."""
    S = pointage.qi(m.objs)
    ensure_table(engine, m)
    with engine.begin() as c:
        c.execute(text(f"DELETE FROM {S}.pointage_arrets_maladie WHERE id = :i"), {"i": leave.id})
        if leave.status == "valide":
            c.execute(text(f"INSERT INTO {S}.pointage_arrets_maladie (id, emp_key, matricule, du, au) "
                           f"VALUES (:i, :k, :m, :d, :a)"),
                      {"i": leave.id, "k": leave.emp_key, "m": leave.matricule, "d": leave.start_date.date(),
                       "a": leave.end_date.date()})


# --------------------------------------------------------------------------- déclaration et décisions


def _log(db, leave: SickLeave, actor: str, action: str, label: str = "", comment: str = "") -> None:
    db.add(SickLeaveAction(leave_id=leave.id, actor=actor, action=action, step_label=label, comment=comment or ""))


def declare(db, engine: Engine, m: pointage.Mapping, *, matricule: str, start: date, end: date, comment: str,
            file: Optional[tuple[str, bytes]], actor: str, by_hr: bool) -> SickLeave:
    if start is None or end is None:
        raise SickLeaveError("Indiquez les dates de début et de fin de l'arrêt.")
    if end < start:
        raise SickLeaveError("La date de fin doit être postérieure ou égale à la date de début.")
    if (end - start).days >= MAX_DAYS:
        raise SickLeaveError(f"Un arrêt ne peut pas dépasser {MAX_DAYS} jours : saisissez plusieurs périodes.")
    found = pointage.resolve_employee(engine, m, matricule or "")
    if found is None:
        raise SickLeaveError(f"Aucun employé trouvé pour le matricule « {matricule} ».")
    emp_key, name = found
    overlap = db.scalars(select(SickLeave).where(
        SickLeave.emp_key == emp_key, SickLeave.status.in_(("en_attente", "valide")),
        SickLeave.start_date <= datetime.combine(end, datetime.min.time()),
        SickLeave.end_date >= datetime.combine(start, datetime.min.time()))).first()
    if overlap is not None:
        raise SickLeaveError(f"Un arrêt couvre déjà une partie de cette période (du {overlap.start_date:%d/%m/%Y} "
                             f"au {overlap.end_date:%d/%m/%Y}, {overlap.status_label.lower()}).")
    if file is None and not by_hr:
        raise SickLeaveError("Joignez le justificatif d'arrêt maladie (PDF, JPG ou PNG).")
    stored = store_file(*file) if file is not None else ("", "", "")
    manager = pointage.manager_of(engine, m, emp_key)
    leave = SickLeave(emp_key=emp_key, name=name, start_date=datetime.combine(start, datetime.min.time()),
                      end_date=datetime.combine(end, datetime.min.time()), comment=(comment or "").strip()[:2000],
                      source="rh" if by_hr else "employe", created_by=actor,
                      file_name=stored[0], file_path=stored[1], file_type=stored[2])
    leave.matricule = pointage.employee_matricule(engine, m, emp_key) or matricule.strip()
    if manager:
        leave.manager_matricule, leave.manager_name = manager
    steps = [] if by_hr else get_workflow(db)
    leave.workflow = json.dumps(steps)
    leave.status = "valide" if not steps else "en_attente"
    if leave.status == "valide":
        leave.decided_at = utcnow()
    db.add(leave)
    db.flush()
    if by_hr:
        _log(db, leave, actor, "saisi_rh", comment=leave.comment)
    else:
        _log(db, leave, actor, "declare", comment=leave.comment)
        if not steps:
            _log(db, leave, "système", "valide", "Aucune étape de validation paramétrée")
    db.commit()
    if leave.status == "valide":
        sync_pg(engine, m, leave)
    return leave


def decide(db, engine: Engine, m: pointage.Mapping, leave: SickLeave, *, approve: bool, comment: str,
           user: Optional[User], role: Optional[str], username: str) -> SickLeave:
    if not can_act(leave, user, role, username):
        raise SickLeaveError("Vous ne pouvez pas valider cette étape de l'arrêt.")
    steps = leave_steps(leave)
    label = step_label(steps[leave.step])
    if not approve:
        if not (comment or "").strip():
            raise SickLeaveError("Indiquez le motif du refus.")
        leave.status, leave.decided_at = "refuse", utcnow()
        _log(db, leave, username, "refuse", label, comment)
        db.commit()
        return leave
    _log(db, leave, username, "valide", label, comment)
    leave.step += 1
    if leave.step >= len(steps):
        leave.status, leave.decided_at = "valide", utcnow()
    db.commit()
    if leave.status == "valide":
        sync_pg(engine, m, leave)
    return leave


def cancel(db, engine: Engine, m: pointage.Mapping, leave: SickLeave, *, comment: str, user: Optional[User],
           role: Optional[str], username: str) -> SickLeave:
    hr = is_hr(user, role, username)
    if leave.status in ("refuse", "annule"):
        raise SickLeaveError("Cet arrêt est déjà clos.")
    if leave.status == "valide" and not hr:
        raise SickLeaveError("Un arrêt validé ne peut être annulé que par les RH.")
    if leave.status == "en_attente" and not (hr or leave.created_by == username):
        raise SickLeaveError("Seul l'auteur de la déclaration ou les RH peuvent l'annuler.")
    was_valid = leave.status == "valide"
    leave.status, leave.decided_at = "annule", utcnow()
    _log(db, leave, username, "annule", comment=comment)
    db.commit()
    if was_valid:
        sync_pg(engine, m, leave)
    return leave


def history(db, leave: SickLeave) -> list[SickLeaveAction]:
    return db.scalars(select(SickLeaveAction).where(SickLeaveAction.leave_id == leave.id)
                      .order_by(SickLeaveAction.at, SickLeaveAction.id)).all()


def timeline(leave: SickLeave) -> list[dict]:
    """Étapes du circuit avec leur état (faite, en cours, à venir, refusée)."""
    out = []
    for i, s in enumerate(leave_steps(leave)):
        if leave.status == "refuse" and i == leave.step:
            state = "refusee"
        elif i < leave.step or leave.status == "valide":
            state = "faite"
        elif i == leave.step and leave.status == "en_attente":
            state = "en_cours"
        else:
            state = "a_venir"
        label = step_label(s)
        if s.get("type") == "responsable" and leave.manager_name:
            label += f" — {leave.manager_name}"
        out.append({"label": label, "state": state})
    return out


def working_days(start: date, end: date) -> int:
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).isoweekday() <= 5)


# --------------------------------------------------------------------------- notifications par e-mail
#
# Envoyées avec le serveur SMTP des « Mails de badge » et soumises à son mode : en mode test, tout part vers les
# adresses de test (bandeau « MODE TEST ») ; aucun mail n'atteint un employé tant que le mode production n'est
# pas choisi. Adresse d'un compte : celle saisie dans Utilisateurs, sinon celle de la fiche employé (matricule).

EVENTS = {
    "a_valider": "Arrêt maladie à valider",
    "valide": "Arrêt maladie validé",
    "refuse": "Arrêt maladie refusé",
}


def notify_enabled(db) -> bool:
    row = db.scalars(select(SickLeaveSettings).order_by(SickLeaveSettings.id)).first()
    return bool(row and row.notify)


def set_notify(db, enabled: bool, author: str) -> None:
    row = db.scalars(select(SickLeaveSettings).order_by(SickLeaveSettings.id)).first()
    if row is None:
        row = SickLeaveSettings(workflow=json.dumps(DEFAULT_WORKFLOW))
        db.add(row)
    row.notify, row.updated_at, row.updated_by = enabled, utcnow(), author
    db.commit()


def step_recipients(db, leave: SickLeave) -> list[User]:
    """Comptes qui peuvent valider l'étape en cours (responsable sans compte : les agents RH)."""
    steps = leave_steps(leave)
    if leave.status != "en_attente" or leave.step >= len(steps):
        return []
    step = steps[leave.step]
    active = db.query(User).filter(User.active.is_(True))
    hr = active.filter(User.sick_leave_hr.is_(True)).all()
    if step["type"] == "rh":
        return hr
    if step["type"] == "utilisateur":
        return active.filter(User.username == step.get("user")).all()
    managers = active.filter(User.emp_matricule == leave.manager_matricule).all() if leave.manager_matricule else []
    return managers or hr


def outcome_recipients(db, leave: SickLeave) -> list[User]:
    """Auteur de la déclaration et employé concerné (s'il a un compte)."""
    return db.query(User).filter(User.active.is_(True), (User.username == leave.created_by)
                                 | (User.emp_matricule == leave.matricule)).all()


def _directory_emails(db, matricules: set[str]) -> dict[str, str]:
    from .models import PointageConfig
    from .sync import make_engine

    if not matricules:
        return {}
    cfg = db.scalars(select(PointageConfig).order_by(PointageConfig.id)).first()
    m = pointage.Mapping.from_json(cfg.data) if cfg and cfg.data else None
    if cfg is None or cfg.conn is None or m is None or not m.email_col:
        return {}
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
    try:
        with engine.connect() as c:
            rows = c.execute(text(f"SELECT matricule, email FROM {pointage.qi(m.objs)}.v_pointage_employes "
                                  f"WHERE matricule IN :m AND email IS NOT NULL")
                             .bindparams(bindparam("m", expanding=True)), {"m": sorted(matricules)}).all()
        return {mat: email for mat, email in rows}
    except Exception:  # noqa: BLE001 - l'annuaire est facultatif
        return {}
    finally:
        engine.dispose()


def addresses(db, users: list[User]) -> list[tuple[str, str]]:
    """(adresse, nom) des comptes, sans doublon ; les comptes sans adresse sont ignorés."""
    from .mails import valid_email

    directory = _directory_emails(db, {u.emp_matricule for u in users if not u.email and u.emp_matricule})
    out, seen = [], set()
    for u in users:
        email = (u.email or directory.get(u.emp_matricule or "", "") or "").strip()
        if valid_email(email) and email.lower() not in seen:
            seen.add(email.lower())
            out.append((email, u.full_name or u.username))
    return out


def _mail_html(title: str, intro: str, leave: SickLeave, rows: list[tuple[str, str]], link: str, button: str,
               company: str) -> str:
    import html as h

    cells = "".join(
        f'<tr><td style="padding:7px 0;color:#7a8898;font-size:13px;width:40%;">{h.escape(k)}</td>'
        f'<td style="padding:7px 0;color:#0b1a2b;font-size:14px;font-weight:600;">{h.escape(v)}</td></tr>' for k, v in rows)
    cta = (f'<p style="margin:24px 0 4px;"><a href="{h.escape(link)}" style="display:inline-block;padding:11px 20px;'
           f'background:#1b4f8a;color:#ffffff;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;">'
           f'{h.escape(button)}</a></p>') if link else ""
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>{h.escape(title)}</title></head>
<body style="margin:0;padding:0;background:#f2f5f9;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f2f5f9;">
<tr><td align="center" style="padding:32px 12px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;">
<tr><td style="background:#ffffff;border:1px solid #e3e9f0;border-radius:16px;padding:30px 34px;font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
<p style="margin:0 0 6px;font-size:12px;font-weight:600;letter-spacing:1.2px;text-transform:uppercase;color:#57534e;">Arrêt maladie</p>
<h1 style="margin:0;font-size:22px;line-height:1.3;color:#0b1a2b;">{h.escape(title)}</h1>
<p style="margin:12px 0 18px;font-size:15px;line-height:1.6;color:#46566a;">{h.escape(intro)}</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-top:1px solid #eef2f6;">{cells}</table>
{cta}</td></tr>
<tr><td style="padding:18px 8px 0;text-align:center;font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:12px;color:#a3afbc;">
Message automatique — merci de ne pas y répondre.{(' © ' + h.escape(company)) if company else ''}</td></tr>
</table></td></tr></table></body></html>"""


def build_notification(leave: SickLeave, event: str, base_url: str, company: str = "", comment: str = "") -> tuple:
    """(objet, HTML, texte) du mail d'un événement de l'arrêt."""
    period = f"du {leave.start_date:%d/%m/%Y} au {leave.end_date:%d/%m/%Y}"
    rows = [("Employé", f"{leave.name} ({leave.matricule})"), ("Période", f"{period} · {leave.days} jour(s)")]
    link = f"{base_url.rstrip('/')}/arrets/{leave.id}" if base_url else ""
    if event == "a_valider":
        steps = leave_steps(leave)
        rows.append(("Étape à valider", step_label(steps[leave.step]) if leave.step < len(steps) else "—"))
        rows.append(("Déclaré par", leave.created_by))
        if leave.comment:
            rows.append(("Commentaire", leave.comment[:300]))
        title, intro, button = (f"Arrêt maladie à valider — {leave.name}",
                                "Un arrêt maladie attend votre validation dans l'application de pointage.",
                                "Examiner l'arrêt")
    elif event == "valide":
        title, intro, button = (f"Arrêt maladie validé — {leave.name}",
                                "L'arrêt maladie a été validé : ses jours apparaissent en « Arrêt maladie » dans le suivi.",
                                "Voir l'arrêt")
    else:
        if comment:
            rows.append(("Motif du refus", comment[:500]))
        title, intro, button = (f"Arrêt maladie refusé — {leave.name}",
                                "L'arrêt maladie a été refusé. Rapprochez-vous de votre responsable ou du service RH.",
                                "Voir l'arrêt")
    text_body = f"{title}\n\n{intro}\n\n" + "\n".join(f"{k} : {v}" for k, v in rows) + (f"\n\n{link}" if link else "")
    return title, _mail_html(title, intro, leave, rows, link, button, company), text_body


def notify(leave_id: int, event: str, base_url: str = "", actor: str = "", comment: str = "") -> dict:
    """Envoie les mails d'un événement. Ne lève jamais d'exception (un souci de mail ne bloque pas l'arrêt)."""
    from email.message import EmailMessage
    from email.utils import formataddr, formatdate, make_msgid, parseaddr

    from .database import SessionLocal
    from .joblog import write_log
    from .mails import _quit, get_settings, open_smtp, parse_addresses, test_banner, valid_email

    try:
        with SessionLocal() as db:
            if not notify_enabled(db) or event not in EVENTS:
                return {"status": "disabled"}
            leave = db.get(SickLeave, leave_id)
            s = get_settings(db)
            if leave is None or not s.smtp_host or not s.from_email:
                return {"status": "unconfigured"}
            users = step_recipients(db, leave) if event == "a_valider" else outcome_recipients(db, leave)
            users = [u for u in users if u.username != actor]  # pas de mail à l'auteur de l'action
            recipients = addresses(db, users)
            if not recipients:
                if users:
                    write_log("WARNING", f"Arrêt maladie n°{leave.id} ({EVENTS[event].lower()}) : aucun destinataire "
                                         f"n'a d'adresse e-mail ({', '.join(u.username for u in users)}).")
                return {"status": "no_recipient", "sent": 0}
            subject, body, text_body = build_notification(leave, event, base_url, s.company, comment)
            test_to = [a for a in parse_addresses(s.test_recipients) if valid_email(a)]
            production = s.mode == "production"
            if not production and not test_to:
                write_log("WARNING", "Arrêts maladie : mode test sans adresse de test, notification non envoyée.")
                return {"status": "no_test_recipient", "sent": 0}
            messages = []
            for email, name in recipients:
                msg = EmailMessage()
                msg["Subject"] = subject if production else f"[TEST] {subject}"
                msg["From"] = formataddr((s.from_name or "", s.from_email))
                msg["To"] = email if production else ", ".join(test_to)
                if s.reply_to:
                    msg["Reply-To"] = s.reply_to
                msg["Date"] = formatdate(localtime=True)
                msg["Message-ID"] = make_msgid(domain=(parseaddr(s.from_email)[1].split("@")[-1] or None))
                msg["Auto-Submitted"] = "auto-generated"
                html_body = body if production else re.sub(r"(<body[^>]*>)", lambda mm: mm.group(1)
                                                           + test_banner(email, name), body, count=1)
                msg.set_content(text_body if production else f"[MODE TEST — destinataire prévu : {name} <{email}>]\n\n"
                                + text_body)
                msg.add_alternative(html_body, subtype="html")
                messages.append(msg)
        client = open_smtp(s)
        try:
            for msg in messages:
                client.send_message(msg)
        finally:
            _quit(client)
        write_log("INFO", f"Arrêt maladie n°{leave_id} : {len(messages)} mail(s) « {EVENTS[event].lower()} » envoyé(s)"
                          + ("" if production else " (mode test)") + ".")
        return {"status": "sent", "sent": len(messages), "to": [e for e, _ in recipients]}
    except Exception as exc:  # noqa: BLE001
        try:
            write_log("ERROR", f"Arrêt maladie n°{leave_id} : notification impossible ({exc.__class__.__name__}: {exc}).")
        except Exception:  # noqa: BLE001
            pass
        return {"status": "error", "error": str(exc)}


def start_notify(leave_id: int, event: str, base_url: str = "", actor: str = "", comment: str = "") -> None:
    """Envoi en arrière-plan : la page répond sans attendre le serveur SMTP."""
    import threading

    threading.Thread(target=notify, args=(leave_id, event, base_url, actor, comment), daemon=True).start()
