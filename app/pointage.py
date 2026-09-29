"""Module de pointage : calculs dans PostgreSQL (fonction + vue v_pointage_journalier) et requêtes de l'écran.

Les calculs sont faits par PostgreSQL pour que l'application, les exports et Power BI donnent exactement
les mêmes résultats. Les paramètres horaires sont historisés (valeur + date d'effet) dans la table
pointage_parametres : chaque jour est calculé avec les paramètres en vigueur ce jour-là.
"""
import json
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, time, timedelta
from typing import Any, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

# --------------------------------------------------------------------------- paramètres et statuts

# clé, libellé, type, valeur par défaut
PARAMS = [
    ("debut_journee", "Début de journée", "time", "07:30"),
    ("debut_pause", "Début de pause", "time", "13:00"),
    ("fin_pause", "Fin de pause", "time", "14:00"),
    ("fin_journee", "Fin de journée", "time", "16:30"),
    ("seuil_retard", "Seuil de retard (retard si arrivée ≥)", "time", "07:45"),
    ("duree_pause_deduite", "Durée de pause déduite", "duration", "01:30"),
    ("jours_ouvres", "Jours ouvrés", "days", "1,2,3,4,5"),
    ("duree_conge", "Durée attribuée par jour de congé", "duration", "08:00"),
    ("duree_teletravail", "Durée attribuée par jour de télétravail", "duration", "08:00"),
    ("duree_terrain", "Durée validée minimale par jour pour un agent terrain", "duration", "08:00"),
    ("objectif_duree", "Objectif de durée validée (vert si atteint, rouge sinon)", "duration", "08:00"),
    ("duree_arret_maladie", "Durée attribuée par jour d'arrêt maladie", "duration", "08:00"),
]
PARAM_LABELS = {k: label for k, label, _, _ in PARAMS}
PARAM_TYPES = {k: kind for k, _, kind, _ in PARAMS}
PARAM_DEFAULTS = {k: default for k, _, _, default in PARAMS}
WEEKDAYS = [(1, "Lundi"), (2, "Mardi"), (3, "Mercredi"), (4, "Jeudi"), (5, "Vendredi"), (6, "Samedi"), (7, "Dimanche")]

# code, libellé, classe CSS (vert, orange, rouge, gris)
STATUTS = {
    "A_L_HEURE": ("À l'heure", "st-ok"),
    "RETARD": ("En retard", "st-late"),
    "ABSENT": ("Absent", "st-abs"),
    "INCOMPLET": ("Pointage incomplet", "st-inc"),
    "NON_OUVRE": ("Jour non ouvré", "st-off"),
    "CONGE_ANNUEL": ("Congé Annuel", "st-leave"),
    "CONGE_EXCEP": ("Congé exceptionnel", "st-leave"),
    "TELETRAVAIL": ("Télétravail", "st-remote"),
    "TERRAIN": ("Sur le terrain", "st-field"),
    "ARRET_MALADIE": ("Arrêt maladie", "st-sick"),
}
CONGES = ("CONGE_ANNUEL", "CONGE_EXCEP")
# Version des objets PostgreSQL : si elle change, ils sont réinstallés automatiquement.
SQL_VERSION = 13
# Limites des requêtes lancées depuis les pages web : une attente de verrou ou une requête lente ne doit jamais
# bloquer le site (au pire, la page affiche une erreur au bout de 2 minutes).
WEB_LIMITS = {"lock_timeout_s": 15, "statement_timeout_s": 120}

SORTABLE = {
    "jour": "jour", "matricule": "matricule", "nom": "nom", "service": "service", "responsable": "responsable",
    "premier": "premier_pointage", "dernier": "dernier_pointage", "statut": "statut",
    "validee": "duree_validee", "effective": "duree_effective",
}


class PointageError(Exception):
    pass


def validate_param(key: str, raw: str) -> str:
    """Normalise une valeur saisie (HH:MM, durée HH:MM, jours « 1,2,3 ») ou lève PointageError."""
    kind = PARAM_TYPES[key]
    raw = (raw or "").strip()
    if kind == "days":
        days = sorted({int(d) for d in re.split(r"[,\s]+", raw) if d.isdigit() and 1 <= int(d) <= 7})
        if not days:
            raise PointageError("Choisissez au moins un jour ouvré.")
        return ",".join(str(d) for d in days)
    m = re.fullmatch(r"(\d{1,2})\s*[:hH]\s*(\d{1,2})?", raw)
    if not m:
        raise PointageError(f"{PARAM_LABELS[key]} : format attendu HH:MM (ex. 07:30).")
    hours, minutes = int(m.group(1)), int(m.group(2) or 0)
    if minutes > 59 or (kind == "time" and hours > 23) or hours > 23:
        raise PointageError(f"{PARAM_LABELS[key]} : valeur invalide ({raw}).")
    return f"{hours:02d}:{minutes:02d}"


def check_consistency(values: dict) -> Optional[str]:
    t = {k: values[k] for k in values if PARAM_TYPES.get(k) == "time"}
    if not (t["debut_journee"] <= t["seuil_retard"]):
        return "Le seuil de retard doit être postérieur ou égal au début de journée."
    if not (t["debut_journee"] < t["debut_pause"] < t["fin_pause"] < t["fin_journee"]):
        return "Ordre attendu : début de journée < début de pause < fin de pause < fin de journée."
    return None


# --------------------------------------------------------------------------- correspondance des tables


@dataclass
class Mapping:
    """Où trouver les pointages et les employés dans PostgreSQL (tables copiées par les jobs)."""

    schema: str = "public"            # schéma des tables de pointage et d'employés
    objects_schema: str = ""          # schéma de la fonction, de la vue et des paramètres (vide = schema)
    punch_table: str = ""             # ex. punchlog
    punch_emp_col: str = ""           # identifiant de l'employé dans les pointages
    punch_ts_col: str = ""            # date/heure du pointage (ou date seule si punch_time_col)
    punch_time_col: str = ""          # heure du pointage si elle est dans une colonne séparée
    emp_table: str = ""               # table des employés
    emp_key_col: str = ""             # colonne qui correspond à punch_emp_col
    emp_matricule_col: str = ""       # matricule affiché (vide = emp_key_col)
    emp_nom_col: str = ""
    emp_prenom_col: str = ""
    emp_service_col: str = ""
    service_table: str = ""           # facultatif : table des services (si emp_service_col est un identifiant)
    service_key_col: str = ""
    service_label_col: str = ""
    emp_active_col: str = ""          # facultatif : seuls les employés actifs peuvent être « absents »
    emp_active_values: str = ""       # valeurs considérées comme actives (séparées par des virgules)
    # Facultatif : nom et prénom dans une autre table (ex. Personnel), reliée à la table des employés.
    person_table: str = ""
    emp_person_col: str = ""          # colonne de la table des employés qui pointe vers person_table
    person_key_col: str = ""          # identifiant dans person_table
    person_nom_col: str = ""
    person_prenom_col: str = ""
    # Facultatif : catégorie du personnel (cadre, non cadre…), dans la table des noms ou celle des employés,
    # avec une table de libellés si la colonne contient un code.
    cat_col: str = ""
    cat_in: str = "person"            # « person » : table des noms (ex. Personnel) ; « emp » : table des employés
    cat_table: str = ""
    cat_key_col: str = ""
    cat_label_col: str = ""
    # Facultatif : adresse e-mail des employés (mails de confirmation de badge).
    email_col: str = ""
    email_in: str = "person"          # « person » : table des noms ; « emp » : table des employés
    # Facultatif : hiérarchie (responsable N+1), dans la table des employés ou une table dédiée.
    hier_table: str = ""
    hier_emp_col: str = ""            # l'employé
    hier_manager_col: str = ""        # son responsable
    hier_ref: str = "key"             # ces deux colonnes contiennent la clé employé (« key ») ou le matricule
    # Liste de référence des personnes attendues chaque jour : table des employés (« emp ») ou table des noms
    # (« person », ex. Personnel : tout le personnel est attendu, même sans fiche dans la table des employés).
    reference: str = "emp"
    # Facultatif : demandes de congé. Un jour « Absent » couvert par un congé approuvé devient « Congé Annuel »
    # ou « Congé exceptionnel » (durées validée et effective = paramètre « durée attribuée par jour de congé »).
    leave_table: str = ""
    leave_emp_col: str = ""           # l'employé concerné
    leave_ref: str = "key"            # cette colonne contient la clé employé, le matricule ou l'id de la table des noms
    leave_start_col: str = ""         # ex. datedebut
    leave_end_col: str = ""           # ex. Datefin
    leave_state_col: str = ""         # ex. etat (vide = toutes les demandes)
    leave_state_values: str = "Approuvée"
    leave_type_col: str = ""          # ex. Document (vide = tout est congé annuel)
    leave_annual_values: str = "CONGE"
    leave_excep_values: str = "CONGE EXCEP"
    # Facultatif : demandes de télétravail (même principe ; un congé l'emporte sur un télétravail le même jour).
    tw_table: str = ""                # ex. TdemandedeTeleTravail
    tw_emp_col: str = ""
    tw_ref: str = "key"
    tw_start_col: str = ""
    tw_end_col: str = ""
    tw_state_col: str = ""
    tw_state_values: str = "Approuvée"

    def __post_init__(self) -> None:
        # Configuration enregistrée avant l'ajout des congés (ou champ laissé vide) : valeurs par défaut.
        for name in ("leave_state_values", "leave_annual_values", "leave_excep_values", "leave_ref",
                     "tw_state_values", "tw_ref", "cat_in", "email_in"):
            if not getattr(self, name):
                setattr(self, name, type(self).__dataclass_fields__[name].default)

    @classmethod
    def from_json(cls, raw: Optional[str]) -> "Mapping":
        data = json.loads(raw or "{}")
        names = {f.name for f in fields(cls)}
        return cls(**{k: str(v or "").strip() for k, v in data.items() if k in names})

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @property
    def objs(self) -> str:
        return self.objects_schema or self.schema

    def missing(self) -> list[str]:
        required = {
            "punch_table": "table des pointages", "punch_emp_col": "colonne employé des pointages",
            "punch_ts_col": "colonne date/heure des pointages", "emp_table": "table des employés",
            "emp_key_col": "colonne de correspondance des employés",
        }
        out = [label for key, label in required.items() if not getattr(self, key)]
        if self.person_table:
            if not (self.emp_person_col and self.person_key_col and self.person_nom_col):
                out.append("colonnes de liaison et du nom de la table des noms")
        elif not self.emp_nom_col:
            out.append("colonne du nom (ou table des noms)")
        if self.reference == "person" and not self.person_table:
            out.append("table des noms (liste de référence choisie : table des noms)")
        if self.service_table and not (self.service_key_col and self.service_label_col):
            out.append("colonnes de la table des services")
        if self.cat_table and not (self.cat_col and self.cat_key_col and self.cat_label_col):
            out.append("colonnes de la table des catégories")
        if self.hier_table and not (self.hier_emp_col and self.hier_manager_col):
            out.append("colonnes employé et responsable de la hiérarchie")
        if self.leave_table and not (self.leave_emp_col and self.leave_start_col and self.leave_end_col):
            out.append("colonnes employé, date de début et date de fin des congés")
        if self.leave_table and self.leave_ref == "person" and not self.person_table:
            out.append("table des noms (les congés désignent les personnes)")
        if self.tw_table and not (self.tw_emp_col and self.tw_start_col and self.tw_end_col):
            out.append("colonnes employé, date de début et date de fin du télétravail")
        if self.tw_table and self.tw_ref == "person" and not self.person_table:
            out.append("table des noms (le télétravail désigne les personnes)")
        return out


def qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def qt(schema: str, table: str) -> str:
    return f"{qi(schema)}.{qi(table)}"


def list_schemas(engine: Engine) -> list[str]:
    with engine.connect() as c:
        return list(c.execute(text(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'pg_toast') "
            "AND schema_name NOT LIKE 'pg_temp%' AND schema_name NOT LIKE 'pg_toast_temp%' ORDER BY 1")).scalars())


def list_tables(engine: Engine, schema: str) -> list[str]:
    with engine.connect() as c:
        return list(c.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = :s "
            "AND table_type IN ('BASE TABLE', 'VIEW') ORDER BY 1"), {"s": schema}).scalars())


def column_types(engine: Engine, schema: str, table: str) -> dict[str, str]:
    if not table:
        return {}
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = :t ORDER BY ordinal_position"), {"s": schema, "t": table})
        return {name: dtype for name, dtype in rows}


_GUESSES = {
    "punch_table": [r"^punch", r"pointage", r"badge", r"punch"],
    "emp_table": [r"^employ", r"^personnel$", r"^agent", r"salari"],
    "punch_emp_col": [r"^id_?employ", r"employ", r"matric", r"badge", r"^id_?pers", r"user"],
    "punch_ts_col": [r"date_?heure", r"datetime", r"horodat", r"timestamp", r"^date", r"date", r"heure"],
    "punch_time_col": [r"^heure", r"^time$"],
    "emp_key_col": [r"^id_?employ", r"^id$", r"matric"],
    "emp_matricule_col": [r"matric", r"^code", r"^id_?employ"],
    "emp_nom_col": [r"^nom$", r"^nom_", r"^lastname", r"^name$", r"nom"],
    "emp_prenom_col": [r"pr[ée]nom", r"firstname"],
    "emp_service_col": [r"service", r"d[ée]part", r"direction", r"^id_?serv"],
    "emp_person_col": [r"^id_?person", r"person", r"^id_?ident", r"^id_?agent"],
    "person_key_col": [r"^id_?person", r"^id$", r"person", r"^id"],
    "person_nom_col": [r"^nom$", r"^nom_", r"^lastname", r"^name$", r"nom"],
    "person_prenom_col": [r"pr[ée]nom", r"firstname"],
    "email_col": [r"^e?-?mail", r"courriel", r"mail"],
    "cat_col": [r"^statut$", r"^statut_?(salari|employ|pers|agent|cadre)", r"cat[ée]gor", r"coll[èe]ge", r"^cadre",
                r"qualif", r"^statut"],
    "cat_key_col": [r"^id", r"code", r"statut", r"cat[ée]gor"],
    "cat_label_col": [r"libell", r"^nom", r"label", r"d[ée]sign", r"intitul"],
    "hier_emp_col": [r"^id_?employ", r"employ", r"^id_?agent", r"collab", r"^id_?person", r"matric"],
    "leave_table": [r"demande_?cong", r"cong[ée]", r"absence", r"leave"],
    "tw_table": [r"t[ée]l[ée]_?travail", r"remote", r"home_?office"],
    "leave_emp_col": [r"^id_?employ", r"employ", r"matric", r"^id_?person", r"person", r"^id_?agent", r"agent"],
    "leave_start_col": [r"^date_?d[ée]but", r"d[ée]but", r"start"],
    "leave_end_col": [r"^date_?fin", r"fin", r"end"],
    "leave_state_col": [r"^[ée]tat", r"statut", r"state", r"valid"],
    "leave_type_col": [r"^document", r"nature", r"^type", r"motif"],
    "hier_manager_col": [r"respons", r"manager", r"sup[ée]rieur", r"^id_?chef", r"chef", r"n\+?1", r"valideur",
                         r"hi[ée]rarch"],
}


def guess(kind: str, names: list[str], exclude: tuple = ()) -> str:
    # Colonnes du télétravail : mêmes noms habituels que celles des congés (datedebut, Datefin, etat...).
    for pattern in _GUESSES.get(kind) or _GUESSES.get(kind.replace("tw_", "leave_", 1), []):
        for name in names:
            if name not in exclude and re.search(pattern, name, re.I):
                return name
    return ""


# --------------------------------------------------------------------------- génération du SQL

_TEXT = ("text", "character varying", "character", "varchar", "char")
_NUM = ("integer", "bigint", "smallint", "numeric", "double precision", "real")


_ACCENTS = ("àâäéèêëîïôöùûüç", "aaaeeeeiioouuuc")


def norm(value: str) -> str:
    """Valeur comparée sans casse, accents ni espaces superflus (« Approuvée » = « APPROUVEE »)."""
    table = str.maketrans(*_ACCENTS)
    return re.sub(r"\s+", " ", (value or "").strip().lower().translate(table))


def _norm_sql(expr: str) -> str:
    return f"regexp_replace(translate(lower(btrim({expr}::text)), {lit(_ACCENTS[0])}, {lit(_ACCENTS[1])}), '\\s+', ' ', 'g')"


def _values(raw: str) -> list[str]:
    return [norm(v) for v in (raw or "").split(",") if v.strip()]


def _as_text(expr: str, dtype: str) -> str:
    return f"btrim({expr}::text)" if dtype in _TEXT or dtype in _NUM else f"{expr}::text"


def _date_expr(expr: str, dtype: str) -> str:
    if dtype in _TEXT or dtype in _NUM:
        t = f"btrim({expr}::text)"
        return f"(CASE WHEN {t} ~ '^\\d{{8}}' THEN to_date(substr({t}, 1, 8), 'YYYYMMDD') ELSE nullif({t}, '')::date END)"
    return f"{expr}::date"


def _time_expr(expr: str, dtype: str) -> str:
    if dtype == "interval":
        return f"(time '00:00' + {expr})"
    if dtype in _TEXT or dtype in _NUM:
        t = f"btrim({expr}::text)"
        # HFSQL stocke souvent l'heure en « HHMMSS » ou « HHMMSSCC » (texte ou nombre).
        digits = f"lpad({t}, CASE WHEN length({t}) > 6 THEN 8 ELSE 6 END, '0')"
        return (f"(CASE WHEN {t} ~ '^\\d{{3,8}}$' THEN make_time(substr({digits}, 1, 2)::int, "
                f"substr({digits}, 3, 2)::int, substr({digits}, 5, 2)::int) ELSE {t}::time END)")
    return f"{expr}::time"


def _ts_expr(m: Mapping, types: dict[str, str]) -> str:
    col = f"p.{qi(m.punch_ts_col)}"
    dtype = types.get(m.punch_ts_col, "")
    if m.punch_time_col:
        tcol = f"p.{qi(m.punch_time_col)}"
        return f"({_date_expr(col, dtype)} + {_time_expr(tcol, types.get(m.punch_time_col, ''))})"
    if dtype in _TEXT:
        t = f"btrim({col}::text)"
        return (f"(CASE WHEN {t} ~ '^\\d{{14}}' THEN to_timestamp(substr({t}, 1, 14), 'YYYYMMDDHH24MISS')::timestamp "
                f"ELSE {t}::timestamp END)")
    return f"{col}::timestamp"


