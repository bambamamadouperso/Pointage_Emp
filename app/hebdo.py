"""Analyse RH « Temps hebdomadaire » : temps travaillé par employé et par semaine (lundi → dimanche) comparé à
l'objectif hebdomadaire (paramètre historisé « objectif_hebdo », 40h par défaut).

Temps travaillé d'une journée = durée effective du suivi : du 1er au dernier pointage, pause déduite, plus les
durées attribuées (congé, télétravail, arrêt maladie, mission… selon le paramètre « Durées attribuées
automatiquement »), comme dans le suivi journalier.
"""
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from . import pointage

WEEK_CHOICES = (1, 2, 4, 8, 13)
SORTS = {"ecart": "Écart à l'objectif (plus grand déficit d'abord)", "moyenne": "Moyenne hebdomadaire (décroissante)",
         "nom": "Nom", "service": "Service"}


def monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def minutes(value: str) -> int:
    try:
        h, m = (value or "").split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        return 40 * 60


def fmt(mins: Optional[float]) -> str:
    if mins is None:
        return "—"
    sign = "-" if mins < 0 else ""
    mins = int(round(abs(mins)))
    return f"{sign}{mins // 60}h{mins % 60:02d}"


def weekly(engine: Engine, m: pointage.Mapping, f: pointage.Filters) -> dict:
    """Semaines de la période, objectif de chaque semaine et, par employé, minutes travaillées par semaine."""
    S = pointage.qi(m.objs)
    weeks = []
    day = monday(f.du)
    while day <= f.au:
        weeks.append(day)
        day += timedelta(days=7)
    targets = {w: minutes(pointage.params_at(engine, m, w).get("objectif_hebdo", "40:00")) for w in weeks}
    where, params, binds = pointage._where(f, S)
    sql = text(f"""SELECT emp_key, max(matricule) AS matricule, max(nom) AS nom, max(prenom) AS prenom,
            max(direction) AS direction, max(service) AS service, max(categorie) AS categorie,
            date_trunc('week', jour)::date AS semaine,
            COALESCE(sum(extract(epoch FROM duree_effective)) / 60, 0) AS minutes,
            count(*) FILTER (WHERE duree_effective IS NOT NULL) AS jours
        FROM (SELECT * FROM {S}.f_pointage_journalier(:du, :au)) t{where}
        GROUP BY emp_key, date_trunc('week', jour)""").bindparams(*binds)
    employees: dict[str, dict] = {}
    with engine.connect() as c:
        for r in c.execute(sql, params).mappings():
            e = employees.setdefault(r["emp_key"], {
                "emp_key": r["emp_key"], "matricule": r["matricule"],
                "nom": " ".join(filter(None, [r["nom"], r["prenom"]])), "direction": r["direction"],
                "service": r["service"], "categorie": r["categorie"], "weeks": {}, "jours": 0})
            e["weeks"][r["semaine"]] = float(r["minutes"])
            e["jours"] += r["jours"]
    rows = []
    for e in employees.values():
        total = sum(e["weeks"].get(w, 0.0) for w in weeks)
        target = sum(targets[w] for w in weeks)
        e["total"], e["objectif"] = total, target
        e["moyenne"] = total / len(weeks) if weeks else 0
        e["ecart"] = (total - target) / len(weeks) if weeks else 0   # écart moyen par semaine
        e["taux"] = total * 100.0 / target if target else None
        e["sous"] = sum(1 for w in weeks if e["weeks"].get(w, 0.0) < targets[w])
        rows.append(e)
    return {"weeks": weeks, "targets": targets, "rows": rows}


def sort_rows(rows: list[dict], key: str) -> list[dict]:
    keys = {"ecart": lambda e: (e["ecart"], e["nom"].lower()),
            "moyenne": lambda e: (-e["moyenne"], e["nom"].lower()),
            "nom": lambda e: (e["nom"].lower(),),
            "service": lambda e: ((e["service"] or "").lower(), e["nom"].lower())}
    return sorted(rows, key=keys.get(key, keys["ecart"]))


def summary(data: dict) -> dict:
    rows = data["rows"]
    at = sum(1 for e in rows if e["ecart"] >= 0)
    return {"employes": len(rows), "a_objectif": at, "sous_objectif": len(rows) - at,
            "moyenne": sum(e["moyenne"] for e in rows) / len(rows) if rows else None,
            "objectif": sum(data["targets"].values()) / len(data["targets"]) if data["targets"] else None}
