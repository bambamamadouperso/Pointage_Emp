"""Ressources humaines → Temps hebdomadaire : temps travaillé par employé et par semaine, comparé à l'objectif."""
import io
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import hebdo, pointage
from ..database import get_db
from ..errors import friendly
from ..sync import make_engine
from ..web import render, require_login
from .suivi import load_config

router = APIRouter(prefix="/admin/hebdo", dependencies=[Depends(require_login)])


def _filters(request: Request) -> tuple[pointage.Filters, int, str, bool]:
    p = request.query_params
    try:
        count = int(p.get("semaines", 4))
    except ValueError:
        count = 4
    count = count if count in hebdo.WEEK_CHOICES else 4
    last = hebdo.monday(pointage.parse_day(p.get("semaine")) or (date.today() - timedelta(days=7)))
    du = last - timedelta(days=7 * (count - 1))
    f = pointage.Filters(du=du, au=last + timedelta(days=6), q=(p.get("q") or "").strip(),
                         service=pointage.multi(p.getlist("service")), direction=pointage.multi(p.getlist("direction")),
                         population="liste")
    sort = p.get("tri", "ecart")
    return f, count, sort if sort in hebdo.SORTS else "ecart", p.get("sous") == "1"


def _data(request: Request, db: Session):
    cfg, mapping = load_config(db)
    f, count, sort, under = _filters(request)
    out = dict(f=f, count=count, sort=sort, under=under, configured=mapping is not None, data=None, error=None,
               services=[], directions=[], has_direction=bool(mapping and mapping.dir_col), stats=None)
    if mapping is None:
        return out
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
    try:
        data = hebdo.weekly(engine, mapping, f)
        out["stats"] = hebdo.summary(data)
        rows = hebdo.sort_rows(data["rows"], sort)
        data["rows"] = [e for e in rows if e["ecart"] < 0] if under else rows
        out["data"] = data
        out["services"] = pointage.services(engine, mapping)
        out["directions"] = pointage.directions(engine, mapping) if mapping.dir_col else []
    except Exception as exc:  # noqa: BLE001
        out["error"] = friendly(exc)
    finally:
        engine.dispose()
    return out


@router.get("")
def page(request: Request, db: Session = Depends(get_db)):
    ctx = _data(request, db)
    return render(request, "admin/hebdo.html", fmt=hebdo.fmt, sorts=hebdo.SORTS, week_choices=hebdo.WEEK_CHOICES,
                  query=str(request.url.query), **ctx)


@router.get("/export.xlsx")
def export(request: Request, db: Session = Depends(get_db)):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    ctx = _data(request, db)
    if ctx["data"] is None:
        return Response(ctx["error"] or "Module de pointage non configuré.", status_code=400,
                        media_type="text/plain; charset=utf-8")
    data, f = ctx["data"], ctx["f"]
    weeks, targets = data["weeks"], data["targets"]
    hours = lambda mins: round(mins / 60, 2)  # noqa: E731
    wb = Workbook()
    ws = wb.active
    ws.title = "Temps hebdomadaire"
    ws.append(["Matricule", "Nom", "Direction", "Service", "Statut du personnel",
               *[f"Semaine du {w:%d/%m/%Y} (h)" for w in weeks], "Moyenne hebdo (h)", "Objectif hebdo (h)",
               "Écart moyen (h)", "Taux d'atteinte", "Semaines sous l'objectif"])
    for c in ws[1]:
        c.font = Font(bold=True)
    green, red = PatternFill("solid", fgColor="DCFCE7"), PatternFill("solid", fgColor="FEE2E2")
    for e in data["rows"]:
        ws.append([e["matricule"], e["nom"], e["direction"], e["service"], e["categorie"],
                   *[hours(e["weeks"].get(w, 0.0)) for w in weeks], hours(e["moyenne"]),
                   hours(e["objectif"] / len(weeks)), hours(e["ecart"]),
                   round(e["taux"] / 100, 4) if e["taux"] is not None else None, e["sous"]])
        row = ws.max_row
        for i, w in enumerate(weeks):
            ws.cell(row, 6 + i).fill = green if e["weeks"].get(w, 0.0) >= targets[w] else red
        ws.cell(row, 6 + len(weeks) + 4).number_format = "0.0%"
    ws.freeze_panes = "C2"
    buf = io.BytesIO()
    wb.save(buf)
    name = f"temps_hebdo_{f.du:%Y%m%d}_{f.au:%Y%m%d}.xlsx"
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
