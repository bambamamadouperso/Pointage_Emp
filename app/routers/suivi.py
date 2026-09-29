"""Écran principal : suivi journalier des pointages (filtres dans l'URL, détail, export Excel)."""
import io
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from .. import pointage
from ..auth import audit
from ..database import get_db
from ..joblog import write_log
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
    mapping = pointage.Mapping.from_json(cfg.data)
    if (cfg.sql_version or 0) < pointage.SQL_VERSION and _upgrade_allowed():
        # Nouvelle version de l'application : fonctions et vues PostgreSQL réinstallées une fois.
        # Une seule tentative à la fois (les autres pages continuent avec les objets actuels) ;
        # après un échec, nouvel essai dans 10 minutes seulement.
        try:
            engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
            try:
                pointage.install(engine, mapping, "mise à jour automatique")
                cfg.sql_version = pointage.SQL_VERSION
                db.commit()
                audit(None, "Calculs du pointage mis à jour automatiquement", f"version {pointage.SQL_VERSION}",
                      username="système")
            except Exception as exc:
                _upgrade_state["failed_at"] = time.monotonic()
                write_log("ERROR", f"Mise à jour automatique des calculs du pointage impossible : {friendly(exc)}. "
                                   "Nouvel essai dans 10 minutes, ou réinstallez-les depuis Administration → "
                                   "Source des pointages.")
            finally:
                engine.dispose()
        finally:
            _upgrade_state["lock"].release()
    return cfg, mapping


_upgrade_state = {"lock": threading.Lock(), "failed_at": None}


def _upgrade_allowed() -> bool:
    failed = _upgrade_state["failed_at"]
    if failed is not None and time.monotonic() - failed < 600:
        return False
    return _upgrade_state["lock"].acquire(blocking=False)


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
        du=du, au=au, q=p.get("q", "").strip(), service=pointage.multi(p.getlist("service")),
        statuts=[s for s in p.getlist("statut") if s in pointage.STATUTS],
        sort=p.get("sort", "nom") if p.get("sort", "nom") in pointage.SORTABLE else "nom",
        desc=p.get("dir") == "desc", team=p.get("equipe", ""), directs=p.get("directs") == "1" and bool(p.get("equipe")),
        population=p.get("pop", "") if p.get("pop") in ("liste", "hors") else "", categorie=pointage.multi(p.getlist("categorie")),
    ), warning


