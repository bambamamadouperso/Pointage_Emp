"""Résumés des pointages envoyés par e-mail aux responsables : le point de la veille (quotidien) ou de la semaine
passée (hebdomadaire) sur leur équipe (collaborateurs directs N-1, ou toute leur hiérarchie), avec des constats.

Garde-fous (comme les mails de badge) :
- serveur SMTP et mode test / production des « Mails de badge » : en mode test, tout part vers les adresses de test ;
- un seul envoi par responsable, type de résumé et période (journal digest_log), 3 tentatives au plus ;
- rien n'est envoyé pour une période où personne de l'équipe n'était attendu (option).
"""
import html
import logging
import threading
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, text

from . import pointage, rapports
from .config import settings
from .database import SessionLocal
from .joblog import write_log
from .models import DIGEST_KINDS, DigestLog, DigestSettings, DigestSubscriber, utcnow

logger = logging.getLogger("digests")
MAX_ATTEMPTS = 3
WEEKDAYS = {1: "lundi", 2: "mardi", 3: "mercredi", 4: "jeudi", 5: "vendredi", 6: "samedi", 7: "dimanche"}
_MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre",
         "décembre"]
# Couleurs des statuts dans les mails (styles en ligne : les clients de messagerie ignorent les feuilles de style).
_PILLS = {
    "st-ok": ("#d1fae5", "#047857"), "st-late": ("#ffedd5", "#c2410c"), "st-abs": ("#ffe4e6", "#be123c"),
    "st-inc": ("#e2e8f0", "#475569"), "st-off": ("#e0f2fe", "#0369a1"), "st-leave": ("#e4f5d3", "#3f6212"),
    "st-remote": ("#cdeee7", "#0f6b5f"), "st-field": ("#dbeafe", "#1d4ed8"), "st-sick": ("#ece8e5", "#57534e"),
    "st-train": ("#ede9fe", "#6d28d9"), "st-mission": ("#f3e8ff", "#7e22ce"),
}
_TONES = {"bad": "#e11d48", "warn": "#f97316", "ok": "#10b981", "info": "#3b82f6"}
_ORDER = {"ABSENT": 0, "RETARD": 1, "INCOMPLET": 2}


# --------------------------------------------------------------------------- réglages et périodes


def get_settings(db) -> DigestSettings:
    row = db.scalars(select(DigestSettings).order_by(DigestSettings.id)).first()
    if row is None:
        row = DigestSettings()
        db.add(row)
        db.commit()
    return row


