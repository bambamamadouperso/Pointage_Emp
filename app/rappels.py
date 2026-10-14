"""Rappels de pointage envoyés par e-mail aux employés, pour limiter les oublis de badge.

Cinq messages, chacun activable, à heure paramétrable et au texte personnalisable :
- matin, avertissement (arrivée non pointée) : employé attendu aujourd'hui (jour ouvré, ni congé, ni arrêt, ni
  télétravail, ni terrain, ni mission…) qui n'a pas encore badgé, à l'heure indiquée ; agent en horaires postés :
  N minutes après le début de son poste ;
- matin, alerte : toujours aucun pointage à une heure plus tardive (poste : N minutes après son début), avec le
  responsable N+1 en copie si souhaité ;
- soir, avertissement avant le départ : employé toujours « entré » (nombre impair de pointages) à l'heure indiquée,
  ou N minutes avant la fin de son poste : « n'oubliez pas de badger en partant » ;
- soir, départ non pointé : employé qui n'a qu'un seul pointage (son arrivée) à l'heure indiquée ; poste planifié :
  N minutes après la fin de son poste (nuits comprises) ;
- oubli de la veille : le matin, rappel des journées restées incomplètes (un seul pointage) des 3 derniers jours,
  avec copie au responsable si souhaité.

Garde-fous (comme les résumés) : serveur SMTP et mode test / production des « Mails de badge » ; un seul rappel par
employé, type et journée (journal reminder_log), 3 tentatives au plus ; un rappel n'est plus envoyé 3 heures après son
heure prévue (redémarrage de l'application) ; aucun rappel si les pointages ne sont plus synchronisés (sinon des
employés qui ont badgé recevraient un rappel à tort).
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
from .digests import _context, _copy, french_day, local_now
from .joblog import write_log
from .models import (REMINDER_AUDIENCES, REMINDER_KINDS, JobRun, MailSubscriber, ReminderLog, ReminderSettings,
                     SyncJob, TableMapping, utcnow)

logger = logging.getLogger("rappels")
MAX_ATTEMPTS = 3
WINDOW = timedelta(hours=3)  # au-delà, un rappel non envoyé (application arrêtée) est abandonné
LOOKBACK_DAYS = 3            # oubli de la veille : journées incomplètes des 3 derniers jours
_TIME = re.compile(r"^([01]?\d|2[0-3])[:hH]([0-5]\d)$")
PRESENT = ("A_L_HEURE", "RETARD", "INCOMPLET")

# Textes par défaut. Variables : {prenom}, {nom}, {matricule}, {date}, {poste}, {arrivee} (1er pointage), {dernier}
# (dernier pointage) ; {_poste} = « (poste …) » précédé d'une espace, ou rien (textes par défaut seulement).
DEFAULT_TEXTS = {
    "arrivee": {
        "subject": "Rappel : votre arrivée n'est pas encore pointée",
        "title": "Pensez à pointer votre arrivée",
        "body": "Nous n'avons encore enregistré aucun pointage pour vous aujourd'hui, {date}{_poste}.\n\n"
                "Si vous êtes arrivé(e), badgez dès maintenant. Si vous êtes absent(e) (congé, maladie, mission, "
                "télétravail…), prévenez votre responsable pour que votre journée soit correctement enregistrée.",
    },
    "alerte": {
        "subject": "Alerte : aucun pointage enregistré ce matin",
        "title": "Aucun pointage ce matin",
        "body": "Aucun pointage n'est encore enregistré pour vous aujourd'hui, {date}{_poste}.\n\n"
                "Si vous êtes présent(e), badgez immédiatement. Sinon, informez sans attendre votre responsable de "
                "votre absence.",
    },
    "avant_depart": {
        "subject": "N'oubliez pas de badger en partant",
        "title": "N'oubliez pas de badger en partant",
        "body": "Vous avez pointé votre arrivée à {arrivee} aujourd'hui, {date}{_poste}.\n\n"
                "Avant de quitter votre poste, pensez à badger votre départ : sans ce pointage, votre journée restera "
                "incomplète.",
    },
    "depart": {
        "subject": "Rappel : pensez à pointer votre départ",
        "title": "Pensez à pointer votre départ",
        "body": "Votre dernier pointage aujourd'hui date de {dernier}{_poste} : vous êtes toujours enregistré(e) "
                "comme présent(e).\n\nPensez à badger en quittant votre poste, sans quoi votre journée restera "
                "incomplète.",
    },
    "oubli": {
        "subject": "Pointage incomplet le {date}",
        "title": "Un pointage manque sur votre journée",
        "body": "Un seul pointage a été enregistré le {date}, à {arrivee}{_poste} : il manque votre arrivée ou votre "
                "départ.\n\nSignalez l'heure manquante à votre responsable pour régulariser votre journée, et pensez "
                "à badger à chaque entrée et sortie.",
    },
}
VARIABLES = ["prenom", "nom", "matricule", "date", "poste", "arrivee", "dernier"]


# --------------------------------------------------------------------------- réglages


def get_settings(db) -> ReminderSettings:
    row = db.scalars(select(ReminderSettings).order_by(ReminderSettings.id)).first()
    if row is None:
        row = ReminderSettings(arrival_enabled=False, arrival_time="08:15", arrival_delay=15, departure_enabled=False,
                               departure_time="17:45", departure_delay=30, forgot_enabled=False, forgot_time="09:00",
                               forgot_cc_manager=False, alert_enabled=False, alert_time="09:30", alert_delay=60,
                               alert_cc_manager=False, predeparture_enabled=False, predeparture_time="16:45",
                               predeparture_before=15, texts="{}", services="[]", sites="[]", directions="[]",
                               excluded="", max_lag_minutes=60, message="", audience="abonnes")
        db.add(row)
        db.commit()
    return row


def parse_time(value: str) -> Optional[str]:
    """« 8h15 », « 08:15 » → « 08:15 » ; None si l'heure est invalide."""
    m = _TIME.match((value or "").strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def clock(value: Optional[str], default: str) -> time:
    h, m = (int(x) for x in (parse_time(value or "") or default).split(":"))
    return time(h, m)


def values(raw: Optional[str]) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    return [str(v) for v in data if str(v).strip()] if isinstance(data, list) else []


def excluded_keys(raw: Optional[str]) -> set[str]:
    return {v.strip().lower() for v in re.split(r"[,;\s]+", raw or "") if v.strip()}


def enabled_kinds(s: ReminderSettings) -> list[str]:
    on = {"arrivee": s.arrival_enabled, "alerte": s.alert_enabled, "avant_depart": s.predeparture_enabled,
          "depart": s.departure_enabled, "oubli": s.forgot_enabled}
    return [k for k in REMINDER_KINDS if on[k]]


def custom_texts(s: ReminderSettings) -> dict:
    """Objets et textes personnalisés : {type: {"subject": …, "body": …}} (valeurs vides = texte par défaut)."""
    try:
        data = json.loads(s.texts or "{}")
    except ValueError:
        return {}
    return {k: {f: str(v.get(f) or "") for f in ("subject", "body")} for k, v in data.items()
            if k in REMINDER_KINDS and isinstance(v, dict)}


# --------------------------------------------------------------------------- qui doit recevoir un rappel


def day_rows(engine, m: pointage.Mapping, du: date, au: date) -> list[dict]:
    """Journées calculées (statut, pointages) des employés actifs, avec leur poste planifié et leur responsable."""
    S = pointage.qi(m.objs)
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(f"""
            SELECT j.emp_key, j.matricule, j.nom, j.prenom, j.service, j.direction, j.site, j.jour, j.statut,
                   j.nb_pointages AS nb, j.premier_pointage AS p1, j.dernier_pointage AS p2,
                   e.email, r.email AS resp_email, concat_ws(' ', r.prenom, r.nom) AS resp_nom,
                   s.code AS poste_code, s.type AS poste_type, s.s_debut, s.s_fin
            FROM {S}.f_pointage_journalier(:du, :au) j
            JOIN {S}.v_pointage_employes e ON e.emp_key = j.emp_key
            LEFT JOIN {S}.v_pointage_employes r ON r.emp_key = e.responsable_key
            LEFT JOIN LATERAL (
                SELECT po.code, po.type, pp.jour + po.debut AS s_debut,
                       pp.jour + po.fin + CASE WHEN po.fin <= po.debut THEN interval '1 day' ELSE interval '0' END AS s_fin
                FROM {S}.pointage_planning pp JOIN {S}.pointage_postes po ON po.code = pp.code
                WHERE pp.jour = j.jour AND pp.matricule IN (e.matricule, e.emp_key)
                ORDER BY (pp.matricule = e.matricule) DESC LIMIT 1
            ) s ON true
            WHERE NOT j.hors_liste AND e.actif
            ORDER BY j.nom, j.prenom, j.jour"""), {"du": du, "au": au}).mappings()]