def _raw_day_col(m: Mapping, types: dict[str, str]) -> Optional[str]:
    """Colonne date/horodatage « native » des pointages : permet un filtre rapide (index) avant tout calcul."""
    if types.get(m.punch_ts_col, "") in ("date", "timestamp without time zone", "timestamp with time zone"):
        return f"p.{qi(m.punch_ts_col)}"
    return None


def _requests_cte(m: Mapping, prefix: str, types: dict[str, str], default_status: str) -> str:
    """Jours couverts par une demande approuvée (congé ou télétravail) de chaque employé sur la période."""
    table = getattr(m, f"{prefix}_table")
    if not table:
        return "SELECT NULL::text AS emp_key, NULL::date AS jour, NULL::text AS statut WHERE false"
    get = lambda name: getattr(m, f"{prefix}_{name}")  # noqa: E731
    col = lambda name: f"l.{qi(name)}"  # noqa: E731
    start = _date_expr(col(get("start_col")), types.get(get("start_col"), ""))
    end = f"COALESCE({_date_expr(col(get('end_col')), types.get(get('end_col'), ''))}, {start})"
    ref = {"matricule": "matricule", "person": "person_ref"}.get(get("ref"), "emp_key")
    who = f"btrim({col(get('emp_col'))}::text)"
    conds = [f"{start} <= p_au", f"{end} >= p_du"]
    if get("state_col") and _values(get("state_values")):
        conds.append(f"{_norm_sql(col(get('state_col')))} IN ({', '.join(lit(v) for v in _values(get('state_values')))})")
    kind = lit(default_status)
    if prefix == "leave" and m.leave_type_col:
        typ = _norm_sql(col(m.leave_type_col))
        excep = _values(m.leave_excep_values) or ["__aucun__"]
        annual = _values(m.leave_annual_values)
        conds.append(f"{typ} IN ({', '.join(lit(v) for v in annual + excep)})")
        kind = f"CASE WHEN {typ} IN ({', '.join(lit(v) for v in excep)}) THEN 'CONGE_EXCEP' ELSE 'CONGE_ANNUEL' END"
    return f"""SELECT DISTINCT ON (e.emp_key, d::date) e.emp_key, d::date AS jour, {kind} AS statut
    FROM {qt(m.schema, table)} l
    JOIN emp e ON e.{ref} = {who}
    CROSS JOIN LATERAL generate_series(GREATEST({start}, p_du)::timestamp, LEAST({end}, p_au)::timestamp,
                                       interval '1 day') AS d
    WHERE {' AND '.join(conds)}
    ORDER BY e.emp_key, d::date, 3"""


