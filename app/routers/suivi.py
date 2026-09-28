"""Écran principal : suivi journalier des pointages (filtres dans l'URL, détail, export Excel)."""
import io
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from .. import pointage
from ..database import get_db
from ..errors import friendly
from ..models import PointageConfig, User
from ..sync import make_engine
from ..web import render, require_login

router = APIRouter(prefix="/suivi", dependencies=[Depends(require_login)])

PAGE_SIZES = (50, 100, 250, 500)
MAX_DAYS = 366


def load_config(db: Session) -> tuple[Optional[PointageConfig], Optional[pointage.Mapping]]:
    cfg = db.query(PointageConfig).order_by(PointageConfig.id).first()
    if cfg is None or cfg.conn is None or cfg.installed_at is None:
        return cfg, None
    return cfg, pointage.Mapping.from_json(cfg.data)


def _filters(request: Request) -> tuple[pointage.Filters, Optional[str]]:
    p = request.query_params
    today = date.today()
    day = pointage.parse_day(p.get("date"))
    du, au = pointage.parse_day(p.get("du")), pointage.parse_day(p.get("au"))
    warning = None
    if day:
        du = au = day
    du = du or au or today
    au = au or du
    if au < du:
        du, au = au, du
    if (au - du).days >= MAX_DAYS:
        au = du + timedelta(days=MAX_DAYS - 1)
        warning = f"Période limitée à {MAX_DAYS} jours : affichage du {du:%d/%m/%Y} au {au:%d/%m/%Y}."
    return pointage.Filters(
        du=du, au=au, q=p.get("q", "").strip(), service=p.get("service", ""),
        statuts=[s for s in p.getlist("statut") if s in pointage.STATUTS],
        sort=p.get("sort", "nom") if p.get("sort", "nom") in pointage.SORTABLE else "nom",
        desc=p.get("dir") == "desc", team=p.get("equipe", ""), directs=p.get("directs") == "1",
    ), warning


class ScopeError(Exception):
    pass


def apply_scope(request: Request, db: Session, engine, mapping: pointage.Mapping, f: pointage.Filters) -> Optional[str]:
    """Compte limité à son équipe : impose sa hiérarchie comme périmètre. Renvoie le libellé du périmètre."""
    user = db.query(User).filter(User.username == request.session.get("user", "")).one_or_none()
    if user is None or not user.team_only:
        return None
    if not mapping.hier_table:
        raise ScopeError("Votre compte est limité à votre équipe, mais la hiérarchie n'est pas configurée : "
                         "contactez un administrateur.")
    found = pointage.resolve_employee(engine, mapping, user.emp_matricule or "") if user.emp_matricule else None
    if found is None:
        raise ScopeError("Votre compte est limité à votre équipe mais n'est rattaché à aucun employé "
                         "(matricule manquant ou introuvable) : contactez un administrateur.")
    f.scope_root = found[0]
    if f.team and not pointage.in_scope(engine, mapping, f.scope_root, f.team):
        f.team = ""
    return found[1]


