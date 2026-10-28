"""Administration de la surveillance des sites de pointage (alerte quotidienne si un site n'a pas pointé)."""
import json

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth, pointage, sites
from ..database import get_db
from ..errors import friendly
from ..mails import get_settings as mail_settings_of, parse_addresses, valid_email
from ..models import SiteAlertLog, utcnow
from ..sync import make_engine
from ..web import flash, redirect, render, require_login
from .suivi import load_config

router = APIRouter(prefix="/admin/sites", dependencies=[Depends(require_login)])


@router.get("")
def page(request: Request, db: Session = Depends(get_db)):
    s = sites.get_settings(db)
    cfg, mapping = load_config(db)
    now = sites.local_now()
    activity, checked, silent, error = [], [], [], ""
    if mapping is not None and mapping.punch_site_col:
        engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
        try:
            activity = sites.site_activity(engine, mapping, now, s.lookback_days or 30)
            checked, silent = sites.evaluate(activity, sites.monitored(s))
        except Exception as exc:  # noqa: BLE001
            error = friendly(exc)
        finally:
            engine.dispose()
    logs = db.scalars(select(SiteAlertLog).order_by(SiteAlertLog.jour.desc()).limit(60)).all()
    for log in logs:
        log.silent_list = json.loads(log.silent or "[]")
    return render(request, "admin/sites.html", s=s, configured=mapping is not None,
                  site_col=bool(mapping and mapping.punch_site_col), activity=activity, checked=checked,
                  silent={x["site"] for x in silent}, error=error, logs=logs, weekdays=sites.WEEKDAYS,
                  days=sites.weekdays(s), chosen=sites.monitored(s), now=now, mail=mail_settings_of(db),
                  status_labels=sites.STATUS, default_subject=sites.DEFAULT_SUBJECT)


@router.post("")
async def save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    get = lambda key, default="": str(form.get(key, default) or default).strip()  # noqa: E731
    check_time = sites.parse_time(get("check_time", "09:00"))
    if check_time is None:
        flash(request, "Heure de contrôle invalide : format HH:MM (ex. 09:00).", "err")
        return redirect("/admin/sites")
    recipients = parse_addresses(get("recipients"))
    bad = [a for a in recipients if not valid_email(a)]
    if bad:
        flash(request, "Adresse(s) invalide(s) : " + ", ".join(bad) + ".", "err")
        return redirect("/admin/sites")
    enabled = bool(form.get("enabled"))
    if enabled and not recipients:
        flash(request, "Indiquez au moins un destinataire avant d'activer la surveillance.", "err")
        return redirect("/admin/sites")
    try:
        lookback = min(max(int(get("lookback_days", "30") or 30), 1), 365)
    except ValueError:
        lookback = 30
    s = sites.get_settings(db)
    s.enabled, s.check_time, s.recipients = enabled, check_time, ", ".join(recipients)
    s.weekdays = ",".join(str(d) for d in sorted({int(d) for d in form.getlist("weekdays") if str(d).isdigit()
                                                   and 1 <= int(d) <= 7}))
    s.skip_holidays = bool(form.get("skip_holidays"))
    s.sites = ", ".join(pointage.multi([str(v) for v in form.getlist("sites")]))
    s.lookback_days = lookback
    s.subject, s.message = get("subject")[:300], get("message")[:2000]
    s.updated_at, s.updated_by = utcnow(), request.session.get("user", "")
    db.commit()
    auth.audit(request, "Surveillance des sites : paramètres", f"{'activée' if enabled else 'désactivée'}",
               f"{check_time} ({s.weekdays}), sites {s.sites or 'tous'}, destinataires {s.recipients or '—'}")
    flash(request, "Surveillance des sites enregistrée.", "ok")
    return redirect("/admin/sites")


@router.post("/controler")
def check_now(request: Request):
    result = sites.run_due(force=True)
    status = result.get("status")
    if status in ("sent", "ok", "no_site", "no_recipient", "failed"):
        silent = ", ".join(result.get("silent") or []) or "aucun"
        flash(request, f"Contrôle effectué : {result.get('checked', 0)} site(s), sans pointage : {silent} — "
                       f"{sites.STATUS[status].lower()}{' (' + result['error'] + ')' if result.get('error') else ''}.",
              "ok" if status in ("ok", "sent") else "warn")
        auth.audit(request, "Surveillance des sites : contrôle manuel", sites.STATUS[status], silent)
    else:
        flash(request, f"Contrôle impossible : {result.get('reason') or result.get('error') or status}.", "err")
    return redirect("/admin/sites#journal")


@router.post("/essai")
def test(request: Request, to: str = Form("")):
    to = to.strip()
    if not valid_email(to):
        flash(request, "Indiquez une adresse e-mail valide pour l'essai.", "err")
        return redirect("/admin/sites")
    try:
        mode = sites.send_test(to)
    except Exception as exc:  # noqa: BLE001
        flash(request, f"Envoi de l'essai impossible : {friendly(exc)}", "err")
        return redirect("/admin/sites")
    auth.audit(request, "Surveillance des sites : essai", to)
    flash(request, f"Alerte d'essai envoyée {'à ' + to if mode == 'production' else '(mode test : vers les adresses de test)'}.", "ok")
    return redirect("/admin/sites")