def build_sql(m: Mapping, punch_types: dict[str, str], emp_types: dict[str, str],
              leave_types: Optional[dict[str, str]] = None, tw_types: Optional[dict[str, str]] = None) -> dict[str, str]:
    """SQL des objets PostgreSQL du module (tables de paramètres, vue des employés, fonctions, vues)."""
    S = qi(m.objs)
    punch = qt(m.schema, m.punch_table)
    emp = qt(m.schema, m.emp_table)
    ts = _ts_expr(m, punch_types)
    pkey = _as_text(f"p.{qi(m.punch_emp_col)}", punch_types.get(m.punch_emp_col, ""))
    ekey = _as_text(f"e.{qi(m.emp_key_col)}", emp_types.get(m.emp_key_col, ""))
    mat = _as_text(f"e.{qi(m.emp_matricule_col or m.emp_key_col)}",
                   emp_types.get(m.emp_matricule_col or m.emp_key_col, ""))
    person_join, person_ref = "", "NULL::text"
    if m.person_table:
        # Nom et prénom dans une autre table (ex. Personnel), reliée par une colonne de la table des employés.
        person_join = (f"LEFT JOIN {qt(m.schema, m.person_table)} pe ON "
                       f"btrim(pe.{qi(m.person_key_col)}::text) = btrim(e.{qi(m.emp_person_col)}::text)")
        person_ref = f"btrim(e.{qi(m.emp_person_col)}::text)"
        nom = f"pe.{qi(m.person_nom_col)}::text"
        prenom = (f"pe.{qi(m.person_prenom_col)}::text" if m.person_prenom_col
                  else f"e.{qi(m.emp_prenom_col)}::text" if m.emp_prenom_col else "NULL::text")
    else:
        nom = f"e.{qi(m.emp_nom_col)}::text"
        prenom = f"e.{qi(m.emp_prenom_col)}::text" if m.emp_prenom_col else "NULL::text"
    service_join = ""
    if m.emp_service_col and m.service_table:
        service = f"s.{qi(m.service_label_col)}::text"
        service_join = (f"LEFT JOIN {qt(m.schema, m.service_table)} s ON "
                        f"btrim(s.{qi(m.service_key_col)}::text) = btrim(e.{qi(m.emp_service_col)}::text)")
    elif m.emp_service_col:
        service = f"e.{qi(m.emp_service_col)}::text"
    else:
        service = "NULL::text"
    by_person = m.reference == "person" and bool(m.person_table)
    cat_join = ""
    if m.cat_col:
        src = "pe" if m.cat_in == "person" and m.person_table else "e"
        if m.cat_table:
            categorie = f"nullif(btrim(ct.{qi(m.cat_label_col)}::text), '')"
            cat_join = (f" LEFT JOIN {qt(m.schema, m.cat_table)} ct ON "
                        f"btrim(ct.{qi(m.cat_key_col)}::text) = btrim({src}.{qi(m.cat_col)}::text)")
        else:
            categorie = f"nullif(btrim({src}.{qi(m.cat_col)}::text), '')"
    else:
        categorie = "NULL::text"
    if m.email_col:
        email_src = "pe" if m.email_in == "person" and m.person_table else "e"
        email = f"nullif(lower(btrim({email_src}.{qi(m.email_col)}::text)), '')"
    else:
        email = "NULL::text"
    if m.emp_active_col:
        values = [v.strip().lower() for v in m.emp_active_values.split(",") if v.strip()] or ["1", "true", "t", "oui"]
        alias = "pe" if by_person else "e"  # la colonne « actif » appartient à la liste de référence
        actif = f"lower(btrim({alias}.{qi(m.emp_active_col)}::text)) IN ({', '.join(lit(v) for v in values)})"
        actif_raw = f"btrim({alias}.{qi(m.emp_active_col)}::text)"
    else:
        actif, actif_raw = "true", "NULL::text"
    if by_person:
        # Tout le personnel est attendu : on part de la table des noms, reliée (si possible) à la table des employés.
        pkey_person = f"btrim(pe.{qi(m.person_key_col)}::text)"
        base_key = f"COALESCE({ekey}, 'P-' || {pkey_person})"
        base_mat = f"COALESCE({mat}, {pkey_person})"
        person_ref = pkey_person
        base_from = (f"FROM {qt(m.schema, m.person_table)} pe\n    LEFT JOIN {emp} e ON "
                     f"btrim(e.{qi(m.emp_person_col)}::text) = {pkey_person} {service_join}{cat_join}")
        base_where = f"pe.{qi(m.person_key_col)} IS NOT NULL"
    else:
        base_key, base_mat = ekey, mat
        base_from = f"FROM {emp} e {person_join} {service_join}{cat_join}"
        base_where = f"e.{qi(m.emp_key_col)} IS NOT NULL"

    # Hiérarchie : chaque employé a au plus un responsable (N+1), identifié par clé, matricule ou personne.
    ref_col = {"matricule": "matricule", "person": "person_ref"}.get(m.hier_ref, "emp_key")
    if m.hier_table:
        hier_cte = f""",
hier AS (
    SELECT DISTINCT ON (1) btrim(h.{qi(m.hier_emp_col)}::text) AS emp_ref,
           nullif(btrim(h.{qi(m.hier_manager_col)}::text), '') AS mgr_ref
    FROM {qt(m.schema, m.hier_table)} h
    WHERE h.{qi(m.hier_emp_col)} IS NOT NULL
    ORDER BY 1, 2 NULLS LAST
)"""
        hier_select = ("r.emp_key AS responsable_key, nullif(concat_ws(' ', r.nom, r.prenom), '') AS responsable")
        hier_join = (f"LEFT JOIN hier h ON h.emp_ref = b.{ref_col}\n"
                     f"LEFT JOIN base r ON r.{ref_col} = h.mgr_ref AND r.emp_key <> b.emp_key")
    else:
        hier_cte, hier_join = "", ""
        hier_select = "NULL::text AS responsable_key, NULL::text AS responsable"
    employees_view = f"""CREATE VIEW {S}.v_pointage_employes AS
WITH base AS (
    SELECT DISTINCT ON (1) {base_key} AS emp_key, {base_mat} AS matricule, {nom} AS nom, {prenom} AS prenom,
           {service} AS service, COALESCE({actif}, false) AS actif, {person_ref} AS person_ref,
           {actif_raw} AS actif_valeur, {categorie} AS categorie, {email} AS email
    {base_from}
    WHERE {base_where}
    ORDER BY 1, 6 DESC  -- plusieurs fiches pour une même personne : la fiche active l'emporte
){hier_cte}
SELECT b.emp_key, b.matricule, b.nom, b.prenom, b.service, b.actif, {hier_select}, b.actif_valeur, b.person_ref,
       b.categorie, b.email
FROM base b
{hier_join}"""
    team_function = f"""
CREATE FUNCTION {S}.f_pointage_equipe(p_racine text)
RETURNS TABLE (emp_key text, niveau integer)
LANGUAGE sql STABLE
SET jit = off
AS $fn$
WITH RECURSIVE t(emp_key, niveau) AS (
    SELECT p_racine, 0
    UNION
    SELECT e.emp_key, t.niveau + 1 FROM {S}.v_pointage_employes e JOIN t ON e.responsable_key = t.emp_key
    WHERE t.niveau < 30
)
SELECT emp_key, min(niveau)::int FROM t GROUP BY 1
$fn$"""

    def param(key: str, cast: str) -> str:
        return (f"COALESCE((SELECT x.valeur FROM {S}.pointage_parametres x WHERE x.cle = {lit(key)} "
                f"AND x.date_effet <= d::date ORDER BY x.date_effet DESC, x.id DESC LIMIT 1), "
                f"{lit(PARAM_DEFAULTS[key])}){cast}")

    labels = " ".join(f"WHEN {lit(code)} THEN {lit(label)}" for code, (label, _) in STATUTS.items())
    raw = _raw_day_col(m, punch_types)
    # Seules les lignes de la période (± 1 jour) sont lues : indispensable sur une grosse table de pointages.
    raw_filter = f"\n    WHERE {raw} >= p_du - 1 AND {raw} < p_au + 2" if raw else ""
    function = f"""
CREATE FUNCTION {S}.f_pointage_journalier(p_du date, p_au date)
RETURNS TABLE (
    emp_key text, matricule text, nom text, prenom text, service text, responsable_key text, responsable text,
    jour date, jour_ouvre boolean, premier_pointage timestamp, dernier_pointage timestamp, nb_pointages integer,
    statut text, statut_libelle text, debut_valide time, fin_validee time, pause_deduite interval,
    duree_validee interval, duree_effective interval, duree_validee_min numeric, duree_effective_min numeric,
    hors_liste boolean, terrain boolean, retard_min numeric, categorie text
)
LANGUAGE sql STABLE
-- La compilation JIT coûte plus d'une seconde par appel, pour aucun gain sur ce type de requête.
SET jit = off
AS $fn$
WITH emp AS (
    -- Agents terrain (ex. commerciaux) : désignés par leur service ou individuellement (table pointage_terrain).
    SELECT v.*, EXISTS (
        SELECT 1 FROM {S}.pointage_terrain t
        WHERE (t.type = 'service' AND lower(btrim(t.valeur)) = lower(btrim(v.service)))
           OR (t.type = 'employe' AND btrim(t.valeur) IN (v.matricule, v.emp_key))
    ) AS terrain
    FROM {S}.v_pointage_employes v
),
pl AS (
    SELECT {pkey} AS emp_key, {ts} AS ts FROM {punch} p{raw_filter}
),
agg AS (
    SELECT emp_key, ts::date AS jour, min(ts) AS p1, max(ts) AS p2, count(DISTINCT ts)::int AS n
    FROM pl WHERE ts >= p_du AND ts < p_au + 1 AND emp_key IS NOT NULL
    GROUP BY 1, 2
),
par AS (
    SELECT d::date AS jour,
        {param('debut_journee', '::time')} AS debut_journee,
        {param('debut_pause', '::time')} AS debut_pause,
        {param('fin_pause', '::time')} AS fin_pause,
        {param('fin_journee', '::time')} AS fin_journee,
        {param('seuil_retard', '::time')} AS seuil_retard,
        {param('duree_pause_deduite', '::interval')} AS duree_pause,
        {param('jours_ouvres', '')} AS jours_ouvres,
        {param('duree_conge', '::interval')} AS duree_conge,
        {param('duree_teletravail', '::interval')} AS duree_teletravail,
        {param('duree_terrain', '::interval')} AS duree_terrain,
        {param('duree_arret_maladie', '::interval')} AS duree_maladie,
        EXISTS (SELECT 1 FROM {S}.pointage_jours_feries f WHERE f.jour = d::date) AS ferie
    FROM generate_series(p_du::timestamp, LEAST(p_au, current_date)::timestamp, interval '1 day') AS d
),
conge AS (
    {_requests_cte(m, "leave", leave_types or {}, "CONGE_ANNUEL")}
),
tele AS (
    {_requests_cte(m, "tw", tw_types or {}, "TELETRAVAIL")}
),
maladie AS (  -- arrêts maladie validés dans l'application (déclarés par l'employé ou saisis par les RH)
    SELECT DISTINCT a.emp_key, d::date AS jour
    FROM {S}.pointage_arrets_maladie a
    CROSS JOIN LATERAL generate_series(GREATEST(a.du, p_du)::timestamp, LEAST(a.au, p_au)::timestamp,
                                       interval '1 day') AS d
    WHERE a.du <= p_au AND a.au >= p_du
),
base AS (
    SELECT e.emp_key, p.jour FROM emp e CROSS JOIN par p WHERE e.actif
    UNION
    SELECT a.emp_key, a.jour FROM agg a
),
g AS (
    SELECT b.emp_key, b.jour, e.matricule, e.nom, e.prenom, e.service, e.responsable_key, e.responsable, e.categorie,
           a.p1, a.p2, a.n, (e.emp_key IS NULL) AS hors_liste,
           p.debut_journee, p.debut_pause, p.fin_pause, p.fin_journee, p.seuil_retard, p.duree_pause,
           p.duree_conge, p.duree_teletravail, p.duree_terrain, p.duree_maladie, k.statut AS conge, t.statut AS tele,
           (mal.emp_key IS NOT NULL) AS maladie,
           COALESCE(e.terrain, false) AS terrain,
           (NOT p.ferie AND extract(isodow FROM b.jour)::int = ANY (
               string_to_array(regexp_replace(p.jours_ouvres, '[^0-9,]', '', 'g'), ',')::int[])) AS jour_ouvre
    FROM base b
    JOIN par p ON p.jour = b.jour
    LEFT JOIN emp e ON e.emp_key = b.emp_key
    LEFT JOIN agg a ON a.emp_key = b.emp_key AND a.jour = b.jour
    LEFT JOIN conge k ON k.emp_key = b.emp_key AND k.jour = b.jour
    LEFT JOIN tele t ON t.emp_key = b.emp_key AND t.jour = b.jour
    LEFT JOIN maladie mal ON mal.emp_key = b.emp_key AND mal.jour = b.jour
),
c AS (
    SELECT g.*,
        CASE WHEN g.n IS NULL THEN CASE WHEN g.jour_ouvre
                                          THEN COALESCE(CASE WHEN g.maladie THEN 'ARRET_MALADIE' END, g.conge, g.tele,
                                                        CASE WHEN g.terrain THEN 'TERRAIN' END, 'ABSENT')
                                     ELSE COALESCE(g.tele, 'NON_OUVRE') END  -- le télétravail vaut aussi les jours non ouvrés
             -- Agent terrain qui passe au bureau un jour ouvré : ni retard ni pointage incomplet.
             WHEN g.terrain AND g.jour_ouvre THEN 'TERRAIN'
             WHEN g.n < 2 THEN 'INCOMPLET'
             WHEN g.p1::time >= g.seuil_retard THEN 'RETARD'
             ELSE 'A_L_HEURE' END AS statut,
        CASE WHEN g.n >= 2 THEN
            CASE WHEN g.p1::time < g.debut_pause AND g.p2::time > g.fin_pause THEN g.duree_pause
                 ELSE interval '0' END
        END AS pause,
        CASE WHEN g.n >= 2 THEN CASE WHEN g.p1::time < g.seuil_retard THEN g.debut_journee ELSE g.p1::time END END
            AS debut_valide,
        CASE WHEN g.n >= 2 THEN LEAST(g.p2::time, g.fin_journee) END AS fin_validee
    FROM g
)
SELECT c.emp_key, COALESCE(c.matricule, c.emp_key), COALESCE(c.nom, 'Hors liste'), c.prenom, c.service,
       c.responsable_key, c.responsable, c.jour, c.jour_ouvre, c.p1, c.p2, COALESCE(c.n, 0), c.statut, CASE c.statut {labels} END,
       c.debut_valide, c.fin_validee, c.pause, v.hv, v.de,
       round((extract(epoch FROM v.hv) / 60)::numeric, 2), round((extract(epoch FROM v.de) / 60)::numeric, 2),
       c.hors_liste, c.terrain,
       CASE WHEN c.statut = 'RETARD' THEN round((extract(epoch FROM c.p1::time - c.debut_journee) / 60)::numeric, 0) END,
       c.categorie
FROM c
CROSS JOIN LATERAL (
    SELECT CASE WHEN c.statut IN ('CONGE_ANNUEL', 'CONGE_EXCEP') THEN c.duree_conge
                WHEN c.statut = 'TELETRAVAIL' THEN c.duree_teletravail
                WHEN c.statut = 'ARRET_MALADIE' THEN c.duree_maladie
                -- Agent terrain : au moins la durée prévue, davantage si ses pointages le justifient.
                WHEN c.statut = 'TERRAIN' THEN GREATEST(c.duree_terrain, CASE WHEN c.n >= 2 THEN
                    (LEAST(c.p2::time, c.fin_journee) - LEAST(c.p1::time, c.debut_journee)) - c.pause END)
                WHEN c.n >= 2 THEN GREATEST((c.fin_validee - c.debut_valide) - c.pause, interval '0') END AS hv,
           CASE WHEN c.statut IN ('CONGE_ANNUEL', 'CONGE_EXCEP') THEN c.duree_conge
                WHEN c.statut = 'TELETRAVAIL' THEN c.duree_teletravail
                WHEN c.statut = 'ARRET_MALADIE' THEN c.duree_maladie
                WHEN c.n >= 2 THEN GREATEST((c.p2 - c.p1) - c.pause, interval '0') END AS de
) v
WHERE c.statut IS NOT NULL
$fn$"""
    first_day = f"(SELECT min(({ts})::date) FROM {punch} p)"
    return {
        "schema": f"CREATE SCHEMA IF NOT EXISTS {S}",
        "params_table": f"""CREATE TABLE IF NOT EXISTS {S}.pointage_parametres (
    id serial PRIMARY KEY,
    cle text NOT NULL,
    valeur text NOT NULL,
    date_effet date NOT NULL,
    auteur text,
    modifie_le timestamptz NOT NULL DEFAULT now()
)""",
        "params_index": f"CREATE INDEX IF NOT EXISTS pointage_parametres_cle_date ON {S}.pointage_parametres (cle, date_effet)",
        "sick_table": sick_table_sql(m),
        "field_table": f"""CREATE TABLE IF NOT EXISTS {S}.pointage_terrain (
    id serial PRIMARY KEY,
    type text NOT NULL CHECK (type IN ('service', 'employe')),
    valeur text NOT NULL,
    libelle text,
    auteur text,
    modifie_le timestamptz NOT NULL DEFAULT now(),
    UNIQUE (type, valeur)
)""",
        "holidays_table": f"""CREATE TABLE IF NOT EXISTS {S}.pointage_jours_feries (
    jour date PRIMARY KEY,
    libelle text,
    auteur text,
    modifie_le timestamptz NOT NULL DEFAULT now()
)""",
        "drop_view": f"DROP VIEW IF EXISTS {S}.v_pointage_journalier",
        "drop_raw_view": f"DROP VIEW IF EXISTS {S}.v_pointage_brut",
        "drop_function": f"DROP FUNCTION IF EXISTS {S}.f_pointage_journalier(date, date)",
        "drop_team_function": f"DROP FUNCTION IF EXISTS {S}.f_pointage_equipe(text)",
        "drop_employees_view": f"DROP VIEW IF EXISTS {S}.v_pointage_employes",
        "employees_view": employees_view,
        "team_function": team_function,
        "function": function,
        "view": f"""CREATE VIEW {S}.v_pointage_journalier AS
SELECT * FROM {S}.f_pointage_journalier(COALESCE({first_day}, current_date), current_date)""",
        "raw_view": f"""CREATE VIEW {S}.v_pointage_brut AS
SELECT {pkey} AS emp_key, {ts} AS horodatage, ({ts})::date AS jour FROM {punch} p""",
        "comment": f"COMMENT ON VIEW {S}.v_pointage_journalier IS "
                   f"{lit('Suivi journalier des pointages (1er/dernier pointage, statut, durée validée, durée effective).')}",
    }


