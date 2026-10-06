"""Administration des rappels de pointage envoyés aux employés (arrivée, départ, oubli de la veille)."""
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth, pointage, rappels
from ..database import get_db
from ..errors import friendly
from ..mails import get_settings as mail_settings_of, valid_email
from ..models import REMINDER_KINDS, ReminderLog, utcnow
from ..sync import make_engine
from ..web import flash, redirect, render, require_login
from .suivi import load_config

router = APIRouter(prefix="/admin/rappels", dependencies=[Depends(require_login)])


def _choices(db: Session) -> dict:
    """Valeurs proposées pour les filtres de destinataires (services, sites, directions)."""
    cfg, mapping = load_config(db)
    out = {"service": [], "site": [], "direction": []}
    if mapping is None:
        return out
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
    try:
        out["service"] = pointage.services(engine, mapping)
        for name in ("site", "direction"):
            out[name] = pointage.dimension_values(engine, mapping, name)
    except Exception:  # noqa: BLE001 - les filtres restent saisissables
        pass
    finally:
        engine.dispose()
    return out


@router.get("")
def page(request: Request, db: Session = Depends(get_db)):
    s = rappels.get_settings(db)
    state, error = None, ""
    try:
        state = rappels.situation()
    except Exception as exc:  # noqa: BLE001
        error = friendly(exc) if not isinstance(exc, ValueError) else str(exc)
    logs = db.scalars(select(ReminderLog).order_by(ReminderLog.created_at.desc(), ReminderLog.id.desc()).limit(100)).all()
    _, mapping = load_config(db)
    return render(request, "admin/rappels.html", s=s, mail=mail_settings_of(db), state=state, error=error, logs=logs,
                  kinds=REMINDER_KINDS, enabled=rappels.enabled_kinds(s), choices=_choices(db), chosen={"service": rappels.values(s.services),
                  "site": rappels.values(s.sites), "direction": rappels.values(s.directions)},
                  email_configured=bool(mapping and mapping.email_col), now=rappels.local_now(),
                  lookback=rappels.LOOKBACK_DAYS, window_hours=int(rappels.WINDOW.total_seconds() // 3600))


def _minutes(value: str, label: str) -> int:
    try:
        n = int((value or "0").strip())
    except ValueError:
        raise ValueError(f"{label} : nombre de minutes attendu.") from None
    if not 0 <= n <= 600:
        raise ValueError(f"{label} : entre 0 et 600 minutes.")
    return n


@router.post("/parametres")
async def save_settings(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    get = lambda key, default="": str(form.get(key, default) or default).strip()  # noqa: E731
    times = {}
    for key, label in (("arrival_time", "Heure du rappel d'arrivée"), ("departure_time", "Heure du rappel de départ"),
                       ("forgot_time", "Heure du rappel des oublis")):
        times[key] = rappels.parse_time(get(key))
        if times[key] is None:
            flash(request, f"{label} invalide : format HH:MM (ex. 08:15).", "err")
            return redirect("/admin/rappels")
    try:
        arrival_delay = _minutes(get("arrival_delay", "15"), "Délai après le début du poste")
        departure_delay = _minutes(get("departure_delay", "30"), "Délai après la fin du poste")
        max_lag = _minutes(get("max_lag_minutes", "60"), "Garde-fou de synchronisation")
    except ValueError as exc:
        flash(request, str(exc), "err")
        return redirect("/admin/rappels")
    s = rappels.get_settings(db)
    s.arrival_enabled, s.arrival_time, s.arrival_delay = bool(form.get("arrival_enabled")), times["arrival_time"], \
        arrival_delay
    s.departure_enabled, s.departure_time, s.departure_delay = bool(form.get("departure_enabled")), \
        times["departure_time"], departure_delay
    s.forgot_enabled, s.forgot_time = bool(form.get("forgot_enabled")), times["forgot_time"]
    s.forgot_cc_manager = bool(form.get("forgot_cc_manager"))
    s.services = json.dumps(pointage.multi([str(v) for v in form.getlist("service")]), ensure_ascii=False)
    s.sites = json.dumps(pointage.multi([str(v) for v in form.getlist("site")]), ensure_ascii=False)
    s.directions = json.dumps(pointage.multi([str(v) for v in form.getlist("direction")]), ensure_ascii=False)
    s.excluded = get("excluded")[:5000]
    s.max_lag_minutes = max_lag
    s.message = get("message")[:2000]
    s.updated_at, s.updated_by = utcnow(), request.session.get("user", "")
    db.commit()

    def state(on, detail):
        return f"oui ({detail})" if on else "non"
    auth.audit(request, "Rappels de pointage : paramètres", "",
               f"arrivée {state(s.arrival_enabled, f'{s.arrival_time}, postes +{s.arrival_delay} min')}, "
               f"départ {state(s.departure_enabled, f'{s.departure_time}, postes +{s.departure_delay} min')}, "
               f"oubli de la veille {state(s.forgot_enabled, s.forgot_time)}, "
               f"services {rappels.values(s.services) or 'tous'}, sites {rappels.values(s.sites) or 'tous'}, "
               f"directions {rappels.values(s.directions) or 'toutes'}")
    flash(request, "Paramètres des rappels enregistrés.", "ok")
    return redirect("/admin/rappels")


@router.get("/apercu", response_class=HTMLResponse)
def preview(kind: str = "arrivee"):
    _, body = rappels.preview(kind if kind in REMINDER_KINDS else "arrivee")
    return HTMLResponse(body, headers={"Content-Security-Policy": "script-src 'none'"})


@router.post("/essai")
def send_test(request: Request, kind: str = Form("arrivee"), to: str = Form(...)):
    to = to.strip()
    if not valid_email(to):
        flash(request, "Indiquez une adresse e-mail valide pour l'essai.", "err")
        return redirect("/admin/rappels")
    kind = kind if kind in REMINDER_KINDS else "arrivee"
    try:
        detail = rappels.send_test(kind, to)
    except Exception as exc:  # noqa: BLE001
        flash(request, f"Envoi de l'essai impossible : {friendly(exc)}", "err")
        return redirect("/admin/rappels")
    auth.audit(request, "Rappels de pointage : essai", to, REMINDER_KINDS[kind])
    flash(request, f"Rappel d'essai « {REMINDER_KINDS[kind]} » envoyé à {detail}.", "ok")
    return redirect("/admin/rappels")


@router.post("/envoyer")
def run_now(request: Request):
    result = rappels.run_due()
    labels = {"idle": "rien à envoyer pour le moment (heure non atteinte, déjà envoyé ou personne de concerné)",
              "unconfigured": "serveur SMTP ou source des pointages non configuré", "busy": "un envoi est déjà en cours"}
    if result["status"] == "done":
        flash(request, f"Rappels : {result['sent']} envoyé(s), {result['skipped']} ignoré(s), {result['failed']} échec(s).",
              "ok" if not result["failed"] else "warn")
    elif result["status"] == "stale":
        flash(request, f"Aucun rappel envoyé : {result['reason']}.", "warn")
    elif result["status"] == "error":
        flash(request, f"Envoi impossible : {result['error']}", "err")
    else:
        flash(request, f"Rappels : {labels.get(result['status'], result['status'])}.", "warn")
    return redirect("/admin/rappels#journal")
