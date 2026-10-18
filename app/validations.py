"""Relances des demandes en attente de validation (congés, télétravail, missions).

Une demande non validée n'est pas prise en compte dans le pointage : l'employé apparaît « Absent ». Chaque jour
paramétré, à l'heure choisie, le prochain validateur de chaque demande en attente (colonne de la table des demandes,
ex. « ProchainvalidateurMatricule » d'HR Smart) reçoit un seul e-mail qui regroupe ses demandes, avec le nombre de
jours déjà comptés absents et un bouton vers l'outil RH où la validation se fait.

- Demande « en attente » : son état fait partie des statuts paramétrés (ex. « En Validation », « Validé Par N1 ») ;
  seules les demandes utiles sont relancées (période commencée, ou qui commence dans les N prochains jours, et
  terminée depuis moins de N jours).
- Garde-fous : SMTP et mode test / production des « Mails de badge » ; un seul mail par validateur et par jour
  (journal), espacement et nombre maximal de relances par demande ; validateur introuvable ou sans adresse : mail aux
  adresses RH en copie si elles sont renseignées, sinon rien ; pas d'envoi si la table des demandes n'est plus
  synchronisée (seuil facultatif).
"""
import hashlib
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
from .models import (APPROVAL_SOURCES, ApprovalReminderItem, ApprovalReminderLog, ApprovalSettings, JobRun, SyncJob,
                     TableMapping, utcnow)

logger = logging.getLogger("validations")
MAX_ATTEMPTS = 3
UNKNOWN = "(inconnu)"
DEFAULT_SOURCES = {
    "leave": {"enabled": True, "pending": "En Validation, Validé Par N1", "validator_col": "ProchainvalidateurMatricule",
              "step_col": "", "created_col": ""},
    "tw": {"enabled": False, "pending": "En Validation, Validé Par N1", "validator_col": "ProchainvalidateurMatricule",
           "step_col": "", "created_col": ""},
    "mission": {"enabled": False, "pending": "En Validation, Validé Par N1",
                "validator_col": "ProchainvalidateurMatricule", "step_col": "", "created_col": ""},
}
WEEKDAYS = {1: "lun.", 2: "mar.", 3: "mer.", 4: "jeu.", 5: "ven.", 6: "sam.", 7: "dim."}
_TIME = re.compile(r"^([01]?\d|2[0-3])[:hH]([0-5]\d)$")


# --------------------------------------------------------------------------- réglages


def get_settings(db) -> ApprovalSettings:
    row = db.scalars(select(ApprovalSettings).order_by(ApprovalSettings.id)).first()
    if row is None:
        row = ApprovalSettings(enabled=False, send_time="08:30", weekdays="1,2,3,4,5",
                               app_url="https://ico-hrsmart.ea.holcim.net/HRSmartV3", app_name="HR Smart",
                               sources=json.dumps(DEFAULT_SOURCES), grace_days=0, repeat_days=1, max_reminders=0,
                               horizon_days=14, past_days=31, cc_addresses="", cc_after_absent=0, subject="",
                               message="", max_lag_minutes=0, show_in_suivi=True)
        db.add(row)
        db.commit()
    return row