def install(engine: Engine, m: Mapping, author: str) -> None:
    """Crée ou met à jour les objets PostgreSQL du module (transaction unique, contrôlée avant validation)."""
    missing = m.missing()
    if missing:
        raise PointageError("Configuration incomplète : " + ", ".join(missing) + ".")
    punch_types = column_types(engine, m.schema, m.punch_table)
    emp_types = column_types(engine, m.schema, m.emp_table)
    if not punch_types:
        raise PointageError(f"Table {m.schema}.{m.punch_table} introuvable.")
    if not emp_types:
        raise PointageError(f"Table {m.schema}.{m.emp_table} introuvable.")
    for col in (m.punch_emp_col, m.punch_ts_col, m.punch_time_col):
        if col and col not in punch_types:
            raise PointageError(f"Colonne « {col} » absente de {m.punch_table}.")
    by_person = m.reference == "person" and bool(m.person_table)
    for col in (m.emp_key_col, m.emp_matricule_col, m.emp_nom_col, m.emp_prenom_col, m.emp_service_col,
                None if by_person else m.emp_active_col, m.emp_person_col):
        if col and col not in emp_types:
            raise PointageError(f"Colonne « {col} » absente de {m.emp_table}.")
    if m.email_col:
        email_src = m.person_table if m.email_in == "person" and m.person_table else m.emp_table
        if m.email_col not in column_types(engine, m.schema, email_src):
            raise PointageError(f"Colonne « {m.email_col} » absente de {email_src}.")
    if m.cat_col:
        cat_src = m.person_table if m.cat_in == "person" and m.person_table else m.emp_table
        if m.cat_col not in column_types(engine, m.schema, cat_src):
            raise PointageError(f"Colonne « {m.cat_col} » absente de {cat_src}.")
    for table, cols in ((m.person_table, (m.person_key_col, m.person_nom_col, m.person_prenom_col,
                                          m.emp_active_col if by_person else None)),
                        (m.cat_table, (m.cat_key_col, m.cat_label_col)),
                        (m.service_table, (m.service_key_col, m.service_label_col)),
                        (m.hier_table, (m.hier_emp_col, m.hier_manager_col)),
                        (m.leave_table, (m.leave_emp_col, m.leave_start_col, m.leave_end_col, m.leave_state_col,
                                         m.leave_type_col)),
                        (m.tw_table, (m.tw_emp_col, m.tw_start_col, m.tw_end_col, m.tw_state_col))):
        if not table:
            continue
        types = column_types(engine, m.schema, table)
        if not types:
            raise PointageError(f"Table {m.schema}.{table} introuvable.")
        for col in cols:
            if col and col not in types:
                raise PointageError(f"Colonne « {col} » absente de {table}.")
    if m.hier_ref == "person" and not m.person_table:
        raise PointageError("La hiérarchie désigne les personnes : choisissez aussi la table des noms.")
    sql = build_sql(m, punch_types, emp_types, column_types(engine, m.schema, m.leave_table),
                    column_types(engine, m.schema, m.tw_table))
    S = qi(m.objs)
    with engine.begin() as c:
        raw = _raw_day_col(m, punch_types)
        if raw:  # index sur la date des pointages (lecture rapide d'une période) ; ignoré si pas les droits
            name = f"ix_pointage_{m.punch_table}_{m.punch_ts_col}"[:63]
            try:
                with c.begin_nested():
                    c.execute(text(f"CREATE INDEX IF NOT EXISTS {qi(name)} ON {qt(m.schema, m.punch_table)} "
                                   f"({qi(m.punch_ts_col)})"))
            except Exception:
                pass
        for key in ("schema", "params_table", "params_index", "holidays_table", "field_table", "sick_table", "drop_view", "drop_raw_view",
                    "drop_function", "drop_team_function", "drop_employees_view", "employees_view",
                    "team_function", "function", "view", "raw_view", "comment"):
            c.execute(text(sql[key]))
        for key, default in PARAM_DEFAULTS.items():
            c.execute(text(
                f"INSERT INTO {S}.pointage_parametres (cle, valeur, date_effet, auteur) "
                f"SELECT :k, :v, DATE '2000-01-01', :a WHERE NOT EXISTS "
                f"(SELECT 1 FROM {S}.pointage_parametres WHERE cle = :k)"), {"k": key, "v": default, "a": author})
        # Contrôle : la fonction doit s'exécuter sur les données réelles (conversion des dates, etc.).
        try:
            c.execute(text(f"SELECT count(*) FROM {S}.f_pointage_journalier(current_date - 31, current_date)"))
        except Exception as exc:
            raise PointageError(f"Les calculs échouent sur les données actuelles : {exc.__class__.__name__}: "
                                f"{str(exc).splitlines()[0]}") from exc


