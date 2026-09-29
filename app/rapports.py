"""Rapports d'analyse des pointages : indicateurs de la période, tendances, habitudes, services, employés, alertes.

Les journées de la période sont calculées une seule fois par PostgreSQL (f_pointage_journalier) dans une table
temporaire, puis agrégées : tous les chiffres d'une page portent exactement sur les mêmes données.
Seuls les employés de la liste sont analysés (les badges « hors liste » n'ont ni service ni responsable).
"""
from dataclasses import replace
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from . import pointage
from .pointage import Filters, Mapping, qi

# Seuils des alertes (sur la période analysée).
ALERT_ABSENCES = 3
ALERT_RETARDS = 3
ALERT_INCOMPLETS = 3
ALERT_LATE_MINUTES = 120

PRESETS = [
    ("7j", "7 derniers jours"), ("30j", "30 derniers jours"), ("mois", "Mois en cours"),
    ("mois-1", "Mois précédent"), ("90j", "3 derniers mois"), ("annee", "Année en cours"),
]

# Ordre et couleurs des statuts dans les graphiques (palette validée : écarts suffisants, y compris en daltonisme).
SERIES = [
    ("A_L_HEURE", "À l'heure", "#10b981"),
    ("RETARD", "En retard", "#f97316"),
    ("INCOMPLET", "Pointage incomplet", "#94a3b8"),
    ("TERRAIN", "Sur le terrain", "#3b82f6"),
    ("TELETRAVAIL", "Télétravail", "#14b8a6"),
    ("CONGE", "Congé", "#84cc16"),
    ("ARRET_MALADIE", "Arrêt maladie", "#78716c"),
    ("ABSENT", "Absent", "#e11d48"),
]
_WEEKDAYS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

# Journée « attendue » : jour ouvré ; « disponible » : attendue hors congé ; « présent » : au travail sous une forme ou une autre.
_ATTENDU = "jour_ouvre AND statut <> 'NON_OUVRE'"
_DISPO = f"{_ATTENDU} AND statut NOT IN ('CONGE_ANNUEL', 'CONGE_EXCEP', 'ARRET_MALADIE')"
_PRESENT = "statut IN ('A_L_HEURE', 'RETARD', 'INCOMPLET', 'TERRAIN', 'TELETRAVAIL')"
_BUREAU = "statut IN ('A_L_HEURE', 'RETARD')"
# Les oublis de badge ne sont comptés que les jours terminés (aujourd'hui, le départ n'a pas encore eu lieu).


def period(preset: str, today: Optional[date] = None) -> tuple[date, date]:
    today = today or date.today()
    if preset == "7j":
        return today - timedelta(days=6), today
    if preset == "mois":
        return today.replace(day=1), today
    if preset == "mois-1":
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last
    if preset == "90j":
        return today - timedelta(days=89), today
    if preset == "annee":
        return today.replace(month=1, day=1), today
    return today - timedelta(days=29), today


def _kpi_sql() -> str:
    return f"""SELECT
        count(DISTINCT emp_key) AS employes,
        count(*) FILTER (WHERE {_ATTENDU}) AS attendus,
        count(*) FILTER (WHERE {_DISPO}) AS disponibles,
        count(*) FILTER (WHERE {_DISPO} AND {_PRESENT}) AS presents,
        count(*) FILTER (WHERE statut = 'A_L_HEURE') AS a_l_heure,
        count(*) FILTER (WHERE statut = 'RETARD') AS retards,
        count(*) FILTER (WHERE statut = 'ABSENT') AS absences,
        count(*) FILTER (WHERE statut = 'INCOMPLET' AND jour < current_date) AS incomplets,
        count(*) FILTER (WHERE statut IN ('CONGE_ANNUEL', 'CONGE_EXCEP')) AS conges,
        count(*) FILTER (WHERE statut = 'TELETRAVAIL') AS teletravail,
        count(*) FILTER (WHERE statut = 'TERRAIN') AS terrain,
        count(*) FILTER (WHERE statut = 'ARRET_MALADIE') AS maladies,
        COALESCE(sum(retard_min), 0) AS retard_min_total,
        avg(retard_min) AS retard_min_moyen,
        sum(duree_validee_min) / 60.0 AS heures_validees,
        avg(duree_validee_min) FILTER (WHERE {_BUREAU}) / 60.0 AS heures_moy_bureau
        FROM r"""


