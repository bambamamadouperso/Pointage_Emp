"""Surveillance des sites de pointage : chaque jour, à l'heure paramétrée, contrôle que chaque site (lecteur,
pointeuse… : colonne « site du pointage » de la source des pointages) a remonté au moins un pointage depuis le début
de la journée. Sinon, un mail part vers la liste de destinataires paramétrée (panne de lecteur, coupure réseau,
synchronisation arrêtée…).

Garde-fous : un seul contrôle par jour (journal site_alert_log) ; jours de contrôle et jours fériés paramétrables ;
serveur d'envoi et mode test / production communs à tous les mails de l'application.
"""
import html
import json
import logging
import re
import threading
from datetime import date, datetime, time, timedelta
from typing import Optional

from sqlalchemy import select, text

from . import pointage
from .database import SessionLocal
from .digests import local_now
from .joblog import write_log
from .models import PointageConfig, SiteAlertLog, SiteAlertSettings, utcnow

logger = logging.getLogger("sites")

_TIME = re.compile(r"^([01]?\d|2[0-3])[:hH]([0-5]\d)$")
WEEKDAYS = [(1, "Lun"), (2, "Mar"), (3, "Mer"), (4, "Jeu"), (5, "Ven"), (6, "Sam"), (7, "Dim")]
DEFAULT_SUBJECT = "Pointages : aucun pointage reçu de {nb} site(s) à {heure}"
STATUS = {"ok": "Tous les sites ont pointé", "sent": "Alerte envoyée", "failed": "Échec de l'envoi",
          "no_recipient": "Aucun destinataire", "no_site": "Aucun site à contrôler"}


def get_settings(db) -> SiteAlertSettings:
    row = db.scalars(select(SiteAlertSettings).order_by(SiteAlertSettings.id)).first()
    if row is None:
        row = SiteAlertSettings()
        db.add(row)
        db.commit()
    return row


def parse_time(value: str) -> Optional[str]:
    match = _TIME.match((value or "").strip())
    return f"{int(match.group(1)):02d}:{match.group(2)}" if match else None


def check_clock(s: SiteAlertSettings) -> time:
    value = parse_time(s.check_time or "") or "09:00"
    return time(int(value[:2]), int(value[3:]))


def weekdays(s: SiteAlertSettings) -> list[int]:
    return [int(d) for d in (s.weekdays or "").split(",") if d.strip().isdigit()]


def monitored(s: SiteAlertSettings) -> list[str]:
    return pointage.multi((s.sites or "").split(","))


def _mapping(db) -> tuple[Optional[PointageConfig], Optional[pointage.Mapping]]:
    cfg = db.scalars(select(PointageConfig).order_by(PointageConfig.id)).first()
    if cfg is None or cfg.conn is None or cfg.installed_at is None:
        return cfg, None
    return cfg, pointage.Mapping.from_json(cfg.data)


# --------------------------------------------------------------------------- situation des sites


def site_activity(engine, m: pointage.Mapping, now: datetime, lookback_days: int = 30) -> list[dict]:
    """Sites ayant pointé sur les N derniers jours : dernier pointage et nombre de pointages aujourd'hui (jusqu'à
    maintenant)."""
    if not m.punch_site_col:
        return []
    start = datetime.combine(now.date(), time())
    with engine.connect() as c:
        rows = c.execute(text(
            f"SELECT site, max(horodatage) AS dernier, count(*) FILTER (WHERE horodatage >= :today) AS aujourdhui "
            f"FROM {pointage.qi(m.objs)}.v_pointage_brut "
            f"WHERE horodatage >= :since AND horodatage <= :now AND site IS NOT NULL GROUP BY site ORDER BY site"),
            {"today": start, "since": start - timedelta(days=max(lookback_days, 1)), "now": now}).mappings().all()
    return [dict(r) for r in rows]


def evaluate(activity: list[dict], wanted: list[str]) -> tuple[list[dict], list[dict]]:
    """(sites contrôlés, sites sans pointage aujourd'hui). Site surveillé jamais vu : sans pointage, dernier inconnu."""
    by_name = {a["site"].lower(): a for a in activity}
    if wanted:
        checked = [by_name.get(w.lower(), {"site": w, "dernier": None, "aujourdhui": 0}) for w in wanted]
    else:
        checked = activity
    return checked, [c for c in checked if not c["aujourdhui"]]


