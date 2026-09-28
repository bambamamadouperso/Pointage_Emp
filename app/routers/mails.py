"""Administration des mails de confirmation de badge : serveur SMTP, mode, abonnés, modèle HTML, journal."""
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import auth, mails, pointage
from ..crypto import encrypt
from ..database import get_db
from ..errors import friendly
from ..models import MailLog, MailSubscriber, utcnow
from ..sync import make_engine
from ..web import flash, redirect, render, require_login
from .suivi import load_config

router = APIRouter(prefix="/admin/mails", dependencies=[Depends(require_login)])

SECURITY = {"starttls": "STARTTLS (port 587)", "ssl": "SSL/TLS (port 465)", "none": "Aucun chiffrement (port 25)"}


def _context(db: Session, tab: str) -> dict:
    s = mails.get_settings(db)
    counts = {
        "abonnes": db.scalar(select(func.count(MailSubscriber.id))) or 0,
        "envoyes": db.scalar(select(func.count(MailLog.id)).where(MailLog.status == "sent")) or 0,
        "echecs": db.scalar(select(func.count(MailLog.id)).where(MailLog.status == "failed")) or 0,
    }
    return dict(s=s, tab=tab, counts=counts, security=SECURITY)


@router.get("")
def settings_page(request: Request, db: Session = Depends(get_db)):
    return render(request, "admin/mails.html", **_context(db, "parametres"))


@router.post("/parametres")
async def settings_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    s = mails.get_settings(db)
    before = (s.enabled, s.mode)
    host = str(form.get("smtp_host", "")).strip()
    try:
        port = int(str(form.get("smtp_port", "")).strip() or 0)
    except ValueError:
        port = 0
    from_email = str(form.get("from_email", "")).strip()
    tests = mails.parse_addresses(str(form.get("test_recipients", "")))
    bad = [a for a in tests if not mails.valid_email(a)]
    mode = "production" if form.get("mode") == "production" else "test"
    enabled = form.get("enabled") == "1"
    problems = []
    if bad:
        problems.append("adresse(s) de test invalide(s) : " + ", ".join(bad))
    if from_email and not mails.valid_email(from_email):
        problems.append("adresse d'expéditeur invalide")
    if enabled and not (host and from_email):
        problems.append("renseignez le serveur SMTP et l'adresse d'expéditeur avant d'activer l'envoi")
    if enabled and mode == "test" and not tests:
        problems.append("indiquez au moins une adresse de test (en mode test, tous les mails y sont envoyés)")
    if mode == "production" and s.mode != "production" and form.get("confirm_production") != "1":
        problems.append("cochez la confirmation pour passer en mode production (les employés recevront les mails)")
    if problems:
        flash(request, "Paramètres non enregistrés : " + " ; ".join(problems) + ".", "err")
        return redirect("/admin/mails")
    s.smtp_host, s.smtp_port = host, port or (465 if form.get("smtp_security") == "ssl" else 587)
    s.smtp_security = str(form.get("smtp_security", "starttls")) if form.get("smtp_security") in SECURITY else "starttls"
    s.smtp_user = str(form.get("smtp_user", "")).strip()
    password = str(form.get("smtp_password", ""))
    if password:
        s.smtp_password_enc = encrypt(password)
    elif form.get("clear_password") == "1":
        s.smtp_password_enc = ""
    s.from_email, s.from_name = from_email, str(form.get("from_name", "")).strip()
    s.reply_to = str(form.get("reply_to", "")).strip()
    s.company = str(form.get("company", "")).strip()
    s.test_recipients = ", ".join(tests)
    try:
        s.max_per_run = min(max(int(str(form.get("max_per_run", "200")) or 200), 1), 5000)
    except ValueError:
        s.max_per_run = 200
    s.enabled, s.mode = enabled, mode
    if (enabled, mode) != before and enabled:
        # Activation ou changement de mode : seuls les pointages à venir seront notifiés.
        s.since = datetime.now()
    s.updated_at = utcnow()
    db.commit()
    auth.audit(request, "Mails de badge : paramètres", f"{'activés' if enabled else 'désactivés'} · mode {mode}",
               f"serveur {host}:{s.smtp_port} ({s.smtp_security}) · expéditeur {from_email} · tests {s.test_recipients}")
    if mode == "production" and before[1] != "production":
        flash(request, "Mode PRODUCTION activé : les employés abonnés recevront désormais un mail à chaque nouveau "
                       "pointage.", "warn")
    else:
        flash(request, "Paramètres des mails enregistrés.", "ok")
    return redirect("/admin/mails")


@router.post("/essai")
def send_test(request: Request, to: str = Form(...), db: Session = Depends(get_db)):
    s = mails.get_settings(db)
    to = to.strip()
    if not mails.valid_email(to):
        flash(request, "Adresse d'essai invalide.", "err")
        return redirect(request.headers.get("referer") or "/admin/mails")
    try:
        mails.send_test(s, to)
    except Exception as exc:  # noqa: BLE001
        flash(request, f"Envoi d'essai impossible : {exc.__class__.__name__}: {exc}", "err")
    else:
        auth.audit(request, "Mails de badge : essai envoyé", to)
        flash(request, f"Mail d'essai envoyé à {to} (données d'exemple, marqué « TEST »).", "ok")
    return redirect(request.headers.get("referer") or "/admin/mails")


