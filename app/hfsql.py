"""Source HFSQL Client/Serveur (PC SOFT / WINDEV), lue via le pilote ODBC HFSQL.

Le pilote ODBC HFSQL doit être installé sur le serveur qui exécute l'application (version 64 bits
pour un Python 64 bits). Il est fourni gratuitement par PC SOFT (« Pilote ODBC HFSQL »).

Chaîne de connexion utilisée :
    DRIVER={HFSQL};Server Name=<hôte>;Server Port=<port>;Database=<base>;UID=<utilisateur>;PWD=<mot de passe>
Le champ « options » de la connexion permet de choisir un autre nom de pilote (ex. « HyperFileSQL »
pour les anciennes versions) et d'ajouter des paramètres : « DRIVER=HyperFileSQL;Password=secret ».
"""
import datetime as dt
import os
import threading
import time
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
    dsn = options.pop("DSN", None) or options.pop("dsn", None)
    if dsn:
        # Source ODBC déclarée dans l'administrateur ODBC Windows (DSN système) : le pilote y trouve
        # le serveur, le port et la base ; on ne transmet que l'utilisateur et le mot de passe.
        parts = {"DSN": dsn, "UID": conn.username, "PWD": decrypt(conn.password_enc)}
    else:
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
    return ";".join(f"{k}={v if k == 'DRIVER' else _brace(str(v))}" for k, v in parts.items() if v is not None) + ";"


def masked(connection: str) -> str:
    """Chaîne de connexion affichable (mot de passe masqué)."""
    import re

    return re.sub(r"((?:PWD|Password)=)(\{[^}]*\}|[^;]*)", r"\1*****", connection, flags=re.IGNORECASE)


# Certains serveurs HFSQL mettent plus d'une minute à ouvrir une connexion ODBC : délais larges, configurables.
CONNECT_TIMEOUT = int(os.getenv("HFSQL_CONNECT_TIMEOUT", "240"))
QUERY_TIMEOUT = int(os.getenv("HFSQL_QUERY_TIMEOUT", "1800"))
META_TIMEOUT = 300
IDLE_CHECK_AFTER = 300  # s : au-delà, on vérifie que la connexion gardée répond encore


def odbc_message(exc: Exception) -> str:
    args = getattr(exc, "args", ())
    return str(args[1] if len(args) > 1 else exc).strip()


def _missing_dsn_message(visible) -> str:
    return (
        "Source ODBC introuvable pour l'application. Elle tourne en tâche de fond (compte SYSTEM) et ne voit "
        "que les sources déclarées dans l'onglet « DSN système » de l'administrateur ODBC 64 bits "
        "(C:\\Windows\\System32\\odbcad32.exe), pas les « DSN utilisateur » ni l'administrateur 32 bits. "
        f"Sources visibles par l'application : {', '.join(visible or []) or 'aucune'}."
    )


# --------------------------------------------------------------------------- processus du pilote


class _ProcessWorker:
    """Pilote ODBC exécuté dans un processus séparé (un plantage du pilote n'arrête pas le site)."""

    def __init__(self):
        import multiprocessing

        from . import odbc_worker

        ctx = multiprocessing.get_context("spawn")
        self.pipe, child = ctx.Pipe()
        self.proc = ctx.Process(target=odbc_worker.serve, args=(child,), daemon=True, name="pilote-hfsql")
        self.proc.start()
        child.close()

    def call(self, op: str, timeout: float, **kw):
        try:
            self.pipe.send({"op": op, **kw})
            ready = self.pipe.poll(timeout)
        except (BrokenPipeError, EOFError, OSError):
            ready = True
        if not ready:
            self.kill()
            raise HfsqlError(f"Le pilote ODBC HFSQL ne répond pas après {timeout:.0f} s (opération : {op}).")
        try:
            reply = self.pipe.recv()
        except (EOFError, OSError):
            self.proc.join(5)
            code = self.proc.exitcode
            if isinstance(code, int) and code < 0:
                code_txt = f"signal {-code}"
            elif isinstance(code, int) and code > 255:
                code_txt = f"0x{code:08X}"  # ex. 0xC0000005 : violation d'accès dans le pilote
            else:
                code_txt = str(code)
            raise HfsqlError(
                f"Le pilote ODBC HFSQL s'est arrêté brutalement (code {code_txt}) pendant « {op} ». "
                "Le site reste disponible ; vérifiez la version du pilote ODBC HFSQL (identique à celle du serveur) "
                "et lancez diagnostic-hfsql.bat pour plus de détails."
            ) from None
        if reply[0] == "err":
            raise _RemoteError(reply[1], reply[2])
        return reply[1]

    def alive(self) -> bool:
        return self.proc.is_alive()

    def kill(self) -> None:
        try:
            self.pipe.send({"op": "quit"})
        except Exception:
            pass
        self.proc.join(2)
        if self.proc.is_alive():
            self.proc.kill()
            self.proc.join(5)
        try:
            self.pipe.close()
        except Exception:
            pass