def audience(s: ReminderSettings) -> str:
    return s.audience if s.audience in REMINDER_AUDIENCES else "abonnes"


def subscribers(db) -> dict[str, str]:
    """Employés abonnés aux mails de badge : {clé employé: adresse saisie (vide = adresse de la fiche)}."""
    return {x.emp_key: (x.email or "").strip() for x in db.scalars(select(MailSubscriber))}


def for_audience(db, s: ReminderSettings, rows: list[dict]) -> list[dict]:
    """Journées des destinataires choisis : par défaut, seulement les employés cochés dans « Mails de badge →
    Abonnés », avec l'adresse saisie pour eux si elle existe (comme pour la confirmation de pointage)."""
    if audience(s) != "abonnes":
        return rows
    subs = subscribers(db)
    return [{**r, "email": subs[r["emp_key"]] or r.get("email")} for r in rows if r["emp_key"] in subs]


def _targeted(row: dict, s: ReminderSettings) -> bool:
    """Employé dans le périmètre des rappels (services, sites, directions choisis ; matricules exclus)."""
    for field, raw in (("service", s.services), ("site", s.sites), ("direction", s.directions)):
        chosen = values(raw)
        if chosen and (row.get(field) or "") not in chosen:
            return False
    excluded = excluded_keys(s.excluded)
    return not ({(row.get("matricule") or "").lower(), (row.get("emp_key") or "").lower()} & excluded)


