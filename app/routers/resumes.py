"""Administration des résumés par mail aux responsables (quotidien / hebdomadaire)."""
import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth, digests, pointage
from ..database import get_db
from ..errors import friendly
from ..mails import get_settings as mail_settings_of, valid_email
from ..models import DIGEST_FREQUENCIES, DIGEST_KINDS, DigestLog, DigestSubscriber, utcnow
from ..sync import make_engine
from ..web import flash, redirect, render, require_login
from .suivi import load_config

router = APIRouter(prefix="/admin/resumes", dependencies=[Depends(require_login)])
_TIME = re.compile(r"^([01]?\d|2[0-3])[:hH]([0-5]\d)$")


def _managers(db: Session) -> tuple[dict, str]:
    cfg, mapping = load_config(db)
    if mapping is None:
        return {}, "Configurez d'abord la source des pointages."
    if not mapping.hier_table:
        return {}, "Aucune hiérarchie configurée (Source des pointages → Hiérarchie) : impossible de connaître les équipes."
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
    try:
        return digests.manager_directory(engine, mapping), ""
    except Exception as exc:  # noqa: BLE001
        return {}, friendly(exc)
    finally:
        engine.dispose()


@router.get("")
def page(request: Request, db: Session = Depends(get_db)):
    managers, error = _managers(db)
    subs = {s.manager_key: s for s in db.scalars(select(DigestSubscriber)).all()}
    logs = db.scalars(select(DigestLog).order_by(DigestLog.created_at.desc(), DigestLog.id.desc()).limit(60)).all()
    return render(request, "admin/resumes.html", s=digests.get_settings(db), mail=mail_settings_of(db),
                  managers=managers, subs=subs, error=error, logs=logs, frequencies=DIGEST_FREQUENCIES,
                  kinds=DIGEST_KINDS, weekdays=digests.WEEKDAYS,
                  periods={k: digests.period(k, digests.local_now().date()) for k in DIGEST_KINDS})


@router.post("/parametres")
def save_settings(request: Request, daily_enabled: bool = Form(False), daily_time: str = Form("07:30"),
                  weekly_enabled: bool = Form(False), weekly_day: int = Form(1), weekly_time: str = Form("07:30"),
                  scope: str = Form("directs"), skip_empty: bool = Form(False), app_url: str = Form(""),
                  db: Session = Depends(get_db)):
    times = {}
    for label, value in (("quotidien", daily_time), ("hebdomadaire", weekly_time)):
        m = _TIME.match(value.strip())
        if not m:
            flash(request, f"Heure d'envoi {label} invalide : format HH:MM (ex. 07:30).", "err")
            return redirect("/admin/resumes")
        times[label] = f"{int(m.group(1)):02d}:{m.group(2)}"
    app_url = app_url.strip().rstrip("/")
    if app_url and not re.match(r"^https?://", app_url):
        flash(request, "Adresse de l'application : elle doit commencer par http:// ou https://.", "err")
        return redirect("/admin/resumes")
    s = digests.get_settings(db)
    s.daily_enabled, s.daily_time = daily_enabled, times["quotidien"]
    s.weekly_enabled, s.weekly_day, s.weekly_time = weekly_enabled, min(7, max(1, weekly_day)), times["hebdomadaire"]
    s.scope = scope if scope in ("directs", "equipe") else "directs"
    s.skip_empty, s.app_url = skip_empty, app_url
    s.updated_at, s.updated_by = utcnow(), request.session.get("user", "")
    db.commit()
    auth.audit(request, "Résumés par mail : paramètres", "",
               f"quotidien {'oui ' + s.daily_time if s.daily_enabled else 'non'}, hebdomadaire "
               f"{'oui ' + digests.WEEKDAYS[s.weekly_day] + ' ' + s.weekly_time if s.weekly_enabled else 'non'}, "
               f"périmètre {s.scope}")
    flash(request, "Paramètres des résumés enregistrés.", "ok")
    return redirect("/admin/resumes")


@router.post("/abonnes")
async def save_subscribers(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    managers, _ = _managers(db)
    existing = {s.manager_key: s for s in db.scalars(select(DigestSubscriber)).all()}
    added = removed = 0
    bad = []
    for key, info in managers.items():
        freq = str(form.get(f"freq_{key}", "") or "")
        email = str(form.get(f"email_{key}", "") or "").strip()
        if email and not valid_email(email):
            bad.append(f"{info['nom']} ({email})")
            email = ""
        sub = existing.get(key)
        if freq not in DIGEST_FREQUENCIES:
            if sub is not None:
                db.delete(sub)
                removed += 1
            continue
        if sub is None:
            sub = DigestSubscriber(manager_key=key, added_by=request.session.get("user", ""))
            db.add(sub)
            added += 1
        sub.frequency, sub.email, sub.name, sub.matricule = freq, email, info["nom"], info["matricule"] or ""
    db.commit()
    total = db.query(DigestSubscriber).count()
    auth.audit(request, "Résumés par mail : destinataires", f"{total} responsable(s)",
               f"{added} ajouté(s), {removed} retiré(s)")
    flash(request, f"Destinataires enregistrés : {total} responsable(s) abonné(s).", "ok")
    if bad:
        flash(request, "Adresse(s) invalide(s) ignorée(s) : " + ", ".join(bad) + ".", "warn")
    return redirect("/admin/resumes#abonnes")


@router.get("/apercu", response_class=HTMLResponse)
def preview(manager: str, kind: str = "hebdomadaire"):
    try:
        _, body = digests.preview(manager, kind if kind in DIGEST_KINDS else "hebdomadaire")
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(f"<p style='font-family:sans-serif'>Aperçu impossible : {friendly(exc)}</p>", status_code=400)
    return HTMLResponse(body, headers={"Content-Security-Policy": "script-src 'none'"})


@router.post("/essai")
def send_test(request: Request, manager: str = Form(...), kind: str = Form("hebdomadaire"), to: str = Form(...)):
    to = to.strip()
    if not valid_email(to):
        flash(request, "Indiquez une adresse e-mail valide pour l'essai.", "err")
        return redirect("/admin/resumes")
    try:
        detail = digests.send_test(manager, kind if kind in DIGEST_KINDS else "hebdomadaire", to)
    except Exception as exc:  # noqa: BLE001
        flash(request, f"Envoi de l'essai impossible : {friendly(exc)}", "err")
        return redirect("/admin/resumes")
    auth.audit(request, "Résumés par mail : essai", to, f"{kind}, responsable {manager}")
    flash(request, f"Résumé {kind} d'essai envoyé à {detail}.", "ok")
    return redirect("/admin/resumes")


@router.post("/envoyer")
def run_now(request: Request):
    result = digests.run_due()
    labels = {"idle": "rien à envoyer pour le moment (heure non atteinte, déjà envoyé ou aucun abonné)",
              "unconfigured": "serveur SMTP ou source des pointages non configuré", "busy": "un envoi est déjà en cours"}
    if result["status"] == "done":
        flash(request, f"Résumés : {result['sent']} envoyé(s), {result['skipped']} ignoré(s), {result['failed']} échec(s).",
              "ok" if not result["failed"] else "warn")
    elif result["status"] == "error":
        flash(request, f"Envoi impossible : {result['error']}", "err")
    else:
        flash(request, f"Résumés : {labels.get(result['status'], result['status'])}.", "warn")
    return redirect("/admin/resumes#journal")
