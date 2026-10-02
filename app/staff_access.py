"""Accès de tout le personnel : chaque employé actif se connecte avec l'adresse e-mail de sa fiche (colonne e-mail
de la source des pointages) et le mot de passe standard défini par l'administrateur.

- Aucun compte à créer : il est créé automatiquement à la première connexion (rôle lecteur, périmètre « son
  équipe » : lui-même et toutes les personnes placées sous lui dans la hiérarchie).
- Un employé qui a choisi son propre mot de passe (Mon compte) se connecte ensuite avec celui-ci ; tant qu'il ne l'a
  pas fait, le mot de passe standard en vigueur s'applique (même s'il est changé par l'administrateur).
- Un employé qui n'est plus actif dans la liste ne peut plus se connecter par ce moyen.
- Les comptes administrateurs (et autres comptes créés à la main) ne changent pas.
"""
import logging
from typing import Optional

from sqlalchemy import select, text

from . import pointage
from .models import PointageConfig, StaffAccessSettings, User, utcnow

logger = logging.getLogger("staff_access")


def get_settings(db) -> StaffAccessSettings:
    row = db.scalars(select(StaffAccessSettings).order_by(StaffAccessSettings.id)).first()
    if row is None:
        row = StaffAccessSettings()
        db.add(row)
        db.commit()
    return row


def _directory_query(db, sql: str, params: dict) -> Optional[list]:
    """Requête sur la liste des employés (v_pointage_employes) ; None si la source n'est pas configurée."""
    from .sync import make_engine

    cfg = db.scalars(select(PointageConfig).order_by(PointageConfig.id)).first()
    if cfg is None or cfg.conn is None or cfg.installed_at is None:
        return None
    m = pointage.Mapping.from_json(cfg.data)
    if not m.email_col:
        return None
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
    try:
        with engine.connect() as c:
            return [dict(r) for r in c.execute(text(sql.format(S=pointage.qi(m.objs))), params).mappings()]
    finally:
        engine.dispose()


def find_employee(db, email: str) -> Optional[dict]:
    """Employé actif dont la fiche porte cette adresse (une seule fiche, sinon refus : adresse partagée)."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    try:
        rows = _directory_query(db, "SELECT emp_key, matricule, nom, prenom, email FROM {S}.v_pointage_employes "
                                    "WHERE actif AND email = :e LIMIT 2", {"e": email})
    except Exception:  # noqa: BLE001 - base indisponible : pas de connexion par ce moyen
        logger.exception("Recherche de l'employé %s impossible", email)
        return None
    if not rows or len({r["emp_key"] for r in rows}) > 1:
        return None
    return rows[0]


def overview(db) -> dict:
    """Employés actifs, avec ou sans adresse e-mail, et comptes déjà ouverts par ce moyen."""
    try:
        rows = _directory_query(db, "SELECT matricule, nom, prenom, email FROM {S}.v_pointage_employes WHERE actif "
                                    "ORDER BY nom, prenom", {})
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc).splitlines()[0][:300], "configured": True}
    if rows is None:
        return {"configured": False}
    emails = [r["email"] for r in rows if r["email"]]
    duplicates = sorted({e for e in emails if emails.count(e) > 1})
    opened = db.query(User).filter(User.auto_account.is_(True)).count()
    return {"configured": True, "employes": len(rows), "avec_email": len(emails), "sans_email": [r for r in rows if not r["email"]],
            "doublons": duplicates, "comptes": opened}


def standard_ok(db, email: str, password: str) -> bool:
    """Mot de passe standard valable pour cette adresse (accès ouvert, employé actif)."""
    from .auth import verify_password

    s = get_settings(db)
    return bool(s.enabled and s.password_hash and verify_password(password, s.password_hash)
                and find_employee(db, email) is not None)


def login(db, identifier: str, password: str) -> Optional[User]:
    """Connexion d'un employé par e-mail + mot de passe standard : compte créé à la première connexion."""
    from .auth import hash_password, verify_password

    s = get_settings(db)
    if not s.enabled or not s.password_hash or not verify_password(password, s.password_hash):
        return None
    employee = find_employee(db, identifier)
    if employee is None:
        return None
    email = identifier.strip().lower()
    user = db.query(User).filter(User.username == email).one_or_none()
    if user is None:
        name = " ".join(x for x in (employee["nom"], employee["prenom"]) if x)
        user = User(username=email, email=email, full_name=name[:200], role="lecteur", scope="equipe",
                    emp_matricule=employee["matricule"], active=True, auto_account=True, personal_password=False,
                    password_hash=hash_password(password), must_change_password=bool(s.force_change))
        db.add(user)
    elif s.force_change and not user.personal_password:
        user.must_change_password = True
    user.emp_matricule = employee["matricule"] or user.emp_matricule
    user.last_login_at = utcnow()
    db.commit()
    return user