def _rates(d: dict) -> dict:
    """Taux de présence, d'absentéisme et de ponctualité (None si rien à mesurer)."""
    dispo = d.get("disponibles") or 0
    ponct = (d.get("a_l_heure") or 0) + (d.get("retards") or 0)
    d["taux_presence"] = d["presents"] * 100.0 / dispo if dispo else None
    d["taux_absence"] = d["absences"] * 100.0 / dispo if dispo else None
    d["taux_ponctualite"] = d["a_l_heure"] * 100.0 / ponct if ponct else None
    return d


def _load(c, S: str, f: Filters) -> None:
    where, params, binds = pointage._where(replace(f, population="liste", statuts=[], q=f.q), S)
    c.execute(text("DROP TABLE IF EXISTS pg_temp.r"))
    c.execute(text(f"CREATE TEMP TABLE r ON COMMIT DROP AS SELECT * FROM "
                   f"(SELECT * FROM {S}.f_pointage_journalier(:du, :au)) t{where}").bindparams(*binds), params)


def _group(c, expr: str) -> list[dict]:
    """Indicateurs par groupe (service, catégorie…), du plus fort au plus faible absentéisme."""
    rows = [_rates(dict(x)) for x in c.execute(text(f"""
        SELECT {expr} AS groupe, count(DISTINCT emp_key) AS employes,
            count(*) FILTER (WHERE {_DISPO}) AS disponibles,
            count(*) FILTER (WHERE {_DISPO} AND {_PRESENT}) AS presents,
            count(*) FILTER (WHERE statut = 'A_L_HEURE') AS a_l_heure,
            count(*) FILTER (WHERE statut = 'RETARD') AS retards,
            count(*) FILTER (WHERE statut = 'ABSENT') AS absences,
            count(*) FILTER (WHERE statut = 'INCOMPLET' AND jour < current_date) AS incomplets,
            COALESCE(sum(retard_min), 0) AS retard_min_total,
            avg(duree_validee_min) FILTER (WHERE {_BUREAU}) / 60.0 AS heures_moy_bureau,
            sum(duree_validee_min) / 60.0 AS heures_validees
        FROM r GROUP BY 1""")).mappings()]
    for row in rows:
        row["service"] = row["groupe"]  # compatibilité des gabarits et de l'export
    rows.sort(key=lambda g: (-(g["taux_absence"] or 0), g["groupe"]))
    return rows


