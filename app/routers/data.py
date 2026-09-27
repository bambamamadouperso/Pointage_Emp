"""Menu « Données » : tables PostgreSQL synchronisées, nombre de lignes, consultation filtrée."""
import csv
import io
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import MetaData, Table, func, select, text
from sqlalchemy.orm import Session

from .. import explorer, gsheet, hfsql
from ..database import get_db
from ..errors import friendly
from ..models import Connection, SyncJob, TableMapping
from ..sync import make_engine
from ..web import render, require_login

router = APIRouter(prefix="/data", dependencies=[Depends(require_login)])

PAGE_SIZES = (25, 50, 100, 250, 500)


def _targets(db: Session):
    return db.scalars(select(Connection).where(Connection.kind == "postgresql").order_by(Connection.name)).all()


@router.get("")
def overview(request: Request, conn_id: Optional[str] = None, q: str = "", db: Session = Depends(get_db)):
    targets = _targets(db)
    conn = None
    if conn_id and conn_id.isdigit():
        conn = db.get(Connection, int(conn_id))
    if conn is None or conn.kind != "postgresql":
        # Par défaut : la première cible utilisée par un job, sinon la première cible.
        used = db.scalar(select(SyncJob.target_id).order_by(SyncJob.id))
        conn = db.get(Connection, used) if used else (targets[0] if targets else None)

    tables, error = [], None
    if conn is not None:
        engine = make_engine(conn)
        try:
            tables = explorer.list_tables(engine)
        except Exception as exc:
            error = friendly(exc)
        finally:
            engine.dispose()
        # Rattache chaque table au job et à la table source qui l'alimentent.
        mappings = db.scalars(
            select(TableMapping).join(SyncJob).where(SyncJob.target_id == conn.id)
        ).all()
        by_target = {}
        for m in mappings:
            by_target.setdefault((m.job.target_schema, m.target_table), []).append(m)
        for t in tables:
            t.mappings = by_target.get((t.schema, t.name), [])
    if q:
        tables = [t for t in tables if q.lower() in f"{t.schema}.{t.name}".lower()]
    synced_only = request.query_params.get("synced") == "1"
    if synced_only:
        tables = [t for t in tables if t.mappings]
    return render(
        request,
        "data.html",
        targets=targets,
        conn=conn,
        tables=tables,
        error=error,
        q=q,
        synced_only=synced_only,
        total_rows=sum(t.rows or 0 for t in tables),
        job_ids=sorted({m.job_id for t in tables for m in t.mappings}),
    )


@router.get("/source-counts/{job_id}")
def source_counts(job_id: int, db: Session = Depends(get_db)):
    """Nombre de lignes côté source pour chaque table d'un job (pour comparer avec PostgreSQL)."""
    job = db.get(SyncJob, job_id)
    if job is None:
        return JSONResponse({"error": "Job introuvable"}, status_code=404)
    counts: dict[int, dict] = {}
    try:
        if job.source.is_odbc:
            with hfsql.Source(job.source) as src:
                for m in job.tables:
                    try:
                        counts[m.id] = {"rows": src.count(m.source_table)}
                    except Exception as exc:
                        counts[m.id] = {"error": friendly(exc)[:200]}
        elif job.source.is_gsheet:
            sheets = gsheet.load_sheets(job.source)
            for m in job.tables:
                data = sheets.get(m.source_table)
                counts[m.id] = {"rows": len(data.rows)} if data else {"error": "onglet introuvable"}
        else:
            engine = make_engine(job.source)
            try:
                with engine.connect() as conn:
                    for m in job.tables:
                        try:
                            table = Table(m.source_table, MetaData(), autoload_with=engine)
                            counts[m.id] = {"rows": conn.execute(select(func.count()).select_from(table)).scalar()}
                        except Exception as exc:
                            conn.rollback()
                            counts[m.id] = {"error": friendly(exc)[:200]}
            finally:
                engine.dispose()
    except Exception as exc:
        return JSONResponse({"error": friendly(exc)}, status_code=400)
    return {"counts": counts}


def _filters(request: Request) -> list[explorer.Filter]:
    params = request.query_params
    columns, ops, values = params.getlist("fc"), params.getlist("fo"), params.getlist("fv")
    out = []
    for i, column in enumerate(columns):
        op = ops[i] if i < len(ops) else "contains"
        value = values[i] if i < len(values) else ""
        if column and (value != "" or op in explorer.NO_VALUE_OPERATORS):
            out.append(explorer.Filter(column, op, value))
    return out


def _page_size(request: Request) -> int:
    try:
        size = int(request.query_params.get("size", 50))
    except ValueError:
        size = 50
    return size if size in PAGE_SIZES else 50


@router.get("/{conn_id}/{schema}/{table}")
def table_view(conn_id: int, schema: str, table: str, request: Request, db: Session = Depends(get_db)):
    conn = db.get(Connection, conn_id)
    if conn is None or conn.kind != "postgresql":
        return render(request, "data_table.html", conn=None, error="Connexion introuvable.", schema=schema, table=table)
    params = request.query_params
    filters = _filters(request)
    search = params.get("q", "").strip()
    sort, desc = params.get("sort", ""), params.get("dir") == "desc"
    try:
        page = max(int(params.get("page", 1)), 1)
    except ValueError:
        page = 1
    size = _page_size(request)

    result, error, columns = None, None, []
    engine = make_engine(conn)
    try:
        tbl = explorer.reflect(engine, schema, table)
        columns = [(c.name, str(c.type)) for c in tbl.columns]
        result = explorer.query_table(engine, tbl, filters, search, sort, desc, page, size)
    except explorer.ExplorerError as exc:
        error = str(exc)
    except Exception as exc:
        error = friendly(exc)
    finally:
        engine.dispose()

    mappings = db.scalars(
        select(TableMapping).join(SyncJob)
        .where(SyncJob.target_id == conn.id, SyncJob.target_schema == schema, TableMapping.target_table == table)
    ).all()
    pages = max(((result.filtered if result else 0) + size - 1) // size, 1)
    return render(
        request,
        "data_table.html",
        conn=conn,
        schema=schema,
        table=table,
        columns=columns,
        result=result,
        error=error,
        filters=filters or [explorer.Filter("", "contains", "")],
        operators=explorer.OPERATORS,
        no_value_ops=explorer.NO_VALUE_OPERATORS,
        search=search,
        sort=sort,
        desc=desc,
        page=page,
        pages=pages,
        size=size,
        page_sizes=PAGE_SIZES,
        mappings=mappings,
        display=explorer.display,
    )


@router.get("/{conn_id}/{schema}/{table}/export.csv")
def table_export(conn_id: int, schema: str, table: str, request: Request, db: Session = Depends(get_db)):
    conn = db.get(Connection, conn_id)
    if conn is None or conn.kind != "postgresql":
        return Response("Connexion introuvable", status_code=404)
    params = request.query_params
    engine = make_engine(conn)
    try:
        tbl = explorer.reflect(engine, schema, table)
        result = explorer.query_table(engine, tbl, _filters(request), params.get("q", "").strip(),
                                      params.get("sort", ""), params.get("dir") == "desc",
                                      limit=explorer.EXPORT_LIMIT)
    except Exception as exc:
        return Response(f"Export impossible : {exc}", status_code=400, media_type="text/plain; charset=utf-8")
    finally:
        engine.dispose()
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow([name for name, _ in result.columns])
    for row in result.rows:
        writer.writerow([explorer.csv_value(v) for v in row])
    return Response(
        "﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{schema}.{table}.csv"'},
    )