def local_now() -> datetime:
    try:
        tz = ZoneInfo(settings.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now()
    return datetime.now(tz).replace(tzinfo=None)


def period(kind: str, today: date) -> tuple[date, date]:
    """Quotidien : la veille ; hebdomadaire : la semaine passée, du lundi au dimanche."""
    if kind == "quotidien":
        day = today - timedelta(days=1)
        return day, day
    monday = today - timedelta(days=today.isoweekday() - 1)
    return monday - timedelta(days=7), monday - timedelta(days=1)


def _clock(value: str) -> time:
    try:
        h, m = (int(x) for x in (value or "07:30").split(":"))
        return time(h, m)
    except ValueError:
        return time(7, 30)


def due_kinds(s: DigestSettings, now: datetime) -> list[str]:
    """Résumés dont l'heure d'envoi est passée (le journal empêche tout second envoi pour la même période)."""
    out = []
    if s.daily_enabled and now.time() >= _clock(s.daily_time):
        out.append("quotidien")
    if s.weekly_enabled:
        day = now.isoweekday()
        if day > s.weekly_day or (day == s.weekly_day and now.time() >= _clock(s.weekly_time)):
            out.append("hebdomadaire")
    return out


def wants(subscriber: DigestSubscriber, kind: str) -> bool:
    return subscriber.frequency in (kind, "les_deux")


def french_day(d: date) -> str:
    return f"{WEEKDAYS[d.isoweekday()]} {d.day} {_MOIS[d.month - 1]} {d.year}"


def period_label(kind: str, du: date, au: date) -> str:
    if kind == "quotidien":
        return french_day(du)
    return f"semaine du {du.day} {_MOIS[du.month - 1]} au {au.day} {_MOIS[au.month - 1]} {au.year}"


# --------------------------------------------------------------------------- contenu


def collect(engine, m: pointage.Mapping, manager_key: str, kind: str, du: date, au: date, scope: str = "directs") -> dict:
    """Indicateurs de l'équipe du responsable (sans lui-même) sur la période, avec la période précédente."""
    f = pointage.Filters(du=du, au=au, team=manager_key, directs=scope != "equipe", exclude=manager_key,
                         population="liste")
    report = rapports.build(engine, m, f, sort="retards", compare=True)
    rows = []
    if kind == "quotidien":
        rows = list(pointage.daily(engine, m, replace(f, sort="nom"), size=500)["rows"])
        rows.sort(key=lambda r: (_ORDER.get(r["statut"], 9), (r["nom"] or "").lower()))
    objectif = pointage.params_at(engine, m, du)["objectif_duree"].split(":")
    return {"kind": kind, "du": du, "au": au, "kpi": report["kpi"], "precedent": report.get("precedent"),
            "employes": report["employes"], "alertes": report["alertes"], "rows": rows,
            "objectif_min": int(objectif[0]) * 60 + int(objectif[1])}


def _name(e) -> str:
    return f"{e['nom'] or ''} {e['prenom'] or ''}".strip() or e["matricule"]


def _names(items: list[str], limit: int = 5) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" et {len(items) - limit} autre(s)" if len(items) > limit else "")


