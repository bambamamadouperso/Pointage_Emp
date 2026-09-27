"""Découverte et création de bases sur un serveur (utilisé par le formulaire de connexion)."""
import re

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import DBAPIError

DB_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# Bases système masquées dans la liste.
HIDDEN = {
    "postgresql": {"template0", "template1"},
    "mariadb": {"information_schema", "performance_schema", "mysql", "sys"},
}


class DbAdminError(Exception):
    pass


def _engine(kind: str, host: str, port: int, username: str, password: str, database: str) -> Engine:
    if kind == "postgresql":
        url = URL.create("postgresql+psycopg", username=username, password=password,
                         host=host, port=port, database=database)
        return create_engine(url, isolation_level="AUTOCOMMIT", pool_pre_ping=True,
                             connect_args={"connect_timeout": 10, "application_name": "mariadb-pg-sync"})
    if kind == "mariadb":
        url = URL.create("mysql+pymysql", username=username, password=password,
                         host=host, port=port, database=database or None, query={"charset": "utf8mb4"})
        return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 10})
    raise DbAdminError("Type de serveur non pris en charge.")


def _message(exc: Exception) -> str:
    msg = str(getattr(exc, "orig", None) or exc).strip()
    return msg.splitlines()[0] if msg else exc.__class__.__name__


def _connect(kind: str, host: str, port: int, username: str, password: str, preferred: str = ""):
    """Se connecte au serveur via une base d'administration (postgres, template1) ou la base saisie."""
    candidates = ["postgres", "template1", preferred] if kind == "postgresql" else ["", preferred]
    last_error = None
    if kind == "postgresql":
        candidates = [c for c in candidates if c]
    for database in dict.fromkeys(candidates):
        engine = _engine(kind, host, port, username, password, database)
        try:
            conn = engine.connect()
            return engine, conn
        except DBAPIError as exc:
            engine.dispose()
            last_error = exc
            text_ = _message(exc).lower()
            # Erreur d'authentification ou serveur injoignable : inutile d'essayer une autre base.
            if "password" in text_ or "authentication" in text_ or "access denied" in text_ \
                    or "connection refused" in text_ or "timeout" in text_ or "could not translate" in text_:
                break
    raise DbAdminError(f"Connexion au serveur impossible : {_message(last_error)}")


def list_databases(kind: str, host: str, port: int, username: str, password: str, preferred: str = "") -> list[str]:
    engine, conn = _connect(kind, host, port, username, password, preferred)
    try:
        if kind == "postgresql":
            names = conn.execute(text(
                "SELECT datname FROM pg_database WHERE NOT datistemplate AND datallowconn ORDER BY datname"
            )).scalars().all()
        else:
            names = conn.execute(text("SHOW DATABASES")).scalars().all()
        return [n for n in names if n not in HIDDEN.get(kind, set())]
    finally:
        conn.close()
        engine.dispose()


def create_database(host: str, port: int, username: str, password: str, name: str) -> None:
    """Crée une base PostgreSQL (propriétaire : l'utilisateur de la connexion)."""
    name = (name or "").strip()
    if not DB_NAME_RE.match(name):
        raise DbAdminError(
            "Nom invalide : lettres, chiffres et « _ » uniquement, sans espace ni accent, "
            "commençant par une lettre (63 caractères max.)."
        )
    engine, conn = _connect("postgresql", host, port, username, password)
    try:
        exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).first()
        if exists:
            raise DbAdminError(f"La base « {name} » existe déjà : sélectionnez-la dans la liste.")
        quoted = engine.dialect.identifier_preparer.quote(name)
        try:
            conn.execute(text(f"CREATE DATABASE {quoted} ENCODING 'UTF8' TEMPLATE template0"))
        except DBAPIError as exc:
            if "permission denied" in _message(exc).lower():
                raise DbAdminError(
                    f"L'utilisateur « {username} » n'a pas le droit de créer une base. "
                    f"Un administrateur PostgreSQL peut l'accorder : ALTER ROLE \"{username}\" CREATEDB; "
                    "ou créer la base lui-même (pgAdmin)."
                ) from exc
            raise DbAdminError(f"Création impossible : {_message(exc)}") from exc
    finally:
        conn.close()
        engine.dispose()