class _InlineWorker:
    """Même protocole, exécuté dans le processus courant (tests avec un faux pyodbc)."""

    def __init__(self):
        self.state: dict = {}
        self.dead = False

    def call(self, op: str, timeout: float, **kw):
        from . import odbc_worker

        pyodbc = _pyodbc()
        try:
            return call_with_timeout(lambda: odbc_worker.handle(self.state, {"op": op, **kw}, pyodbc), timeout,
                                     f"Le pilote ODBC HFSQL ne répond pas après {timeout:.0f} s (opération : {op}).")
        except NetError as exc:
            self.dead = True
            raise HfsqlError(str(exc)) from exc
        except odbc_worker.OdbcFailure as exc:
            raise _RemoteError(str(exc), exc.visible) from exc
        except Exception as exc:
            raise _RemoteError(odbc_message(exc), None) from exc

    def alive(self) -> bool:
        return not self.dead

    def kill(self) -> None:
        cnx = self.state.get("cnx")
        if cnx is not None:
            try:
                cnx.close()
            except Exception:
                pass
        self.dead = True


class _RemoteError(Exception):
    def __init__(self, message: str, visible=None):
        super().__init__(message)
        self.visible = visible


def _new_worker():
    return _InlineWorker() if os.getenv("HFSQL_ISOLATION", "process") == "inline" else _ProcessWorker()


def _open_worker(conn, cs: str):
    """Démarre un processus pilote et y ouvre la connexion ODBC."""
    try:
        check_port(conn.host, conn.port or 4900)
    except NetError as exc:
        raise HfsqlError(str(exc)) from exc
    worker = _new_worker()
    try:
        worker.call("connect", CONNECT_TIMEOUT, cs=cs, login_timeout=min(CONNECT_TIMEOUT, 600))
    except _RemoteError as exc:
        worker.kill()
        if exc.visible is not None:
            raise HfsqlError(_missing_dsn_message(exc.visible)) from exc
        raise HfsqlError(f"Connexion HFSQL impossible : {exc}") from exc
    except HfsqlError as exc:
        worker.kill()
        if "ne répond pas" in str(exc):
            raise HfsqlError(
                f"Le pilote ODBC HFSQL ne répond pas après {CONNECT_TIMEOUT} s (le port {conn.port} est pourtant "
                "joignable). Vérifiez le nom de la base, l'utilisateur et le mot de passe, et testez la connexion "
                "dans l'administrateur ODBC 64 bits (odbcad32)."
            ) from exc
        raise
    return worker


# Processus pilotes gardés ouverts entre deux exécutions (l'ouverture peut prendre plus d'une minute).
_pool: dict[str, dict] = {}
_pool_guard = threading.Lock()


def reset_pool() -> None:
    with _pool_guard:
        entries = list(_pool.values())
        _pool.clear()
    for entry in entries:
        entry["worker"].kill()