def build(engine: Engine, m: Mapping, f: Filters, sort: str = "absences", compare: bool = True) -> dict:
    S = qi(m.objs)
    has_categories = bool(m.cat_col)
    out: dict = {}
    with engine.begin() as c:
        _load(c, S, f)
        out["kpi"] = _rates(dict(c.execute(text(_kpi_sql())).mappings().one()))

        weekly = (f.au - f.du).days > 62
        bucket = "date_trunc('week', jour)::date" if weekly else "jour"
        out["granularite"] = "semaine" if weekly else "jour"
        out["jours"] = [dict(x) for x in c.execute(text(f"""
            SELECT {bucket} AS jour,
                count(*) FILTER (WHERE statut = 'A_L_HEURE') AS "A_L_HEURE",
                count(*) FILTER (WHERE statut = 'RETARD') AS "RETARD",
                count(*) FILTER (WHERE statut = 'INCOMPLET') AS "INCOMPLET",
                count(*) FILTER (WHERE statut = 'TERRAIN') AS "TERRAIN",
                count(*) FILTER (WHERE statut = 'TELETRAVAIL') AS "TELETRAVAIL",
                count(*) FILTER (WHERE statut IN ('CONGE_ANNUEL', 'CONGE_EXCEP')) AS "CONGE",
                count(*) FILTER (WHERE statut = 'ARRET_MALADIE') AS "ARRET_MALADIE",
                count(*) FILTER (WHERE statut = 'ABSENT') AS "ABSENT",
                bool_or(jour_ouvre) AS ouvre
            FROM r GROUP BY 1 ORDER BY 1""")).mappings()]

        out["semaine"] = [dict(x) for x in c.execute(text(f"""
            SELECT extract(isodow FROM jour)::int AS dow,
                count(*) FILTER (WHERE {_DISPO}) AS disponibles,
                count(*) FILTER (WHERE statut = 'ABSENT') AS absences,
                count(*) FILTER (WHERE statut = 'RETARD') AS retards,
                count(*) FILTER (WHERE statut = 'A_L_HEURE') AS a_l_heure
            FROM r WHERE {_ATTENDU} GROUP BY 1 ORDER BY 1""")).mappings()]
        for w in out["semaine"]:
            w["jour"] = _WEEKDAYS[w["dow"] - 1]
            ponct = w["retards"] + w["a_l_heure"]
            w["taux_retard"] = w["retards"] * 100.0 / ponct if ponct else None
            w["taux_absence"] = w["absences"] * 100.0 / w["disponibles"] if w["disponibles"] else None

        # Heures d'arrivée au bureau, par quart d'heure.
        out["arrivees"] = [dict(x) for x in c.execute(text(f"""
            SELECT (extract(hour FROM premier_pointage)::int * 60
                    + (extract(minute FROM premier_pointage)::int / 15) * 15) AS minute, count(*) AS n
            FROM r WHERE {_BUREAU} GROUP BY 1 ORDER BY 1""")).mappings()]

        out["services"] = _group(c, "COALESCE(service, '(sans service)')")
        out["categories"] = _group(c, "COALESCE(categorie, '(non renseignée)')") if has_categories else []

        employees = [_rates(dict(x)) for x in c.execute(text(f"""
            SELECT emp_key, max(matricule) AS matricule, max(nom) AS nom, max(prenom) AS prenom,
                max(service) AS service, max(categorie) AS categorie, max(responsable) AS responsable,
                bool_or(terrain) AS terrain,
                count(*) FILTER (WHERE {_ATTENDU}) AS attendus,
                count(*) FILTER (WHERE {_DISPO}) AS disponibles,
                count(*) FILTER (WHERE {_DISPO} AND {_PRESENT}) AS presents,
                count(*) FILTER (WHERE statut = 'A_L_HEURE') AS a_l_heure,
                count(*) FILTER (WHERE statut = 'RETARD') AS retards,
                COALESCE(sum(retard_min), 0) AS retard_min_total,
                count(*) FILTER (WHERE statut = 'ABSENT') AS absences,
                count(*) FILTER (WHERE statut = 'ABSENT' AND extract(isodow FROM jour) IN (1, 5)) AS absences_lun_ven,
                count(*) FILTER (WHERE statut = 'INCOMPLET' AND jour < current_date) AS incomplets,
                count(*) FILTER (WHERE statut IN ('CONGE_ANNUEL', 'CONGE_EXCEP')) AS conges,
                count(*) FILTER (WHERE statut = 'TELETRAVAIL') AS teletravail,
                count(*) FILTER (WHERE statut = 'TERRAIN') AS terrain_jours,
                count(*) FILTER (WHERE statut = 'ARRET_MALADIE') AS maladies,
                sum(duree_validee_min) / 60.0 AS heures_validees,
                avg(extract(epoch FROM premier_pointage::time) / 60) FILTER (WHERE {_BUREAU}) AS arrivee_moy_min
            FROM r GROUP BY emp_key""")).mappings()]

    keys = {
        "absences": lambda e: (-e["absences"], -e["retards"]), "retards": lambda e: (-e["retards"], -e["retard_min_total"]),
        "retard_min": lambda e: (-e["retard_min_total"], -e["retards"]), "incomplets": lambda e: (-e["incomplets"],),
        "presence": lambda e: (e["taux_presence"] if e["taux_presence"] is not None else 999,),
        "heures": lambda e: (-(e["heures_validees"] or 0),), "nom": lambda e: ((e["nom"] or "").lower(),),
    }
    employees.sort(key=lambda e: (*keys.get(sort, keys["absences"])(e), (e["nom"] or "").lower()))
    out["employes"] = employees
    out["alertes"] = alerts(employees)
    if compare and (f.au - f.du).days <= 120:
        length = (f.au - f.du).days + 1
        prev = replace(f, du=f.du - timedelta(days=length), au=f.du - timedelta(days=1))
        with engine.begin() as c:
            _load(c, S, prev)
            first = c.execute(text("SELECT min(jour) FROM r WHERE nb_pointages > 0")).scalar()
            # Comparaison seulement si les pointages couvrent la période précédente (sinon tout y paraît « absent »).
            if first is not None and first <= prev.du + timedelta(days=3):
                out["precedent"] = _rates(dict(c.execute(text(_kpi_sql())).mappings().one()))
                out["precedent_du"], out["precedent_au"] = prev.du, prev.au
    return out


