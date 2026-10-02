"""Utilisateurs, rôles (admin, manager, lecteur) et journal d'audit."""
import hashlib
import hmac
import re
import secrets
from typing import Optional

from fastapi import Request

from .config import settings
from .database import SessionLocal
from .models import ROLES, AuditEntry, User, utcnow

ROLE_LEVEL = {"lecteur": 1, "rh": 1, "manager": 2, "admin": 3}
# Rôle RH : tout le personnel (suivi, rapports, arrêts) et les réglages RH, mais ni la synchronisation (tableau de
# bord, jobs, exécutions, logs, données, connexions) ni la source des pointages, les utilisateurs ou l'audit.
RH_ROLES = ("rh", "admin")
PBKDF2_ROUNDS = 240_000

# --------------------------------------------------------------------------- mots de passe


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ROUNDS).hex()
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds)).hex()
    return hmac.compare_digest(candidate, digest)


def password_problem(password: str) -> Optional[str]:
    if len(password) < 8:
        return "Le mot de passe doit contenir au moins 8 caractères."
    return None


def is_rescue_admin(username: str) -> bool:
    """Compte administrateur défini dans .env (ADMIN_USERNAME) : toujours disponible, rôle admin."""
    return secrets.compare_digest(username.encode(), settings.admin_username.encode())


def authenticate(username: str, password: str) -> Optional[str]:
    """Rôle de l'utilisateur si les identifiants sont bons, sinon None (voir login)."""
    found = login(username, password)
    return found[0] if found else None


def login(identifier: str, password: str) -> Optional[tuple[str, str]]:
    """(rôle, identifiant du compte) si la connexion est acceptée, sinon None.

    L'identifiant peut être celui du compte ou son adresse e-mail. Un employé (accès de tout le personnel) se
    connecte avec l'e-mail de sa fiche et le mot de passe standard tant qu'il n'a pas choisi le sien ; son compte
    est créé à la première connexion."""
    from . import staff_access
    from sqlalchemy import func

    identifier = identifier.strip()
    if is_rescue_admin(identifier):
        ok = secrets.compare_digest(password.encode(), settings.admin_password.encode())
        return ("admin", identifier) if ok else None
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == identifier).one_or_none()
        if user is None and "@" in identifier:
            email = identifier.lower()
            user = db.query(User).filter((func.lower(User.username) == email) | (func.lower(User.email) == email)) \
                .order_by(User.id).first()
        if user is not None:
            if not user.active:
                return None
            if user.auto_account:
                # Compte d'employé : il doit toujours figurer parmi les employés actifs.
                if staff_access.find_employee(db, user.email or user.username) is None:
                    return None
                if not user.personal_password:
                    return (user.role, user.username) if staff_access.login(db, user.username, password) else None
            if not verify_password(password, user.password_hash):
                # Compte d'employé créé autrement (ex. par matricule) et sans mot de passe personnel : le mot de passe
                # standard de l'accès du personnel vaut aussi pour lui, s'il est encore actif.
                if not (user.role == "lecteur" and user.emp_matricule and not user.personal_password
                        and user.email and staff_access.standard_ok(db, user.email, password)):
                    return None
                if staff_access.get_settings(db).force_change:
                    user.must_change_password = True
            user.last_login_at = utcnow()
            db.commit()
            return user.role, user.username
        created = staff_access.login(db, identifier, password) if "@" in identifier else None
        return (created.role, created.username) if created else None


def account_state(username: str) -> tuple[Optional[str], bool]:
    """(rôle actuel, mot de passe provisoire à changer) ; rôle None si le compte n'existe plus ou est désactivé."""
    if is_rescue_admin(username):
        return "admin", False
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username).one_or_none()
        if user is None or not user.active or user.role not in ROLES:
            return None, False
        return user.role, bool(user.must_change_password)


def current_role(username: str) -> Optional[str]:
    """Rôle actuel (relu à chaque requête : un changement de rôle ou une désactivation s'applique aussitôt)."""
    if is_rescue_admin(username):
        return "admin"
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username).one_or_none()
        return user.role if user is not None and user.active and user.role in ROLES else None


def has_role(role: Optional[str], minimum: str) -> bool:
    if minimum == "rh":
        return role in RH_ROLES
    return ROLE_LEVEL.get(role or "", 0) >= ROLE_LEVEL[minimum]


# --------------------------------------------------------------------------- droits d'accès

