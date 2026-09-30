"""Rapports d'analyse : indicateurs, tendances, habitudes d'arrivée, services, employés à suivre, export Excel."""
import io
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import pointage, rapports
from ..database import get_db
from ..errors import friendly
from ..sync import make_engine
from ..web import render, require_login
from .suivi import ScopeError, apply_scope, load_config

router = APIRouter(prefix="/rapports", dependencies=[Depends(require_login)])

SORTS = {"absences": "Absences", "retards": "Retards", "retard_min": "Minutes de retard", "incomplets": "Oublis de badge",
         "presence": "Taux de présence (croissant)", "heures": "Heures validées", "nom": "Nom"}


def _filters(request: Request) -> tuple[pointage.Filters, str]:
    p = request.query_params
    presets = p.getlist("p")
    preset = "perso" if "perso" in presets else (presets[0] if presets else "30j")
    du, au = pointage.parse_day(p.get("du")), pointage.parse_day(p.get("au"))
    if du and au and preset == "perso":
        du, au = min(du, au), max(du, au)
        au = min(au, du + timedelta(days=365))
    else:
        preset = preset if preset in dict(rapports.PRESETS) else "30j"
        du, au = rapports.period(preset)
    team = p.get("equipe", "")
    return pointage.Filters(du=du, au=au, service=pointage.multi(p.getlist("service")), team=team, categorie=pointage.multi(p.getlist("categorie")),
                            direction=pointage.multi(p.getlist("direction")),
                            directs=p.get("directs") == "1" and bool(team)), preset


@router.get("")
def page(request: Request, db: Session = Depends(get_db)):
    cfg, mapping = load_config(db)
    f, preset = _filters(request)
    sort = request.query_params.get("tri", "absences")
    sort = sort if sort in SORTS else "absences"
    context = dict(f=f, preset=preset, presets=rapports.PRESETS, sorts=SORTS, sort=sort, configured=mapping is not None,
                   data=None, error=None, services=[], managers=[], categories=[], directions=[], scope_label=None,
                   hhmm=pointage.hhmm, has_directions=bool(mapping and mapping.dir_col),
                   series=rapports.SERIES, has_hierarchy=bool(mapping and mapping.hier_table), today=date.today())
    if mapping is None:
        return render(request, "rapports.html", **context)
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
    try:
        context["scope_label"] = apply_scope(request, db, engine, mapping, f)
        data = rapports.build(engine, mapping, f, sort)
        params = pointage.params_at(engine, mapping, f.au)
        data["trend"] = rapports.trend_chart(data["jours"])
        data["arrival"] = rapports.arrival_chart(data["arrivees"], params["seuil_retard"], params["debut_journee"])
        context["data"] = data
        context["services"] = pointage.services(engine, mapping, f.scope_root)
        context["managers"] = pointage.managers(engine, mapping, f.scope_root)
        context["categories"] = pointage.categories(engine, mapping, f.scope_root)
        context["directions"] = pointage.directions(engine, mapping, f.scope_root)
    except ScopeError as exc:
        context["error"] = str(exc)
    except Exception as exc:
        context["error"] = friendly(exc)
    finally:
        engine.dispose()
    return render(request, "rapports.html", **context)


