"""Consultation des données synchronisées dans PostgreSQL : tables, comptages, filtres."""
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import MetaData, String, Table, Text, and_, cast, func, literal, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

COUNT_TIMEOUT_MS = 5000
MAX_EXACT_COUNTS = 150
EXPORT_LIMIT = 100_000

OPERATORS = {
    "contains": "contient",
    "eq": "=",
    "neq": "≠",
    "gt": ">",
    "gte": "≥",
    "lt": "<",
    "lte": "≤",
    "starts": "commence par",
    "empty": "est vide",
    "notempty": "n'est pas vide",
}
NO_VALUE_OPERATORS = {"empty", "notempty"}


class ExplorerError(Exception):
    pass


# --------------------------------------------------------------------------- liste des tables


@dataclass
class TableInfo:
    schema: str
    name: str
    rows: Optional[int]
    exact: bool
    size: int
    mappings: list = field(default_factory=list)

    @property
    def size_label(self) -> str:
        size = float(self.size or 0)
        for unit in ("o", "Ko", "Mo", "Go"):
            if size < 1024 or unit == "Go":
                return f"{size:.0f} {unit}" if unit == "o" else f"{size:.1f} {unit}"
            size /= 1024
        return ""


def list_tables(engine: Engine) -> list[TableInfo]:
    """Tables lisibles de la base, avec nombre de lignes exact (ou estimé si trop long)."""
    with engine.connect() as conn, conn.begin():
        rows = conn.execute(text("""
            SELECT n.nspname, c.relname, GREATEST(c.reltuples, 0)::bigint, pg_total_relation_size(c.oid)
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg_toast%%' AND n.nspname NOT LIKE 'pg_temp%%'
              AND has_table_privilege(c.oid, 'SELECT')
            ORDER BY 1, 2
        """)).all()
    tables = [TableInfo(schema, name, int(estimate), False, int(size)) for schema, name, estimate, size in rows]
    if len(tables) > MAX_EXACT_COUNTS:
        return tables
    with engine.connect() as conn:
        preparer = engine.dialect.identifier_preparer
        for info in tables:
            qualified = f"{preparer.quote(info.schema)}.{preparer.quote(info.name)}"
            try:
                with conn.begin():
                    conn.execute(text(f"SET LOCAL statement_timeout = {COUNT_TIMEOUT_MS}"))
                    info.rows = conn.execute(text(f"SELECT count(*) FROM {qualified}")).scalar()
                    info.exact = True
            except DBAPIError:
                pass  # trop long : on garde l'estimation de PostgreSQL
    return tables


# --------------------------------------------------------------------------- contenu d'une table


@dataclass
class Filter:
    column: str
    op: str
    value: str


@dataclass
class QueryResult:
    columns: list  # [(nom, type)]
    rows: list
    total: int
    filtered: int


def reflect(engine: Engine, schema: str, name: str) -> Table:
    try:
        return Table(name, MetaData(), schema=schema, autoload_with=engine)
    except Exception as exc:
        raise ExplorerError(f"Table {schema}.{name} introuvable ou illisible.") from exc


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _conditions(table: Table, filters: list[Filter], search: str) -> list:
    conds = []
    for f in filters:
        if f.column not in table.c or f.op not in OPERATORS:
            continue
        col = table.c[f.column]
        as_text = cast(col, Text)
        if f.op == "empty":
            conds.append(or_(col.is_(None), as_text == ""))
        elif f.op == "notempty":
            conds.append(and_(col.is_not(None), as_text != ""))
        elif f.op == "contains":
            conds.append(as_text.ilike(f"%{_escape_like(f.value)}%", escape="\\"))
        elif f.op == "starts":
            conds.append(as_text.ilike(f"{_escape_like(f.value)}%", escape="\\"))
        else:
            # La valeur saisie (texte) est convertie par PostgreSQL dans le type de la colonne.
            value = cast(literal(f.value, String()), col.type)
            conds.append({
                "eq": col == value, "neq": col != value, "gt": col > value,
                "gte": col >= value, "lt": col < value, "lte": col <= value,
            }[f.op])
    if search:
        pattern = f"%{_escape_like(search)}%"
        conds.append(or_(*[cast(c, Text).ilike(pattern, escape="\\") for c in table.columns]))
    return conds


def query_table(
    engine: Engine,
    table: Table,
    filters: list[Filter],
    search: str = "",
    sort: str = "",
    desc: bool = False,
    page: int = 1,
    page_size: int = 50,
    limit: Optional[int] = None,
) -> QueryResult:
    conds = _conditions(table, filters, search)
    order_col = table.c[sort] if sort in table.c else (
        list(table.primary_key.columns)[0] if table.primary_key.columns else list(table.columns)[0])
    order = order_col.desc().nulls_last() if desc else order_col.asc().nulls_last()
    base = select(table).where(*conds)
    try:
        with engine.connect() as conn:
            with conn.begin():
                conn.execute(text("SET LOCAL statement_timeout = 60000"))
                total = conn.execute(select(func.count()).select_from(table)).scalar()
                filtered = conn.execute(select(func.count()).select_from(base.subquery())).scalar() if conds else total
                stmt = base.order_by(order)
                if limit is not None:
                    stmt = stmt.limit(limit)
                else:
                    stmt = stmt.limit(page_size).offset((max(page, 1) - 1) * page_size)
                rows = conn.execute(stmt).all()
    except DBAPIError as exc:
        msg = str(getattr(exc, "orig", exc)).splitlines()[0]
        low = msg.lower()
        if "invalid input" in low or "entrée invalide" in low or "out of range" in low or "invalide" in low:
            raise ExplorerError(f"Valeur de filtre incompatible avec le type de la colonne : {msg}") from exc
        if "statement timeout" in low or "annulation" in low:
            raise ExplorerError("Requête trop longue : ajoutez un filtre plus précis.") from exc
        raise ExplorerError(msg) from exc
    columns = [(c.name, str(c.type)) for c in table.columns]
    return QueryResult(columns, [tuple(r) for r in rows], total, filtered)


def display(value: Any) -> tuple[str, str]:
    """Valeur affichée + classe CSS."""
    if value is None:
        return "—", "null"
    if isinstance(value, bool):
        return ("✓" if value else "✗"), "bool"
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M:%S" if value.time() != time(0) else "%d/%m/%Y 00:00"), "num"
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y"), "num"
    if isinstance(value, time):
        return value.strftime("%H:%M:%S"), "num"
    if isinstance(value, (int, float, Decimal)):
        return str(value), "num"
    if isinstance(value, (bytes, memoryview)):
        return f"<binaire {len(bytes(value))} octets>", "null"
    return str(value), ""


def csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    return str(value)