def alerts(employees: list[dict]) -> list[dict]:
    """Situations à examiner avec le manager (une ligne par employé et par motif)."""
    out = []
    for e in employees:
        who = {"emp_key": e["emp_key"], "matricule": e["matricule"], "nom": f"{e['nom'] or ''} {e['prenom'] or ''}".strip(),
               "service": e["service"], "responsable": e["responsable"]}
        if e["absences"] >= ALERT_ABSENCES:
            out.append({**who, "gravite": 3, "motif": "Absences répétées",
                        "detail": f"{e['absences']} jour(s) d'absence sans congé ni télétravail"})
        if e["absences"] >= 2 and e["absences_lun_ven"] * 100 >= e["absences"] * 70:
            out.append({**who, "gravite": 2, "motif": "Absences en début ou fin de semaine",
                        "detail": f"{e['absences_lun_ven']} absence(s) sur {e['absences']} un lundi ou un vendredi"})
        if e["retards"] >= ALERT_RETARDS or e["retard_min_total"] >= ALERT_LATE_MINUTES:
            out.append({**who, "gravite": 2, "motif": "Retards fréquents",
                        "detail": f"{e['retards']} retard(s), {pointage.hhmm(timedelta(minutes=float(e['retard_min_total'])))} cumulées"})
        if e["incomplets"] >= ALERT_INCOMPLETS:
            out.append({**who, "gravite": 1, "motif": "Oublis de badge",
                        "detail": f"{e['incomplets']} journée(s) avec un seul pointage (arrivée ou départ manquant)"})
    out.sort(key=lambda a: (-a["gravite"], a["motif"], a["nom"]))
    return out


# --------------------------------------------------------------------------- géométrie des graphiques (SVG)


