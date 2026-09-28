"""Mails de confirmation de badge envoyés aux employés abonnés, après chaque synchronisation.

Garde-fous :
- seuls les pointages postérieurs à l'activation (MailSettings.since) sont notifiés, jamais l'historique ;
- chaque pointage n'est notifié qu'une fois (journal mail_log, clé employé + horodatage) ;
- hors mode production, AUCUN mail ne part vers un employé : tout est redirigé vers les adresses de test ;
- nombre de mails limité par passage (le reste part au passage suivant).
"""
import html
import logging
import re
import smtplib
import ssl
import threading
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from typing import Optional

from sqlalchemy import select

from . import pointage
from .crypto import decrypt
from .database import SessionLocal
from .joblog import write_log
from .models import MailLog, MailSettings, MailSubscriber, PointageConfig, utcnow

logger = logging.getLogger("mails")

MAX_ATTEMPTS = 3
_JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
         "novembre", "décembre"]
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")

VARIABLES = [
    ("prenom", "Prénom", "Aminata"), ("nom", "Nom", "DIOP"), ("nom_complet", "Prénom et nom", "Aminata DIOP"),
    ("matricule", "Matricule", "590394"), ("service", "Service", "ADV et Service Clients"),
    ("libelle", "Arrivée / Départ", "Arrivée"), ("date", "Date du pointage", "lundi 28 septembre 2026"),
    ("heure", "Heure du pointage", "07h42"), ("pointages", "Pointages de la journée (liste)", ""),
    ("nb_pointages", "Nombre de pointages du jour", "1"), ("entreprise", "Nom de l'entreprise", "Mon entreprise"),
    ("annee", "Année", "2026"),
]

DEFAULT_SUBJECT = "Pointage enregistré — {{libelle}} à {{heure}}, {{date}}"
_OLD_SUBJECTS = ("{{libelle}} enregistrée — {{date}} à {{heure}}",)

DEFAULT_HTML = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light only">
<title>Confirmation de pointage</title>
</head>
<body style="margin:0;padding:0;background:#f2f5f9;-webkit-text-size-adjust:100%;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f2f5f9;">
  <tr><td align="center" style="padding:32px 12px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;">

      <tr><td style="padding:0 4px 18px;font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
          <td style="width:34px;height:34px;background:#0e7c6e;border-radius:9px;text-align:center;vertical-align:middle;color:#ffffff;font-size:18px;line-height:34px;">&#9719;</td>
          <td style="padding-left:10px;font-size:15px;font-weight:700;color:#0b1a2b;letter-spacing:-0.2px;">{{entreprise}}</td>
        </tr></table>
      </td></tr>

      <tr><td style="background:#ffffff;border:1px solid #e3e9f0;border-radius:16px;overflow:hidden;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr><td style="height:4px;background:#0e7c6e;background-image:linear-gradient(90deg,#1b4f8a,#0e7c6e);font-size:0;line-height:0;">&nbsp;</td></tr>
          <tr><td style="padding:34px 36px 8px;font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
              <td style="width:44px;height:44px;border-radius:50%;background:#e3f6ef;text-align:center;vertical-align:middle;color:#0e7c6e;font-size:22px;font-weight:700;line-height:44px;">&#10003;</td>
            </tr></table>
            <p style="margin:20px 0 6px;font-size:13px;font-weight:600;letter-spacing:1.2px;text-transform:uppercase;color:#0e7c6e;">Pointage enregistré</p>
            <h1 style="margin:0;font-size:24px;line-height:1.3;font-weight:700;color:#0b1a2b;letter-spacing:-0.3px;">Bonjour {{prenom}},</h1>
            <p style="margin:12px 0 0;font-size:15px;line-height:1.6;color:#46566a;">Votre badge a bien été pris en compte. Voici le détail de ce pointage.</p>
          </td></tr>
          <tr><td style="padding:22px 36px 6px;font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f7f9fc;border:1px solid #e6ecf3;border-radius:12px;">
              <tr>
                <td style="padding:18px 20px;border-right:1px solid #e6ecf3;" width="50%">
                  <p style="margin:0;font-size:12px;color:#7a8898;text-transform:uppercase;letter-spacing:.8px;">{{libelle}}</p>
                  <p style="margin:4px 0 0;font-size:28px;font-weight:700;color:#0b1a2b;letter-spacing:-0.5px;">{{heure}}</p>
                </td>
                <td style="padding:18px 20px;" width="50%">
                  <p style="margin:0;font-size:12px;color:#7a8898;text-transform:uppercase;letter-spacing:.8px;">Date</p>
                  <p style="margin:6px 0 0;font-size:15px;font-weight:600;color:#0b1a2b;">{{date}}</p>
                </td>
              </tr>
            </table>
          </td></tr>
          <tr><td style="padding:16px 36px 4px;font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
            <p style="margin:0 0 8px;font-size:13px;font-weight:600;color:#0b1a2b;">Vos pointages de la journée</p>
            {{pointages}}
          </td></tr>
          <tr><td style="padding:14px 36px 30px;font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
            <p style="margin:0;font-size:13px;line-height:1.6;color:#7a8898;">Matricule {{matricule}} · {{service}}</p>
          </td></tr>
        </table>
      </td></tr>

      <tr><td style="padding:20px 8px 0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;text-align:center;">
        <p style="margin:0;font-size:12px;line-height:1.6;color:#8896a6;">Si vous n'êtes pas à l'origine de ce pointage, prévenez rapidement votre responsable ou le service RH.</p>
        <p style="margin:8px 0 0;font-size:12px;color:#a3afbc;">Message automatique — merci de ne pas y répondre. © {{annee}} {{entreprise}}</p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>
