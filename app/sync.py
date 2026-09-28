"""Moteur de synchronisation MariaDB -> PostgreSQL."""
import socket
import threading
import time as _time
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Double,
    Float,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Table,
    Text,
    Time,
    create_engine,
    event,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateSchema
from sqlalchemy.types import JSON, TypeEngine

from . import gsheet, hfsql, watermark
from .config import settings
from .database import SessionLocal
from .errors import friendly
from .netcheck import NetError, call_with_timeout, check_port
from .joblog import RunLogger, write_log
from .models import MODE_FULL, MODE_INCREMENTAL, SOURCE_KINDS, Connection, JobRun, SyncJob, TableMapping, utcnow

class JobCancelled(Exception):
    """Exécution arrêtée à la demande (bouton « Arrêter » ou durée maximale dépassée)."""


class RunControl:
    """État d'une exécution en cours : permet de l'arrêter, même bloquée dans une requête."""

    def __init__(self, job_id: int):
        self.job_id = job_id
        self.run_id: Optional[int] = None
        self.started = _time.monotonic()
        self.stop = threading.Event()
        self.reason = ""
        self.forced = False
        self._raw: list = []  # connexions DBAPI ouvertes par cette exécution
        self._workers: list = []  # processus pilotes HFSQL utilisés par cette exécution

    def check(self) -> None:
        if self.stop.is_set():
            raise JobCancelled(self.reason or "Arrêté à la demande.")

    def watch(self, engine: Engine) -> Engine:
        event.listen(engine, "connect", lambda dbapi_conn, _rec: self._raw.append(dbapi_conn))
        return engine

    def add_worker(self, worker) -> None:
        self._workers.append(worker)
        if self.stop.is_set():
            worker.kill()

    def interrupt(self, reason: str) -> None:
        """Demande l'arrêt et débloque les requêtes en cours (annulation côté serveur, pilote arrêté)."""
        self.reason = reason
        self.stop.set()
        for raw in list(self._raw):
            try:
                if type(raw).__module__.startswith("psycopg"):
                    raw.cancel()  # annule la requête PostgreSQL en cours (ex. attente d'un verrou)
                elif getattr(raw, "_sock", None) is not None:  # pymysql
                    raw._sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
        for worker in list(self._workers):
            try:
                worker.kill()
            except Exception:
                pass


# Une exécution au plus par job : une même synchronisation ne tourne jamais deux fois en parallèle.
_active: dict[int, RunControl] = {}
_active_guard = threading.Lock()


def is_running(job_id: int) -> bool:
    with _active_guard:
        return job_id in _active


def cancel_job(job_id: int, reason: str = "Arrêté à la demande.", wait: float = 20) -> str:
    """Arrête l'exécution en cours d'un job.

    Renvoie « stopped » (arrêt propre), « forced » (exécution bloquée abandonnée), « orphan » (exécution
    restée « En cours » sans tâche active, clôturée) ou « none » (rien à arrêter).
    """
    with _active_guard:
        control = _active.get(job_id)
    if control is None:
        return "orphan" if _close_orphan_runs(job_id, reason) else "none"
    write_log("WARNING", f"Arrêt demandé : {reason}", job_id=job_id, run_id=control.run_id)
    control.interrupt(reason)
    deadline = _time.monotonic() + wait
    while _time.monotonic() < deadline:
        with _active_guard:
            if _active.get(job_id) is not control:
                return "stopped"
        _time.sleep(0.2)
    # La tâche ne rend pas la main : elle est abandonnée, le job peut être relancé.
    control.forced = True
    with _active_guard:
        if _active.get(job_id) is control:
            del _active[job_id]
    _close_run(control.run_id, job_id, f"{reason} (arrêt forcé : la tâche ne répondait plus).")
    write_log("ERROR", "Arrêt forcé : la tâche bloquée est abandonnée, le job peut être relancé.",
              job_id=job_id, run_id=control.run_id)
    return "forced"


def cancel_overdue(max_minutes: int) -> None:
    """Arrête les exécutions qui dépassent la durée maximale (appelé régulièrement par le planificateur)."""
    if max_minutes <= 0:
        return
    with _active_guard:
        late = [c for c in _active.values()
                if not c.stop.is_set() and _time.monotonic() - c.started > max_minutes * 60]
    for c in late:
        cancel_job(c.job_id, f"Durée maximale dépassée ({max_minutes} min).")