def _fr(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def _delta(now: Optional[float], before: Optional[float]) -> Optional[float]:
    return None if now is None or before is None else now - before


def insights(data: dict) -> list[tuple[str, str]]:
    """Constats rédigés : (ton, phrase) avec ton = bad, warn, ok ou info."""
    k, prev, out = data["kpi"], data.get("precedent") or {}, []
    daily = data["kind"] == "quotidien"
    if not k.get("attendus"):
        return [("info", "Personne de l'équipe n'était attendu sur cette période (jours non ouvrés, repos ou congés).")]
    presence, delta = k.get("taux_presence"), _delta(k.get("taux_presence"), prev.get("taux_presence"))
    if presence is not None:
        trend = "" if delta is None or abs(delta) < 0.5 else \
            f" ({'+' if delta > 0 else '−'}{_fr(abs(delta))} pt par rapport à la période précédente)"
        tone = "ok" if presence >= 95 else "warn" if presence >= 85 else "bad"
        out.append((tone, f"Taux de présence de {presence:.0f} %{trend}."))
    if daily:
        absents = [_name(r) for r in data["rows"] if r["statut"] == "ABSENT"]
        late = [(r, float(r["retard_min"] or 0)) for r in data["rows"] if r["statut"] == "RETARD"]
        inc = [_name(r) for r in data["rows"] if r["statut"] == "INCOMPLET"]
        short = [_name(r) for r in data["rows"] if r["statut"] in ("A_L_HEURE", "RETARD")
                 and r["duree_validee_min"] is not None and float(r["duree_validee_min"]) < data["objectif_min"]]
        if absents:
            out.append(("bad", f"{len(absents)} absence(s) sans justificatif : {_names(absents)}."))
        if late:
            late.sort(key=lambda x: -x[1])
            out.append(("warn", f"{len(late)} retard(s) : " + _names([f"{_name(r)} ({int(mn)} min)" for r, mn in late]) + "."))
        if inc:
            out.append(("info", f"Badge oublié (un seul pointage) : {_names(inc)}."))
        if short:
            h = data["objectif_min"]
            out.append(("warn", f"Sous l'objectif de {h // 60}h{h % 60:02d} de durée validée : {_names(short)}."))
    else:
        emps = data["employes"]
        absents = sorted((e for e in emps if e["absences"]), key=lambda e: -e["absences"])
        late = sorted((e for e in emps if e["retards"]), key=lambda e: (-e["retards"], -float(e["retard_min_total"])))
        inc = [e for e in emps if e["incomplets"]]
        if absents:
            out.append(("bad", f"{k['absences']} jour(s) d'absence sans justificatif : "
                               + _names([f"{_name(e)} ({e['absences']} j)" for e in absents]) + "."))
        if late:
            out.append(("warn", f"{k['retards']} retard(s) cumulant {pointage.hhmm(timedelta(minutes=float(k['retard_min_total'])))} : "
                                + _names([f"{_name(e)} ({e['retards']}×)" for e in late], 3) + "."))
        if inc:
            out.append(("info", "Badges oubliés : " + _names([f"{_name(e)} ({e['incomplets']} j)" for e in inc]) + "."))
        for a in data["alertes"]:
            if a["gravite"] >= 2 and a["motif"] == "Absences en début ou fin de semaine":
                out.append(("warn", f"{a['nom']} : {a['detail']}."))
        best = [e for e in emps if e["a_l_heure"] >= 3 and not e["retards"] and not e["absences"] and not e["incomplets"]]
        if best and (absents or late):
            out.append(("ok", "Assiduité exemplaire : " + _names([_name(e) for e in best], 4) + "."))
    punct, pdelta = k.get("taux_ponctualite"), _delta(k.get("taux_ponctualite"), prev.get("taux_ponctualite"))
    if punct is not None and (punct < 100 or pdelta):
        trend = "" if pdelta is None or abs(pdelta) < 0.5 else f" ({'+' if pdelta > 0 else '−'}{_fr(abs(pdelta))} pt)"
        out.append(("ok" if punct >= 95 else "warn", f"Ponctualité de {punct:.0f} %{trend}."))
    others = [(k.get("conges"), "en congé"), (k.get("maladies"), "en arrêt maladie"), (k.get("missions"), "en mission"),
              (k.get("teletravail"), "en télétravail"), (k.get("terrain"), "sur le terrain")]
    others = [f"{n} {label}" for n, label in others if n]
    if others:
        out.append(("info", ("Autres situations (journées) : " if not daily else "Autres situations : ") + ", ".join(others) + "."))
    if not k.get("absences") and not k.get("retards") and not k.get("incomplets"):
        out.append(("ok", "Aucune absence, aucun retard, aucun oubli de badge : bravo à l'équipe !"))
    return out


def _pill(code: str) -> str:
    label, cls = pointage.STATUTS.get(code, (code, "st-inc"))
    bg, fg = _PILLS.get(cls, ("#e2e8f0", "#475569"))
    return (f'<span style="display:inline-block;padding:2px 9px;border-radius:10px;background:{bg};color:{fg};'
            f'font-size:12px;font-weight:600;white-space:nowrap;">{html.escape(label)}</span>')


def _dur(minutes, objectif: int) -> str:
    if minutes is None:
        return '<span style="color:#9aa6b2;">—</span>'
    ok = float(minutes) >= objectif
    value = pointage.hhmm(timedelta(minutes=float(minutes)))
    return (f'<span style="display:inline-block;padding:1px 7px;border-radius:6px;font-weight:600;'
            f'background:{"#d1fae5" if ok else "#ffe4e6"};color:{"#047857" if ok else "#be123c"};">{value}</span>')


def _tile(label: str, value: str, sub: str, color: str) -> str:
    return (f'<td width="25%" style="padding:6px;"><div style="border:1px solid #e6ecf3;border-radius:12px;padding:12px 12px 10px;'
            f'background:#ffffff;"><div style="font-size:11px;letter-spacing:.6px;text-transform:uppercase;color:#7a8898;">'
            f'<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:{color};margin-right:5px;"></span>'
            f'{html.escape(label)}</div><div style="font-size:24px;font-weight:700;color:#0b1a2b;margin-top:4px;">{value}</div>'
            f'<div style="font-size:12px;color:#7a8898;margin-top:2px;">{sub}</div></div></td>')


def _pct(value) -> str:
    return "—" if value is None else f"{value:.0f} %"


def _trend(now, before, good_up: bool = True) -> str:
    d = _delta(now, before)
    if d is None or abs(d) < 0.5:
        return "vs période précédente : stable" if d is not None else "&nbsp;"
    better = (d > 0) == good_up
    return (f'<span style="color:{"#047857" if better else "#be123c"};font-weight:600;">'
            f'{"▲" if d > 0 else "▼"} {_fr(abs(d))} pt</span> vs précédente')


def _week_row(e: dict, td: str) -> str:
    late = ""
    if e["retards"]:
        late = f' <span style="color:#9aa6b2;">({pointage.hhmm(timedelta(minutes=float(e["retard_min_total"])))})</span>'
    absences = f'<strong style="color:#be123c;">{e["absences"]}</strong>' if e["absences"] else "·"
    hours = pointage.hhmm(timedelta(hours=float(e["heures_validees"] or 0)))
    return (f"<tr><td {td}><strong>{html.escape(_name(e))}</strong></td><td {td}>{_pct(e['taux_presence'])}</td>"
            f"<td {td}>{e['retards'] or '·'}{late}</td><td {td}>{absences}</td><td {td}>{e['incomplets'] or '·'}</td>"
            f"<td {td}>{hours}</td></tr>")


def render(data: dict, manager_name: str, company: str = "", app_url: str = "") -> tuple[str, str, str]:
    """(objet, HTML, texte) du résumé."""
    k, prev = data["kpi"], data.get("precedent") or {}
    daily = data["kind"] == "quotidien"
    label = period_label(data["kind"], data["du"], data["au"])
    title = f"{'Point du jour' if daily else 'Point de la semaine'} — équipe de {manager_name}"
    subject = f"{'Pointages' if daily else 'Bilan hebdomadaire des pointages'} de votre équipe — {label}"
    tiles = "".join([
        _tile("Présence", _pct(k.get("taux_presence")), _trend(k.get("taux_presence"), prev.get("taux_presence")), "#10b981"),
        _tile("Ponctualité", _pct(k.get("taux_ponctualite")),
              _trend(k.get("taux_ponctualite"), prev.get("taux_ponctualite")), "#3b82f6"),
        _tile("Absences", str(k.get("absences") or 0), "sans justificatif", "#e11d48"),
        _tile("Retards", str(k.get("retards") or 0),
              f"{pointage.hhmm(timedelta(minutes=float(k.get('retard_min_total') or 0)))} cumulées", "#f97316"),
    ])
    notes = "".join(
        f'<tr><td valign="top" style="padding:5px 10px 5px 0;width:10px;"><span style="display:inline-block;width:8px;height:8px;'
        f'border-radius:50%;background:{_TONES[tone]};margin-top:6px;"></span></td>'
        f'<td style="padding:5px 0;font-size:14px;line-height:1.5;color:#2c3a4b;">{html.escape(sentence)}</td></tr>'
        for tone, sentence in insights(data))
    th = 'style="padding:8px 6px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:#7a8898;border-bottom:1px solid #e6ecf3;"'
    td = 'style="padding:8px 6px;font-size:13px;color:#2c3a4b;border-bottom:1px solid #f0f3f7;"'
    if daily:
        head = f"<tr><th {th}>Collaborateur</th><th {th}>Statut</th><th {th}>Arrivée</th><th {th}>Départ</th><th {th}>Durée validée</th></tr>"
        body = "".join(
            f"<tr><td {td}><strong>{html.escape(_name(r))}</strong><br><span style=\"color:#9aa6b2;font-size:11px;\">{html.escape(r['matricule'] or '')}</span></td>"
            f"<td {td}>{_pill(r['statut'])}</td><td {td}>{pointage.hhmm(r['premier_pointage']) if r['premier_pointage'] else '—'}</td>"
            f"<td {td}>{pointage.hhmm(r['dernier_pointage']) if r['nb_pointages'] and r['nb_pointages'] > 1 else '—'}</td>"
            f"<td {td}>{_dur(r['duree_validee_min'], data['objectif_min'])}</td></tr>" for r in data["rows"])
    else:
        head = (f"<tr><th {th}>Collaborateur</th><th {th}>Présence</th><th {th}>Retards</th><th {th}>Absences</th>"
                f"<th {th}>Oublis</th><th {th}>Heures validées</th></tr>")
        body = "".join(_week_row(e, td) for e in sorted(data["employes"], key=lambda e: _name(e).lower()))
    count = len(data["rows"]) if daily else len(data["employes"])
    link = ""
    if app_url:
        url = f"{app_url.rstrip('/')}/{'suivi' if daily else 'rapports'}?du={data['du'].isoformat()}&au={data['au'].isoformat()}"
        link = (f'<p style="margin:22px 0 0;"><a href="{html.escape(url)}" style="display:inline-block;padding:11px 20px;'
                f'background:#1b4f8a;color:#ffffff;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;">'
                f'{"Ouvrir le suivi" if daily else "Ouvrir les rapports"}</a></p>')
    body_html = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title></head>
<body style="margin:0;padding:0;background:#f2f5f9;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f2f5f9;"><tr><td align="center" style="padding:28px 10px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:680px;font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
<tr><td style="background:#ffffff;border:1px solid #e3e9f0;border-radius:16px;overflow:hidden;">
  <div style="height:4px;background:#0e7c6e;background-image:linear-gradient(90deg,#1b4f8a,#0e7c6e);"></div>
  <div style="padding:26px 28px 6px;">
    <p style="margin:0 0 6px;font-size:12px;font-weight:600;letter-spacing:1.2px;text-transform:uppercase;color:#0e7c6e;">{'Résumé quotidien' if daily else 'Résumé hebdomadaire'}</p>
    <h1 style="margin:0;font-size:22px;line-height:1.3;color:#0b1a2b;">{html.escape(title)}</h1>
    <p style="margin:6px 0 0;font-size:14px;color:#46566a;">{html.escape(label[0].upper() + label[1:])} · {count} collaborateur(s) · {k.get('attendus') or 0} journée(s) attendue(s)</p>
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="padding:10px 22px 0;"><tr>{tiles}</tr></table>
  <div style="padding:14px 28px 4px;">
    <h2 style="margin:8px 0 6px;font-size:15px;color:#0b1a2b;">À retenir</h2>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{notes}</table>
  </div>
  <div style="padding:10px 28px 26px;">
    <h2 style="margin:14px 0 8px;font-size:15px;color:#0b1a2b;">{'Détail de la journée' if daily else 'Détail par collaborateur'}</h2>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">{head}{body}</table>
    {link}
  </div>
</td></tr>
<tr><td style="padding:16px 8px 0;text-align:center;font-size:12px;color:#a3afbc;">Message automatique envoyé aux responsables abonnés — merci de ne pas y répondre.{(' © ' + html.escape(company)) if company else ''}</td></tr>
</table></td></tr></table></body></html>"""
    lines = [title, label, "", *[f"- {s}" for _, s in insights(data)]]
    return subject, body_html, "\n".join(lines)


# --------------------------------------------------------------------------- envoi


def manager_directory(engine, m: pointage.Mapping) -> dict[str, dict]:
    """Responsables (au moins un collaborateur) : {clé: {nom, matricule, email, n}}."""
    S = pointage.qi(m.objs)
    out = {}
    with engine.connect() as c:
        for key, mat, nom, prenom, email, n in c.execute(text(
                f"SELECT r.emp_key, r.matricule, r.nom, r.prenom, r.email, count(*) FROM {S}.v_pointage_employes e "
                f"JOIN {S}.v_pointage_employes r ON r.emp_key = e.responsable_key WHERE e.actif "
                f"GROUP BY 1, 2, 3, 4, 5 ORDER BY 3, 4")):
            out[key] = {"nom": " ".join(x for x in (nom, prenom) if x) or mat, "matricule": mat, "email": email or "",
                        "n": n}
    return out


def _context():
    from .models import PointageConfig
    from .sync import make_engine

    with SessionLocal() as db:
        cfg = db.scalars(select(PointageConfig).order_by(PointageConfig.id)).first()
        if cfg is None or cfg.conn is None or cfg.installed_at is None:
            return None, None
        m = pointage.Mapping.from_json(cfg.data)
        return make_engine(cfg.conn, **pointage.WEB_LIMITS), m


def send_one(db, engine, m, sub: DigestSubscriber, kind: str, du: date, au: date, s: DigestSettings, mail_settings,
             client, directory: dict, force_to: Optional[str] = None) -> tuple[str, str]:
    """Construit et envoie un résumé ; renvoie (statut, détail)."""
    from .mails import compose, valid_email

    info = directory.get(sub.manager_key, {})
    name = sub.name or info.get("nom") or sub.manager_key
    email = (sub.email or info.get("email") or "").strip()
    data = collect(engine, m, sub.manager_key, kind, du, au, s.scope)
    if s.skip_empty and not data["kpi"].get("attendus") and not force_to:
        return "skipped", "personne n'était attendu"
    subject, body, text_body = render(data, name, mail_settings.company, s.app_url)
    if force_to:
        msg = compose(mail_settings.__class__(**{**_copy(mail_settings), "mode": "test", "test_recipients": force_to}),
                      subject, body, text_body, email or "(aucune adresse)", name)
    else:
        if not valid_email(email):
            return "skipped", "aucune adresse e-mail"
        msg = compose(mail_settings, subject, body, text_body, email, name)
    if msg is None:
        return "skipped", "mode test sans adresse de test"
    client.send_message(msg)
    return "sent", msg["To"]


def _copy(s) -> dict:
    return {c.name: getattr(s, c.name) for c in s.__table__.columns if c.name != "id"}


_lock = threading.Lock()


def run_due(now: Optional[datetime] = None) -> dict:
    """Envoie les résumés dus (appelé toutes les 10 minutes). Ne lève jamais d'exception."""
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        return _run_due(now or local_now())
    except Exception as exc:  # noqa: BLE001
        logger.exception("Résumés par mail impossibles")
        write_log("ERROR", f"Résumés par mail : traitement impossible ({exc.__class__.__name__}: {exc}).")
        return {"status": "error", "error": str(exc)}
    finally:
        _lock.release()


def _run_due(now: datetime) -> dict:
    from .mails import _quit, get_settings as mail_settings_of, open_smtp

    with SessionLocal() as db:
        s = get_settings(db)
        kinds = due_kinds(s, now)
        if not kinds:
            return {"status": "idle"}
        ms = mail_settings_of(db)
        if not ms.smtp_host or not ms.from_email:
            return {"status": "unconfigured"}
        todo = []
        for kind in kinds:
            du, au = period(kind, now.date())
            done = {(l.manager_key) for l in db.scalars(select(DigestLog).where(
                DigestLog.kind == kind, DigestLog.period_start == datetime.combine(du, time()),
                (DigestLog.status != "failed") | (DigestLog.attempts >= MAX_ATTEMPTS)))}
            todo += [(sub, kind, du, au) for sub in db.scalars(select(DigestSubscriber)).all()
                     if wants(sub, kind) and sub.manager_key not in done]
        if not todo:
            return {"status": "idle"}
        engine, m = _context()
        if engine is None:
            return {"status": "unconfigured"}
        counts = {"sent": 0, "skipped": 0, "failed": 0}
        client = None
        try:
            directory = manager_directory(engine, m)
            for sub, kind, du, au in todo:
                log = db.scalars(select(DigestLog).where(
                    DigestLog.kind == kind, DigestLog.period_start == datetime.combine(du, time()),
                    DigestLog.manager_key == sub.manager_key)).first()
                if log is None:
                    log = DigestLog(kind=kind, period_start=datetime.combine(du, time()),
                                    period_end=datetime.combine(au, time()), manager_key=sub.manager_key, attempts=0)
                    db.add(log)
                log.attempts += 1
                log.name, log.mode = sub.name, ms.mode
                log.intended = sub.email or directory.get(sub.manager_key, {}).get("email", "")
                try:
                    if client is None:
                        client = open_smtp(ms)
                    status, detail = send_one(db, engine, m, sub, kind, du, au, s, ms, client, directory)
                    log.status, log.recipient, log.error = status, detail if status == "sent" else "", \
                        "" if status == "sent" else detail
                except Exception as exc:  # noqa: BLE001
                    log.status, log.error = "failed", f"{exc.__class__.__name__}: {exc}"[:500]
                    if client is not None:
                        _quit(client)
                        client = None
                counts[log.status] = counts.get(log.status, 0) + 1
                db.commit()
        finally:
            if client is not None:
                _quit(client)
            engine.dispose()
        s = get_settings(db)
        s.last_run_at = utcnow()
        s.last_run_summary = (f"{counts['sent']} envoyé(s), {counts['skipped']} ignoré(s), {counts['failed']} échec(s)"
                              + ("" if ms.mode == "production" else " — mode test"))
        db.commit()
        if counts["sent"] or counts["failed"]:
            write_log("INFO" if not counts["failed"] else "WARNING", f"Résumés par mail : {s.last_run_summary}.")
        return {"status": "done", **counts}


def preview(manager_key: str, kind: str, today: Optional[date] = None) -> tuple[str, str]:
    """(objet, HTML) du résumé d'un responsable, sans envoi."""
    from .mails import get_settings as mail_settings_of

    engine, m = _context()
    if engine is None:
        raise ValueError("Le module de pointage n'est pas configuré.")
    try:
        with SessionLocal() as db:
            s, ms = get_settings(db), mail_settings_of(db)
            info = manager_directory(engine, m).get(manager_key, {})
            du, au = period(kind, today or local_now().date())
            data = collect(engine, m, manager_key, kind, du, au, s.scope)
            subject, body, _ = render(data, info.get("nom", manager_key), ms.company, s.app_url)
            return subject, body
    finally:
        engine.dispose()


def send_test(manager_key: str, kind: str, to: str) -> str:
    """Envoie le résumé d'un responsable à une adresse de test (bandeau « MODE TEST »)."""
    from .mails import _quit, get_settings as mail_settings_of, open_smtp

    engine, m = _context()
    if engine is None:
        raise ValueError("Le module de pointage n'est pas configuré.")
    try:
        with SessionLocal() as db:
            s, ms = get_settings(db), mail_settings_of(db)
            directory = manager_directory(engine, m)
            info = directory.get(manager_key, {})
            sub = db.scalars(select(DigestSubscriber).where(DigestSubscriber.manager_key == manager_key)).first() \
                or DigestSubscriber(manager_key=manager_key, name=info.get("nom", ""), email="")
            du, au = period(kind, local_now().date())
            client = open_smtp(ms)
            try:
                status, detail = send_one(db, engine, m, sub, kind, du, au, s, ms, client, directory, force_to=to)
            finally:
                _quit(client)
            return detail if status == "sent" else status
    finally:
        engine.dispose()