def due_at(kind: str, row: dict, s: ReminderSettings, today: date) -> Optional[datetime]:
    """Heure prévue du rappel pour cette journée, ou None si l'employé n'est pas concerné."""
    shift = row.get("poste_type") == "travail" and row.get("s_debut") is not None
    if kind == "arrivee":
        if row["statut"] != "ABSENT":
            return None
        if shift:
            return row["s_debut"] + timedelta(minutes=s.arrival_delay or 0)
        return datetime.combine(today, clock(s.arrival_time, "08:15")) if row["jour"] == today else None
    if kind == "alerte":
        if row["statut"] != "ABSENT":
            return None
        if shift:
            return row["s_debut"] + timedelta(minutes=s.alert_delay or 0)
        return datetime.combine(today, clock(s.alert_time, "09:30")) if row["jour"] == today else None
    if kind == "avant_depart":
        # Toujours « entré » (nombre impair de pointages) : prévenir avant le départ.
        if row["statut"] not in PRESENT or (row["nb"] or 0) % 2 == 0:
            return None
        if shift:
            return row["s_fin"] - timedelta(minutes=s.predeparture_before or 0)
        return datetime.combine(today, clock(s.predeparture_time, "16:45")) if row["jour"] == today else None
    if kind == "depart":
        if row["statut"] != "INCOMPLET":  # un seul pointage : l'arrivée (même définition que le suivi)
            return None
        if shift:
            return row["s_fin"] + timedelta(minutes=s.departure_delay or 0)
        return datetime.combine(today, clock(s.departure_time, "17:45")) if row["jour"] == today else None
    if kind == "oubli":
        if row["statut"] != "INCOMPLET" or not (today - timedelta(days=LOOKBACK_DAYS) <= row["jour"] < today):
            return None
        return datetime.combine(today, clock(s.forgot_time, "09:00"))
    return None