@router.post("/traiter")
def process_now(request: Request):
    result = mails.process()
    labels = {"disabled": "L'envoi est désactivé.", "not_configured": "Module de pointage non configuré.",
              "no_subscribers": "Aucun abonné.", "busy": "Un traitement est déjà en cours."}
    if result.get("status") == "ok":
        flash(request, f"Traitement terminé : {result['sent']} envoyé(s), {result['failed']} en échec, "
                       f"{result['skipped']} ignoré(s) (mode {result['mode']}).", "ok")
    elif result.get("status") == "error":
        flash(request, f"Traitement impossible : {result.get('error')}", "err")
    else:
        flash(request, labels.get(result.get("status"), "Rien à faire."), "info")
    return redirect("/admin/mails/journal")


# --------------------------------------------------------------------------- abonnés


@router.get("/abonnes")
def subscribers_page(request: Request, db: Session = Depends(get_db)):
    ctx = _context(db, "abonnes")
    cfg, mapping = load_config(db)
    ctx.update(employees=[], error=None, services=[], has_email_col=bool(mapping and mapping.email_col))
    subs = {x.emp_key: x for x in db.scalars(select(MailSubscriber))}
    ctx["subs"] = subs
    if mapping is None:
        ctx["error"] = "Configurez d'abord la source des pointages."
        return render(request, "admin/mails_abonnes.html", **ctx)
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
    try:
        ctx["employees"] = pointage.employee_directory(engine, mapping)
    except Exception as exc:  # noqa: BLE001
        ctx["error"] = friendly(exc)
    finally:
        engine.dispose()
    ctx["services"] = sorted({e["service"] for e in ctx["employees"] if e["service"]})
    return render(request, "admin/mails_abonnes.html", **ctx)


@router.post("/abonnes")
async def subscribers_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form(max_fields=50000)
    cfg, mapping = load_config(db)
    if mapping is None:
        return redirect("/admin/mails/abonnes")
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
    try:
        directory = {e["emp_key"]: e for e in pointage.employee_directory(engine, mapping)}
    finally:
        engine.dispose()
    wanted = [k for k in form.getlist("sub") if k in directory]
    current = {x.emp_key: x for x in db.scalars(select(MailSubscriber))}
    invalid, added = [], 0
    user = request.session.get("user", "")
    for key in wanted:
        e = directory[key]
        override = str(form.get(f"email_{key}", "")).strip()
        if override and not mails.valid_email(override):
            invalid.append(f"{e['matricule']} ({override})")
            override = ""
        sub = current.pop(key, None)
        if sub is None:
            sub = MailSubscriber(emp_key=key, added_by=user)
            db.add(sub)
            added += 1
        sub.matricule, sub.name = e["matricule"] or "", f"{e['prenom'] or ''} {e['nom'] or ''}".strip()
        sub.email = override
    # Les abonnés décochés (et seulement ceux de la liste affichée) sont retirés.
    removed = [k for k in current if k in directory]
    if removed:
        db.execute(delete(MailSubscriber).where(MailSubscriber.emp_key.in_(removed)))
    db.commit()
    auth.audit(request, "Mails de badge : abonnés", f"{len(wanted)} abonné(s)", f"+{added} / -{len(removed)}")
    flash(request, f"{len(wanted)} abonné(s) enregistré(s) ({added} ajouté(s), {len(removed)} retiré(s)).", "ok")
    if invalid:
        flash(request, "Adresse(s) ignorée(s), format invalide : " + ", ".join(invalid[:20]), "warn")
    return redirect("/admin/mails/abonnes")


# --------------------------------------------------------------------------- modèle HTML


@router.get("/modele")
def template_page(request: Request, db: Session = Depends(get_db)):
    ctx = _context(db, "modele")
    ctx.update(variables=mails.VARIABLES, default_subject=mails.DEFAULT_SUBJECT,
               sample={k: v for k, v in mails.sample_context(ctx["s"].company).items() if k != "pointages"})
    return render(request, "admin/mails_modele.html", **ctx)


@router.post("/modele")
async def template_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    s = mails.get_settings(db)
    if form.get("reset") == "1":
        s.subject, s.html = mails.DEFAULT_SUBJECT, mails.DEFAULT_HTML
        message = "Modèle par défaut rétabli."
    else:
        subject, body = str(form.get("subject", "")).strip(), str(form.get("html", ""))
        if not subject or not body.strip():
            flash(request, "L'objet et le contenu du mail sont obligatoires.", "err")
            return redirect("/admin/mails/modele")
        s.subject, s.html = subject, body
        message = "Modèle enregistré."
    s.updated_at = utcnow()
    db.commit()
    auth.audit(request, "Mails de badge : modèle modifié", s.subject)
    flash(request, message, "ok")
    return redirect("/admin/mails/modele")


@router.post("/apercu", response_class=HTMLResponse)
async def preview(request: Request, db: Session = Depends(get_db)):
    """Aperçu du modèle (données d'exemple) ; affiché dans un cadre isolé, sans scripts."""
    form = await request.form()
    s = mails.get_settings(db)
    ctx = mails.sample_context(s.company)
    body = mails.render(str(form.get("html", "")) or mails.DEFAULT_HTML, ctx)
    return HTMLResponse(body, headers={"Content-Security-Policy": "script-src 'none'"})


# --------------------------------------------------------------------------- journal


@router.get("/journal")
def journal_page(request: Request, db: Session = Depends(get_db)):
    ctx = _context(db, "journal")
    status = request.query_params.get("statut", "")
    q = select(MailLog).order_by(MailLog.created_at.desc(), MailLog.id.desc()).limit(300)
    if status in ("sent", "failed", "skipped"):
        q = q.where(MailLog.status == status)
    ctx.update(rows=db.scalars(q).all(), statut=status)
    return render(request, "admin/mails_journal.html", **ctx)
