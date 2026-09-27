"""Source HFSQL Client/Serveur (PC SOFT / WINDEV), lue via le pilote ODBC HFSQL.

Le pilote ODBC HFSQL doit être installé sur le serveur qui exécute l'application (version 64 bits
pour un Python 64 bits). Il est fourni gratuitement par PC SOFT (« Pilote ODBC HFSQL »).

Chaîne de connexion utilisée :
    DRIVER={HFSQL};Server Name=<hôte>;Server Port=<port>;Database=<base>;UID=<utilisateur>;PWD=<mot de passe>
Le champ « options » de la connexion permet de choisir un autre nom de pilote (ex. « HyperFileSQL »
pour les anciennes versions) et d'ajouter des paramètres : « DRIVER=HyperFileSQL;Password=secret ».
"""
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Double,
    LargeBinary,
    MetaData,
    Numeric,
    Table,
    Text,
    Time,
)

from .crypto import decrypt
from .netcheck import NetError, call_with_timeout, check_port

DRIVER_HINTS = ("hfsql", "hyperfile")
SQL_IDENTIFIER_QUOTE_CHAR = 29
SQL_DBMS_NAME = 17
SQL_DBMS_VER = 18


class HfsqlError(Exception):
    pass


def _pyodbc():
    try:
        import pyodbc
    except ImportError as exc:  # bibliothèque ou unixODBC absents
        raise HfsqlError(
            "Le module pyodbc n'est pas disponible : relancez l'installateur (pip install pyodbc)."
        ) from exc
    return pyodbc


def parse_options(options: Optional[str]) -> dict[str, str]:
    """« DRIVER=HFSQL;Password=x » -> {"DRIVER": "HFSQL", "Password": "x"}"""
    out: dict[str, str] = {}
    for part in (options or "").split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            if key.strip():
                out[key.strip()] = value.strip()
    return out


def find_driver(preferred: Optional[str] = None) -> str:
    drivers = _pyodbc().drivers()
    if preferred:
        return preferred
    for name in drivers:
        if any(h in name.lower() for h in DRIVER_HINTS):
            return name
    raise HfsqlError(
        "Pilote ODBC HFSQL introuvable sur ce serveur. Installez le « Pilote ODBC HFSQL » 64 bits de PC SOFT "
        f"(pilotes ODBC installés : {', '.join(drivers) or 'aucun'})."
    )


def _brace(value: str) -> str:
    """Protège une valeur contenant des caractères spéciaux dans une chaîne ODBC."""
    if any(c in value for c in ";{}=") or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


def connection_string(conn) -> str:
    options = parse_options(getattr(conn, "options", None))
    driver = find_driver(options.pop("DRIVER", None) or options.pop("Driver", None))
    parts = {
        "DRIVER": "{" + driver + "}",
        "Server Name": conn.host,
        "Server Port": str(conn.port or 4900),
        "Database": conn.database,
        "UID": conn.username,
        "PWD": decrypt(conn.password_enc),
    }
    parts.update(options)
    return ";".join(f"{k}={v if k == 'DRIVER' else _brace(str(v))}" for k, v in parts.items()) + ";"


CONNECT_TIMEOUT = 30


def connect(conn, timeout: int = 15):
    pyodbc = _pyodbc()
    cs = connection_string(conn)  # vérifie aussi la présence du pilote
    # 1) Le port HFSQL répond-il ? (réponse en quelques secondes au lieu d'un blocage du pilote)
    try:
        check_port(conn.host, conn.port or 4900)
    except NetError as exc:
        raise HfsqlError(str(exc)) from exc

    # 2) Connexion ODBC, avec un délai maximal : certains pilotes ignorent leur propre délai.
    def _open():
        return pyodbc.connect(cs, timeout=timeout, autocommit=True)

    try:
        return call_with_timeout(
            _open, CONNECT_TIMEOUT,
            f"Le pilote ODBC HFSQL ne répond pas après {CONNECT_TIMEOUT} s (le port {conn.port} est pourtant "
            "joignable). Vérifiez le nom de la base, l'utilisateur et le mot de passe, et testez la connexion "
            "dans l'administrateur ODBC 64 bits (odbcad32).",
        )
    except NetError as exc:
        raise HfsqlError(str(exc)) from exc
    except pyodbc.Error as exc:
        raise HfsqlError(f"Connexion HFSQL impossible : {odbc_message(exc)}") from exc