def candidates(rows: list[dict], s: ReminderSettings, now: datetime, kinds: list[str],
               timed: bool = True) -> list[tuple[str, dict, datetime]]:
    """(type, journée, heure prévue) des rappels à envoyer. timed=False : toutes les personnes concernées aujourd'hui,
    même si l'heure du rappel n'est pas encore venue (aperçu de l'administration)."""
    out = []
    for row in rows:
        if not _targeted(row, s):
            continue
        for kind in kinds:
            when = due_at(kind, row, s, now.date())
            if when is None:
                continue
            if timed and not (when <= now < when + WINDOW):
                continue
            if not timed and when + WINDOW <= now:
                continue  # rappel de la nuit dernière, déjà passé
            out.append((kind, row, when))
    return out


def last_sync(db, m: pointage.Mapping) -> Optional[datetime]:
    """Fin (UTC) de la dernière synchronisation réussie de la table des pointages ; None si aucun job ne l'alimente."""
    job_ids = list(db.scalars(select(TableMapping.job_id).join(SyncJob).where(
        SyncJob.target_schema == m.schema, TableMapping.target_table == m.punch_table)))
    if not job_ids:
        return None
    return db.scalar(select(JobRun.finished_at).where(JobRun.job_id.in_(job_ids),
                                                      JobRun.status.in_(("success", "partial")))
                     .order_by(JobRun.finished_at.desc()).limit(1)) or datetime(1970, 1, 1)


def stale_reason(db, m: pointage.Mapping, s: ReminderSettings) -> Optional[str]:
    """Raison de ne rien envoyer (pointages plus synchronisés depuis trop longtemps), sinon None."""
    if not s.max_lag_minutes:
        return None
    last = last_sync(db, m)
    if last is None:
        return None  # table des pointages alimentée hors de l'application : pas de contrôle possible
    lag = utcnow() - last
    if lag > timedelta(minutes=s.max_lag_minutes):
        minutes = int(lag.total_seconds() // 60)
        when = f"depuis {minutes} min" if minutes < 24 * 60 else "depuis plus d'un jour"
        return f"pointages non synchronisés {when} (seuil : {s.max_lag_minutes} min)"
    return None


# --------------------------------------------------------------------------- contenu


def _hm(value) -> str:
    return f"{value:%H}h{value:%M}" if value else "—"


def _poste(row: dict) -> str:
    if row.get("poste_type") == "travail" and row.get("s_debut"):
        return f"poste {row['poste_code']} ({_hm(row['s_debut'])} – {_hm(row['s_fin'])})"
    return ""


def fill(template: str, ctx: dict) -> str:
    """Remplace les {variables} connues ; le reste du texte est laissé tel quel."""
    return re.sub(r"\{(_?[a-z]+)\}", lambda m: str(ctx[m.group(1)]) if m.group(1) in ctx else m.group(0), template or "")


def text_context(row: dict) -> dict:
    poste = _poste(row)
    return {"prenom": (row.get("prenom") or row.get("nom") or "").strip(), "nom": (row.get("nom") or "").strip(),
            "matricule": row.get("matricule") or "", "date": french_day(row["jour"]), "poste": poste,
            "_poste": f" ({poste})" if poste else "", "arrivee": _hm(row.get("p1")), "dernier": _hm(row.get("p2"))}


# Présentation des rappels, sur le modèle du mail de confirmation de badge : (bandeau, pastille, couleur, fond de
# la pastille, intitulé de la case de gauche, valeur de la case de gauche).
_LOOK = {
    "arrivee": ("Rappel de pointage", "!", "#b45309", "#fff4e0", "Arrivée", "Non pointée"),
    "alerte": ("Alerte de pointage", "!", "#be123c", "#ffe4e6", "Arrivée", "Non pointée"),
    "avant_depart": ("Rappel de pointage", "&#9719;", "#b45309", "#fff4e0", "Arrivée", None),
    "depart": ("Départ non pointé", "!", "#b45309", "#fff4e0", "Départ", "Non pointé"),
    "oubli": ("Pointage incomplet", "!", "#be123c", "#ffe4e6", "Pointage manquant", "Arrivée ou départ"),
}


def _day_punches(row: dict) -> str:
    """Pointages connus de la journée (premier et dernier), au format de la liste du mail de badge."""
    nb = row.get("nb") or 0
    if not nb:
        items = [("Aucun pointage enregistré", "—")]
    else:
        items = [("Arrivée", _hm(row.get("p1")))]
        if nb > 1:
            items.append(("Dernier pointage", _hm(row.get("p2"))))
        if nb > 2:
            items.append(("Nombre de pointages", str(nb)))
    cell = "padding:8px 0;border-bottom:1px solid #eef2f6;font-size:14px;"
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">' + "".join(
        f'<tr><td style="{cell}color:#46566a;">{html.escape(label)}</td>'
        f'<td align="right" style="{cell}font-weight:600;color:#0b1a2b;">{html.escape(value)}</td></tr>'
        for label, value in items) + "</table>")