class Source:
    """Connexion HFSQL (via le processus pilote), réutilisée d'une exécution à l'autre si possible."""

    def __init__(self, conn, reuse: bool = True):
        self.key = connection_string(conn)
        self.pooled = False
        self.worker = None
        if reuse:
            with _pool_guard:
                entry = _pool.get(self.key)
                if entry and entry["lock"].acquire(blocking=False):
                    self.worker, self.entry, self.pooled = entry["worker"], entry, True
            if self.pooled and not self._healthy():
                self._discard()
        if self.worker is None:
            self.worker = _open_worker(conn, self.key)
            if reuse:
                with _pool_guard:
                    if self.key not in _pool:
                        self.entry = {"worker": self.worker, "lock": threading.Lock(), "used": time.monotonic()}
                        self.entry["lock"].acquire()
                        _pool[self.key] = self.entry
                        self.pooled = True
        try:
            quote = self._call("getinfo", META_TIMEOUT, code=SQL_IDENTIFIER_QUOTE_CHAR)
        except HfsqlError:
            quote = '"'
        self.quote_char = (quote or "").strip()

    def _healthy(self) -> bool:
        if not self.worker.alive():
            return False
        if time.monotonic() - self.entry["used"] <= IDLE_CHECK_AFTER:
            return True
        try:
            self.worker.call("probe", 60)
            return True
        except Exception:
            return False

    def _call(self, op: str, timeout: float, **kw):
        try:
            return self.worker.call(op, timeout, **kw)
        except _RemoteError as exc:
            raise HfsqlError(str(exc)) from exc

    def _discard(self) -> None:
        with _pool_guard:
            if _pool.get(self.key) is getattr(self, "entry", None):
                del _pool[self.key]
        self.worker.kill()
        self.worker, self.pooled = None, False

    def close(self, discard: bool = False) -> None:
        """Rend la connexion au pool (ou l'arrête si elle a posé problème)."""
        if self.worker is None:
            return
        if self.pooled and not discard and self.worker.alive():
            self.entry["used"] = time.monotonic()
            self.entry["lock"].release()
            self.worker = None
            return
        if self.pooled:
            self.entry["lock"].release()
            self._discard()
            return
        self.worker.kill()
        self.worker = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *exc):
        self.close(discard=exc_type is not None)

    def quote(self, name: str) -> str:
        if not self.quote_char:
            return name
        return f"{self.quote_char}{name}{self.quote_char}"

    def describe(self) -> str:
        try:
            return f"{self._call('getinfo', META_TIMEOUT, code=SQL_DBMS_NAME)} " \
                   f"{self._call('getinfo', META_TIMEOUT, code=SQL_DBMS_VER)}".strip()
        except HfsqlError:
            return "HFSQL"

    def tables(self) -> list[str]:
        return sorted({n for n in self._call("tables", META_TIMEOUT) if n})

    def primary_key(self, table: str) -> list[str]:
        try:
            return self._call("primary_keys", META_TIMEOUT, table=table)
        except HfsqlError:
            return []

    def build_table(self, name: str) -> Table:
        """Décrit la table (types déduits de la description ODBC d'une requête vide)."""
        try:
            description = self._call("describe", META_TIMEOUT, sql=f"SELECT * FROM {self.quote(name)} WHERE 1=0")
        except HfsqlError as exc:
            raise HfsqlError(f"Table HFSQL « {name} » illisible : {exc}") from exc
        pk = set(self.primary_key(name))
        columns = [
            Column(col, map_type(python_type, precision, scale), primary_key=col in pk)
            for col, python_type, precision, scale in description
        ]
        return Table(name, MetaData(), *columns)

    def count(self, table: str) -> int:
        return int(self._call("scalar", QUERY_TIMEOUT, sql=f"SELECT COUNT(*) FROM {self.quote(table)}"))

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
        self._call("execute", QUERY_TIMEOUT, sql=sql, params=params)
        return _RemoteCursor(self)


class _RemoteCursor:
    def __init__(self, source: Source):
        self.source = source

    def fetchmany(self, n: int) -> list[tuple]:
        return self.source._call("fetchmany", QUERY_TIMEOUT, n=n)

    def close(self) -> None:
        try:
            self.source._call("close_cursor", 60)
        except HfsqlError:
            pass


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