def odbc_message(exc: Exception) -> str:
    args = getattr(exc, "args", ())
    return str(args[1] if len(args) > 1 else exc).strip()


class Source:
    """Connexion ODBC ouverte pour la durée d'un job."""

    def __init__(self, conn):
        self.cnx = connect(conn)
        try:
            quote = self.cnx.getinfo(SQL_IDENTIFIER_QUOTE_CHAR)
        except Exception:
            quote = '"'
        self.quote_char = (quote or "").strip()

    def close(self) -> None:
        try:
            self.cnx.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def quote(self, name: str) -> str:
        if not self.quote_char:
            return name
        return f"{self.quote_char}{name}{self.quote_char}"

    def describe(self) -> str:
        try:
            return f"{self.cnx.getinfo(SQL_DBMS_NAME)} {self.cnx.getinfo(SQL_DBMS_VER)}".strip()
        except Exception:
            return "HFSQL"

    def tables(self) -> list[str]:
        cur = self.cnx.cursor()
        try:
            names = [row.table_name for row in cur.tables(tableType="TABLE")]
        finally:
            cur.close()
        return sorted({n for n in names if n})

    def primary_key(self, table: str) -> list[str]:
        cur = self.cnx.cursor()
        try:
            rows = sorted(cur.primaryKeys(table=table), key=lambda r: r.key_seq or 0)
            return [r.column_name for r in rows]
        except Exception:
            return []  # fonction non gérée par le pilote
        finally:
            cur.close()

    def build_table(self, name: str) -> Table:
        """Décrit la table (types déduits de la description ODBC d'une requête vide)."""
        cur = self.cnx.cursor()
        try:
            cur.execute(f"SELECT * FROM {self.quote(name)} WHERE 1=0")
            description = cur.description
        except Exception as exc:
            raise HfsqlError(f"Table HFSQL « {name} » illisible : {odbc_message(exc)}") from exc
        finally:
            cur.close()
        pk = set(self.primary_key(name))
        columns = [
            Column(d[0], map_type(d[1], d[4], d[5]), primary_key=d[0] in pk)
            for d in description
        ]
        return Table(name, MetaData(), *columns)

    def count(self, table: str) -> int:
        cur = self.cnx.cursor()
        try:
            cur.execute(f"SELECT COUNT(*) FROM {self.quote(table)}")
            return int(cur.fetchone()[0])
        finally:
            cur.close()

    def select(self, table: str, columns: list[str], inc: Optional[str] = None,
               last: Any = None, strict: bool = True):
        """Curseur ouvert sur les lignes à lire (filtrées sur la colonne de suivi si demandé)."""
        sql = f"SELECT {', '.join(self.quote(c) for c in columns)} FROM {self.quote(table)}"
        params: list = []
        if inc:
            sql += f" WHERE {self.quote(inc)} IS NOT NULL"
            if last is not None:
                sql += f" AND {self.quote(inc)} {'>' if strict else '>='} ?"
                params.append(last)
            sql += f" ORDER BY {self.quote(inc)}"
        cur = self.cnx.cursor()
        cur.execute(sql, *params)
        return cur


def map_type(python_type, precision, scale):
    """Type ODBC (classe Python renvoyée par pyodbc) -> type PostgreSQL."""
    if python_type is bool:
        return Boolean()
    if python_type is int:
        return BigInteger()
    if python_type is float:
        return Double()
    if python_type is Decimal:
        if precision and 0 < precision <= 1000 and scale is not None and 0 <= scale <= precision:
            return Numeric(precision, scale)
        return Numeric()
    if python_type is dt.datetime:
        return DateTime()
    if python_type is dt.date:
        return Date()
    if python_type is dt.time:
        return Time()
    if python_type in (bytes, bytearray):
        return LargeBinary()
    return Text()  # chaînes, mémos, UUID…


def clean(value: Any, target) -> Any:
    """Nettoie une valeur lue en ODBC avant écriture dans PostgreSQL."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace("\x00", "")
        if isinstance(target, (Date, DateTime, Time)):
            # HFSQL peut renvoyer une date vide ("" ou "00000000") sous forme de texte.
            stripped = value.strip()
            if not stripped or set(stripped) <= set("0-:/ "):
                return None
            from .gsheet import convert

            try:
                return convert(stripped, target)
            except (ValueError, TypeError):
                return None
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytearray):
        return bytes(value)
    return value
