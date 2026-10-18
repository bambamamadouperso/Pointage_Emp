"""Administration des relances des demandes en attente de validation (congés, télétravail, missions)."""
import json
import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth, validations
from ..database import get_db
from ..errors import friendly
from ..mails import get_settings as mail_settings_of, parse_addresses, valid_email
from ..models import APPROVAL_SOURCES, ApprovalReminderLog, utcnow
from ..web import flash, redirect, render, require_login

router = APIRouter(prefix="/admin/validations", dependencies=[Depends(require_login)])


@router.get("")
def page(request: Request, db: Session = Depends(get_db)):
    s = validations.get_settings(db)
    state, error = None, ""
    try:
        state = validations.situation()
    except Exception as exc:  # noqa: BLE001
        error = str(exc) if isinstance(exc, ValueError) else friendly(exc)
    columns = {k: validations.table_columns(k) for k in APPROVAL_SOURCES} if state else {}
    logs = db.scalars(select(ApprovalReminderLog).order_by(ApprovalReminderLog.created_at.desc(),
                                                           ApprovalReminderLog.id.desc()).limit(100)).all()
    return render(request, "admin/validations.html", s=s, mail=mail_settings_of(db), state=state, error=error,
                  logs=logs, labels=APPROVAL_SOURCES, cfg=validations.sources(s), columns=columns,
                  weekdays=validations.WEEKDAYS, days=validations.weekdays(s), unknown=validations.UNKNOWN)


def _int(value: str, label: str, low: int, high: int) -> int:
    try:
        n = int((value or "0").strip())
    except ValueError:
        raise ValueError(f"{label} : nombre entier attendu.") from None
    if not low <= n <= high:
        raise ValueError(f"{label} : entre {low} et {high}.")
    return n


@router.post("/parametres")
async def save_settings(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    get = lambda key, default="": str(form.get(key, default) or default).strip()  # noqa: E731
    send_time = validations.parse_time(get("send_time", "08:30"))
    if send_time is None:
        flash(request, "Heure d'envoi invalide : format HH:MM (ex. 08:30).", "err")
        return redirect("/admin/validations")
    url = get("app_url")
    if url and not re.match(r"^https?://", url):
        flash(request, "Adresse de l'outil RH : elle doit commencer par http:// ou https://.", "err")
        return redirect("/admin/validations")
    bad = [a for a in parse_addresses(get("cc_addresses")) if not valid_email(a)]
    if bad:
        flash(request, "Adresse(s) RH invalide(s) : " + ", ".join(bad) + ".", "err")
        return redirect("/admin/validations")
    try:
        numbers = {key: _int(get(key, str(default)), label, low, high) for key, label, default, low, high in (
            ("grace_days", "Délai avant la première relance", 0, 0, 60),
            ("repeat_days", "Espacement des relances", 1, 1, 30),
            ("max_reminders", "Nombre maximal de relances", 0, 0, 100),
            ("horizon_days", "Demandes qui commencent dans", 14, 0, 365),
            ("past_days", "Demandes terminées depuis", 31, 0, 365),
            ("cc_after_absent", "Copie RH à partir de", 0, 0, 60),
            ("max_lag_minutes", "Garde-fou de synchronisation", 0, 0, 1440))}
    except ValueError as exc:
        flash(request, str(exc), "err")
        return redirect("/admin/validations")
    days = sorted({int(d) for d in form.getlist("weekdays") if str(d).isdigit() and 1 <= int(d) <= 7})
    srcs = {}
    for key in APPROVAL_SOURCES:
        srcs[key] = {"enabled": bool(form.get(f"{key}_enabled")), "pending": get(f"{key}_pending"),
                     "validator_col": get(f"{key}_validator_col"), "step_col": get(f"{key}_step_col"),
                     "created_col": get(f"{key}_created_col")}
        if srcs[key]["enabled"] and not srcs[key]["pending"]:
            flash(request, f"{APPROVAL_SOURCES[key]} : indiquez au moins un statut « en attente ».", "err")
            return redirect("/admin/validations")
    s = validations.get_settings(db)
    s.enabled, s.send_time, s.weekdays = bool(form.get("enabled")), send_time, ",".join(map(str, days))
    s.app_url, s.app_name = url, get("app_name", "HR Smart")[:60]
    s.sources = json.dumps(srcs, ensure_ascii=False)
    for key, value in numbers.items():
        setattr(s, key, value)
    s.cc_addresses = ", ".join(parse_addresses(get("cc_addresses")))
    s.subject, s.message = get("subject")[:300], get("message")[:2000]
    s.show_in_suivi = bool(form.get("show_in_suivi"))
    s.updated_at, s.updated_by = utcnow(), request.session.get("user", "")
    db.commit()
    active = [APPROVAL_SOURCES[k] for k, c in srcs.items() if c["enabled"]]
    auth.audit(request, "Relances des demandes : paramètres", "",
               f"{'activées' if s.enabled else 'désactivées'}, {s.send_time} ({s.weekdays}), sources "
               f"{', '.join(active) or 'aucune'}, tous les {s.repeat_days} j, copie RH {s.cc_addresses or '—'}")
    flash(request, "Paramètres des relances enregistrés.", "ok")
    return redirect("/admin/validations")


@router.get("/apercu", response_class=HTMLResponse)
def preview(validateur: str = ""):
    try:
        _, body = validations.preview(validateur)
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(f"<p style='font-family:sans-serif'>Aperçu impossible : {friendly(exc)}</p>", status_code=400)
    return HTMLResponse(body, headers={"Content-Security-Policy": "script-src 'none'"})


@router.post("/essai")
def send_test(request: Request, validateur: str = Form(""), to: str = Form(...)):
    to = to.strip()
    if not valid_email(to):
        flash(request, "Indiquez une adresse e-mail valide pour l'essai.", "err")
        return redirect("/admin/validations")
    try:
        detail = validations.send_test(validateur, to)
    except Exception as exc:  # noqa: BLE001
        flash(request, f"Envoi de l'essai impossible : {friendly(exc)}", "err")
        return redirect("/admin/validations")
    auth.audit(request, "Relances des demandes : essai", to, f"validateur {validateur or 'exemple'}")
    flash(request, f"Relance d'essai envoyée à {detail}.", "ok")
    return redirect("/admin/validations")


@router.post("/envoyer")
def run_now(request: Request):
    result = validations.run_due()
    labels = {"idle": "rien à envoyer (relances désactivées, jour ou heure non atteints, déjà envoyées ou aucune demande)",
              "unconfigured": "serveur SMTP ou source des pointages non configuré", "busy": "un envoi est déjà en cours"}
    if result["status"] == "done":
        flash(request, f"Relances : {result['sent']} envoyée(s), {result['skipped']} ignorée(s), "
                       f"{result['failed']} échec(s).", "ok" if not result["failed"] else "warn")
    elif result["status"] == "stale":
        flash(request, f"Aucune relance envoyée : {result['reason']}.", "warn")
    elif result["status"] == "error":
        flash(request, f"Envoi impossible : {result['error']}", "err")
    else:
        flash(request, f"Relances : {labels.get(result['status'], result['status'])}.", "warn")
    return redirect("/admin/validations#journal")