def diagnostics(engine: Engine, m: Mapping, days: int = 31) -> dict:
    """Contrôle de la configuration : qui est attendu, qui est exclu, qui pointe sans fiche, dernier pointage."""
    S = qi(m.objs)
    since = date.today() - timedelta(days=days)
    types = column_types(engine, m.schema, m.punch_table)
    raw = _raw_day_col(m, types)
    pkey = _as_text(f"p.{qi(m.punch_emp_col)}", types.get(m.punch_emp_col, ""))
    if raw:  # lecture limitée à la période grâce à la colonne date (et à son index)
        recent = (f"(SELECT DISTINCT {pkey} AS emp_key FROM {qt(m.schema, m.punch_table)} p "
                  f"WHERE {raw} >= :d AND {pkey} IS NOT NULL)")
    else:
        recent = f"(SELECT DISTINCT emp_key FROM {S}.v_pointage_brut WHERE horodatage >= :d AND emp_key IS NOT NULL)"
    d = {"dernier_pointage": last_punch(engine, m, use_cache=False)}
    with engine.connect() as c:
        d.update(c.execute(text(
            f"SELECT count(*) AS total, count(*) FILTER (WHERE actif) AS actifs, "
            f"count(*) FILTER (WHERE NOT actif) AS inactifs FROM {S}.v_pointage_employes")).mappings().one())
        d["sans_pointage"] = c.execute(text(
            f"SELECT count(*) FROM {S}.v_pointage_employes e WHERE actif AND e.emp_key NOT IN {recent}"),
            {"d": since}).scalar()
        d["inactifs_qui_pointent"] = c.execute(text(
            f"SELECT matricule, concat_ws(' ', nom, prenom) FROM {S}.v_pointage_employes e "
            f"WHERE NOT actif AND e.emp_key IN {recent} ORDER BY 2 LIMIT 20"), {"d": since}).all()
        d["hors_liste"] = c.execute(text(
            f"SELECT count(*) FROM {recent} r WHERE r.emp_key NOT IN (SELECT emp_key FROM {S}.v_pointage_employes)"),
            {"d": since}).scalar()
    d["jours"] = days
    return d


def population(engine: Engine, m: Mapping) -> dict:
    """Combien de personnes de la liste sont attendues (actives), et valeurs trouvées dans la colonne « actif »."""
    S = qi(m.objs)
    with engine.connect() as c:
        d = dict(c.execute(text(
            f"SELECT count(*) AS total, count(*) FILTER (WHERE actif) AS actifs FROM {S}.v_pointage_employes"
        )).mappings().one())
        d["valeurs"] = c.execute(text(
            f"SELECT COALESCE(actif_valeur, '(vide)') AS valeur, bool_or(actif) AS active, count(*) AS n "
            f"FROM {S}.v_pointage_employes GROUP BY 1 ORDER BY 3 DESC LIMIT 8")).all() if m.emp_active_col else []
    # Moins de la moitié de la liste attendue : la colonne « actif » ou ses valeurs sont probablement mal réglées.
    d["suspect"] = bool(m.emp_active_col) and d["total"] > 0 and d["actifs"] * 2 < d["total"]
    return d


def inspect_employee(engine: Engine, m: Mapping, query: str, du: date, au: date) -> list[dict]:
    """« Pourquoi cet employé n'apparaît pas absent ? » : fiche, statut actif, pointages et calcul jour par jour."""
    S = qi(m.objs)
    like = f"%{query.strip()}%"
    with engine.connect() as c:
        people = c.execute(text(
            f"SELECT * FROM {S}.v_pointage_employes WHERE matricule ILIKE :q OR emp_key = :exact "
            f"OR concat_ws(' ', nom, prenom) ILIKE :q OR concat_ws(' ', prenom, nom) ILIKE :q "
            f"ORDER BY nom, prenom LIMIT 5"), {"q": like, "exact": query.strip()}).mappings().all()
        out = []
        for p in people:
            days = c.execute(text(
                f"SELECT jour, jour_ouvre, statut, statut_libelle, nb_pointages, premier_pointage, dernier_pointage "
                f"FROM {S}.f_pointage_journalier(:du, :au) WHERE emp_key = :k ORDER BY jour"),
                {"du": du, "au": au, "k": p["emp_key"]}).mappings().all()
            punches = c.execute(text(
                f"SELECT count(*), min(horodatage), max(horodatage) FROM {S}.v_pointage_brut WHERE emp_key = :k"),
                {"k": p["emp_key"]}).one()
            out.append({"emp": dict(p), "days": days, "punch_count": punches[0], "first_punch": punches[1],
                        "last_punch": punches[2]})
        # Badges du même numéro enregistrés sous une autre clé (mauvaise correspondance employé ↔ pointages).
        others = c.execute(text(
            f"SELECT emp_key, count(*) AS n, max(horodatage) AS dernier FROM {S}.v_pointage_brut "
            f"WHERE emp_key ILIKE :q AND emp_key NOT IN (SELECT emp_key FROM {S}.v_pointage_employes) "
            f"GROUP BY 1 ORDER BY 3 DESC LIMIT 5"), {"q": like}).mappings().all()
    return out, others


_last_punch_cache: dict[str, tuple[float, Optional[datetime]]] = {}


def last_punch(engine: Engine, m: Mapping, use_cache: bool = True) -> Optional[datetime]:
    """Dernier pointage reçu (toutes personnes). Rapide avec une colonne date ; mis en cache 2 minutes."""
    import time as _t

    key = m.to_json()
    cached = _last_punch_cache.get(key)
    if use_cache and cached and _t.monotonic() - cached[0] < 120:
        return cached[1]
    types = column_types(engine, m.schema, m.punch_table)
    raw, punch = _raw_day_col(m, types), qt(m.schema, m.punch_table)
    ts = _ts_expr(m, types)
    with engine.connect() as c:
        if raw:  # le plus grand jour d'abord (index), puis l'heure exacte sur ce seul jour
            value = c.execute(text(f"SELECT max({ts}) FROM {punch} p WHERE {raw} >= "
                                   f"(SELECT max({raw}) FROM {punch} p) - interval '1 day'")).scalar()
        else:
            value = c.execute(text(f"SELECT max(horodatage) FROM {qi(m.objs)}.v_pointage_brut")).scalar()
    _last_punch_cache[key] = (_t.monotonic(), value)
    return value


def punches_since(engine: Engine, m: Mapping, since: datetime, keys: list[str]) -> list[dict]:
    """Pointages postérieurs à « since » des employés indiqués, avec leur rang dans la journée (1 = arrivée)
    et la fiche de l'employé (nom, service, e-mail). Lecture limitée aux jours concernés grâce à la colonne date."""
    if not keys:
        return []
    S = qi(m.objs)
    types = column_types(engine, m.schema, m.punch_table)
    raw = _raw_day_col(m, types)
    ts = _ts_expr(m, types)
    pkey = _as_text(f"p.{qi(m.punch_emp_col)}", types.get(m.punch_emp_col, ""))
    prefilter = f" AND {raw} >= :jour - 1" if raw else ""
    sql = text(f"""
        WITH jour AS (
            SELECT {pkey} AS emp_key, {ts} AS ts FROM {qt(m.schema, m.punch_table)} p
            WHERE {pkey} IN :keys{prefilter}
        ), rang AS (
            SELECT DISTINCT emp_key, ts FROM jour WHERE ts >= CAST(:jour AS date)
        ), numerote AS (
            SELECT emp_key, ts, row_number() OVER (PARTITION BY emp_key, ts::date ORDER BY ts) AS rang,
                   count(*) OVER (PARTITION BY emp_key, ts::date) AS nb_jour
            FROM rang
        )
        SELECT n.emp_key, n.ts, n.rang, n.nb_jour, e.matricule, e.nom, e.prenom, e.service, e.email
        FROM numerote n JOIN {S}.v_pointage_employes e ON e.emp_key = n.emp_key
        WHERE n.ts >= :since AND n.ts <= :limite
        ORDER BY n.emp_key, n.ts""").bindparams(bindparam("keys", expanding=True))
    with engine.connect() as c:
        return [dict(r) for r in c.execute(sql, {"keys": keys, "since": since, "jour": since.date(),
                                                  "limite": datetime.now() + timedelta(days=1)}).mappings()]


def employee_directory(engine: Engine, m: Mapping) -> list[dict]:
    """Employés actifs de la liste (pour choisir les abonnés aux mails), avec leur e-mail éventuel."""
    S = qi(m.objs)
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(
            f"SELECT emp_key, matricule, nom, prenom, service, categorie, email FROM {S}.v_pointage_employes "
            f"WHERE actif ORDER BY nom, prenom, matricule")).mappings()]


def is_installed(engine: Engine, m: Mapping) -> bool:
    with engine.connect() as c:
        return bool(c.execute(text(
            "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = :s AND p.proname = 'f_pointage_journalier'"), {"s": m.objs}).first())


# --------------------------------------------------------------------------- paramètres historisés


def params_at(engine: Engine, m: Mapping, day: date) -> dict[str, str]:
    S = qi(m.objs)
    values = dict(PARAM_DEFAULTS)
    with engine.connect() as c:
        rows = c.execute(text(
            f"SELECT DISTINCT ON (cle) cle, valeur FROM {S}.pointage_parametres WHERE date_effet <= :d "
            f"ORDER BY cle, date_effet DESC, id DESC"), {"d": day})
        values.update({k: v for k, v in rows if k in values})
    return values


def params_history(engine: Engine, m: Mapping, limit: int = 300) -> list:
    S = qi(m.objs)
    with engine.connect() as c:
        return c.execute(text(
            f"SELECT id, cle, valeur, date_effet, auteur, modifie_le FROM {S}.pointage_parametres "
            f"ORDER BY date_effet DESC, id DESC LIMIT :n"), {"n": limit}).all()


