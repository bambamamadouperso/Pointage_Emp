"""Préférences e-mail de chaque employé : mails de badge (confirmation de pointage) et rappels de pointage.

Elles sont portées par la liste des abonnés (MailSubscriber, une ligne par employé) : l'employé les choisit sur sa
page « Mon compte » (si l'administrateur ne les a pas verrouillées), l'administrateur pour chaque utilisateur
(Administration → Utilisateurs) ou en masse (Mails de badge → Abonnés).
- Mails de badge : employés abonnés dont la case « mails de badge » est cochée.
- Rappels : selon le public choisi dans Rappels de pointage — « abonnés » : case « rappels » cochée ;
  « tous » : tous les employés, sauf ceux qui ont décoché la case.
"""
from typing import Optional

from sqlalchemy import or_, select

from . import pointage
from .models import MailSubscriber, User


def badge_on(sub: Optional[MailSubscriber]) -> bool:
    return sub is not None and sub.badge_enabled is not False


def reminders_on(sub: Optional[MailSubscriber], everyone: bool) -> bool:
    """Rappels reçus : abonné qui ne les a pas refusés ; public « tous » : tout employé qui ne les a pas refusés."""
    if sub is None:
        return everyone
    return sub.reminders_enabled is not False


def reminders_for_all(db) -> bool:
    from .rappels import audience, get_settings

    return audience(get_settings(db)) == "tous"


def subscriber_of(db, matricule: str, emp_key: str = "") -> Optional[MailSubscriber]:
    keys = [k for k in {(matricule or "").strip(), (emp_key or "").strip()} if k]
    if not keys:
        return None
    return db.scalars(select(MailSubscriber).where(or_(MailSubscriber.matricule.in_(keys),
                                                       MailSubscriber.emp_key.in_(keys)))).first()


def current(db, user: Optional[User]) -> Optional[dict]:
    """Préférences d'un compte rattaché à un employé : {badge, rappels, verrouillé} ; None sans matricule."""
    if user is None or not user.emp_matricule:
        return None
    sub = subscriber_of(db, user.emp_matricule)
    return {"badge": badge_on(sub), "rappels": reminders_on(sub, reminders_for_all(db)),
            "locked": bool(user.mail_prefs_locked)}


def save(db, engine, m: pointage.Mapping, matricule: str, *, badge: bool, rappels: bool, actor: str) -> MailSubscriber:
    """Enregistre les deux choix pour l'employé de ce matricule (ligne d'abonné créée si besoin). Lève ValueError."""
    found = pointage.resolve_employee(engine, m, (matricule or "").strip())
    if found is None:
        raise ValueError(f"Aucun employé trouvé pour le matricule « {matricule} ».")
    emp_key, name = found
    sub = db.scalars(select(MailSubscriber).where(MailSubscriber.emp_key == emp_key)).first()
    if sub is None:
        sub = MailSubscriber(emp_key=emp_key, added_by=actor, email="")
        db.add(sub)
    sub.matricule = pointage.employee_matricule(engine, m, emp_key) or matricule.strip()
    sub.name = name
    sub.badge_enabled, sub.reminders_enabled = badge, rappels
    db.commit()
    return sub


def label(prefs: Optional[dict]) -> str:
    if prefs is None:
        return ""
    return ", ".join(name for name, on in (("badge", prefs["badge"]), ("rappels", prefs["rappels"])) if on) or "aucun"