def objectif_minutes(engine, mapping, day) -> int:
    """Objectif de durée validée (paramètre historisé, 8h par défaut) en minutes."""
    try:
        h, m = pointage.params_at(engine, mapping, day)["objectif_duree"].split(":")
        return int(h) * 60 + int(m)
    except Exception:  # noqa: BLE001
        return 480


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
    context = dict(f=f, warning=warning, today=date.today(), statuts=pointage.STATUTS, page=page, size=size, page_sizes=PAGE_SIZES,
                   hhmm=pointage.hhmm, configured=mapping is not None, data=None, error=None, services=[],
                   managers=[], categories=[], objectif_min=480, scope_label=None, population=None, last_punch=None, stale=False, has_hierarchy=bool(mapping and mapping.hier_table),
                   single_day=f.du == f.au, mode="jour" if f.du == f.au else "periode")
    if mapping is None:
        return render(request, "suivi.html", **context)
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
    try:
        context["scope_label"] = apply_scope(request, db, engine, mapping, f)
        context["data"] = pointage.daily(engine, mapping, f, page, size)
        context["services"] = pointage.services(engine, mapping, f.scope_root)
        context["managers"] = pointage.managers(engine, mapping, f.scope_root)
        context["categories"] = pointage.categories(engine, mapping, f.scope_root)
        context["objectif_min"] = objectif_minutes(engine, mapping, f.au)
        try:
            context["population"] = pointage.population(engine, mapping)
        except Exception as exc:  # contrôle facultatif : ne doit pas empêcher l'affichage
            write_log("WARNING", f"Contrôle de la liste des employés impossible : {friendly(exc)}")
        last = pointage.last_punch(engine, mapping)
        context["last_punch"] = last
        # Pointages plus anciens que la période affichée : synchronisation probablement arrêtée.
        context["stale"] = last is not None and last.date() < min(f.au, date.today()) - timedelta(days=1)
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
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
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
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
    try:
        apply_scope(request, db, engine, mapping, f)
        rows = pointage.export_rows(engine, mapping, f)
        objectif = objectif_minutes(engine, mapping, f.au)
    except ScopeError as exc:
        return Response(str(exc), status_code=403, media_type="text/plain; charset=utf-8")
    except Exception as exc:
        return Response(f"Export impossible : {friendly(exc)}", status_code=400, media_type="text/plain; charset=utf-8")
    finally:
        engine.dispose()

    wb = Workbook()
    ws = wb.active
    ws.title = "Suivi journalier"
    headers = ["Date", "Matricule", "Nom", "Prénom", "Service", "Statut du personnel", "1er pointage", "Dernier pointage", "Nb pointages",
               "Statut", "Durée validée", "Durée effective", "Durée validée (min)", "Durée effective (min)",
               "Responsable", "Dans la liste des employés"]
    ws.append(headers)
    fills = {"A_L_HEURE": "DCFCE7", "RETARD": "FFEDD5", "ABSENT": "FEE2E2", "INCOMPLET": "E5E7EB", "NON_OUVRE": "E0F2FE",
             "CONGE_ANNUEL": "E4F5D3", "CONGE_EXCEP": "E4F5D3",
             "TELETRAVAIL": "CDEEE7", "TERRAIN": "DBEAFE",
             "ARRET_MALADIE": "E7E5E4"}
    for r in rows:
        ws.append([
            r["jour"], r["matricule"], r["nom"], r["prenom"], r["service"], r["categorie"],
            r["premier_pointage"].time() if r["premier_pointage"] else None,
            r["dernier_pointage"].time() if r["dernier_pointage"] else None,
            r["nb_pointages"], r["statut_libelle"], r["duree_validee"], r["duree_effective"],
            float(r["duree_validee_min"]) if r["duree_validee_min"] is not None else None,
            float(r["duree_effective_min"]) if r["duree_effective_min"] is not None else None,
            r["responsable"], "Non" if r["hors_liste"] else "Oui",
        ])
        row = ws.max_row
        ws.cell(row, 1).number_format = "DD/MM/YYYY"
        for col in (7, 8):
            ws.cell(row, col).number_format = "HH:MM"
        for col in (11, 12):
            ws.cell(row, col).number_format = "[H]:MM"
        ws.cell(row, 10).fill = PatternFill("solid", fgColor=fills.get(r["statut"], "FFFFFF"))
        if r["duree_validee_min"] is not None:  # objectif de durée validée : vert atteint, rouge sinon
            ok = float(r["duree_validee_min"]) >= objectif
            ws.cell(row, 11).fill = PatternFill("solid", fgColor="C6EFCE" if ok else "FFC7CE")
            ws.cell(row, 11).font = Font(color="006100" if ok else "9C0006", bold=True)
    for i, h in enumerate(headers, 1):
        ws.cell(1, i).font = Font(bold=True)
        ws.cell(1, i).alignment = Alignment(wrap_text=True, vertical="top")
        ws.column_dimensions[get_column_letter(i)].width = max(12, len(h) + 2)
    ws.column_dimensions["C"].width = 24
    ws.column_dimensions["E"].width = 22
    ws.column_dimensions["O"].width = 26
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    # Ligne « Moyenne » (présents au bureau), comme à l'écran.
    def avg(values):
        values = [v for v in values if v is not None]
        return sum(values, timedelta()) / len(values) if values else None

    def clock(td):
        return (datetime.min + td).time().replace(microsecond=0) if td is not None else None

    bureau = [r for r in rows if r["statut"] in ("A_L_HEURE", "RETARD", "INCOMPLET")]
    first = avg([timedelta(hours=r["premier_pointage"].hour, minutes=r["premier_pointage"].minute,
                           seconds=r["premier_pointage"].second) for r in bureau if r["premier_pointage"]])
    last = avg([timedelta(hours=r["dernier_pointage"].hour, minutes=r["dernier_pointage"].minute,
                          seconds=r["dernier_pointage"].second)
                for r in bureau if r["dernier_pointage"] and r["nb_pointages"] >= 2 and r["statut"] != "INCOMPLET"])
    complete = [r for r in bureau if r["statut"] != "INCOMPLET"]
    ws.append([])
    ws.append(["Moyenne", "", "présents au bureau", "", "", "", clock(first), clock(last), "", "",
               avg([r["duree_validee"] for r in complete]), avg([r["duree_effective"] for r in complete])])
    row = ws.max_row
    for col in range(1, 13):
        ws.cell(row, col).font = Font(bold=True)
    for col in (7, 8):
        ws.cell(row, col).number_format = "HH:MM"
    for col in (11, 12):
        ws.cell(row, col).number_format = "[H]:MM"
    info = wb.create_sheet("Filtres")
    for label, value in [
        ("Du", f.du.strftime("%d/%m/%Y")), ("Au", f.au.strftime("%d/%m/%Y")), ("Recherche", f.q or "—"),
        ("Objectif de durée validée", f"{objectif // 60}h{objectif % 60:02d} (vert si atteint, rouge sinon)"),
        ("Service", ", ".join(f.service) or "Tous"), ("Statut du personnel", ", ".join(f.categorie) or "Tous"),
        ("Personnes", {"liste": "employés de la liste", "hors": "hors liste"}.get(f.population, "toutes")),
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