def render(kind: str, row: dict, company: str = "", message: str = "",
           custom: Optional[dict] = None) -> tuple[str, str, str]:
    """(objet, HTML, texte) d'un rappel, présenté comme le mail de confirmation de badge ; custom = objet et texte
    personnalisés de ce type (vides = par défaut)."""
    ctx = text_context(row)
    prenom = ctx["prenom"]
    default, custom = DEFAULT_TEXTS[kind], custom or {}
    subject = fill(custom.get("subject") or default["subject"], ctx).replace("\n", " ").strip()
    title = subject if custom.get("subject") else default["title"]
    lines = [p.strip() for p in re.split(r"\n\s*\n", fill(custom.get("body") or default["body"], ctx)) if p.strip()]
    extra = (message or "").strip()
    band, icon, color, soft, left_label, left_value = _LOOK[kind]
    left_value = left_value or _hm(row.get("p1"))
    font = "font-family:'Segoe UI',Helvetica,Arial,sans-serif;"
    esc = lambda t: html.escape(t, quote=False)  # noqa: E731
    paragraphs = "".join(f'<p style="margin:12px 0 0;font-size:15px;line-height:1.6;color:#46566a;">'
                         f'{esc(t).replace(chr(10), "<br>")}</p>' for t in lines)
    if extra:
        paragraphs += (f'<p style="margin:16px 0 0;padding:12px 14px;background:#f7f9fc;border:1px solid #e6ecf3;'
                       f'border-radius:10px;font-size:14px;line-height:1.55;color:#46566a;">'
                       f'{esc(extra).replace(chr(10), "<br>")}</p>')
    entreprise = esc(company or "")
    service = esc(row.get("service") or "")
    body = f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light only">