"""


# --------------------------------------------------------------------------- réglages


def get_settings(db) -> MailSettings:
    row = db.scalars(select(MailSettings).order_by(MailSettings.id)).first()
    if row is None:
        row = MailSettings(subject=DEFAULT_SUBJECT, html=DEFAULT_HTML)
        db.add(row)
        db.commit()
    elif row.subject in _OLD_SUBJECTS:  # ancien objet par défaut (accord incorrect avec « Départ »)
        row.subject = DEFAULT_SUBJECT
        db.commit()
    return row


def parse_addresses(raw: str) -> list[str]:
    return [a for a in (x.strip() for x in re.split(r"[,;\s]+", raw or "")) if a]


def valid_email(value: str) -> bool:
    return bool(_EMAIL.match((value or "").strip()))


# --------------------------------------------------------------------------- rendu du modèle


def french_date(day) -> str:
    return f"{_JOURS[day.weekday()]} {day.day} {_MOIS[day.month - 1]} {day.year}"


def _punch_list(times: list[datetime], new: set) -> str:
    """Liste des pointages du jour (HTML pour e-mail, styles en ligne)."""
    rows = []
    for i, t in enumerate(times):
        label = "Arrivée" if i == 0 else ("Départ" if i == len(times) - 1 else f"Pointage {i + 1}")
        mark = ('<span style="display:inline-block;margin-left:8px;padding:1px 8px;border-radius:10px;'
                'background:#e3f6ef;color:#0e7c6e;font-size:11px;font-weight:600;">nouveau</span>') if t in new else ""
        rows.append(f'<tr><td style="padding:8px 0;border-bottom:1px solid #eef2f6;font-size:14px;color:#46566a;">'
                    f'{label}{mark}</td><td align="right" style="padding:8px 0;border-bottom:1px solid #eef2f6;'
                    f'font-size:14px;font-weight:600;color:#0b1a2b;">{t:%H}h{t:%M}</td></tr>')
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            + "".join(rows) + "</table>")


def context_for(emp: dict, punches: list[datetime], day_times: list[datetime], company: str) -> dict:
    """Variables d'un mail : l'employé, son dernier nouveau pointage et tous ses pointages du jour."""
    last = max(punches)
    rank = day_times.index(last) + 1 if last in day_times else 1
    prenom, nom = (emp.get("prenom") or "").strip(), (emp.get("nom") or "").strip()
    return {
        "prenom": prenom or nom, "nom": nom, "nom_complet": f"{prenom} {nom}".strip(),
        "matricule": emp.get("matricule") or "", "service": emp.get("service") or "",
        "libelle": "Arrivée" if rank == 1 else "Départ", "date": french_date(last.date()),
        "heure": f"{last:%H}h{last:%M}", "nb_pointages": str(len(day_times)),
        "pointages": _punch_list(day_times, set(punches)), "entreprise": company or "", "annee": str(last.year),
    }


def sample_context(company: str = "") -> dict:
    now = datetime.now().replace(second=0, microsecond=0)
    arrival = now.replace(hour=7, minute=42)
    ctx = {k: v for k, _, v in VARIABLES}
    ctx.update({"entreprise": company or "Mon entreprise", "date": french_date(arrival.date()), "annee": str(arrival.year),
                "pointages": _punch_list([arrival], {arrival})})
    return ctx


def render(template: str, ctx: dict, escape: bool = True) -> str:
    """Remplace les {{variables}} ; les valeurs sont échappées (sauf la liste des pointages, générée ici)."""
    def value(m):
        key = m.group(1)
        if key not in ctx:
            return ""
        v = str(ctx[key])
        return v if (key == "pointages" or not escape) else html.escape(v)
    return _PLACEHOLDER.sub(value, template or "")


def _plain_text(ctx: dict) -> str:
    return (f"Bonjour {ctx['prenom']},\n\nVotre badge a bien été pris en compte.\n"
            f"{ctx['libelle']} : {ctx['heure']} — {ctx['date']}\n\n"
            f"Matricule {ctx['matricule']}\n\nSi vous n'êtes pas à l'origine de ce pointage, prévenez votre "
            f"responsable ou le service RH.\n\nMessage automatique — merci de ne pas y répondre.")


def test_banner(intended: str, name: str) -> str:
    who = html.escape(f"{name} <{intended}>" if intended else f"{name} (aucune adresse e-mail)")
    return ('<div style="margin:0;padding:10px 16px;background:#fff7e6;border-bottom:1px solid #f5d9a8;'
            "font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:13px;color:#8a5300;text-align:center;\">"
            f"<strong>MODE TEST</strong> — ce message aurait été envoyé à {who}</div>")


def build_message(s: MailSettings, ctx: dict, to: list[str], test_for: Optional[tuple[str, str]] = None) -> EmailMessage:
    subject = render(s.subject or DEFAULT_SUBJECT, ctx, escape=False).replace("\n", " ").strip()
    body = render(s.html or DEFAULT_HTML, ctx)
    text = _plain_text(ctx)
    if test_for is not None:
        subject = f"[TEST] {subject}"
        banner = test_banner(*test_for)
        body = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + banner, body, count=1, flags=re.I) \
            if re.search(r"<body[^>]*>", body, re.I) else banner + body
        text = f"[MODE TEST — destinataire prévu : {test_for[1]} <{test_for[0]}>]\n\n" + text
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((s.from_name or "", s.from_email))
    msg["To"] = ", ".join(to)
    if s.reply_to:
        msg["Reply-To"] = s.reply_to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(parseaddr(s.from_email)[1].split("@")[-1] or None))
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(text)
    msg.add_alternative(body, subtype="html")
    return msg


# --------------------------------------------------------------------------- SMTP


def open_smtp(s: MailSettings) -> smtplib.SMTP:
    if not s.smtp_host:
        raise ValueError("Serveur SMTP non renseigné.")
    if s.smtp_security == "ssl":
        client = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port or 465, timeout=20, context=ssl.create_default_context())
    else:
        client = smtplib.SMTP(s.smtp_host, s.smtp_port or 25, timeout=20)
        client.ehlo()
        if s.smtp_security == "starttls":
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
    if s.smtp_user:
        client.login(s.smtp_user, decrypt(s.smtp_password_enc))
    return client


def send_test(s: MailSettings, to: str) -> None:
    """Mail d'essai (données d'exemple) vers une adresse de test : vérifie le serveur et le rendu du modèle."""
    ctx = sample_context(s.company)
    msg = build_message(s, ctx, [to], test_for=("employe@exemple.com", ctx["nom_complet"]))
    client = open_smtp(s)
    try:
        client.send_message(msg)
    finally:
        _quit(client)


def _quit(client) -> None:
    try:
        client.quit()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- traitement après synchronisation

_lock = threading.Lock()


def process(sender=None) -> dict:
    """Notifie les nouveaux pointages des abonnés. Renvoie un résumé ; ne lève jamais d'exception."""
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        return _process(sender)
    except Exception as exc:  # noqa: BLE001 - un souci de mail ne doit jamais bloquer la synchronisation
        logger.exception("Envoi des mails de badge impossible")
        write_log("ERROR", f"Mails de badge : traitement impossible ({exc.__class__.__name__}: {exc}).")
        return {"status": "error", "error": str(exc)}
    finally:
        _lock.release()