def _close_run(run_id: Optional[int], job_id: int, message: str) -> None:
    if run_id is None:
        return
    db = SessionLocal()
    try:
        run = db.get(JobRun, run_id)
        if run is not None and run.status == "running":
            run.status, run.message, run.finished_at = "cancelled", message, utcnow()
            job = db.get(SyncJob, job_id)
            if job is not None:
                job.last_status, job.last_run_at = "cancelled", run.finished_at
            db.commit()
    finally:
        db.close()


def _close_orphan_runs(job_id: int, reason: str) -> int:
    db = SessionLocal()
    try:
        runs = db.scalars(select(JobRun).where(JobRun.job_id == job_id, JobRun.status == "running")).all()
        for run in runs:
            run.status, run.finished_at = "cancelled", utcnow()
            run.message = f"{reason} (exécution sans tâche active)."
        db.commit()
        return len(runs)
    finally:
        db.close()


# --------------------------------------------------------------------------- connexions


def make_engine(conn: Connection) -> Engine:
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if conn.kind == "mariadb":
        kwargs["connect_args"] = {"connect_timeout": 10, "read_timeout": 3600}
    elif conn.kind == "postgresql":
        kwargs["connect_args"] = {
            "connect_timeout": 10, "application_name": "mariadb-pg-sync",
            # Connexion coupée détectée en quelques minutes ; attente d'un verrou limitée à 10 min.
            "keepalives": 1, "keepalives_idle": 60, "keepalives_interval": 10, "keepalives_count": 5,
            "options": "-c lock_timeout=600000",
        }
    return create_engine(conn.sqlalchemy_url(), **kwargs)


def test_connection(conn: Connection) -> str:
    """Teste la connexion et renvoie la version du serveur (ou un résumé du classeur)."""
    if conn.kind == "gsheet":
        return gsheet.describe(conn)
    if conn.kind == "hfsql":
        with hfsql.Source(conn) as src:
            summary = src.describe()
            try:
                count = call_with_timeout(lambda: len(src.tables()), 120, "liste des tables trop longue")
                return f"{summary} — {count} table(s)"
            except NetError:
                return f"{summary} — connexion réussie (la liste des tables met plus de 2 min à répondre)"
    check_port(conn.host, conn.port)
    engine = make_engine(conn)
    try:
        with engine.connect() as c:
            return str(c.execute(text("SELECT version()")).scalar())
    finally:
        engine.dispose()