def is_holiday(engine, m: pointage.Mapping, day: date) -> bool:
    try:
        with engine.connect() as c:
            return bool(c.execute(text(f"SELECT EXISTS (SELECT 1 FROM {pointage.qi(m.objs)}.pointage_jours_feries "
                                       f"WHERE jour = :d)"), {"d": day}).scalar())
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- mail


def _hm(value: Optional[datetime], today: date) -> str:
    if value is None:
        return "aucun pointage sur la période observée"
    return f"aujourd'hui à {value:%H}h{value:%M}" if value.date() == today else f"le {value:%d/%m/%Y} à {value:%H}h{value:%M}"


def build(s: SiteAlertSettings, silent: list[dict], checked: list[dict], now: datetime, company: str = "") -> tuple:
    """(objet, HTML, texte) de l'alerte."""
    heure = f"{now:%H}h{now:%M}"
    subject = (s.subject or DEFAULT_SUBJECT).replace("{nb}", str(len(silent))).replace("{heure}", heure) \
        .replace("{date}", f"{now:%d/%m/%Y}")
    everything = len(silent) == len(checked)
    intro = (f"Au {now:%d/%m/%Y} à {heure}, aucun pointage n'a été reçu depuis le début de la journée pour "
             f"{len(silent)} site(s) sur {len(checked)} surveillé(s).")
    hint = ("Aucun site n'a remonté de pointage : vérifiez d'abord la synchronisation des pointages (serveur, réseau)."
            if everything else "Vérifiez le lecteur de badges, son alimentation et sa connexion réseau.")
    rows = "".join(
        f'<tr><td style="padding:9px 10px;border-bottom:1px solid #eef2f6;font-weight:600;color:#0b1a2b;">{html.escape(x["site"])}</td>'
        f'<td style="padding:9px 10px;border-bottom:1px solid #eef2f6;color:#be123c;">{html.escape(_hm(x["dernier"], now.date()))}</td></tr>'
        for x in silent)
    ok = [x for x in checked if x["aujourdhui"]]
    ok_line = (f'<p style="margin:14px 0 0;font-size:13px;color:#46566a;">Sites qui ont pointé : '
               + html.escape(", ".join(f"{x['site']} ({x['aujourdhui']})" for x in ok)) + "</p>") if ok else ""
    extra = f'<p style="margin:14px 0 0;font-size:14px;line-height:1.6;color:#2c3a4b;">{html.escape(s.message)}</p>' if s.message else ""
    body = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>{html.escape(subject)}</title></head>
<body style="margin:0;padding:0;background:#f2f5f9;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f2f5f9;">
<tr><td align="center" style="padding:32px 12px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;">
<tr><td style="background:#ffffff;border:1px solid #e3e9f0;border-radius:16px;padding:30px 34px;font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
<p style="margin:0 0 6px;font-size:12px;font-weight:600;letter-spacing:1.2px;text-transform:uppercase;color:#be123c;">Surveillance des sites</p>
<h1 style="margin:0;font-size:22px;line-height:1.3;color:#0b1a2b;">{html.escape(subject)}</h1>
<p style="margin:12px 0 16px;font-size:15px;line-height:1.6;color:#46566a;">{html.escape(intro)}</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
<tr><th align="left" style="padding:8px 10px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:#7a8898;border-bottom:1px solid #e6ecf3;">Site</th>
<th align="left" style="padding:8px 10px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:#7a8898;border-bottom:1px solid #e6ecf3;">Dernier pointage reçu</th></tr>
{rows}</table>
<p style="margin:16px 0 0;font-size:14px;line-height:1.6;color:#2c3a4b;">{html.escape(hint)}</p>{ok_line}{extra}
</td></tr>
<tr><td style="padding:18px 8px 0;text-align:center;font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:12px;color:#a3afbc;">
Message automatique — contrôle quotidien des sites de pointage.{(' © ' + html.escape(company)) if company else ''}</td></tr>
</table></td></tr></table></body></html>"""
    text_body = "\n".join([subject, "", intro, "", *[f"- {x['site']} : dernier pointage {_hm(x['dernier'], now.date())}"
                                                    for x in silent], "", hint] + ([s.message] if s.message else []))
    return subject, body, text_body


def _send(ms, recipients: list[str], subject: str, body: str, text_body: str) -> str:
    """Envoie l'alerte ; renvoie le mode (test / production). Lève une exception en cas d'échec."""
    from .arrets import build_messages, send_messages

    messages, production = build_messages(ms, [(a, a) for a in recipients], subject, body, text_body,
                                          "Surveillance des sites")
    if messages is None:
        raise ValueError("mode test sans adresse de test (Serveur d'envoi des e-mails)")
    send_messages(ms, messages)
    return "production" if production else "test"


# --------------------------------------------------------------------------- contrôle quotidien

_lock = threading.Lock()


def run_due(now: Optional[datetime] = None, force: bool = False) -> dict:
    """Contrôle du jour s'il est dû (appelé toutes les 5 minutes ; `force` : bouton « Contrôler maintenant »).
    Ne lève jamais d'exception."""
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        return _run(now or local_now(), force)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Surveillance des sites impossible")
        write_log("ERROR", f"Surveillance des sites : contrôle impossible ({exc.__class__.__name__}: {exc}).")
        return {"status": "error", "error": str(exc)}
    finally:
        _lock.release()


