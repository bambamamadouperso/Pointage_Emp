"""Aide : définition de chaque indicateur (KPI), méthode et formule de calcul, avec les paramètres en vigueur."""
from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from .. import pointage, rapports
from ..database import get_db
from ..sync import make_engine
from ..web import render, require_login
from .suivi import load_config, validee_allowed

router = APIRouter(prefix="/aide", dependencies=[Depends(require_login)])


def _hm(value: str) -> str:
    """« 07:45 » → « 7h45 » (valeurs des paramètres)."""
    if not value or ":" not in value:
        return value or "—"
    h, m = value.split(":")[:2]
    return f"{int(h)}h{m}"


@router.get("")
def page(request: Request, db: Session = Depends(get_db)):
    cfg, mapping = load_config(db)
    params = dict(pointage.PARAM_DEFAULTS)
    if mapping is not None:
        engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
        try:
            params.update({k: str(v) for k, v in pointage.params_at(engine, mapping, date.today()).items() if v is not None})
        except Exception:  # noqa: BLE001 - l'aide s'affiche avec les valeurs par défaut
            pass
        finally:
            engine.dispose()
    days = {str(n): label for n, label in pointage.WEEKDAYS}
    worked = [days[d.strip()] for d in str(params.get("jours_ouvres", "")).split(",") if d.strip() in days]
    p = {k: _hm(v) for k, v in params.items()}
    p["jours_ouvres"] = ", ".join(worked) or "—"
    return render(request, "aide.html", p=p, statuts=pointage.STATUTS, std=validee_allowed(db, request.session.get("role")),
                  alert={"absences": rapports.ALERT_ABSENCES, "retards": rapports.ALERT_RETARDS,
                         "minutes": rapports.ALERT_LATE_MINUTES, "incomplets": rapports.ALERT_INCOMPLETS},
                  has_direction=bool(mapping and mapping.dir_col), configured=mapping is not None)