<title>{esc(subject)}</title>
</head>
<body style="margin:0;padding:0;background:#f2f5f9;-webkit-text-size-adjust:100%;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f2f5f9;">
  <tr><td align="center" style="padding:32px 12px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;">

      <tr><td style="padding:0 4px 18px;{font}">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
          <td style="width:34px;height:34px;background:#0e7c6e;border-radius:9px;text-align:center;vertical-align:middle;color:#ffffff;font-size:18px;line-height:34px;">&#9719;</td>
          <td style="padding-left:10px;font-size:15px;font-weight:700;color:#0b1a2b;letter-spacing:-0.2px;">{entreprise}</td>
        </tr></table>
      </td></tr>

      <tr><td style="background:#ffffff;border:1px solid #e3e9f0;border-radius:16px;overflow:hidden;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr><td style="height:4px;background:{color};font-size:0;line-height:0;">&nbsp;</td></tr>
          <tr><td style="padding:34px 36px 8px;{font}">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
              <td style="width:44px;height:44px;border-radius:50%;background:{soft};text-align:center;vertical-align:middle;color:{color};font-size:22px;font-weight:700;line-height:44px;">{icon}</td>
            </tr></table>
            <p style="margin:20px 0 6px;font-size:13px;font-weight:600;letter-spacing:1.2px;text-transform:uppercase;color:{color};">{esc(band)}</p>
            <h1 style="margin:0;font-size:24px;line-height:1.3;font-weight:700;color:#0b1a2b;letter-spacing:-0.3px;">Bonjour {esc(prenom)},</h1>
            <p style="margin:12px 0 0;font-size:16px;line-height:1.5;font-weight:600;color:#0b1a2b;">{esc(title)}</p>
            {paragraphs}
          </td></tr>
          <tr><td style="padding:22px 36px 6px;{font}">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f7f9fc;border:1px solid #e6ecf3;border-radius:12px;">
              <tr>
                <td style="padding:18px 20px;border-right:1px solid #e6ecf3;" width="50%">
                  <p style="margin:0;font-size:12px;color:#7a8898;text-transform:uppercase;letter-spacing:.8px;">{esc(left_label)}</p>
                  <p style="margin:4px 0 0;font-size:22px;font-weight:700;color:{color if not row.get('nb') or kind != 'avant_depart' else '#0b1a2b'};letter-spacing:-0.4px;">{esc(left_value)}</p>
                </td>
                <td style="padding:18px 20px;" width="50%">
                  <p style="margin:0;font-size:12px;color:#7a8898;text-transform:uppercase;letter-spacing:.8px;">Date</p>
                  <p style="margin:6px 0 0;font-size:15px;font-weight:600;color:#0b1a2b;">{esc(ctx["date"])}</p>
                </td>
              </tr>
            </table>
          </td></tr>
          <tr><td style="padding:16px 36px 4px;{font}">
            <p style="margin:0 0 8px;font-size:13px;font-weight:600;color:#0b1a2b;">Vos pointages de la journée</p>
            {_day_punches(row)}
          </td></tr>
          <tr><td style="padding:14px 36px 30px;{font}">
            <p style="margin:0;font-size:13px;line-height:1.6;color:#7a8898;">Matricule {esc(ctx["matricule"])}{(' · ' + service) if service else ''}{(' · ' + esc(ctx["poste"])) if ctx["poste"] else ''}</p>
          </td></tr>
        </table>
      </td></tr>

      <tr><td style="padding:20px 8px 0;{font}text-align:center;">
        <p style="margin:0;font-size:12px;line-height:1.6;color:#8896a6;">Si vous pensez qu'il s'agit d'une erreur, prévenez votre responsable ou le service RH.</p>
        <p style="margin:8px 0 0;font-size:12px;color:#a3afbc;">Message automatique — merci de ne pas y répondre. © {row["jour"].year} {entreprise}</p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>
"""
    text_body = f"Bonjour {prenom},\n\n{title}\n\n" + "\n\n".join(lines) + (f"\n\n{extra}" if extra else "") \
        + f"\n\nMatricule {ctx['matricule']}\n\nMessage automatique — merci de ne pas y répondre."
    return subject, body, text_body


def full_name(row: dict) -> str:
    return " ".join(x for x in (row.get("nom"), row.get("prenom")) if x) or row.get("matricule") or row["emp_key"]


def build(kind: str, row: dict, s: ReminderSettings, ms, force_to: Optional[str] = None):
    """Message prêt à partir, ou (None, raison) : (message, None) sinon."""
    from .mails import compose, valid_email

    subject, body, text_body = render(kind, row, ms.company, s.message or "", custom_texts(s).get(kind))
    email = (row.get("email") or "").strip()
    if force_to:
        test = ms.__class__(**{**_copy(ms), "mode": "test", "test_recipients": force_to})
        return compose(test, subject, body, text_body, email or "(aucune adresse)", full_name(row)), None
    if not valid_email(email):
        return None, "aucune adresse e-mail"
    msg = compose(ms, subject, body, text_body, email, full_name(row))
    if msg is None:
        return None, "mode test sans adresse de test"
    cc = (row.get("resp_email") or "").strip()
    copy = (kind == "oubli" and s.forgot_cc_manager) or (kind == "alerte" and s.alert_cc_manager)
    if copy and ms.mode == "production" and valid_email(cc) and cc != email:
        msg["Cc"] = cc
    return msg, None


# --------------------------------------------------------------------------- envoi planifié

_lock = threading.Lock()


def run_due(now: Optional[datetime] = None) -> dict:
    """Envoie les rappels dus (appelé toutes les 5 minutes). Ne lève jamais d'exception."""
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        return _run_due(now or local_now())
    except Exception as exc:  # noqa: BLE001
        logger.exception("Rappels de pointage impossibles")
        write_log("ERROR", f"Rappels de pointage : traitement impossible ({exc.__class__.__name__}: {exc}).")
        return {"status": "error", "error": str(exc)}
    finally:
        _lock.release()