@router.get("/export.xlsx")
def export(request: Request, db: Session = Depends(get_db)):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    cfg, mapping = load_config(db)
    if mapping is None:
        return Response("Module de pointage non configuré.", status_code=400)
    f, _ = _filters(request)
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
    try:
        apply_scope(request, db, engine, mapping, f)
        data = rapports.build(engine, mapping, f, request.query_params.get("tri", "absences"), compare=False)
    except ScopeError as exc:
        return Response(str(exc), status_code=403, media_type="text/plain; charset=utf-8")
    except Exception as exc:
        return Response(f"Export impossible : {friendly(exc)}", status_code=400, media_type="text/plain; charset=utf-8")
    finally:
        engine.dispose()

    def pct(v):
        return round(v / 100, 4) if v is not None else None

    def hours(v):
        return round(float(v), 2) if v is not None else None

    wb = Workbook()
    sheets = [
        ("Employés", ["Matricule", "Nom", "Prénom", "Service", "Statut du personnel", "Responsable", "Agent terrain", "Jours attendus",
                      "Présences", "Taux de présence", "À l'heure", "Retards", "Minutes de retard", "Taux de ponctualité",
                      "Absences", "dont lundi/vendredi", "Pointages incomplets", "Congés", "Télétravail", "Sur le terrain",
                      "Heures validées", "Direction"],
         [[e["matricule"], e["nom"], e["prenom"], e["service"], e["categorie"], e["responsable"], "Oui" if e["terrain"] else "Non",
           e["attendus"], e["presents"], pct(e["taux_presence"]), e["a_l_heure"], e["retards"], float(e["retard_min_total"]),
           pct(e["taux_ponctualite"]), e["absences"], e["absences_lun_ven"], e["incomplets"], e["conges"], e["teletravail"],
           e["terrain_jours"], hours(e["heures_validees"]), e.get("direction")] for e in data["employes"]], {10, 14}),
        ("Services", ["Service", "Employés", "Taux de présence", "Taux d'absentéisme", "Taux de ponctualité", "Retards",
                      "Minutes de retard", "Absences", "Pointages incomplets", "Heures validées",
                      "Moyenne heures / jour au bureau"],
         [[s["service"], s["employes"], pct(s["taux_presence"]), pct(s["taux_absence"]), pct(s["taux_ponctualite"]),
           s["retards"], float(s["retard_min_total"]), s["absences"], s["incomplets"], hours(s["heures_validees"]),
           hours(s["heures_moy_bureau"])] for s in data["services"]], {3, 4, 5}),
        *([("Statuts du personnel", ["Statut du personnel", "Employés", "Taux de présence", "Taux d'absentéisme", "Taux de ponctualité",
                            "Retards", "Minutes de retard", "Absences", "Pointages incomplets", "Heures validées",
                            "Moyenne heures / jour au bureau"],
             [[g["groupe"], g["employes"], pct(g["taux_presence"]), pct(g["taux_absence"]), pct(g["taux_ponctualite"]),
               g["retards"], float(g["retard_min_total"]), g["absences"], g["incomplets"], hours(g["heures_validees"]),
               hours(g["heures_moy_bureau"])] for g in data["categories"]], {3, 4, 5})] if data["categories"] else []),
        *([("Directions", ["Direction", "Employés", "Taux de présence", "Taux d'absentéisme", "Taux de ponctualité",
                            "Retards", "Minutes de retard", "Absences", "Pointages incomplets", "Heures validées",
                            "Moyenne heures / jour au bureau"],
             [[g["groupe"], g["employes"], pct(g["taux_presence"]), pct(g["taux_absence"]), pct(g["taux_ponctualite"]),
               g["retards"], float(g["retard_min_total"]), g["absences"], g["incomplets"], hours(g["heures_validees"]),
               hours(g["heures_moy_bureau"])] for g in data["directions"]], {3, 4, 5})] if data.get("directions") else []),
        ("Par jour" if data["granularite"] == "jour" else "Par semaine",
         ["Date"] + [label for _, label, _ in rapports.SERIES],
         [[j["jour"]] + [j[k] for k, _, _ in rapports.SERIES] for j in data["jours"]], set()),
        ("Alertes", ["Motif", "Matricule", "Nom", "Service", "Responsable", "Détail"],
         [[a["motif"], a["matricule"], a["nom"], a["service"], a["responsable"], a["detail"]] for a in data["alertes"]],
         set()),
    ]
    for i, (title, headers, rows, pct_cols) in enumerate(sheets):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = title
        ws.append(headers)
        for row in rows:
            ws.append(row)
            if title.startswith("Par"):
                ws.cell(ws.max_row, 1).number_format = "DD/MM/YYYY"
            for col in pct_cols:
                ws.cell(ws.max_row, col).number_format = "0.0%"
        for col, h in enumerate(headers, 1):
            ws.cell(1, col).font = Font(bold=True)
            ws.column_dimensions[get_column_letter(col)].width = max(12, min(len(h) + 2, 34))
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
    info = wb.create_sheet("Filtres")
    for row in [("Du", f.du.strftime("%d/%m/%Y")), ("Au", f.au.strftime("%d/%m/%Y")), ("Service", ", ".join(f.service) or "Tous"), ("Statut du personnel", ", ".join(f.categorie) or "Tous"),
                ("Direction", ", ".join(f.direction) or "Toutes"),
                ("Équipe", (f.team + (" (directs)" if f.directs else "")) if f.team else "Toutes"),
                ("Exporté par", request.session.get("user", ""))]:
        info.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    name = f"rapport_{f.du:%Y%m%d}_{f.au:%Y%m%d}.xlsx"
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