def _run(now: datetime, force: bool) -> dict:
    from .mails import get_settings as mail_settings_of, parse_addresses, valid_email
    from .sync import make_engine

    with SessionLocal() as db:
        s = get_settings(db)
        day = datetime.combine(now.date(), time())
        if not force:
            if not s.enabled or now.isoweekday() not in weekdays(s) or now.time() < check_clock(s):
                return {"status": "idle"}
            if db.scalars(select(SiteAlertLog).where(SiteAlertLog.jour == day)).first() is not None:
                return {"status": "idle"}
        cfg, m = _mapping(db)
        if m is None or not m.punch_site_col:
            return {"status": "unconfigured", "reason": "colonne « site du pointage » non configurée"}
        ms = mail_settings_of(db)
        engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
        try:
            if s.skip_holidays and not force and is_holiday(engine, m, now.date()):
                return {"status": "idle", "reason": "jour férié"}
            checked, silent = evaluate(site_activity(engine, m, now, s.lookback_days or 30), monitored(s))
        finally:
            engine.dispose()
        recipients = [a for a in parse_addresses(s.recipients or "") if valid_email(a)]
        log = db.scalars(select(SiteAlertLog).where(SiteAlertLog.jour == day)).first() or SiteAlertLog(jour=day)
        log.checked_at, log.checked, log.recipients = utcnow(), len(checked), ", ".join(recipients)
        log.silent = json.dumps([x["site"] for x in silent], ensure_ascii=False)
        log.mode, log.error = ms.mode or "test", ""
        if not checked:
            log.status = "no_site"
        elif not silent:
            log.status = "ok"
        elif not recipients:
            log.status = "no_recipient"
        elif not (ms.smtp_host and ms.from_email):
            log.status, log.error = "failed", "serveur d'envoi des e-mails non configuré"
        else:
            subject, body, text_body = build(s, silent, checked, now, ms.company)
            try:
                log.mode, log.status = _send(ms, recipients, subject, body, text_body), "sent"
            except Exception as exc:  # noqa: BLE001
                log.status, log.error = "failed", f"{exc.__class__.__name__}: {exc}"[:2000]
        db.add(log)
        db.commit()
        names = ", ".join(x["site"] for x in silent) or "aucun"
        write_log("WARNING" if silent else "INFO",
                  f"Surveillance des sites : {len(checked)} site(s) contrôlé(s), sans pointage : {names} "
                  f"({STATUS.get(log.status, log.status).lower()}{', ' + log.error if log.error else ''}).")
        return {"status": log.status, "checked": len(checked), "silent": [x["site"] for x in silent], "error": log.error}


def send_test(to: str, now: Optional[datetime] = None) -> str:
    """Alerte d'essai vers une adresse, avec la situation réelle du moment (ou un exemple si tout va bien)."""
    from .mails import get_settings as mail_settings_of
    from .sync import make_engine

    now = now or local_now()
    with SessionLocal() as db:
        s, ms = get_settings(db), mail_settings_of(db)
        cfg, m = _mapping(db)
        checked, silent = [], []
        if m is not None and m.punch_site_col:
            engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
            try:
                checked, silent = evaluate(site_activity(engine, m, now, s.lookback_days or 30), monitored(s))
            finally:
                engine.dispose()
        if not silent:  # exemple
            silent = [{"site": "Site d'exemple", "dernier": now - timedelta(hours=16), "aujourdhui": 0}]
            checked = checked + silent
        subject, body, text_body = build(s, silent, checked, now, ms.company)
        if not (ms.smtp_host and ms.from_email):
            raise ValueError("serveur d'envoi des e-mails non configuré")
        return _send(ms, [to], f"[ESSAI] {subject}", body, text_body)