def trend_chart(rows: list[dict], width: int = 960, height: int = 260) -> dict:
    """Colonnes empilées par jour (ou semaine) : une colonne = répartition des journées-employé par statut."""
    left, right, top, bottom = 44, 8, 12, 28
    plot_w, plot_h = width - left - right, height - top - bottom
    total_max = max((sum(r[k] for k, _, _ in SERIES) for r in rows), default=0) or 1
    step = _nice_step(total_max)
    y_max = ((total_max + step - 1) // step) * step
    n = max(len(rows), 1)
    slot = plot_w / n
    bar = min(24.0, max(slot - 4, 2.0))
    cols = []
    for i, r in enumerate(rows):
        x = left + i * slot + (slot - bar) / 2
        y_base = top + plot_h
        segs = []
        present = [(k, label, color, r[k]) for k, label, color in SERIES if r[k]]
        for j, (k, label, color, v) in enumerate(present):
            h = v / y_max * plot_h
            gap = 2 if j < len(present) - 1 else 0
            y = y_base - h
            segs.append({"key": k, "label": label, "color": color, "value": v, "x": round(x, 1), "y": round(y, 1),
                         "w": round(bar, 1), "h": round(max(h - gap, 0.5), 1), "top": j == len(present) - 1})
            y_base = y
        cols.append({"row": r, "x": round(x, 1), "cx": round(x + bar / 2, 1), "segs": segs, "slot_x": round(left + i * slot, 1),
                     "slot_w": round(slot, 1), "total": sum(r[k] for k, _, _ in SERIES)})
    ticks = [{"v": v, "y": round(top + plot_h - v / y_max * plot_h, 1)} for v in range(0, y_max + 1, step)]
    every = max(1, round(n / 12))
    labels = [c for i, c in enumerate(cols) if i % every == 0]
    return {"w": width, "h": height, "left": left, "right": width - right, "top": top, "base": top + plot_h,
            "cols": cols, "ticks": ticks, "labels": labels}


def arrival_chart(rows: list[dict], seuil: str, debut: str, width: int = 520, height: int = 250) -> dict:
    """Histogramme des heures d'arrivée au bureau (par quart d'heure), coloré avant / après le seuil de retard."""
    seuil_min = _minutes(seuil)
    debut_min = _minutes(debut)
    counts = {r["minute"]: r["n"] for r in rows}
    lo = min([debut_min - 90, *[m for m in counts if counts[m]]] or [debut_min - 90])
    hi = max([seuil_min + 120, *[m for m in counts if counts[m]]] or [seuil_min + 120])
    lo, hi = max(lo - lo % 15, 0), min(hi - hi % 15 + 15, 24 * 60)
    buckets = list(range(lo, hi, 15))
    left, right, top, bottom = 44, 8, 18, 28
    plot_w, plot_h = width - left - right, height - top - bottom
    vmax = max((counts.get(b, 0) for b in buckets), default=0) or 1
    step = _nice_step(vmax)
    y_max = ((vmax + step - 1) // step) * step
    slot = plot_w / max(len(buckets), 1)
    bar = min(24.0, slot - 2)
    bars = []
    for i, b in enumerate(buckets):
        v = counts.get(b, 0)
        h = v / y_max * plot_h
        late = b >= seuil_min
        bars.append({"x": round(left + i * slot + (slot - bar) / 2, 1), "w": round(bar, 1), "y": round(top + plot_h - h, 1),
                     "h": round(h, 1), "v": v, "label": f"{b // 60:02d}h{b % 60:02d}–{(b + 15) // 60:02d}h{(b + 15) % 60:02d}",
                     "color": "#f97316" if late else "#10b981", "late": late,
                     "slot_x": round(left + i * slot, 1), "slot_w": round(slot, 1)})

    def x_of(minute: int) -> float:
        return round(left + (minute - lo) / 15 * slot, 1)

    ticks = [{"v": v, "y": round(top + plot_h - v / y_max * plot_h, 1)} for v in range(0, y_max + 1, step)]
    hours = [{"x": x_of(mm), "label": f"{mm // 60}h"} for mm in range(lo, hi + 1, 60) if mm % 60 == 0]
    return {"w": width, "h": height, "left": left, "right": width - right, "top": top, "base": top + plot_h,
            "bars": bars, "ticks": ticks, "hours": hours, "seuil_x": x_of(seuil_min), "debut_x": x_of(debut_min),
            "seuil": seuil, "debut": debut, "total": sum(counts.values())}


def _minutes(hhmm: str) -> int:
    h, m = (hhmm or "00:00").split(":")[:2]
    return int(h) * 60 + int(m)


def _nice_step(vmax: float) -> int:
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000, 50000):
        if vmax / step <= 5:
            return step
    return 100000