def list_tables(conn: Connection) -> list[str]:
    if conn.kind == "gsheet":
        return list(gsheet.load_sheets(conn))
    if conn.kind == "hfsql":
        with hfsql.Source(conn) as src:
            return src.tables()
    engine = make_engine(conn)
    try:
        return sorted(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def list_columns(conn: Connection, table: str) -> list[dict]:
    if conn.kind == "gsheet":
        sheets = gsheet.load_sheets(conn)
        if table not in sheets:
            raise ValueError(f"Onglet « {table} » introuvable.")
        data = sheets[table]
        sheet_table = gsheet.build_table(table, data)
        return [
            {"name": c.name, "type": f"{c.type.__class__.__name__} ← « {h} »", "pk": False}
            for c, h in zip(sheet_table.columns, data.headers)
        ]
    if conn.kind == "hfsql":
        with hfsql.Source(conn) as src:
            table_ = src.build_table(table)
        return [{"name": c.name, "type": str(c.type), "pk": c.primary_key} for c in table_.columns]
    engine = make_engine(conn)
    try:
        insp = inspect(engine)
        pk = set(insp.get_pk_constraint(table).get("constrained_columns") or [])
        return [
            {"name": c["name"], "type": str(c["type"]), "pk": c["name"] in pk}
            for c in insp.get_columns(table)
        ]
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- types


def map_type(src: TypeEngine) -> TypeEngine:
    """Convertit un type MariaDB réfléchi en type compatible PostgreSQL."""
    unsigned = getattr(src, "unsigned", False)
    if isinstance(src, mysql.YEAR):
        return SmallInteger()
    if isinstance(src, mysql.BIT):
        return BigInteger()
    if isinstance(src, mysql.SET):
        return Text()
    if isinstance(src, mysql.ENUM):
        return String(max((len(v) for v in src.enums), default=1) or 1)
    if isinstance(src, mysql.BIGINT):
        return Numeric(20, 0) if unsigned else BigInteger()
    if isinstance(src, (mysql.INTEGER, mysql.MEDIUMINT)):
        return BigInteger() if unsigned else Integer()
    if isinstance(src, (mysql.SMALLINT, mysql.TINYINT)):
        return Integer() if unsigned else SmallInteger()
    if isinstance(src, (mysql.TINYTEXT, mysql.MEDIUMTEXT, mysql.LONGTEXT, mysql.TEXT)):
        return Text()
    if isinstance(src, (mysql.DOUBLE, mysql.REAL)):
        return Double()
    if isinstance(src, mysql.FLOAT):
        return Float()
    if isinstance(src, JSON):
        return JSONB()
    if isinstance(src, mysql.TIMESTAMP) or isinstance(src, mysql.DATETIME):
        return DateTime()
    try:
        generic = src.as_generic()
    except NotImplementedError:
        return Text()
    if isinstance(generic, String) and not isinstance(generic, Text):
        return String(generic.length) if generic.length else Text()
    if isinstance(generic, Text):
        return Text()
    if isinstance(generic, LargeBinary):
        return LargeBinary()
    if isinstance(generic, Numeric) and not isinstance(generic, Float):
        return Numeric(generic.precision, generic.scale)
    if isinstance(generic, (Boolean, Date, DateTime, Time, Integer, SmallInteger, BigInteger, Float)):
        return type(generic)()
    return Text()


def _value_converter(src_type: TypeEngine, dst_type: TypeEngine) -> Optional[Callable[[Any], Any]]:
    """Renvoie une fonction de nettoyage des valeurs pour une colonne, ou None si inutile."""
    is_temporal = isinstance(dst_type, (Date, DateTime))
    is_time = isinstance(dst_type, Time)

    def convert(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            # MariaDB autorise les dates "zéro" que PostgreSQL refuse.
            if is_temporal and value.startswith("0000-00-00"):
                return None
            # PostgreSQL refuse le caractère NUL dans les textes.
            return value.replace("\x00", "") if "\x00" in value else value
        if isinstance(value, (set, frozenset)):
            return ",".join(sorted(value))
        if is_time and isinstance(value, timedelta):
            # PyMySQL renvoie les colonnes TIME sous forme de timedelta.
            seconds = value.total_seconds()
            if not 0 <= seconds < 86400:
                raise ValueError(f"Valeur TIME hors plage PostgreSQL : {value}")
            return (datetime.min + value).time()
        return value

    return convert


# --------------------------------------------------------------------------- tables cibles


def ensure_target_table(
    dst_engine: Engine,
    src_table: Table,
    target_name: str,
    schema: str,
    key_columns: list[str],
    log: RunLogger,
) -> Table:
    """Crée la table cible si besoin, ou ajoute les colonnes manquantes."""
    insp = inspect(dst_engine)
    schema = schema or "public"
    if schema not in insp.get_schema_names():
        with dst_engine.begin() as c:
            c.execute(CreateSchema(schema, if_not_exists=True))
        log.info(f"Schéma « {schema} » créé dans la base cible.")
        insp = inspect(dst_engine)

    if not insp.has_table(target_name, schema=schema):
        meta = MetaData()
        columns = [
            Column(
                col.name,
                map_type(col.type),
                primary_key=col.name in key_columns,
                autoincrement=False,
                nullable=col.name not in key_columns,
            )
            for col in src_table.columns
        ]
        table = Table(target_name, meta, *columns, schema=schema)
        meta.create_all(dst_engine)
        log.info(f"Table cible {schema}.{target_name} créée ({len(columns)} colonnes).", src_table.name)
        return table

    meta = MetaData()
    table = Table(target_name, meta, schema=schema, autoload_with=dst_engine)
    missing = [col for col in src_table.columns if col.name not in table.columns]
    if missing:
        preparer = dst_engine.dialect.identifier_preparer
        with dst_engine.begin() as c:
            for col in missing:
                col_type = map_type(col.type).compile(dialect=dst_engine.dialect)
                c.execute(
                    text(
                        f"ALTER TABLE {preparer.format_table(table)} "
                        f"ADD COLUMN {preparer.quote(col.name)} {col_type}"
                    )
                )
        log.info(
            "Colonnes ajoutées dans la cible : " + ", ".join(c.name for c in missing), src_table.name
        )
        meta = MetaData()
        table = Table(target_name, meta, schema=schema, autoload_with=dst_engine)
    return table


# --------------------------------------------------------------------------- synchronisation d'une table


def _split(value: Optional[str]) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _strict_cursor(keys: list[str], inc: str) -> bool:
    """Lecture strictement après le curseur (>) ou en relisant la dernière valeur (>=).

    Si la colonne de suivi est elle-même la clé unique (ex. id), aucune autre ligne ne peut avoir la
    même valeur : on lit strictement après, et « 0 ligne » signifie qu'il n'y a rien de nouveau.
    Si elle ne l'est pas (ex. updated_at), plusieurs lignes peuvent partager la dernière valeur : avec
    une clé, on la relit (>=), l'upsert évitant les doublons. Sans clé, relire créerait des doublons (>).
    """
    return not keys or keys == [inc]


def _make_writer(dst_table: Table, keys: list[str], columns: list[str]) -> Callable:
    """Écriture d'un lot : upsert si des colonnes clés existent, insertion simple sinon."""
    update_cols = [c for c in columns if c not in keys]

    def write(conn, rows: list[dict]) -> None:
        if not rows:
            return
        if keys:
            stmt = pg_insert(dst_table)
            if update_cols:
                stmt = stmt.on_conflict_do_update(
                    index_elements=keys, set_={c: stmt.excluded[c] for c in update_cols}
                )
            else:
                stmt = stmt.on_conflict_do_nothing(index_elements=keys)
            conn.execute(stmt, rows)
        else:
            conn.execute(dst_table.insert(), rows)

    return write


def sync_table(
    src_engine: Engine,
    dst_engine: Engine,
    mapping: TableMapping,
    schema: str,
    log: RunLogger,
    batch_size: Optional[int] = None,
) -> tuple[int, int]:
    """Synchronise une table. Renvoie (lignes lues, lignes écrites)."""
    batch_size = batch_size or settings.batch_size
    name = mapping.source_table
    src_table = Table(name, MetaData(), autoload_with=src_engine)

    keys = _split(mapping.key_columns) or [c.name for c in src_table.primary_key.columns]
    unknown = [k for k in keys if k not in src_table.columns]
    if unknown:
        raise ValueError(f"Colonnes clés introuvables dans la source : {', '.join(unknown)}")

    dst_table = ensure_target_table(dst_engine, src_table, mapping.target_table, schema, keys, log)
    columns = [c.name for c in src_table.columns if c.name in dst_table.columns]
    converters = {
        c: _value_converter(src_table.columns[c].type, dst_table.columns[c].type) for c in columns
    }

    def clean(rows) -> list[dict]:
        out = []
        for row in rows:
            m = row._mapping
            out.append({c: converters[c](m[c]) for c in columns})
        return out

    write = _make_writer(dst_table, keys, columns)

    rows_read = rows_written = 0
    stmt = select(*[src_table.columns[c] for c in columns])

    if mapping.mode == MODE_INCREMENTAL:
        inc = mapping.incremental_column
        if not inc or inc not in src_table.columns:
            raise ValueError(f"Colonne incrémentale « {inc} » introuvable dans la table source.")
        inc_col = src_table.columns[inc]
        last = watermark.decode(mapping.last_value)
        strict = _strict_cursor(keys, inc)
        if last is not None:
            stmt = stmt.where(inc_col > last if strict else inc_col >= last)
        stmt = stmt.where(inc_col.is_not(None)).order_by(inc_col)
        if not keys:
            log.warning(
                "Aucune clé définie : les lignes sont ajoutées sans dédoublonnage (mode ajout).", name
            )
        if last is None:
            log.info(f"Première lecture incrémentale sur « {inc} » : lecture complète.", name)
        else:
            log.info(f"Lecture incrémentale : {inc} {'>' if strict else '>='} {last}.", name)

        with src_engine.connect() as src:
            result = src.execution_options(stream_results=True, yield_per=batch_size).execute(stmt)
            for part in result.partitions():
                _checkpoint(log)
                rows = clean(part)
                rows_read += len(rows)
                with dst_engine.begin() as dst:
                    write(dst, rows)
                rows_written += len(rows)
                new_last = part[-1]._mapping[inc]
                # Sauvegarde du point de reprise après chaque lot validé.
                _save_watermark(mapping.id, new_last)
                mapping.last_value = watermark.encode(new_last)
                log.debug(f"Lot de {len(rows)} lignes écrit (curseur = {new_last}).", name)
    else:
        # Mode complet : vidage puis rechargement dans une seule transaction côté cible.
        with src_engine.connect() as src, dst_engine.begin() as dst:
            preparer = dst_engine.dialect.identifier_preparer
            dst.execute(text(f"DELETE FROM {preparer.format_table(dst_table)}"))
            result = src.execution_options(stream_results=True, yield_per=batch_size).execute(stmt)
            for part in result.partitions():
                _checkpoint(log)
                rows = clean(part)
                rows_read += len(rows)
                write(dst, rows)
                rows_written += len(rows)
                log.debug(f"Lot de {len(rows)} lignes écrit.", name)

    return rows_read, rows_written


def sync_sheet(
    sheets: dict,
    dst_engine: Engine,
    mapping: TableMapping,
    schema: str,
    log: RunLogger,
    batch_size: Optional[int] = None,
) -> tuple[int, int]:
    """Synchronise un onglet Google Sheets. Renvoie (lignes lues, lignes écrites)."""
    batch_size = batch_size or settings.batch_size
    name = mapping.source_table
    if name not in sheets:
        raise ValueError(f"Onglet « {name} » introuvable (onglets : {', '.join(sheets) or 'aucun'}).")
    data = sheets[name]
    if not data.columns:
        raise ValueError(f"L'onglet « {name} » est vide.")
    src_table = gsheet.build_table(name, data)

    keys = _split(mapping.key_columns)
    unknown = [k for k in keys if k not in src_table.columns]
    if unknown:
        raise ValueError(
            f"Colonnes clés introuvables : {', '.join(unknown)} (colonnes : {', '.join(data.columns)})"
        )
    dst_table = ensure_target_table(dst_engine, src_table, mapping.target_table, schema, keys, log)
    columns = [c for c in data.columns if c in dst_table.columns]
    index = {c: data.columns.index(c) for c in columns}

    # Conversion vers les types de la table cible ; une valeur illisible devient NULL (et est signalée).
    rows, invalid = [], {}
    for raw in data.rows:
        row = {}
        for c in columns:
            try:
                row[c] = gsheet.convert(raw[index[c]], dst_table.columns[c].type)
            except (ValueError, TypeError, ArithmeticError):
                row[c] = None
                invalid.setdefault(c, []).append(raw[index[c]])
        rows.append(row)
    for c, values in invalid.items():
        log.warning(
            f"Colonne « {c} » : {len(values)} valeur(s) incompatible(s) avec le type "
            f"{dst_table.columns[c].type} remplacée(s) par NULL (ex. {values[0]!r}).", name
        )

    if keys:
        missing = [r for r in rows if any(r[k] is None for k in keys)]
        if missing:
            log.warning(f"{len(missing)} ligne(s) sans valeur de clé ignorée(s).", name)
        # Doublons de clé dans la feuille : la dernière ligne l'emporte.
        unique = {tuple(r[k] for k in keys): r for r in rows if all(r[k] is not None for k in keys)}
        if len(unique) < len(rows) - len(missing):
            log.warning(f"{len(rows) - len(missing) - len(unique)} doublon(s) de clé : dernière ligne conservée.", name)
        rows = list(unique.values())

    rows_read = len(data.rows)
    write = _make_writer(dst_table, keys, columns)
    preparer = dst_engine.dialect.identifier_preparer

    if mapping.mode == MODE_INCREMENTAL:
        inc = mapping.incremental_column
        if not inc or inc not in columns:
            raise ValueError(f"Colonne incrémentale « {inc} » introuvable (colonnes : {', '.join(data.columns)}).")
        last = watermark.decode(mapping.last_value)
        rows = [r for r in rows if r[inc] is not None]
        if last is not None:
            strict = _strict_cursor(keys, inc)
            rows = [r for r in rows if (r[inc] > last if strict else r[inc] >= last)]
            log.info(f"Lecture incrémentale : {inc} {'>' if strict else '>='} {last}.", name)
        else:
            log.info(f"Première lecture incrémentale sur « {inc} » : lecture complète.", name)
        rows.sort(key=lambda r: r[inc])
        with dst_engine.begin() as dst:
            for i in range(0, len(rows), batch_size):
                _checkpoint(log)
                write(dst, rows[i:i + batch_size])
        if rows:
            _save_watermark(mapping.id, rows[-1][inc])
            mapping.last_value = watermark.encode(rows[-1][inc])
    else:
        # Mode complet : vidage puis rechargement dans une seule transaction.
        with dst_engine.begin() as dst:
            dst.execute(text(f"DELETE FROM {preparer.format_table(dst_table)}"))
            for i in range(0, len(rows), batch_size):
                _checkpoint(log)
                write(dst, rows[i:i + batch_size])
    return rows_read, len(rows)


def sync_odbc_table(
    src: "hfsql.Source",
    dst_engine: Engine,
    mapping: TableMapping,
    schema: str,
    log: RunLogger,
    batch_size: Optional[int] = None,
) -> tuple[int, int]:
    """Synchronise une table HFSQL (ODBC). Renvoie (lignes lues, lignes écrites)."""
    batch_size = batch_size or settings.batch_size
    name = mapping.source_table
    src_table = src.build_table(name)

    keys = _split(mapping.key_columns) or [c.name for c in src_table.primary_key.columns]
    unknown = [k for k in keys if k not in src_table.columns]
    if unknown:
        raise ValueError(f"Colonnes clés introuvables dans la source : {', '.join(unknown)}")
    dst_table = ensure_target_table(dst_engine, src_table, mapping.target_table, schema, keys, log)
    columns = [c.name for c in src_table.columns if c.name in dst_table.columns]
    targets = [dst_table.columns[c].type for c in columns]
    write = _make_writer(dst_table, keys, columns)

    def clean(rows) -> list[dict]:
        return [{c: hfsql.clean(v, t) for c, v, t in zip(columns, row, targets)} for row in rows]

    rows_read = rows_written = 0
    if mapping.mode == MODE_INCREMENTAL:
        inc = mapping.incremental_column
        if not inc or inc not in columns:
            raise ValueError(f"Colonne incrémentale « {inc} » introuvable dans la table source.")
        last = watermark.decode(mapping.last_value)
        strict = _strict_cursor(keys, inc)
        if not keys:
            log.warning("Aucune clé définie : les lignes sont ajoutées sans dédoublonnage (mode ajout).", name)
        if last is None:
            log.info(f"Première lecture incrémentale sur « {inc} » : lecture complète.", name)
        else:
            log.info(f"Lecture incrémentale : {inc} {'>' if strict else '>='} {last}.", name)
        inc_index = columns.index(inc)
        cur = src.select(name, columns, inc, last, strict)
        try:
            while True:
                _checkpoint(log)
                part = cur.fetchmany(batch_size)
                if not part:
                    break
                rows = clean(part)
                rows_read += len(rows)
                with dst_engine.begin() as dst:
                    write(dst, rows)
                rows_written += len(rows)
                new_last = part[-1][inc_index]
                _save_watermark(mapping.id, new_last)
                mapping.last_value = watermark.encode(new_last)
                log.debug(f"Lot de {len(rows)} lignes écrit (curseur = {new_last}).", name)
        finally:
            cur.close()
    else:
        preparer = dst_engine.dialect.identifier_preparer
        cur = src.select(name, columns)
        try:
            with dst_engine.begin() as dst:
                dst.execute(text(f"DELETE FROM {preparer.format_table(dst_table)}"))
                while True:
                    _checkpoint(log)
                    part = cur.fetchmany(batch_size)
                    if not part:
                        break
                    rows = clean(part)
                    rows_read += len(rows)
                    write(dst, rows)
                    rows_written += len(rows)
                    log.debug(f"Lot de {len(rows)} lignes écrit.", name)
        finally:
            cur.close()
    return rows_read, rows_written


def _checkpoint(log: RunLogger) -> None:
    control = getattr(log, "control", None)
    if control is not None:
        control.check()


def _save_watermark(mapping_id: int, value: Any) -> None:
    db = SessionLocal()
    try:
        m = db.get(TableMapping, mapping_id)
        if m is not None:
            m.last_value = watermark.encode(value)
            db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------- exécution d'un job


def run_job(
    job_id: int,
    trigger: str = "schedule",
    mapping_id: Optional[int] = None,
    reset: bool = False,
    recreate: bool = False,
) -> Optional[int]:
    """Exécute un job. Renvoie l'id de l'exécution (None si ignorée).

    mapping_id : ne traite que cette table.
    reset      : vide les tables cibles et remet les curseurs à zéro avant de tout réimporter.
    recreate   : supprime les tables cibles (DROP) pour recréer aussi leur structure.
    """
    control = RunControl(job_id)
    with _active_guard:
        if job_id in _active:
            write_log("WARNING", "Exécution ignorée : le job est déjà en cours.", job_id=job_id)
            return None
        _active[job_id] = control
    try:
        return _run_job_locked(control, trigger, mapping_id, reset, recreate)
    finally:
        with _active_guard:
            if _active.get(job_id) is control:
                del _active[job_id]


TRIGGER_LABELS = {"manual": "manuel", "schedule": "planifié", "reload": "réimport complet"}


def reset_target(dst_engine: Engine, schema: str, mapping: TableMapping, recreate: bool, log: RunLogger) -> None:
    """Vide (TRUNCATE) ou supprime (DROP) la table cible et remet le curseur incrémental à zéro."""
    schema = schema or "public"
    preparer = dst_engine.dialect.identifier_preparer
    qualified = f"{preparer.quote_schema(schema)}.{preparer.quote(mapping.target_table)}"
    if inspect(dst_engine).has_table(mapping.target_table, schema=schema):
        with dst_engine.begin() as c:
            c.execute(text(f"DROP TABLE {qualified}" if recreate else f"TRUNCATE TABLE {qualified}"))
        log.warning(
            f"Table {schema}.{mapping.target_table} "
            f"{'supprimée (structure recréée à partir de la source)' if recreate else 'vidée'} à la demande.",
            mapping.source_table,
        )
    mapping.last_value = None
    _save_watermark(mapping.id, None)


def _run_job_locked(
    control: RunControl, trigger: str, mapping_id: Optional[int] = None, reset: bool = False, recreate: bool = False
) -> Optional[int]:
    job_id = control.job_id
    db = SessionLocal()
    try:
        job = db.get(SyncJob, job_id)
        if job is None:
            return None
        run = JobRun(job_id=job.id, trigger=trigger, status="running", started_at=utcnow())
        db.add(run)
        db.commit()
        control.run_id = run.id
        log = RunLogger(job.id, run.id)
        log.control = control
        started = _time.monotonic()
        log.info(f"Démarrage du job « {job.name} » ({TRIGGER_LABELS.get(trigger, trigger)}).")

        src_engine = dst_engine = odbc_src = None
        try:
            if job.source.kind not in SOURCE_KINDS or job.target.kind != "postgresql":
                raise ValueError("La source doit être MariaDB, HFSQL ou Google Sheets et la cible PostgreSQL.")
            is_sheet = job.source.kind == "gsheet"
            is_odbc = job.source.kind == "hfsql"
            dst_engine = control.watch(make_engine(job.target))
            checks = [("cible", dst_engine)]
            if is_odbc:
                try:
                    odbc_src = hfsql.Source(job.source, on_worker=control.add_worker)
                except Exception as exc:
                    raise RuntimeError(f"connexion source impossible : {_short_error(exc)}") from exc
            elif not is_sheet:
                src_engine = control.watch(make_engine(job.source))
                checks.insert(0, ("source", src_engine))
            # Vérifie les connexions avant de traiter les tables.
            for label, eng in checks:
                try:
                    with eng.connect():
                        pass
                except Exception as exc:
                    raise RuntimeError(f"connexion {label} impossible : {_short_error(exc)}") from exc
            if mapping_id is not None:
                mappings = [m for m in job.tables if m.id == mapping_id]
            else:
                mappings = [m for m in job.tables if m.enabled]
            if reset:
                log.warning(
                    f"Réimport complet demandé : {len(mappings)} table(s) "
                    f"{'supprimée(s) et recréée(s)' if recreate else 'vidée(s)'} puis rechargée(s) depuis la source."
                )
                kept = []
                for m in mappings:
                    try:
                        reset_target(dst_engine, job.target_schema, m, recreate, log)
                        kept.append(m)
                    except Exception as exc:
                        run.tables_failed += 1
                        log.error(f"Impossible de vider la table cible : {_short_error(exc)}", m.source_table)
                db.commit()
                mappings = kept
            sheets = None
            if is_sheet and mappings:
                try:
                    sheets = gsheet.load_sheets(job.source)
                except Exception as exc:
                    raise RuntimeError(f"classeur Google Sheets inaccessible : {_short_error(exc)}") from exc
                log.info(f"Classeur Google Sheets téléchargé : {len(sheets)} onglet(s).")
            if not mappings:
                log.warning("Aucune table active à synchroniser.")
            for mapping in mappings:
                control.check()
                t0 = _time.monotonic()
                try:
                    if is_sheet:
                        read, written = sync_sheet(sheets, dst_engine, mapping, job.target_schema, log)
                    elif is_odbc:
                        read, written = sync_odbc_table(odbc_src, dst_engine, mapping, job.target_schema, log)
                    else:
                        read, written = sync_table(src_engine, dst_engine, mapping, job.target_schema, log)
                    run.rows_read += read
                    run.rows_written += written
                    run.tables_ok += 1
                    mapping.last_sync_at = utcnow()
                    mapping.last_rows = written
                    log.info(
                        f"{mapping.source_table} → {job.target_schema}.{mapping.target_table} : "
                        f"{written} ligne(s) en {_time.monotonic() - t0:.1f} s.",
                        mapping.source_table,
                    )
                except Exception as exc:  # une table en échec n'arrête pas les autres
                    control.check()
                    run.tables_failed += 1
                    log.error(f"Échec : {_short_error(exc)}", mapping.source_table)
                db.commit()
            if run.tables_failed and run.tables_ok:
                run.status = "partial"
            elif run.tables_failed:
                run.status = "error"
            else:
                run.status = "success"
            run.message = (
                f"{run.tables_ok} table(s) OK, {run.tables_failed} en échec, {run.rows_written} ligne(s)."
            )
        except Exception as exc:
            if control.stop.is_set():
                run.status = "cancelled"
                run.message = (f"{control.reason or 'Arrêté à la demande.'} {run.tables_ok} table(s) terminée(s), "
                               f"{run.rows_written} ligne(s) écrite(s).")
                log.warning(f"Job arrêté : {run.message}")
            else:
                run.status = "error"
                run.message = _short_error(exc)
                log.error(f"Job interrompu : {run.message}")
        finally:
            for eng in (src_engine, dst_engine):
                if eng is not None:
                    eng.dispose()
            if odbc_src is not None:
                # En cas d'erreur, la connexion n'est pas gardée : la prochaine exécution en ouvre une neuve.
                odbc_src.close(discard=run.tables_failed > 0 or run.status in ("error", "cancelled"))

        if control.forced:
            # L'exécution a déjà été clôturée par l'arrêt forcé : ne pas écraser son état.
            db.rollback()
            return run.id
        run.finished_at = utcnow()
        job.last_run_at = run.finished_at
        job.last_status = run.status
        db.commit()
        level = {"success": "INFO", "partial": "WARNING"}.get(run.status, "ERROR")
        log(level, f"Fin du job ({run.status}) en {_time.monotonic() - started:.1f} s : {run.message}")
        return run.id
    finally:
        db.close()


def _short_error(exc: Exception) -> str:
    return friendly(exc)