@router.get("")
def suivi(request: Request, db: Session = Depends(get_db)):
    cfg, mapping = load_config(db)
    f, warning = _filters(request)
    try:
        page = max(int(request.query_params.get("page", 1)), 1)
    except ValueError:
        page = 1
    try:
        size = int(request.query_params.get("size", 100))
    except ValueError:
        size = 100
    size = size if size in PAGE_SIZES else 100
    context = dict(f=f, warning=warning, statuts=pointage.STATUTS, page=page, size=size, page_sizes=PAGE_SIZES,
                   hhmm=pointage.hhmm, configured=mapping is not None, data=None, error=None, services=[],
                   managers=[], scope_label=None, has_hierarchy=bool(mapping and mapping.hier_table),
                   single_day=f.du == f.au, mode="jour" if f.du == f.au else "periode")
    if mapping is None:
        return render(request, "suivi.html", **context)
    engine = make_engine(cfg.conn)
    try:
        context["scope_label"] = apply_scope(request, db, engine, mapping, f)
        context["data"] = pointage.daily(engine, mapping, f, page, size)
        context["services"] = pointage.services(engine, mapping, f.scope_root)
        context["managers"] = pointage.managers(engine, mapping, f.scope_root)
    except ScopeError as exc:
        context["error"] = str(exc)
    except Exception as exc:
        context["error"] = friendly(exc)
    finally:
        engine.dispose()
    total = context["data"]["total"] if context["data"] else 0
    context["pages"] = max((total + size - 1) // size, 1)
    return render(request, "suivi.html", **context)


@router.get("/detail", response_class=HTMLResponse)
def detail(request: Request, key: str, jour: str, db: Session = Depends(get_db)):
    cfg, mapping = load_config(db)
    day = pointage.parse_day(jour)
    if mapping is None or day is None:
        return HTMLResponse('<p class="empty">Détail indisponible.</p>', status_code=400)
    engine = make_engine(cfg.conn)
    try:
        f = pointage.Filters(du=day, au=day)
        apply_scope(request, db, engine, mapping, f)
        if not pointage.in_scope(engine, mapping, f.scope_root, key):
            raise ScopeError("Cet employé ne fait pas partie de votre équipe.")
        columns, rows = pointage.detail(engine, mapping, key, day)
        error = None
    except ScopeError as exc:
        columns, rows, error = [], [], str(exc)
    except Exception as exc:
        columns, rows, error = [], [], friendly(exc)
    finally:
        engine.dispose()
    return render(request, "_suivi_detail.html", columns=columns, rows=rows, error=error, day=day, key=key)


@router.get("/export.xlsx")
def export(request: Request, db: Session = Depends(get_db)):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    cfg, mapping = load_config(db)
    if mapping is None:
        return Response("Module de pointage non configuré.", status_code=400)
    f, _ = _filters(request)
    engine = make_engine(cfg.conn)
    try:
        apply_scope(request, db, engine, mapping, f)
        rows = pointage.export_rows(engine, mapping, f)
    except ScopeError as exc:
        return Response(str(exc), status_code=403, media_type="text/plain; charset=utf-8")
    except Exception as exc:
        return Response(f"Export impossible : {friendly(exc)}", status_code=400, media_type="text/plain; charset=utf-8")
    finally:
        engine.dispose()

    wb = Workbook()
    ws = wb.active
    ws.title = "Suivi journalier"
    headers = ["Date", "Matricule", "Nom", "Prénom", "Service", "1er pointage", "Dernier pointage", "Nb pointages",
               "Statut", "Heure validée", "Durée effective", "Heure validée (min)", "Durée effective (min)",
               "Responsable"]
    ws.append(headers)
    fills = {"A_L_HEURE": "DCFCE7", "RETARD": "FFEDD5", "ABSENT": "FEE2E2", "INCOMPLET": "E5E7EB"}
    for r in rows:
        ws.append([
            r["jour"], r["matricule"], r["nom"], r["prenom"], r["service"],
            r["premier_pointage"].time() if r["premier_pointage"] else None,
            r["dernier_pointage"].time() if r["dernier_pointage"] else None,
            r["nb_pointages"], r["statut_libelle"], r["heure_validee"], r["duree_effective"],
            float(r["heure_validee_min"]) if r["heure_validee_min"] is not None else None,
            float(r["duree_effective_min"]) if r["duree_effective_min"] is not None else None,
            r["responsable"],
        ])
        row = ws.max_row
        ws.cell(row, 1).number_format = "DD/MM/YYYY"
        for col in (6, 7):
            ws.cell(row, col).number_format = "HH:MM"
        for col in (10, 11):
            ws.cell(row, col).number_format = "[H]:MM"
        ws.cell(row, 9).fill = PatternFill("solid", fgColor=fills.get(r["statut"], "FFFFFF"))
    for i, h in enumerate(headers, 1):
        ws.cell(1, i).font = Font(bold=True)
        ws.cell(1, i).alignment = Alignment(wrap_text=True, vertical="top")
        ws.column_dimensions[get_column_letter(i)].width = max(12, len(h) + 2)
    ws.column_dimensions["C"].width = 24
    ws.column_dimensions["E"].width = 22
    ws.column_dimensions["N"].width = 26
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    info = wb.create_sheet("Filtres")
    for label, value in [
        ("Du", f.du.strftime("%d/%m/%Y")), ("Au", f.au.strftime("%d/%m/%Y")), ("Recherche", f.q or "—"),
        ("Service", f.service or "Tous"),
        ("Équipe", (f.team + (" (directs)" if f.directs else "")) if f.team else "Toutes"),
        ("Périmètre", "équipe du compte" if f.scope_root is not None else "tout le personnel"),
        ("Statuts", ", ".join(pointage.STATUTS[s][0] for s in f.statuts) or "Tous"),
        ("Lignes", len(rows)), ("Exporté par", request.session.get("user", "")),
    ]:
        info.append([label, value])
    info.column_dimensions["A"].width = 14
    info.column_dimensions["B"].width = 40

    buf = io.BytesIO()
    wb.save(buf)
    name = f"suivi_{f.du:%Y%m%d}" + ("" if f.du == f.au else f"_{f.au:%Y%m%d}") + ".xlsx"
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