def save_params(engine: Engine, m: Mapping, values: dict[str, str], effective: date, author: str) -> dict:
    """Enregistre les paramètres modifiés à partir de la date d'effet. Renvoie {clé: (avant, après)}."""
    before = params_at(engine, m, effective)
    changes = {k: (before[k], v) for k, v in values.items() if before.get(k) != v}
    if not changes:
        return {}
    S = qi(m.objs)
    with engine.begin() as c:
        for key, (_, new) in changes.items():
            c.execute(text(f"INSERT INTO {S}.pointage_parametres (cle, valeur, date_effet, auteur) "
                           f"VALUES (:k, :v, :d, :a)"), {"k": key, "v": new, "d": effective, "a": author})
    return changes


def holidays(engine: Engine, m: Mapping) -> list:
    with engine.connect() as c:
        return c.execute(text(f"SELECT jour, libelle, auteur, modifie_le FROM {qi(m.objs)}.pointage_jours_feries "
                              f"ORDER BY jour DESC")).all()


def add_holiday(engine: Engine, m: Mapping, day: date, label: str, author: str) -> None:
    with engine.begin() as c:
        c.execute(text(
            f"INSERT INTO {qi(m.objs)}.pointage_jours_feries (jour, libelle, auteur) VALUES (:j, :l, :a) "
            f"ON CONFLICT (jour) DO UPDATE SET libelle = EXCLUDED.libelle, auteur = EXCLUDED.auteur, modifie_le = now()"),
            {"j": day, "l": label, "a": author})


def delete_holiday(engine: Engine, m: Mapping, day: date) -> None:
    with engine.begin() as c:
        c.execute(text(f"DELETE FROM {qi(m.objs)}.pointage_jours_feries WHERE jour = :j"), {"j": day})


FIELD_TYPES = {"service": "Service entier", "employe": "Employé"}


def field_entries(engine: Engine, m: Mapping) -> list[dict]:
    """Agents terrain : services entiers et employés désignés, avec le nombre de personnes concernées."""
    S = qi(m.objs)
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(
            f"SELECT t.id, t.type, t.valeur, t.libelle, t.auteur, t.modifie_le, "
            f"(SELECT count(*) FROM {S}.v_pointage_employes v WHERE v.actif AND "
            f" ((t.type = 'service' AND lower(btrim(t.valeur)) = lower(btrim(v.service))) "
            f"  OR (t.type = 'employe' AND btrim(t.valeur) IN (v.matricule, v.emp_key)))) AS personnes, "
            f"(SELECT concat_ws(' ', v.nom, v.prenom) FROM {S}.v_pointage_employes v WHERE t.type = 'employe' "
            f" AND btrim(t.valeur) IN (v.matricule, v.emp_key) LIMIT 1) AS nom "
            f"FROM {S}.pointage_terrain t ORDER BY t.type DESC, t.valeur")).mappings()]


def add_field(engine: Engine, m: Mapping, kind: str, value: str, label: str, author: str) -> None:
    if kind not in FIELD_TYPES or not value.strip():
        raise PointageError("Choisissez un service ou saisissez un matricule.")
    if kind == "employe" and resolve_employee(engine, m, value) is None:
        raise PointageError(f"Aucun employé trouvé pour « {value.strip()} ».")
    with engine.begin() as c:
        c.execute(text(
            f"INSERT INTO {qi(m.objs)}.pointage_terrain (type, valeur, libelle, auteur) VALUES (:t, :v, :l, :a) "
            f"ON CONFLICT (type, valeur) DO UPDATE SET libelle = EXCLUDED.libelle, auteur = EXCLUDED.auteur, "
            f"modifie_le = now()"), {"t": kind, "v": value.strip(), "l": label.strip(), "a": author})