def _first_due(s: ReminderSettings, now: datetime) -> bool:
    """Au moins un rappel peut être dû : évite d'interroger PostgreSQL toutes les 5 minutes pour rien."""
    if s.arrival_enabled or s.departure_enabled or s.alert_enabled or s.predeparture_enabled:
        return True  # postes planifiés : heures propres à chacun
    return s.forgot_enabled and now.time() >= clock(s.forgot_time, "09:00") \
        and datetime.combine(now.date(), clock(s.forgot_time, "09:00")) + WINDOW > now


def _run_due(now: datetime) -> dict:
    from .mails import _quit, get_settings as mail_settings_of, open_smtp

    with SessionLocal() as db:
        s = get_settings(db)
        kinds = enabled_kinds(s)
        if not kinds or not _first_due(s, now):
            return {"status": "idle"}
        ms = mail_settings_of(db)
        if not ms.smtp_host or not ms.from_email:
            return {"status": "unconfigured"}
        engine, m = _context()
        if engine is None:
            return {"status": "unconfigured"}
        try:
            reason = stale_reason(db, m, s)
            if reason:
                s.last_run_at, s.last_run_summary = utcnow(), f"aucun rappel envoyé : {reason}"
                db.commit()
                return {"status": "stale", "reason": reason}
            rows = for_audience(db, s, day_rows(engine, m, now.date() - timedelta(days=LOOKBACK_DAYS), now.date()))
        finally:
            engine.dispose()
        todo = candidates(rows, s, now, kinds)
        if not todo:
            return {"status": "idle"}
        logs = {(l.kind, l.jour.date(), l.emp_key): l for l in db.scalars(select(ReminderLog).where(
            ReminderLog.jour >= datetime.combine(now.date() - timedelta(days=LOOKBACK_DAYS + 1), time())))}
        counts = {"sent": 0, "skipped": 0, "failed": 0}
        client = None
        try:
            for kind, row, _ in todo:
                log = logs.get((kind, row["jour"], row["emp_key"]))
                if log is not None and (log.status != "failed" or log.attempts >= MAX_ATTEMPTS):
                    continue
                if log is None:
                    log = ReminderLog(kind=kind, jour=datetime.combine(row["jour"], time()), emp_key=row["emp_key"],
                                      attempts=0)
                    db.add(log)
                    logs[(kind, row["jour"], row["emp_key"])] = log
                log.attempts += 1
                log.matricule, log.name, log.mode = row.get("matricule") or "", full_name(row), ms.mode
                log.intended = row.get("email") or ""
                try:
                    msg, why = build(kind, row, s, ms)
                    if msg is None:
                        log.status, log.recipient, log.error = "skipped", "", why
                    else:
                        if client is None:
                            client = open_smtp(ms)
                        client.send_message(msg)
                        log.status, log.error = "sent", ""
                        log.recipient = msg["To"] + (f" (copie : {msg['Cc']})" if msg["Cc"] else "")
                except Exception as exc:  # noqa: BLE001
                    log.status, log.error = "failed", f"{exc.__class__.__name__}: {exc}"[:500]
                    if client is not None:
                        _quit(client)
                        client = None
                counts[log.status] += 1
                db.commit()
        finally:
            if client is not None:
                _quit(client)
        if not any(counts.values()):
            return {"status": "idle"}
        s = get_settings(db)
        s.last_run_at = utcnow()
        s.last_run_summary = (f"{counts['sent']} envoyé(s), {counts['skipped']} ignoré(s), {counts['failed']} échec(s)"
                              + ("" if ms.mode == "production" else " — mode test"))
        db.commit()
        if counts["sent"] or counts["failed"]:
            write_log("INFO" if not counts["failed"] else "WARNING", f"Rappels de pointage : {s.last_run_summary}.")
        return {"status": "done", **counts}


