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

from sqlalchemy import select, text
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
    return role == "admin" or is_rescue_admin(username or "") or bool(user and user.sick_leave_hr)


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
    hr = bool(user and user.sick_leave_hr)
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