def _process(sender) -> dict:
    from .sync import make_engine

    with SessionLocal() as db:
        s = get_settings(db)
        if not s.enabled:
            return {"status": "disabled"}
        if s.since is None:
            s.since = datetime.now()
            db.commit()
        cfg = db.scalars(select(PointageConfig).order_by(PointageConfig.id)).first()
        if cfg is None or cfg.conn is None or cfg.installed_at is None:
            return {"status": "not_configured"}
        mapping = pointage.Mapping.from_json(cfg.data)
        subs = {x.emp_key: x for x in db.scalars(select(MailSubscriber))}
        if not subs:
            return {"status": "no_subscribers"}
        since = max(s.since, datetime.now() - timedelta(days=2))
        engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
        try:
            rows = pointage.punches_since(engine, mapping, since - timedelta(days=1), list(subs))
        finally:
            engine.dispose()
        done = {(x.emp_key, x.punch_at): x for x in db.scalars(
            select(MailLog).where(MailLog.punch_at >= since - timedelta(days=1)))}

        # Par employé : tous ses pointages du jour (pour la liste) et ceux à notifier.
        by_emp: dict[str, dict] = {}
        for r in rows:
            ts = r["ts"].replace(tzinfo=None) if r["ts"].tzinfo else r["ts"]
            entry = by_emp.setdefault(r["emp_key"], {"emp": r, "days": {}, "new": []})
            entry["days"].setdefault(ts.date(), []).append(ts)
            prev = done.get((r["emp_key"], ts))
            if ts >= since and (prev is None or (prev.status == "failed" and prev.attempts < MAX_ATTEMPTS)):
                entry["new"].append(ts)

        test_mode = s.mode != "production"
        test_to = [a for a in parse_addresses(s.test_recipients) if valid_email(a)]
        summary = {"status": "ok", "mode": s.mode, "sent": 0, "failed": 0, "skipped": 0, "pending": 0}
        limit = max(s.max_per_run or 200, 1)
        client = None
        try:
            for emp_key, entry in by_emp.items():
                if not entry["new"]:
                    continue
                if summary["sent"] + summary["failed"] >= limit:
                    summary["pending"] += len(entry["new"])
                    continue
                emp, sub = entry["emp"], subs[emp_key]
                intended = (sub.email or emp.get("email") or "").strip()
                name = f"{(emp.get('prenom') or '').strip()} {(emp.get('nom') or '').strip()}".strip()
                # Un mail par employé et par jour concerné (en pratique : un seul).
                for day in sorted({t.date() for t in entry["new"]}):
                    new = [t for t in entry["new"] if t.date() == day]
                    ctx = context_for(emp, new, sorted(entry["days"][day]), s.company)
                    if test_mode:
                        to, test_for = test_to, (intended, name)
                        reason = "" if to else "mode test sans adresse de test"
                    else:
                        to, test_for = ([intended] if valid_email(intended) else []), None
                        reason = "" if to else "aucune adresse e-mail valide"
                    status, error = "skipped", reason
                    if to:
                        try:
                            if sender is None and client is None:
                                client = open_smtp(s)
                            msg = build_message(s, ctx, to, test_for)
                            (sender or client.send_message)(msg)
                            status, error = "sent", ""
                        except Exception as exc:  # noqa: BLE001
                            status, error = "failed", f"{exc.__class__.__name__}: {exc}"[:500]
                            if client is not None:
                                _quit(client)
                                client = None
                    summary[{"sent": "sent", "failed": "failed", "skipped": "skipped"}[status]] += 1
                    for t in new:
                        prev = done.get((emp_key, t))
                        if prev is None:
                            prev = MailLog(emp_key=emp_key, punch_at=t, attempts=0)
                            db.add(prev)
                            done[(emp_key, t)] = prev
                        prev.matricule, prev.name, prev.mode = emp.get("matricule") or "", name, s.mode
                        prev.recipient, prev.intended = ", ".join(to), intended
                        prev.status, prev.error = status, error
                        prev.attempts = (prev.attempts or 0) + 1
                        prev.created_at = utcnow()
                    db.commit()
        finally:
            if client is not None:
                _quit(client)
        s.last_run_at = utcnow()
        s.last_run_summary = (f"{summary['sent']} envoyé(s), {summary['failed']} en échec, {summary['skipped']} ignoré(s)"
                              + (f", {summary['pending']} en attente (limite par passage)" if summary["pending"] else "")
                              + (" — mode test" if test_mode else " — production"))
        db.commit()
        if summary["sent"] or summary["failed"]:
            write_log("WARNING" if summary["failed"] else "INFO", f"Mails de badge : {s.last_run_summary}.")
        return summary