def clean_matricule(value) -> str:
    """Matricule lu dans un fichier : les nombres Excel (590394.0) redeviennent « 590394 »."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text_value = str(value).strip()
    return text_value[:-2] if re.fullmatch(r"\d+\.0", text_value) else text_value


def import_fields(engine: Engine, m: Mapping, rows: list[tuple[str, str]], author: str, replace: bool = False) -> dict:
    """Agents terrain importés d'un fichier : (matricule, motif). « replace » retire les employés absents du fichier
    (les services entiers déclarés restent). Les matricules inconnus sont ignorés et listés."""
    S = qi(m.objs)
    wanted: dict[str, str] = {}
    duplicates = 0
    for mat, label in rows:
        mat = clean_matricule(mat)
        if not mat:
            continue
        if mat in wanted:
            duplicates += 1
        wanted[mat] = (label or "").strip()[:200] or wanted.get(mat, "")
    with engine.begin() as c:
        found = {}
        if wanted:
            for matricule, key in c.execute(text(
                    f"SELECT matricule, emp_key FROM {S}.v_pointage_employes "
                    f"WHERE matricule IN :m OR emp_key IN :m").bindparams(bindparam("m", expanding=True)),
                    {"m": list(wanted)}):
                for ref in (matricule, key):
                    if ref in wanted and ref not in found:
                        found[ref] = matricule or key
        existing = {v for (v,) in c.execute(text(f"SELECT valeur FROM {S}.pointage_terrain WHERE type = 'employe'"))}
        kept, added, updated = set(), 0, 0
        for ref, label in wanted.items():
            if ref not in found:
                continue
            value = found[ref]
            kept.add(value)
            added += value not in existing
            updated += value in existing
            c.execute(text(
                f"INSERT INTO {S}.pointage_terrain (type, valeur, libelle, auteur) VALUES ('employe', :v, :l, :a) "
                f"ON CONFLICT (type, valeur) DO UPDATE SET libelle = COALESCE(NULLIF(EXCLUDED.libelle, ''), "
                f"{S}.pointage_terrain.libelle), auteur = EXCLUDED.auteur, modifie_le = now()"),
                {"v": value, "l": label, "a": author})
        removed = 0
        if replace:
            gone = [v for v in existing if v not in kept]
            if gone:
                removed = c.execute(text(f"DELETE FROM {S}.pointage_terrain WHERE type = 'employe' AND valeur IN :g")
                                    .bindparams(bindparam("g", expanding=True)), {"g": gone}).rowcount
    return {"lus": len(wanted), "ajoutes": added, "mis_a_jour": updated, "retires": removed, "doublons": duplicates,
            "inconnus": [r for r in wanted if r not in found]}


def delete_field(engine: Engine, m: Mapping, entry_id: int) -> Optional[tuple[str, str]]:
    with engine.begin() as c:
        return c.execute(text(f"DELETE FROM {qi(m.objs)}.pointage_terrain WHERE id = :i RETURNING type, valeur"),
                         {"i": entry_id}).first()


# --------------------------------------------------------------------------- écran de suivi


@dataclass
class Filters:
    du: date
    au: date
    q: str = ""
    service: list[str] = field(default_factory=list)   # un ou plusieurs services (vide = tous)
    statuts: list[str] = field(default_factory=list)
    sort: str = "nom"
    desc: bool = False
    team: str = ""                    # « Équipe de » : clé du responsable (toute sa hiérarchie)
    directs: bool = False             # seulement ses collaborateurs directs (N-1)
    scope_root: Optional[str] = None  # périmètre imposé par le compte (manager : son équipe)
    population: str = ""              # « liste » : employés de la liste ; « hors » : badges hors liste
    categorie: list[str] = field(default_factory=list)  # statut(s) du personnel : cadre, non cadre… (vide = tous)


def _where(f: Filters, S: str = "") -> tuple[str, dict, list]:
    clauses, params, binds = [], {"du": f.du, "au": f.au}, []
    if f.scope_root is not None:
        clauses.append(f"emp_key IN (SELECT emp_key FROM {S}.f_pointage_equipe(:scope_root))")
        params["scope_root"] = f.scope_root
    if f.team:
        levels = " WHERE niveau <= 1" if f.directs else ""
        clauses.append(f"emp_key IN (SELECT emp_key FROM {S}.f_pointage_equipe(:team){levels})")
        params["team"] = f.team
    if f.q:
        clauses.append("(matricule ILIKE :q OR nom ILIKE :q OR prenom ILIKE :q "
                       "OR concat_ws(' ', nom, prenom) ILIKE :q OR concat_ws(' ', prenom, nom) ILIKE :q)")
        params["q"] = f"%{f.q}%"
    if f.population == "liste":
        clauses.append("NOT hors_liste")
    elif f.population == "hors":
        clauses.append("hors_liste")
    if f.service:
        clauses.append("service IN :services")
        params["services"] = list(f.service)
        binds.append(bindparam("services", expanding=True))
    if f.categorie:
        clauses.append("categorie IN :categories")
        params["categories"] = list(f.categorie)
        binds.append(bindparam("categories", expanding=True))
    statuts = [s for s in f.statuts if s in STATUTS]
    if statuts:
        clauses.append("statut IN :statuts")
        params["statuts"] = statuts
        binds.append(bindparam("statuts", expanding=True))
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params, binds


def multi(values: list[str]) -> list[str]:
    """Valeurs d'un filtre à choix multiples (paramètre répété dans l'URL), sans vides ni doublons."""
    out = []
    for v in values:
        v = (v or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def _order(f: Filters) -> str:
    col = SORTABLE.get(f.sort, "nom")
    direction = "DESC" if f.desc else "ASC"
    return f" ORDER BY {col} {direction} NULLS LAST, jour, nom, matricule"


def daily(engine: Engine, m: Mapping, f: Filters, page: int = 1, size: int = 100) -> dict:
    S = qi(m.objs)
    where, params, binds = _where(f, S)
    source = f"(SELECT * FROM {S}.f_pointage_journalier(:du, :au)) t"
    kpi_sql = text(f"""SELECT count(*) AS lignes,
        count(*) FILTER (WHERE statut IN ('A_L_HEURE', 'RETARD', 'INCOMPLET')) AS presents,
        count(*) FILTER (WHERE statut = 'A_L_HEURE') AS a_l_heure,
        count(*) FILTER (WHERE statut = 'RETARD') AS retards,
        count(*) FILTER (WHERE statut = 'ABSENT') AS absents,
        count(*) FILTER (WHERE statut = 'INCOMPLET') AS incomplets,
        count(*) FILTER (WHERE statut = 'NON_OUVRE') AS non_ouvres,
        count(*) FILTER (WHERE statut IN ('CONGE_ANNUEL', 'CONGE_EXCEP')) AS conges,
        count(*) FILTER (WHERE statut = 'TELETRAVAIL') AS teletravail,
        count(*) FILTER (WHERE statut = 'TERRAIN') AS terrain,
        count(*) FILTER (WHERE statut = 'ARRET_MALADIE') AS maladies,
        count(DISTINCT emp_key) FILTER (WHERE hors_liste) AS hors_liste,
        avg(duree_validee) FILTER (WHERE statut NOT IN ('CONGE_ANNUEL', 'CONGE_EXCEP', 'TELETRAVAIL', 'TERRAIN', 'ARRET_MALADIE')) AS moy_validee,
        -- Heures moyennes de premier et de dernier pointage des personnes venues au bureau.
        time '00:00' + avg(premier_pointage::time - time '00:00')
            FILTER (WHERE statut IN ('A_L_HEURE', 'RETARD', 'INCOMPLET')) AS moy_premier,
        time '00:00' + avg(dernier_pointage::time - time '00:00')
            FILTER (WHERE statut IN ('A_L_HEURE', 'RETARD') AND nb_pointages >= 2) AS moy_dernier,
        avg(duree_effective) FILTER (WHERE statut NOT IN ('CONGE_ANNUEL', 'CONGE_EXCEP', 'TELETRAVAIL', 'TERRAIN', 'ARRET_MALADIE')) AS moy_effective,
        count(DISTINCT emp_key) AS employes
        FROM {source}{where}""").bindparams(*binds)
    rows_sql = text(f"SELECT * FROM {source}{where}{_order(f)} LIMIT :lim OFFSET :off").bindparams(*binds)
    with engine.connect() as c:
        kpi = dict(c.execute(kpi_sql, params).mappings().one())
        rows = c.execute(rows_sql, {**params, "lim": size, "off": (page - 1) * size}).mappings().all()
    ponctuels = kpi["a_l_heure"] + kpi["retards"]
    kpi["taux_ponctualite"] = (kpi["a_l_heure"] * 100.0 / ponctuels) if ponctuels else None
    return {"kpi": kpi, "rows": rows, "total": kpi["lignes"]}


def export_rows(engine: Engine, m: Mapping, f: Filters, limit: int = 200_000):
    S = qi(m.objs)
    where, params, binds = _where(f, S)
    sql = text(f"SELECT * FROM (SELECT * FROM {S}.f_pointage_journalier(:du, :au)) t{where}{_order(f)} "
               f"LIMIT :lim").bindparams(*binds)
    with engine.connect() as c:
        return c.execute(sql, {**params, "lim": limit}).mappings().all()


def _scope_sql(S: str, scope_root: Optional[str]) -> str:
    return "" if scope_root is None else f" AND emp_key IN (SELECT emp_key FROM {S}.f_pointage_equipe(:root))"


def services(engine: Engine, m: Mapping, scope_root: Optional[str] = None) -> list[str]:
    S = qi(m.objs)
    with engine.connect() as c:
        return [s for s in c.execute(text(
            f"SELECT DISTINCT service FROM {S}.v_pointage_employes WHERE nullif(btrim(service), '') IS NOT NULL"
            f"{_scope_sql(S, scope_root)} ORDER BY 1"), {"root": scope_root}).scalars()]


def categories(engine: Engine, m: Mapping, scope_root: Optional[str] = None) -> list[str]:
    if not m.cat_col:
        return []
    S = qi(m.objs)
    with engine.connect() as c:
        return [s for s in c.execute(text(
            f"SELECT DISTINCT categorie FROM {S}.v_pointage_employes WHERE nullif(btrim(categorie), '') IS NOT NULL"
            f"{_scope_sql(S, scope_root)} ORDER BY 1"), {"root": scope_root}).scalars()]


def managers(engine: Engine, m: Mapping, scope_root: Optional[str] = None) -> list[tuple[str, str, int]]:
    """Responsables (employés qui encadrent au moins une personne) : (clé, libellé, nombre de collaborateurs directs)."""
    if not m.hier_table:
        return []
    S = qi(m.objs)
    scope = "" if scope_root is None else f"WHERE r.emp_key IN (SELECT emp_key FROM {S}.f_pointage_equipe(:root)) "
    with engine.connect() as c:
        rows = c.execute(text(
            f"SELECT r.emp_key, concat_ws(' ', r.nom, r.prenom) || ' (' || r.matricule || ')', count(*) "
            f"FROM {S}.v_pointage_employes e JOIN {S}.v_pointage_employes r ON r.emp_key = e.responsable_key "
            f"{scope}GROUP BY 1, 2 ORDER BY 2"), {"root": scope_root}).all()
    return [(k, label, n) for k, label, n in rows]


def resolve_employee(engine: Engine, m: Mapping, ref: str) -> Optional[tuple[str, str]]:
    """Employé désigné par son matricule ou sa clé : (clé, « Nom Prénom »)."""
    S = qi(m.objs)
    with engine.connect() as c:
        row = c.execute(text(
            f"SELECT emp_key, concat_ws(' ', nom, prenom) FROM {S}.v_pointage_employes "
            f"WHERE matricule = :r OR emp_key = :r ORDER BY (matricule = :r) DESC LIMIT 1"), {"r": ref.strip()}).first()
    return (row[0], row[1]) if row else None


def employee_matricule(engine: Engine, m: Mapping, emp_key: str) -> Optional[str]:
    with engine.connect() as c:
        return c.execute(text(f"SELECT matricule FROM {qi(m.objs)}.v_pointage_employes WHERE emp_key = :k LIMIT 1"),
                         {"k": emp_key}).scalar()


def manager_of(engine: Engine, m: Mapping, emp_key: str) -> Optional[tuple[str, str]]:
    """Responsable N+1 d'un employé : (matricule, « Nom Prénom »), ou None sans hiérarchie."""
    if not m.hier_table:
        return None
    S = qi(m.objs)
    with engine.connect() as c:
        row = c.execute(text(
            f"SELECT r.matricule, concat_ws(' ', r.nom, r.prenom) FROM {S}.v_pointage_employes e "
            f"JOIN {S}.v_pointage_employes r ON r.emp_key = e.responsable_key WHERE e.emp_key = :k LIMIT 1"),
            {"k": emp_key}).first()
    return (row[0], row[1]) if row else None


def sick_table_sql(m: Mapping) -> str:
    return f"""CREATE TABLE IF NOT EXISTS {qi(m.objs)}.pointage_arrets_maladie (
    id integer PRIMARY KEY,
    emp_key text NOT NULL,
    matricule text,
    du date NOT NULL,
    au date NOT NULL,
    modifie_le timestamptz NOT NULL DEFAULT now()
)"""


def in_scope(engine: Engine, m: Mapping, scope_root: Optional[str], emp_key: str) -> bool:
    if scope_root is None:
        return True
    S = qi(m.objs)
    with engine.connect() as c:
        return bool(c.execute(text(f"SELECT 1 FROM {S}.f_pointage_equipe(:root) WHERE emp_key = :k"),
                              {"root": scope_root, "k": emp_key}).first())


def detail(engine: Engine, m: Mapping, emp_key: str, day: date) -> tuple[list[str], list]:
    """Tous les pointages bruts d'un employé pour une journée (toutes les colonnes de la table)."""
    punch_types = column_types(engine, m.schema, m.punch_table)
    ts = _ts_expr(m, punch_types)
    pkey = _as_text(f"p.{qi(m.punch_emp_col)}", punch_types.get(m.punch_emp_col, ""))
    sql = text(f"SELECT {ts} AS horodatage, p.* FROM {qt(m.schema, m.punch_table)} p "
               f"WHERE {pkey} = :k AND {ts} >= :d AND {ts} < :d2 ORDER BY 1")
    with engine.connect() as c:
        result = c.execute(sql, {"k": emp_key, "d": day, "d2": day + timedelta(days=1)})
        return list(result.keys()), result.all()


# --------------------------------------------------------------------------- affichage


def hhmm(value: Any) -> str:
    """Durée ou heure en « 7h30 » (les secondes sont ignorées)."""
    if value is None:
        return "—"
    if isinstance(value, timedelta):
        minutes = int(value.total_seconds() // 60)
        return f"{minutes // 60}h{minutes % 60:02d}"
    if isinstance(value, datetime):
        return value.strftime("%Hh%M")
    if isinstance(value, time):
        return value.strftime("%Hh%M")
    return str(value)


def parse_day(value: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None