# --------------------------------------------------------------------------- administration : aperçu et essai


def situation(now: Optional[datetime] = None) -> dict:
    """Personnes concernées aujourd'hui par chaque rappel (même désactivé), heure prévue et état d'envoi."""
    now = now or local_now()
    engine, m = _context()
    if engine is None:
        raise ValueError("Le module de pointage n'est pas configuré.")
    try:
        with SessionLocal() as db:
            s = get_settings(db)
            rows = for_audience(db, s, day_rows(engine, m, now.date() - timedelta(days=LOOKBACK_DAYS), now.date()))
            sent = {(l.kind, l.jour.date(), l.emp_key): l for l in db.scalars(select(ReminderLog).where(
                ReminderLog.jour >= datetime.combine(now.date() - timedelta(days=LOOKBACK_DAYS + 1), time())))}
            out = {k: [] for k in REMINDER_KINDS}
            for kind, row, when in candidates(rows, s, now, list(REMINDER_KINDS), timed=False):
                log = sent.get((kind, row["jour"], row["emp_key"]))
                out[kind].append({"row": row, "name": full_name(row), "when": when, "due": when <= now,
                                  "poste": _poste(row), "log": log})
            for items in out.values():
                items.sort(key=lambda x: (x["when"], x["name"]))
            return {"items": out, "with_email": sum(1 for r in rows if r["jour"] == now.date() and r.get("email")),
                    "employees": len({r["emp_key"] for r in rows}), "stale": stale_reason(db, m, s),
                    "last_sync": last_sync(db, m), "rows": rows, "subscribers": len(subscribers(db))}
    finally:
        engine.dispose()


def sample_row(kind: str, today: Optional[date] = None) -> dict:
    today = today or local_now().date()
    day = today - timedelta(days=1) if kind == "oubli" else today
    arrival = None if kind in ("arrivee", "alerte") else datetime.combine(day, time(7, 52))
    return {"emp_key": "exemple", "matricule": "E000", "nom": "Exemple", "prenom": "Camille", "jour": day,
            "statut": "ABSENT" if arrival is None else "INCOMPLET", "nb": 0 if arrival is None else 1, "p1": arrival,
            "p2": arrival, "email": "camille.exemple@exemple.com", "poste_type": None, "service": "Service exemple"}


def preview(kind: str) -> tuple[str, str]:
    from .mails import get_settings as mail_settings_of

    with SessionLocal() as db:
        s, ms = get_settings(db), mail_settings_of(db)
        subject, body, _ = render(kind, sample_row(kind), ms.company, s.message or "", custom_texts(s).get(kind))
        return subject, body


def send_test(kind: str, to: str) -> str:
    """Rappel d'exemple envoyé à une adresse (bandeau « MODE TEST »)."""
    from .mails import _quit, get_settings as mail_settings_of, open_smtp

    with SessionLocal() as db:
        s, ms = get_settings(db), mail_settings_of(db)
        msg, _ = build(kind, sample_row(kind), s, ms, force_to=to)
        client = open_smtp(ms)
        try:
            client.send_message(msg)
        finally:
            _quit(client)
        return msg["To"]