PUBLIC_PATHS = re.compile(r"^/(login|health|static/.*)$")

# (méthode, chemin, rôle minimum) : la première règle qui correspond s'applique ; sinon « admin ».
RULES = [
    ("GET", re.compile(r"^/jobs/(new|\d+/edit|\d+/columns)$"), "admin"),
    ("*", re.compile(r"^/(suivi|rapports)(/.*)?$"), "lecteur"),
    ("*", re.compile(r"^/arrets(/.*)?$"), "lecteur"),
    ("*", re.compile(r"^/(compte|logout)$"), "lecteur"),
    # Réglages RH (pas la source des pointages, les mails de badge/SMTP, les utilisateurs ni l'audit).
    ("GET", re.compile(r"^/admin$"), "rh"),
    ("*", re.compile(r"^/admin/(parametres|feries|planning|terrain|arrets|resumes|verifier)(/.*)?$"), "rh"),
    ("GET", re.compile(r"^/$"), "manager"),
    ("GET", re.compile(r"^/(jobs|runs|logs|data)(/.*)?$"), "manager"),
    ("POST", re.compile(r"^/jobs/\d+/(run|stop)$"), "manager"),
]


def required_role(method: str, path: str) -> str:
    for rule_method, pattern, role in RULES:
        if rule_method in ("*", method) and pattern.match(path):
            return role
    return "admin"


def home_for(role: Optional[str]) -> str:
    return "/" if has_role(role, "manager") and role != "rh" else "/suivi"


# --------------------------------------------------------------------------- audit


def audit(request: Optional[Request], action: str, target: str = "", details: str = "",
          username: Optional[str] = None) -> None:
    """Enregistre une modification dans le journal d'audit (ne fait jamais échouer l'action)."""
    try:
        user = username if username is not None else (request.session.get("user", "") if request else "")
        ip = request.client.host if request is not None and request.client else ""
        with SessionLocal() as db:
            db.add(AuditEntry(username=user or "", action=action[:200], target=target[:300],
                              details=details or "", ip=ip or ""))
            db.commit()
    except Exception:  # noqa: BLE001 - l'audit ne doit pas bloquer l'application
        import logging

        logging.getLogger("audit").exception("Écriture du journal d'audit impossible")


# Actions des autres pages (connexions, jobs), journalisées automatiquement après une requête réussie.
_AUTO_AUDIT = [
    (re.compile(r"^/connections/save$"), "Connexion enregistrée"),
    (re.compile(r"^/connections/create-database$"), "Base PostgreSQL créée"),
    (re.compile(r"^/connections/(\d+)/delete$"), "Connexion supprimée"),
    (re.compile(r"^/jobs/save$"), "Job enregistré"),
    (re.compile(r"^/jobs/(\d+)/run$"), "Job lancé manuellement"),
    (re.compile(r"^/jobs/(\d+)/stop$"), "Exécution arrêtée"),
    (re.compile(r"^/jobs/(\d+)/reload$"), "Réimport complet demandé"),
    (re.compile(r"^/jobs/(\d+)/toggle$"), "Job activé / désactivé"),
    (re.compile(r"^/jobs/(\d+)/delete$"), "Job supprimé"),
    (re.compile(r"^/jobs/(\d+)/tables$"), "Table ajoutée au job"),
    (re.compile(r"^/jobs/(\d+)/tables/add-all$"), "Toutes les tables ajoutées au job"),
    (re.compile(r"^/jobs/(\d+)/tables/(\d+)/update$"), "Table du job modifiée"),
    (re.compile(r"^/jobs/(\d+)/tables/(\d+)/toggle$"), "Table du job activée / désactivée"),
    (re.compile(r"^/jobs/(\d+)/tables/(\d+)/reset$"), "Curseur de table réinitialisé"),
    (re.compile(r"^/jobs/(\d+)/tables/(\d+)/delete$"), "Table retirée du job"),
]


def auto_audit(request: Request, status_code: int) -> None:
    if request.method != "POST" or status_code >= 400:
        return
    path = request.url.path
    for pattern, label in _AUTO_AUDIT:
        m = pattern.match(path)
        if m:
            ids = m.groups()
            target = ""
            if path.startswith("/jobs/") and ids:
                target = f"job #{ids[0]}" + (f", table #{ids[1]}" if len(ids) > 1 else "")
            elif path.startswith("/connections/") and ids:
                target = f"connexion #{ids[0]}"
            audit(request, label, target)
            return