def parse_time(value: str) -> Optional[str]:
    m = _TIME.match((value or "").strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def clock(value: Optional[str]) -> time:
    h, m = (int(x) for x in (parse_time(value or "") or "08:30").split(":"))
    return time(h, m)


def weekdays(s: ApprovalSettings) -> set[int]:
    return {int(d) for d in re.split(r"[,\s]+", s.weekdays or "") if d.isdigit() and 1 <= int(d) <= 7}


def sources(s: ApprovalSettings) -> dict:
    """Configuration par source (congés, télétravail, missions), complétée par les valeurs par défaut."""
    try:
        data = json.loads(s.sources or "{}")
    except ValueError:
        data = {}
    out = {}
    for key, default in DEFAULT_SOURCES.items():
        cfg = {**default, **(data.get(key) or {})}
        cfg["enabled"] = bool(cfg.get("enabled"))
        out[key] = cfg
    return out


def active_sources(s: ApprovalSettings, m: pointage.Mapping) -> dict:
    """Sources relancées : activées, dont la table est configurée et qui ont au moins un statut « en attente »."""
    return {k: c for k, c in sources(s).items()
            if c["enabled"] and getattr(m, f"{k}_table", "") and pointage._values(c.get("pending", ""))}


# --------------------------------------------------------------------------- demandes en attente


def _pending_sql(m: pointage.Mapping, prefix: str, cfg: dict, types: dict[str, str]) -> Optional[str]:
    """Demandes en attente de la source qui chevauchent [:du, :au], avec leur employé (vue des employés)."""
    table = getattr(m, f"{prefix}_table")
    get = lambda name: getattr(m, f"{prefix}_{name}")  # noqa: E731
    state_col = get("state_col")
    pending = pointage._values(cfg.get("pending", ""))
    if not table or not state_col or not pending or state_col not in types:
        return None
    col = lambda name: f"l.{pointage.qi(name)}"  # noqa: E731
    starts = [pointage._date_expr(col(c), types.get(c, "")) for c in pointage.split_cols(get("start_col"))]
    ends = [pointage._date_expr(col(c), types.get(c, "")) for c in pointage.split_cols(get("end_col"))]
    if not starts:
        return None
    start = starts[0] if len(starts) == 1 else f"LEAST({', '.join(starts)})"
    last = (ends[0] if len(ends) == 1 else f"GREATEST({', '.join(ends)})") if ends else start
    end = f"COALESCE({last}, {start})"
    _, link = pointage._request_link(m, prefix)

    def opt_text(name):
        c = cfg.get(name) or ""
        return f"nullif(btrim({col(c)}::text), '')" if c and c in types else "NULL::text"

    created = cfg.get("created_col") or ""
    created_expr = pointage._date_expr(col(created), types.get(created, "")) if created and created in types \
        else "NULL::date"
    conds = [f"{start} <= :au", f"{end} >= :du",
             f"{pointage._norm_sql(col(state_col))} IN ({', '.join(pointage.lit(v) for v in pending)})"]
    kind = "NULL::text"
    if prefix == "leave" and m.leave_type_col and m.leave_type_col in types:
        typ = pointage._norm_sql(col(m.leave_type_col))
        values = pointage._values(m.leave_annual_values) + (pointage._values(m.leave_excep_values) or [])
        if values:
            conds.append(f"{typ} IN ({', '.join(pointage.lit(v) for v in values)})")
        kind = f"nullif(btrim({col(m.leave_type_col)}::text), '')"
    S = pointage.qi(m.objs)
    return f"""SELECT DISTINCT e.emp_key, e.matricule, e.nom, e.prenom, e.service, {start} AS du, {end} AS au,
               btrim({col(state_col)}::text) AS etat, {opt_text('validator_col')} AS validateur,
               {opt_text('step_col')} AS etape, {created_expr} AS demande_le, {kind} AS nature
        FROM {pointage.qt(m.schema, table)} l
        JOIN {S}.v_pointage_employes e ON {link}
        WHERE {' AND '.join(conds)}"""


def request_key(prefix: str, r: dict) -> str:
    raw = f"{prefix}|{r['emp_key']}|{r['du']}|{r['au']}|{r.get('nature') or ''}"
    return hashlib.sha1(raw.encode()).hexdigest()


def source_label(prefix: str, r: dict) -> str:
    base = {"leave": "Congé", "tw": "Télétravail", "mission": "Mission"}[prefix]
    return f"{base} ({r['nature']})" if r.get("nature") else base


def full_name(r: dict) -> str:
    return " ".join(x for x in (r.get("nom"), r.get("prenom")) if x) or r.get("matricule") or r.get("emp_key", "")


def pending_requests(engine, m: pointage.Mapping, s: ApprovalSettings, du: date, au: date,
                     emp_keys: Optional[list[str]] = None, with_absences: bool = True,
                     today: Optional[date] = None) -> list[dict]:
    """Demandes en attente qui chevauchent [du, au], toutes sources activées : employé, période, état, étape,
    validateur (nom et adresse e-mail retrouvés par son matricule) et jours déjà comptés « Absent »."""
    out = []
    for prefix, cfg in active_sources(s, m).items():
        types = pointage.column_types(engine, m.schema, getattr(m, f"{prefix}_table"))
        sql = _pending_sql(m, prefix, cfg, types)
        if not sql:
            continue
        params = {"du": du, "au": au}
        if emp_keys is not None:
            sql = f"SELECT * FROM ({sql}) q WHERE emp_key = ANY(:keys)"
            params["keys"] = list(emp_keys)
        with engine.connect() as c:
            for r in c.execute(text(f"{sql} ORDER BY 6, 3, 4" if emp_keys is None else sql), params).mappings():
                r = dict(r)
                r.update(source=prefix, key=request_key(prefix, r), label=source_label(prefix, r))
                out.append(r)
    if not out:
        return out
    S = pointage.qi(m.objs)
    refs = sorted({r["validateur"] for r in out if r.get("validateur")})
    found = {}
    if refs:
        with engine.connect() as c:
            for v in c.execute(text(
                    f"SELECT emp_key, matricule, concat_ws(' ', nom, prenom) AS nom, prenom, email FROM {S}.v_pointage_employes "
                    f"WHERE matricule = ANY(:r) OR emp_key = ANY(:r)"), {"r": refs}).mappings():
                for k in (v["matricule"], v["emp_key"]):
                    found.setdefault(k, dict(v))
    today = today or local_now().date()
    absences: dict[tuple, int] = {}
    if with_absences:
        start = min(r["du"] for r in out)
        stop = min(max(r["au"] for r in out), today)
        if start <= stop:
            with engine.connect() as c:
                for key, jour in c.execute(text(
                        f"SELECT emp_key, jour FROM {S}.f_pointage_journalier(:du, :au) "
                        f"WHERE statut = 'ABSENT' AND emp_key = ANY(:k)"),
                        {"du": start, "au": stop, "k": sorted({r["emp_key"] for r in out})}):
                    absences[(key, jour)] = 1
    for r in out:
        v = found.get(r.get("validateur") or "")
        r["validator"] = {"matricule": r.get("validateur") or "", "nom": (v or {}).get("nom") or "",
                          "prenom": ((v or {}).get("prenom") or "").strip(),
                          "email": ((v or {}).get("email") or "").strip(), "found": v is not None}
        r["jours"] = (r["au"] - r["du"]).days + 1
        r["absents"] = sum(1 for i in range((min(r["au"], today) - r["du"]).days + 1)
                           if absences.get((r["emp_key"], r["du"] + timedelta(days=i)))) if r["du"] <= today else 0
        r["attente"] = (today - r["demande_le"]).days if r.get("demande_le") else None
    return out


def pending_map(engine, m: pointage.Mapping, s: ApprovalSettings, du: date, au: date,
                emp_keys: list[str]) -> dict[tuple, str]:
    """Suivi : {(clé employé, jour): « Congé en attente de validation (En Validation) »} pour les jours couverts."""
    if not emp_keys or not s.show_in_suivi:
        return {}
    out = {}
    for r in pending_requests(engine, m, s, du, au, emp_keys, with_absences=False):
        day = max(r["du"], du)
        while day <= min(r["au"], au):
            out.setdefault((r["emp_key"], day), f"{r['label']} en attente de validation ({r['etat']})")
            day += timedelta(days=1)
    return out


# --------------------------------------------------------------------------- qui relancer


def due_items(db, s: ApprovalSettings, reqs: list[dict], today: date) -> list[dict]:
    """Demandes à relancer aujourd'hui : délai de grâce passé, espacement respecté, maximum non atteint."""
    items = {i.req_key: i for i in db.scalars(select(ApprovalReminderItem).where(
        ApprovalReminderItem.req_key.in_([r["key"] for r in reqs])))} if reqs else {}
    out = []
    for r in reqs:
        if r.get("attente") is not None and r["attente"] < (s.grace_days or 0):
            r["next"] = r["demande_le"] + timedelta(days=s.grace_days or 0)
            continue
        item = items.get(r["key"])
        r["sent"] = item.count if item else 0
        if item and s.max_reminders and item.count >= s.max_reminders:
            r["next"] = None
            continue
        if item and item.last_sent and (today - item.last_sent.date()).days < max(s.repeat_days or 1, 1):
            r["next"] = item.last_sent.date() + timedelta(days=max(s.repeat_days or 1, 1))
            continue
        r["next"] = today
        out.append(r)
    return out


def group_by_validator(reqs: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for r in reqs:
        groups.setdefault(r["validator"]["matricule"] or UNKNOWN, []).append(r)
    return groups


def last_sync(db, m: pointage.Mapping, table: str) -> Optional[datetime]:
    job_ids = list(db.scalars(select(TableMapping.job_id).join(SyncJob).where(
        SyncJob.target_schema == m.schema, TableMapping.target_table == table)))
    if not job_ids:
        return None
    return db.scalar(select(JobRun.finished_at).where(JobRun.job_id.in_(job_ids),
                                                      JobRun.status.in_(("success", "partial")))
                     .order_by(JobRun.finished_at.desc()).limit(1)) or datetime(1970, 1, 1)


def stale_reason(db, m: pointage.Mapping, s: ApprovalSettings) -> Optional[str]:
    if not s.max_lag_minutes:
        return None
    for prefix in active_sources(s, m):
        last = last_sync(db, m, getattr(m, f"{prefix}_table"))
        if last is not None and utcnow() - last > timedelta(minutes=s.max_lag_minutes):
            return (f"table des demandes ({APPROVAL_SOURCES[prefix].lower()}) non synchronisée depuis plus de "
                    f"{s.max_lag_minutes} min")
    return None


# --------------------------------------------------------------------------- contenu


def _d(value: Optional[date]) -> str:
    return value.strftime("%d/%m/%Y") if value else "—"


def render(validator: dict, reqs: list[dict], s: ApprovalSettings, company: str = "") -> tuple[str, str, str]:
    """(objet, HTML, texte) du mail d'un validateur, présenté comme le mail de confirmation de badge."""
    esc = lambda t: html.escape(str(t), quote=False)  # noqa: E731
    n = len(reqs)
    app = s.app_name or "l'outil RH"
    kinds = {r["source"] for r in reqs}
    plural = n > 1
    noun = ("demandes" if plural else "demande") + (" de congé" if kinds == {"leave"} else "")
    subject = (s.subject or "").strip().replace("{n}", str(n)) or f"{n} {noun} en attente de votre validation"
    absents = sum(r["absents"] for r in reqs)
    prenom = validator.get("prenom") or ""
    font = "font-family:'Segoe UI',Helvetica,Arial,sans-serif;"
    cell = "padding:10px 8px;border-bottom:1px solid #eef2f6;font-size:13px;color:#46566a;vertical-align:top;"
    rows = []
    for r in reqs:
        waiting = f"<br><span style=\"color:#8896a6;\">en attente depuis {r['attente']} j</span>" \
            if r.get("attente") is not None else ""
        step = f" · {esc(r['etape'])}" if r.get("etape") else ""
        absent = (f'<span style="color:#be123c;font-weight:700;">{r["absents"]} j</span>' if r["absents"]
                  else '<span style="color:#8896a6;">0 j</span>')
        rows.append(
            f'<tr><td style="{cell}"><strong style="color:#0b1a2b;">{esc(full_name(r))}</strong><br>'
            f'<span style="color:#8896a6;">{esc(r.get("matricule") or "")}{(" · " + esc(r["service"])) if r.get("service") else ""}</span></td>'
            f'<td style="{cell}">{esc(r["label"])}<br><span style="color:#8896a6;">{esc(r["etat"])}{step}</span></td>'
            f'<td style="{cell}white-space:nowrap;">{_d(r["du"])} → {_d(r["au"])}<br>'
            f'<span style="color:#8896a6;">{r["jours"]} jour(s)</span>{waiting}</td>'
            f'<td style="{cell}text-align:right;">{absent}</td></tr>')
    extra = (s.message or "").strip()
    extra_html = (f'<p style="margin:16px 0 0;padding:12px 14px;background:#f7f9fc;border:1px solid #e6ecf3;'
                  f'border-radius:10px;font-size:14px;line-height:1.55;color:#46566a;">'
                  f'{esc(extra).replace(chr(10), "<br>")}</p>') if extra else ""
    url = (s.app_url or "").strip()
    button = (f'<p style="margin:22px 0 0;"><a href="{html.escape(url)}" style="display:inline-block;padding:12px 22px;'
              f'background:#0e7c6e;color:#ffffff;text-decoration:none;border-radius:10px;font-weight:600;font-size:14px;">'
              f'Valider dans {esc(app)}</a></p>') if url else ""
    entreprise = esc(company or "")
    body = f"""<!doctype html>
<html lang="fr">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light only"><title>{esc(subject)}</title></head>
<body style="margin:0;padding:0;background:#f2f5f9;-webkit-text-size-adjust:100%;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f2f5f9;">
  <tr><td align="center" style="padding:32px 12px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:640px;">
      <tr><td style="padding:0 4px 18px;{font}">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
          <td style="width:34px;height:34px;background:#0e7c6e;border-radius:9px;text-align:center;vertical-align:middle;color:#ffffff;font-size:18px;line-height:34px;">&#9719;</td>
          <td style="padding-left:10px;font-size:15px;font-weight:700;color:#0b1a2b;letter-spacing:-0.2px;">{entreprise}</td>
        </tr></table>
      </td></tr>
      <tr><td style="background:#ffffff;border:1px solid #e3e9f0;border-radius:16px;overflow:hidden;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr><td style="height:4px;background:#b45309;font-size:0;line-height:0;">&nbsp;</td></tr>
          <tr><td style="padding:34px 36px 8px;{font}">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
              <td style="width:44px;height:44px;border-radius:50%;background:#fff4e0;text-align:center;vertical-align:middle;color:#b45309;font-size:22px;font-weight:700;line-height:44px;">!</td>
            </tr></table>
            <p style="margin:20px 0 6px;font-size:13px;font-weight:600;letter-spacing:1.2px;text-transform:uppercase;color:#b45309;">Demandes à valider</p>
            <h1 style="margin:0;font-size:24px;line-height:1.3;font-weight:700;color:#0b1a2b;letter-spacing:-0.3px;">Bonjour {esc(prenom or validator.get('nom') or '')},</h1>
            <p style="margin:12px 0 0;font-size:16px;line-height:1.5;font-weight:600;color:#0b1a2b;">{n} {noun} {"attendent" if plural else "attend"} votre validation dans {esc(app)}.</p>
            <p style="margin:12px 0 0;font-size:15px;line-height:1.6;color:#46566a;">Tant {"qu'elles ne sont pas validées" if plural else "qu'elle n'est pas validée"}, ces jours ne sont pas pris en
              compte dans le pointage : {"les employés concernés apparaissent" if plural else "l'employé concerné apparaît"} <strong>absent{"s" if plural else ""}</strong>{f" ({absents} jour(s) déjà comptés absents)" if absents else ""}.</p>
            {extra_html}
          </td></tr>
          <tr><td style="padding:18px 36px 6px;{font}">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr><td style="padding:0 8px 6px;font-size:11px;color:#7a8898;text-transform:uppercase;letter-spacing:.8px;">Employé</td>
                <td style="padding:0 8px 6px;font-size:11px;color:#7a8898;text-transform:uppercase;letter-spacing:.8px;">Demande</td>
                <td style="padding:0 8px 6px;font-size:11px;color:#7a8898;text-transform:uppercase;letter-spacing:.8px;">Période</td>
                <td style="padding:0 8px 6px;font-size:11px;color:#7a8898;text-transform:uppercase;letter-spacing:.8px;text-align:right;">Comptés absents</td></tr>
              {''.join(rows)}
            </table>
            {button}
          </td></tr>
          <tr><td style="padding:14px 36px 30px;{font}">
            <p style="margin:0;font-size:13px;line-height:1.6;color:#7a8898;">Vous recevez ce message car vous êtes le prochain validateur de ces demandes.</p>
          </td></tr>
        </table>
      </td></tr>
      <tr><td style="padding:20px 8px 0;{font}text-align:center;">
        <p style="margin:0;font-size:12px;color:#a3afbc;">Message automatique — merci de ne pas y répondre. © {date.today().year} {entreprise}</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>
"""
    lines = [f"- {full_name(r)} ({r.get('matricule') or ''}) : {r['label']}, {_d(r['du'])} → {_d(r['au'])}, "
             f"{r['etat']}, {r['absents']} jour(s) compté(s) absent(s)" for r in reqs]
    text_body = (f"Bonjour {prenom},\n\n{n} {noun} {'attendent' if plural else 'attend'} votre validation dans {app}. Tant qu'elles ne sont pas validées, "
                 f"ces jours ne sont pas pris en compte dans le pointage.\n\n" + "\n".join(lines)
                 + (f"\n\n{extra}" if extra else "") + (f"\n\nValider : {url}" if url else "")
                 + "\n\nMessage automatique — merci de ne pas y répondre.")
    return subject, body, text_body


def build(validator: dict, reqs: list[dict], s: ApprovalSettings, ms, force_to: Optional[str] = None):
    """(message, None) prêt à partir, ou (None, raison)."""
    from .mails import compose, parse_addresses, valid_email

    subject, body, text_body = render(validator, reqs, s, ms.company)
    name = validator.get("nom") or validator.get("matricule") or UNKNOWN
    email = validator.get("email") or ""
    cc = [a for a in parse_addresses(s.cc_addresses or "") if valid_email(a)]
    if force_to:
        test = ms.__class__(**{**_copy(ms), "mode": "test", "test_recipients": force_to})
        return compose(test, subject, body, text_body, email or "(aucune adresse)", name), None
    to = email if valid_email(email) else ""
    if not to:
        if not cc:
            return None, "validateur introuvable ou sans adresse e-mail" if validator.get("matricule") \
                else "aucun validateur indiqué"
        to, cc = ", ".join(cc), []  # pas de validateur joignable : la relance part aux RH
    msg = compose(ms, subject, body, text_body, to, name)
    if msg is None:
        return None, "mode test sans adresse de test"
    worst = max((r["absents"] for r in reqs), default=0)
    if cc and ms.mode == "production" and (not s.cc_after_absent or worst >= s.cc_after_absent):
        msg["Cc"] = ", ".join(a for a in cc if a != to)
    return msg, None


# --------------------------------------------------------------------------- envoi planifié

_lock = threading.Lock()


def run_due(now: Optional[datetime] = None) -> dict:
    """Envoie les relances du jour (appelé toutes les 10 minutes). Ne lève jamais d'exception."""
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        return _run_due(now or local_now())
    except Exception as exc:  # noqa: BLE001
        logger.exception("Relances des demandes impossibles")
        write_log("ERROR", f"Relances des demandes en attente : traitement impossible ({exc.__class__.__name__}: {exc}).")
        return {"status": "error", "error": str(exc)}
    finally:
        _lock.release()


def _run_due(now: datetime) -> dict:
    from .mails import _quit, get_settings as mail_settings_of, open_smtp

    today = now.date()
    with SessionLocal() as db:
        s = get_settings(db)
        if not s.enabled or now.isoweekday() not in weekdays(s) or now.time() < clock(s.send_time):
            return {"status": "idle"}
        ms = mail_settings_of(db)
        if not ms.smtp_host or not ms.from_email:
            return {"status": "unconfigured"}
        engine, m = _context()
        if engine is None:
            return {"status": "unconfigured"}
        try:
            if not active_sources(s, m):
                return {"status": "idle"}
            reason = stale_reason(db, m, s)
            if reason:
                s.last_run_at, s.last_run_summary = utcnow(), f"aucune relance : {reason}"
                db.commit()
                return {"status": "stale", "reason": reason}
            reqs = pending_requests(engine, m, s, today - timedelta(days=s.past_days or 0),
                                    today + timedelta(days=s.horizon_days or 0), today=today)
        finally:
            engine.dispose()
        groups = group_by_validator(due_items(db, s, reqs, today))
        if not groups:
            return {"status": "idle"}
        day = datetime.combine(today, time())
        logs = {l.validator: l for l in db.scalars(select(ApprovalReminderLog).where(ApprovalReminderLog.day == day))}
        counts = {"sent": 0, "skipped": 0, "failed": 0}
        client = None
        try:
            for validator_key, items in groups.items():
                log = logs.get(validator_key)
                if log is not None and (log.status != "failed" or log.attempts >= MAX_ATTEMPTS):
                    continue
                if log is None:
                    log = ApprovalReminderLog(day=day, validator=validator_key, attempts=0)
                    db.add(log)
                v = items[0]["validator"]
                log.attempts += 1
                log.name, log.mode, log.intended, log.requests = v["nom"], ms.mode, v["email"], len(items)
                log.details = "\n".join(f"{full_name(r)} — {r['label']} du {_d(r['du'])} au {_d(r['au'])} ({r['etat']})"
                                        for r in items)[:4000]
                try:
                    msg, why = build(v, items, s, ms)
                    if msg is None:
                        log.status, log.recipient, log.error = "skipped", "", why
                    else:
                        if client is None:
                            client = open_smtp(ms)
                        client.send_message(msg)
                        log.status, log.error = "sent", ""
                        log.recipient = msg["To"] + (f" (copie : {msg['Cc']})" if msg["Cc"] else "")
                        stamp = datetime.combine(today, time())
                        known = {i.req_key: i for i in db.scalars(select(ApprovalReminderItem).where(
                            ApprovalReminderItem.req_key.in_([r["key"] for r in items])))}
                        for r in items:
                            item = known.get(r["key"])
                            if item is None:
                                item = ApprovalReminderItem(req_key=r["key"], source=r["source"], count=0,
                                                            first_sent=stamp)
                                db.add(item)
                            item.label = f"{full_name(r)} — {r['label']} du {_d(r['du'])} au {_d(r['au'])}"[:300]
                            item.count += 1
                            item.last_sent = stamp
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
        s.last_run_summary = (f"{counts['sent']} envoyée(s), {counts['skipped']} ignorée(s), {counts['failed']} échec(s)"
                              + ("" if ms.mode == "production" else " — mode test"))
        db.commit()
        if counts["sent"] or counts["failed"]:
            write_log("INFO" if not counts["failed"] else "WARNING",
                      f"Relances des demandes en attente : {s.last_run_summary}.")
        return {"status": "done", **counts}


# --------------------------------------------------------------------------- administration


def situation(now: Optional[datetime] = None) -> dict:
    """Demandes en attente actuellement relançables, prochaine relance de chacune, regroupées par validateur."""
    now = now or local_now()
    today = now.date()
    engine, m = _context()
    if engine is None:
        raise ValueError("Le module de pointage n'est pas configuré.")
    try:
        with SessionLocal() as db:
            s = get_settings(db)
            reqs = pending_requests(engine, m, s, today - timedelta(days=s.past_days or 0),
                                    today + timedelta(days=s.horizon_days or 0), today=today)
            due = {r["key"] for r in due_items(db, s, reqs, today)}
            for r in reqs:
                r["due"] = r["key"] in due
            return {"requests": reqs, "groups": group_by_validator(reqs), "stale": stale_reason(db, m, s),
                    "active": list(active_sources(s, m)),
                    "tables": {k: getattr(m, f"{k}_table", "") for k in APPROVAL_SOURCES}}
    finally:
        engine.dispose()


def table_columns(prefix: str) -> list[str]:
    """Colonnes de la table des demandes d'une source (choix des colonnes validateur, étape, date de demande)."""
    engine, m = _context()
    if engine is None:
        return []
    try:
        table = getattr(m, f"{prefix}_table", "")
        return list(pointage.column_types(engine, m.schema, table)) if table else []
    finally:
        engine.dispose()


def preview(validator_key: str) -> tuple[str, str]:
    from .mails import get_settings as mail_settings_of

    state = situation()
    items = state["groups"].get(validator_key)
    if not items:
        items = [sample_request()]
    with SessionLocal() as db:
        s, ms = get_settings(db), mail_settings_of(db)
        subject, body, _ = render(items[0]["validator"], items, s, ms.company)
        return subject, body


def sample_request() -> dict:
    today = local_now().date()
    return {"emp_key": "exemple", "matricule": "E000", "nom": "Exemple", "prenom": "Camille", "service": "Service exemple",
            "du": today - timedelta(days=2), "au": today + timedelta(days=2), "etat": "En Validation", "etape": "N+1",
            "demande_le": today - timedelta(days=5), "nature": "CONGE", "source": "leave", "label": "Congé (CONGE)",
            "key": "exemple", "jours": 5, "absents": 2, "attente": 5,
            "validator": {"matricule": "V000", "nom": "Validateur Exemple", "prenom": "Dominique",
                          "email": "validateur@exemple.com",
                          "found": True}}


def send_test(validator_key: str, to: str) -> str:
    from .mails import _quit, get_settings as mail_settings_of, open_smtp

    state = situation()
    items = state["groups"].get(validator_key) or [sample_request()]
    with SessionLocal() as db:
        s, ms = get_settings(db), mail_settings_of(db)
        msg, _ = build(items[0]["validator"], items, s, ms, force_to=to)
        client = open_smtp(ms)
        try:
            client.send_message(msg)
        finally:
            _quit(client)
        return msg["To"]
