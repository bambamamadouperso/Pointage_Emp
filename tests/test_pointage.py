"""Module de pointage : calculs PostgreSQL (exemples du cahier des charges), écran, export, rôles, audit.

Nécessite TEST_POSTGRES_URL (voir test_integration.py).
"""
import io
import os
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import pointage
from app.crypto import encrypt
from app.database import SessionLocal
from app.models import AuditEntry, Connection, PointageConfig, User

POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL non définie")
SCHEMA = "pt_test"
MON, TUE, WED, SAT = date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 26)


def at(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m)


@pytest.fixture(scope="module")
def pg():
    engine = create_engine(POSTGRES_URL)
    with engine.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        c.execute(text(f"CREATE TABLE {SCHEMA}.services (id int PRIMARY KEY, libelle text)"))
        # Noms et prénoms dans une autre table (Personnel) ; hiérarchie dans une table dédiée (par matricule).
        c.execute(text(f"""CREATE TABLE {SCHEMA}."Employes" ("IDEmployes" int PRIMARY KEY, "Matricule" text,
                          "IDPersonnel" int, "IDService" int, "Actif" text)"""))
        c.execute(text(f"""CREATE TABLE {SCHEMA}."Personnel" ("IDPersonnel" int PRIMARY KEY, "Nom" text, "Prenom" text,
                          "IDStatut" int)"""))
        c.execute(text(f"CREATE TABLE {SCHEMA}.statuts (id int PRIMARY KEY, libelle text)"))
        c.execute(text(f"INSERT INTO {SCHEMA}.statuts VALUES (1, 'Cadre'), (2, 'Non cadre')"))
        c.execute(text(f"""CREATE TABLE {SCHEMA}.hierarchie (matricule_employe text, matricule_responsable text)"""))
        c.execute(text(f"CREATE TABLE {SCHEMA}.punchlog (id serial PRIMARY KEY, employe_id int, "
                       f"date_pointage date, heure_pointage text, terminal text)"))
        c.execute(text(f"INSERT INTO {SCHEMA}.services VALUES (1, 'Production'), (2, 'RH')"))
        c.execute(text(f"""INSERT INTO {SCHEMA}."Employes" VALUES
            (1, 'E001', 101, 1, '1'), (2, 'E002', 102, 1, '1'), (3, 'E003', 103, 2, '1'), (4, 'E004', 104, 2, '1'),
            (5, 'E005', 105, 2, '1'), (6, 'E006', 106, 1, '0'), (7, 'E007', 107, 1, '1')"""))
        c.execute(text(f"""INSERT INTO {SCHEMA}."Personnel" VALUES
            (101, 'Diallo', 'Awa', 1), (102, 'Ndiaye', 'Moussa', 2), (103, 'Sow', 'Fatou', 1), (104, 'Fall', 'Ibou', 2),
            (105, 'Ba', 'Khady', 2), (106, 'Gueye', 'Ousmane', 2), (107, 'Diop', 'Aminata', 2),
            (108, 'Kane', 'Omar', 2)"""))  # Kane : dans Personnel, sans fiche dans Employes
        # Diallo encadre Ndiaye, Sow et Gueye ; Sow encadre Fall et Ba ; Diop n'a pas de responsable.
        c.execute(text(f"""INSERT INTO {SCHEMA}.hierarchie VALUES
            ('E002', 'E001'), ('E003', 'E001'), ('E006', 'E001'), ('E004', 'E003'), ('E005', 'E003'), ('E007', NULL)"""))
        punches = [
            (1, MON, "07:20"), (1, MON, "12:00"), (1, MON, "17:10"),   # à l'heure : 7h30 validées, 8h20 effectives
            (2, MON, "08:10"), (2, MON, "16:45"),                      # retard : 6h50 validées
            (3, MON, "07:35"), (3, MON, "15:50"),                      # à l'heure (tolérance) : 6h50 validées
            (4, MON, "07:40"),                                         # un seul pointage : incomplet
            (7, MON, "07:45"), (7, MON, "12:00"),                      # 7h45 pile = retard, pas de pause déduite
            (2, TUE, "08:10"), (2, TUE, "16:30"),                      # seuil passé à 8h15 le mardi : à l'heure
            (1, SAT, "08:00"), (1, SAT, "12:00"),                      # samedi (non ouvré)
            (99, MON, "09:00"), (99, MON, "17:00"),                    # employé inconnu
        ]
        for emp, day, hhmm in punches:
            c.execute(text(f"INSERT INTO {SCHEMA}.punchlog (employe_id, date_pointage, heure_pointage, terminal) "
                           f"VALUES (:e, :d, :h, 'T1')"), {"e": emp, "d": day, "h": hhmm.replace(":", "") + "00"})
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def configured(pg):
    """Configure et installe le module via l'administration."""
    from fastapi.testclient import TestClient

    from app.main import app

    u = make_url(POSTGRES_URL)
    with SessionLocal() as db:
        db.query(PointageConfig).delete()
        db.query(Connection).filter(Connection.name == "pt-pg").delete()
        conn = Connection(name="pt-pg", kind="postgresql", host=u.host, port=u.port or 5432, database=u.database,
                          username=u.username, password_enc=encrypt(u.password or ""))
        db.add(conn)
        db.commit()
        conn_id = conn.id
    with TestClient(app) as client:
        client.post("/login", data={"username": "admin", "password": "secret"})
        page = client.get(f"/admin/pointage?conn_id={conn_id}&schema={SCHEMA}&punch_table=punchlog&emp_table=Employes")
        assert page.status_code == 200 and "heure_pointage" in page.text
        r = client.post("/admin/pointage", data={
            "conn_id": conn_id, "schema": SCHEMA, "objects_schema": "", "punch_table": "punchlog",
            "punch_emp_col": "employe_id", "punch_ts_col": "date_pointage", "punch_time_col": "heure_pointage",
            "emp_table": "Employes", "emp_key_col": "IDEmployes", "emp_matricule_col": "Matricule",
            "emp_service_col": "IDService",
            "service_table": "services", "service_key_col": "id", "service_label_col": "libelle",
            "emp_active_col": "Actif", "emp_active_values": "1",
            "person_table": "Personnel", "emp_person_col": "IDPersonnel", "person_key_col": "IDPersonnel",
            "person_nom_col": "Nom", "person_prenom_col": "Prenom",
            "hier_table": "hierarchie", "hier_emp_col": "matricule_employe",
            "hier_manager_col": "matricule_responsable", "hier_ref": "matricule",
            "cat_in": "person", "cat_col": "IDStatut", "cat_table": "statuts", "cat_key_col": "id", "cat_label_col": "libelle",
        }, follow_redirects=True)
        assert "installées" in r.text, r.text[:3000]
        # Seuil de retard porté à 8h15 à partir du mardi ; mercredi férié.
        values = {k: v for k, _, _, v in pointage.PARAMS if k != "jours_ouvres"}
        r = client.post("/admin/parametres", data={**values, "seuil_retard": "08:15", "jours_ouvres": ["1", "2", "3", "4", "5"],
                                                   "date_effet": TUE.isoformat()}, follow_redirects=True)
        assert "1 paramètre(s) modifié(s)" in r.text
        r = client.post("/admin/feries", data={"jour": WED.isoformat(), "libelle": "Fête"}, follow_redirects=True)
        assert "Fête" in r.text
    return conn_id


def rows(pg, du, au=None):
    with pg.connect() as c:
        return {(r.matricule, r.jour): r for r in c.execute(text(
            f"SELECT * FROM {SCHEMA}.f_pointage_journalier(:du, :au)"), {"du": du, "au": au or du}).mappings()}


def hm(value) -> str:
    return pointage.hhmm(value)


def test_calculations_match_specification(configured, pg):
    r = rows(pg, MON)
    awa = r[("E001", MON)]
    assert awa.statut == "A_L_HEURE" and hm(awa.premier_pointage) == "07h20" and hm(awa.dernier_pointage) == "17h10"
    assert hm(awa.duree_validee) == "7h30" and hm(awa.duree_effective) == "8h20" and awa.nb_pointages == 3
    moussa = r[("E002", MON)]
    assert moussa.statut == "RETARD" and hm(moussa.duree_validee) == "6h50" and hm(moussa.duree_effective) == "7h05"
    fatou = r[("E003", MON)]
    assert fatou.statut == "A_L_HEURE" and hm(fatou.duree_validee) == "6h50" and hm(fatou.duree_effective) == "6h45"
    ibou = r[("E004", MON)]
    assert ibou.statut == "INCOMPLET" and ibou.duree_validee is None and ibou.duree_effective is None
    assert r[("E005", MON)].statut == "ABSENT" and r[("E005", MON)].service == "RH"
    assert ("E006", MON) not in r  # inactif : jamais absent
    aminata = r[("E007", MON)]  # 7h45 pile = retard ; départ 12h00 < fin de pause : pas de déduction
    assert aminata.statut == "RETARD" and hm(aminata.duree_validee) == "4h15" and hm(aminata.duree_effective) == "4h15"
    assert r[("99", MON)].nom == "Hors liste" and r[("99", MON)].hors_liste
    assert not awa.hors_liste
    assert r[("E001", MON)].service == "Production" and r[("E001", MON)].statut_libelle == "À l'heure"
    # Noms venant de la table Personnel, responsable venant de la table hierarchie.
    assert (awa.nom, awa.prenom, awa.responsable) == ("Diallo", "Awa", None)
    assert moussa.responsable == "Diallo Awa" and moussa.responsable_key == "1"
    assert r[("E004", MON)].responsable == "Sow Fatou" and aminata.responsable is None


def test_automatic_upgrade_of_sql_objects(configured, pg, logged_client):
    """Après une mise à jour de l'application, les fonctions PostgreSQL sont réinstallées d'elles-mêmes."""
    with SessionLocal() as db:
        cfg = db.query(PointageConfig).one()
        cfg.sql_version = 1
        db.commit()
    with pg.begin() as c:  # ancienne version : sans la colonne hors_liste
        c.execute(text(f"DROP VIEW {SCHEMA}.v_pointage_journalier"))
        c.execute(text(f"DROP FUNCTION {SCHEMA}.f_pointage_journalier(date, date)"))
    assert "Diallo" in logged_client.get(f"/suivi?date={MON.isoformat()}").text
    with SessionLocal() as db:
        assert db.query(PointageConfig).one().sql_version == pointage.SQL_VERSION
        assert db.query(AuditEntry).filter(AuditEntry.action.like("Calculs du pointage mis à jour%")).count()


def test_hierarchy_team_function(configured, pg):
    with pg.connect() as c:
        team = dict(c.execute(text(f"SELECT emp_key, niveau FROM {SCHEMA}.f_pointage_equipe('1')")).all())
        assert team == {"1": 0, "2": 1, "3": 1, "6": 1, "4": 2, "5": 2}
        assert {k for (k,) in c.execute(text(f"SELECT emp_key FROM {SCHEMA}.f_pointage_equipe('3')"))} == {"3", "4", "5"}
        emp = c.execute(text(f"SELECT * FROM {SCHEMA}.v_pointage_employes WHERE matricule = 'E005'")).mappings().one()
        assert (emp["nom"], emp["responsable"], emp["service"]) == ("Ba", "Sow Fatou", "RH")


def test_history_holidays_and_weekends(configured, pg):
    week = rows(pg, MON, SAT)
    assert week[("E002", TUE)].statut == "A_L_HEURE"  # seuil 8h15 en vigueur depuis mardi
    assert week[("E002", MON)].statut == "RETARD"     # lundi : seuil 7h45 (valeur de l'époque)
    assert week[("E005", TUE)].statut == "ABSENT"
    # On part de la liste des employés : chacun apparaît chaque jour, même sans pointage.
    assert week[("E005", WED)].statut == "NON_OUVRE"  # jour férié : pas d'absence
    assert week[("E005", SAT)].statut == "NON_OUVRE"  # samedi non ouvré
    assert week[("E005", SAT)].statut_libelle == "Jour non ouvré"
    assert ("E006", MON) not in week                  # inactif sans pointage
    sat = week[("E001", SAT)]
    assert sat.jour_ouvre is False and sat.nb_pointages == 2
    with pg.connect() as c:  # la vue couvre tout l'historique
        n = c.execute(text(f"SELECT count(*) FROM {SCHEMA}.v_pointage_journalier WHERE jour = :d"), {"d": MON}).scalar()
        assert n == len(rows(pg, MON))
        assert c.execute(text(f"SELECT count(*) FROM {SCHEMA}.v_pointage_brut")).scalar() == 16


def test_screen_filters_detail_and_export(configured, logged_client):
    from openpyxl import load_workbook

    page = logged_client.get(f"/suivi?date={MON.isoformat()}")
    assert page.status_code == 200
    html = page.text
    assert "Diallo" in html and "st-late" in html and "Lundi 21/09/2026" in html
    assert "Ponctualité" in html and "Durées moyennes" in html
    only_late = logged_client.get(f"/suivi?date={MON.isoformat()}&statut=RETARD").text
    assert "E002</td>" in only_late and "E001</td>" not in only_late
    search = logged_client.get(f"/suivi?date={MON.isoformat()}&q=E003").text
    assert "E003</td>" in search and "E002</td>" not in search
    service = logged_client.get(f"/suivi?date={MON.isoformat()}&service=RH").text
    assert "E003</td>" in service and "E001</td>" not in service
    hors = logged_client.get(f"/suivi?date={MON.isoformat()}&pop=hors").text
    assert "99</td>" in hors and "E001</td>" not in hors and "Hors liste" in hors
    liste = logged_client.get(f"/suivi?date={MON.isoformat()}&pop=liste").text
    assert "99</td>" not in liste and "E005</td>" in liste
    period = logged_client.get(f"/suivi?du={MON.isoformat()}&au={SAT.isoformat()}&sort=validee&dir=desc").text
    assert "journées-employé" in period
    team = logged_client.get(f"/suivi?date={MON.isoformat()}&equipe=3").text
    assert "E003</td>" in team and "E004</td>" in team and "E005</td>" in team and "E001</td>" not in team
    assert "Responsable" in team and "Sow Fatou · 2 direct(s)" in team.replace(" (E003)", "")
    directs = logged_client.get(f"/suivi?date={MON.isoformat()}&equipe=1&directs=1").text
    assert "E002</td>" in directs and "E004</td>" not in directs
    detail = logged_client.get(f"/suivi/detail?key=1&jour={MON.isoformat()}").text
    assert "3 pointage(s)" in detail and "07:20:00" in detail and "T1" in detail
    xlsx = logged_client.get(f"/suivi/export.xlsx?date={MON.isoformat()}&statut=RETARD&statut=ABSENT")
    assert xlsx.status_code == 200
    ws = load_workbook(io.BytesIO(xlsx.content))["Suivi journalier"]
    statuts = {ws.cell(r, 10).value for r in range(2, ws.max_row + 1) if ws.cell(r, 1).value not in (None, "Moyenne")}
    assert statuts == {"En retard", "Absent"}
    assert ws.cell(1, 11).value == "Durée validée"


def test_roles_and_audit(configured, client):
    from app import auth

    with SessionLocal() as db:
        db.query(User).filter(User.username.in_(["lect", "mgr"])).delete()
        db.add_all([User(username="lect", role="lecteur", password_hash=auth.hash_password("motdepasse1")),
                    User(username="mgr", role="manager", password_hash=auth.hash_password("motdepasse2"))])
        db.commit()
    r = client.post("/login", data={"username": "lect", "password": "motdepasse1"}, follow_redirects=False)
    assert r.headers["location"] == "/suivi"
    assert client.get(f"/suivi?date={MON.isoformat()}").status_code == 200
    for path in ("/", "/jobs", "/admin", "/connections"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/suivi", path
    assert client.post("/jobs/1/run", headers={"accept": "application/json"}).status_code == 403
    client.post("/logout")
    client.post("/login", data={"username": "mgr", "password": "motdepasse2"})
    assert client.get("/jobs").status_code == 200 and client.get("/runs").status_code == 200
    assert client.get("/admin", follow_redirects=False).status_code == 303
    assert client.get("/jobs/new", follow_redirects=False).status_code == 303
    client.post("/logout")
    assert client.post("/login", data={"username": "lect", "password": "mauvais"}, follow_redirects=False) \
        .headers["location"] == "/login"
    # Désactivation : la session en cours est coupée immédiatement.
    client.post("/login", data={"username": "lect", "password": "motdepasse1"})
    with SessionLocal() as db:
        db.query(User).filter_by(username="lect").one().active = False
        db.commit()
    assert client.get("/suivi", follow_redirects=False).headers["location"] == "/login"
    with SessionLocal() as db:
        actions = {a for (a,) in db.query(AuditEntry.action)}
    assert {"Source des pointages configurée", "Paramètres horaires modifiés", "Jour férié ajouté", "Connexion",
            "Échec de connexion"} <= actions


def test_account_limited_to_team(configured, client):
    from openpyxl import load_workbook

    from app import auth

    with SessionLocal() as db:
        db.query(User).filter(User.username.in_(["chef", "perdu"])).delete()
        db.add_all([User(username="chef", role="lecteur", password_hash=auth.hash_password("motdepasse4"),
                         emp_matricule="E003", scope="equipe"),
                    User(username="perdu", role="manager", password_hash=auth.hash_password("motdepasse5"),
                         emp_matricule="X999", scope="equipe")])
        db.commit()
    client.post("/login", data={"username": "chef", "password": "motdepasse4"})
    page = client.get(f"/suivi?date={MON.isoformat()}").text
    assert "équipe de <strong>Sow Fatou</strong>" in page
    assert "E004</td>" in page and "E005</td>" in page and "E001</td>" not in page and "E002</td>" not in page
    assert "E002</td>" not in client.get(f"/suivi?date={MON.isoformat()}&equipe=1").text  # hors périmètre : ignoré
    assert "ne fait pas partie de votre équipe" in client.get(f"/suivi/detail?key=2&jour={MON.isoformat()}").text
    assert "3 pointage(s)" not in client.get(f"/suivi/detail?key=1&jour={MON.isoformat()}").text
    ws = load_workbook(io.BytesIO(client.get(f"/suivi/export.xlsx?date={MON.isoformat()}").content))["Suivi journalier"]
    assert {ws.cell(r, 2).value for r in range(2, ws.max_row + 1)
            if ws.cell(r, 1).value not in (None, "Moyenne")} == {"E003", "E004", "E005"}
    client.post("/logout")
    client.post("/login", data={"username": "perdu", "password": "motdepasse5"})
    assert "rattaché à aucun employé" in client.get(f"/suivi?date={MON.isoformat()}").text


def test_admin_users_and_params_pages(configured, logged_client):
    r = logged_client.post("/admin/users/save", data={"username": "nouveau", "full_name": "N. Ouveau",
                                                      "role": "manager", "password": "motdepasse3"},
                           follow_redirects=True)
    assert "« nouveau » enregistré" in r.text
    with SessionLocal() as db:
        uid = db.query(User).filter_by(username="nouveau").one().id
    r = logged_client.post("/admin/users/save", data={"user_id": uid, "full_name": "X", "role": "lecteur",
                                                      "emp_matricule": "E001", "scope": "equipe"},
                           follow_redirects=True)
    with SessionLocal() as db:
        u = db.get(User, uid)
        assert u.role == "lecteur" and not u.active and u.team_only and u.emp_matricule == "E001"
    r = logged_client.post("/admin/users/save", data={"user_id": uid, "role": "lecteur", "scope": "equipe"},
                           follow_redirects=True)
    assert "indiquez le matricule" in r.text
    assert "Son équipe (E001)" in logged_client.get("/admin/users").text
    assert "au moins 8 caractères" in logged_client.post(f"/admin/users/{uid}/password", data={"password": "court"},
                                                         follow_redirects=True).text
    page = logged_client.get("/admin/parametres").text
    assert "08:15" in page and "Historique des modifications" in page and "Fête" in page
    bad = logged_client.post("/admin/parametres", data={"debut_journee": "09:00", "seuil_retard": "08:00",
                             "debut_pause": "13:00", "fin_pause": "14:00", "fin_journee": "16:30",
                             "duree_pause_deduite": "01:30", "jours_ouvres": ["1"], "date_effet": "2026-09-01"},
                             follow_redirects=True).text
    assert "seuil de retard doit être postérieur" in bad
    audit = logged_client.get("/admin/audit?q=Paramètres").text
    assert "07:45 → 08:15" in audit
    assert "Traitements planifiés" in logged_client.get("/admin").text


def test_diagnostics(configured, pg):
    with SessionLocal() as db:
        m = pointage.Mapping.from_json(db.query(PointageConfig).one().data)
    d = pointage.diagnostics(pg, m)
    assert (d["total"], d["actifs"], d["inactifs"]) == (7, 6, 1)
    assert d["hors_liste"] == 1 and d["sans_pointage"] == 1  # badge 99 ; Ba Khady n'a jamais pointé
    assert d["dernier_pointage"] == datetime(2026, 9, 26, 12, 0)


def test_last_punch_with_timestamp_column(configured, pg):
    """Colonne des pointages de type horodatage (date + heure) : « horodatage - 1 » était refusé."""
    import dataclasses

    with pg.begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.punch_ts"))
        c.execute(text(f"CREATE TABLE {SCHEMA}.punch_ts (employe_id int, horodatage timestamp)"))
        c.execute(text(f"INSERT INTO {SCHEMA}.punch_ts VALUES (1, '2026-09-27 07:40'), (1, '2026-09-28 16:05'), "
                       f"(2, '2026-09-28 08:10')"))
    with SessionLocal() as db:
        m = pointage.Mapping.from_json(db.query(PointageConfig).one().data)
    m = dataclasses.replace(m, punch_table="punch_ts", punch_ts_col="horodatage", punch_time_col="")
    assert pointage.last_punch(pg, m, use_cache=False) == datetime(2026, 9, 28, 16, 5)


def test_population_warns_when_most_are_inactive(configured, pg, logged_client, monkeypatch):
    """Colonne « actif » mal réglée (presque personne d'actif) : bandeau d'alerte sur l'écran de suivi."""
    import dataclasses

    with SessionLocal() as db:
        m = pointage.Mapping.from_json(db.query(PointageConfig).one().data)
    ok = pointage.population(pg, m)
    assert (ok["total"], ok["actifs"], ok["suspect"]) == (7, 6, False) and ok["valeurs"]
    assert "jamais absentes" not in logged_client.get("/suivi?date=2026-09-25").text

    wrong = dataclasses.replace(m, objects_schema="pt_test_actif", emp_active_values="zzz")
    with pg.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS pt_test_actif CASCADE"))
    pointage.install(pg, wrong, "test")
    bad = pointage.population(pg, wrong)
    assert (bad["actifs"], bad["suspect"]) == (0, True)
    assert all(not active for _, active, _ in bad["valeurs"])

    monkeypatch.setattr(pointage, "population", lambda engine, mapping: bad)
    page = logged_client.get("/suivi?date=2026-09-25").text
    assert "jamais absentes" in page and "Valeurs considérées actives" in page


def test_filter_bar_and_contradictory_filters(configured, logged_client):
    """Filtres appliqués affichés (retirables), « directs » sans équipe ignoré, hors liste + service expliqué."""
    page = logged_client.get("/suivi?date=2026-09-25&service=Production&statut=ABSENT&statut=RETARD").text
    assert "Filtres appliqués" in page and "Service : Production" in page and "Statut : Absent, En retard" in page
    assert "Tout retirer" in page

    page = logged_client.get("/suivi?date=2026-09-25&directs=1").text
    assert "directs (N-1)" not in page  # sans responsable choisi, la case n'a pas d'effet
    assert "disabled" in page.split('name="directs"')[1].split(">")[0]

    page = logged_client.get("/suivi?date=2026-09-25&service=Production&pop=hors").text
    assert "Aucune ligne" in page and "n'ont ni service ni responsable" in page


def test_approved_leave_and_remote_work_replace_absence(configured, pg, logged_client):
    """Jour « Absent » couvert par un congé approuvé : « Congé Annuel » / « Congé exceptionnel », 8h validées et effectives."""
    import dataclasses

    with pg.begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.demandeconge"))
        c.execute(text(f"""CREATE TABLE {SCHEMA}.demandeconge (matricule text, datedebut timestamp, "Datefin" text,
                          etat text, "Document" text)"""))
        c.execute(text(f"""INSERT INTO {SCHEMA}.demandeconge VALUES
            ('E005', '2026-09-21 00:00', '20260923000000', 'Approuvée', 'CONGE'),        -- lun. → mer. (mer. férié)
            ('E004', '2026-09-21 00:00', '2026-09-21', 'Approuvée', 'CONGE EXCEP'),       -- a badgé : reste incomplet
            ('E007', '2026-09-22 00:00', '2026-09-22', ' APPROUVEE ', 'conge  excep'),    -- casse et espaces ignorés
            ('E003', '2026-09-22 00:00', '2026-09-22', 'En attente', 'CONGE'),            -- non approuvé
            ('E001', '2026-09-22 00:00', '2026-09-22', 'Approuvée', 'MALADIE')            -- autre nature"""))
        c.execute(text(f'DROP TABLE IF EXISTS {SCHEMA}."TdemandedeTeleTravail"'))
        c.execute(text(f"""CREATE TABLE {SCHEMA}."TdemandedeTeleTravail" (matricule text, datedebut timestamp,
                          "Datefin" timestamp, etat text)"""))
        c.execute(text(f"""INSERT INTO {SCHEMA}."TdemandedeTeleTravail" VALUES
            ('E003', '2026-09-22 00:00', '2026-09-22 00:00', 'Approuvée'),   -- télétravail
            ('E005', '2026-09-22 00:00', '2026-09-22 00:00', 'Approuvée'),   -- aussi en congé : le congé l'emporte
            ('E001', '2026-09-22 00:00', '2026-09-22 00:00', 'Refusée'),     -- non approuvé
            ('E002', '2026-09-22 00:00', '2026-09-22 00:00', 'Approuvée'),   -- a badgé : garde son statut
            ('E007', '2026-09-23 00:00', '2026-09-23 00:00', 'Approuvée')    -- mercredi férié : télétravail quand même"""))
    with SessionLocal() as db:
        m = pointage.Mapping.from_json(db.query(PointageConfig).one().data)
    m = dataclasses.replace(
        m, objects_schema="pt_test_conge", leave_table="demandeconge", leave_emp_col="matricule", leave_ref="matricule",
        leave_start_col="datedebut", leave_end_col="Datefin", leave_state_col="etat", leave_type_col="Document",
        tw_table="TdemandedeTeleTravail", tw_emp_col="matricule", tw_ref="matricule", tw_start_col="datedebut",
        tw_end_col="Datefin", tw_state_col="etat")
    assert (m.leave_state_values, m.leave_annual_values, m.leave_excep_values) == ("Approuvée", "CONGE", "CONGE EXCEP")
    with pg.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS pt_test_conge CASCADE"))
    pointage.install(pg, m, "test")
    with pg.begin() as c:
        c.execute(text("INSERT INTO pt_test_conge.pointage_jours_feries (jour, libelle) VALUES (:d, 'Fête')"), {"d": WED})
    with pg.connect() as c:
        r = {(x.matricule, x.jour): x for x in c.execute(text(
            "SELECT * FROM pt_test_conge.f_pointage_journalier(:du, :au)"), {"du": MON, "au": WED}).mappings()}
    khady = r[("E005", MON)]
    assert (khady.statut, khady.statut_libelle) == ("CONGE_ANNUEL", "Congé Annuel")
    assert hm(khady.duree_validee) == "8h00" and hm(khady.duree_effective) == "8h00"
    assert r[("E005", TUE)].statut == "CONGE_ANNUEL" and r[("E005", WED)].statut == "NON_OUVRE"
    assert r[("E004", MON)].statut == "INCOMPLET"
    aminata = r[("E007", TUE)]
    assert (aminata.statut, aminata.statut_libelle) == ("CONGE_EXCEP", "Congé exceptionnel")
    assert r[("E001", TUE)].statut == "ABSENT"
    fatou = r[("E003", TUE)]  # congé non approuvé, mais télétravail approuvé
    assert (fatou.statut, fatou.statut_libelle, hm(fatou.duree_validee), hm(fatou.duree_effective)) == (
        "TELETRAVAIL", "Télétravail", "8h00", "8h00")
    assert r[("E005", TUE)].statut == "CONGE_ANNUEL" and r[("E002", TUE)].statut in ("A_L_HEURE", "RETARD")
    # Le télétravail prime sur les jours non ouvrés (le congé, lui, non) : 8h le mercredi férié.
    assert (r[("E007", WED)].statut, hm(r[("E007", WED)].duree_validee)) == ("TELETRAVAIL", "8h00")
    assert r[("E005", WED)].statut == "NON_OUVRE" and r[("E003", WED)].statut == "NON_OUVRE"

    # Écran : carte « En congé », badge vert clair, moyennes hors congés.
    from app.routers import suivi

    monkey_cfg = suivi.load_config
    cfg, _ = monkey_cfg(SessionLocal())
    suivi.load_config = lambda db: (cfg, m)
    try:
        page = logged_client.get(f"/suivi?date={TUE.isoformat()}").text
    finally:
        suivi.load_config = monkey_cfg
    assert "Autres situations" in page and 'badge st-leave">Congé Annuel' in page and "Congé exceptionnel" in page
    assert 'badge st-remote">Télétravail' in page

    # Administration : la section Congés propose les colonnes de la table choisie.
    admin = logged_client.get(f"/admin/pointage?conn_id={configured}&schema={SCHEMA}&punch_table=punchlog"
                              f"&emp_table=Employes&leave_table=demandeconge&leave_state_values=Approuvée"
                              f"&tw_table=TdemandedeTeleTravail").text
    assert "Table des demandes de congé" in admin and "Datefin (text)" in admin and "Congé exceptionnel" in admin
    tw_section = admin.split("Table des demandes de télétravail")[1]
    assert 'name="tw_start_col"' in tw_section and '<option value="datedebut" selected' in tw_section


def test_reference_whole_personnel(configured, pg, logged_client):
    """Liste de référence = table Personnel, sans colonne « actif » : tout le personnel est attendu."""
    with SessionLocal() as db:
        cfg = db.query(PointageConfig).one()
        original, conn_id = cfg.data, cfg.conn_id
    data = {**pointage.Mapping.from_json(original).__dict__, "reference": "person", "emp_active_col": "",
            "conn_id": conn_id}
    try:
        r = logged_client.post("/admin/pointage", data=data, follow_redirects=True)
        assert "installées" in r.text, r.text[:2000]
        day = rows(pg, MON)
        assert day[("108", MON)].statut == "ABSENT" and day[("108", MON)].nom == "Kane"  # sans fiche Employes
        assert day[("E006", MON)].statut == "ABSENT"  # plus de filtre « actif » : attendu lui aussi
        assert day[("E001", MON)].statut == "A_L_HEURE" and day[("E002", MON)].responsable == "Diallo Awa"
        page = logged_client.get("/admin/pointage").text
        assert "Tout le personnel de la table des noms" in page and "Personnes dans la liste" in page
    finally:
        restore = {**pointage.Mapping.from_json(original).__dict__, "conn_id": conn_id}
        assert "installées" in logged_client.post("/admin/pointage", data=restore, follow_redirects=True).text


def test_check_employee_tool(configured, logged_client):
    """« Vérifier un employé » explique jour par jour pourquoi une personne est (ou n'est pas) absente."""
    period = f"&du={MON.isoformat()}&au={SAT.isoformat()}"
    inactive = logged_client.get(f"/admin/verifier?q=E006{period}").text
    assert "Gueye" in inactive and "Inactif : jamais compté absent" in inactive
    assert "Actif = « 0 »" in inactive and "non affiché" in inactive
    active = logged_client.get(f"/admin/verifier?q=Ba Khady{period}").text
    assert "Actif : attendu chaque jour ouvré" in active and active.count(">Absent<") == 4  # lun., mar., jeu., ven.
    assert "Jour non ouvré" in active  # mercredi férié, samedi
    unknown = logged_client.get(f"/admin/verifier?q=99{period}").text
    assert "Badges hors liste" in unknown and "Aucune personne" in unknown
    assert "Vérifier un employé" in logged_client.get(f"/suivi?date={MON.isoformat()}").text


def test_field_agents_never_absent_nor_late(configured, pg, logged_client):
    """Agents terrain (service entier ou employé) : « Sur le terrain », durée validée minimale, jamais absent/retard."""
    page = logged_client.get("/admin/terrain").text
    assert "Aucun agent terrain" in page and "Production" in page
    r = logged_client.post("/admin/terrain", data={"type": "employe", "matricule": "E002", "libelle": "Commercial"},
                           follow_redirects=True)
    assert "déclaré agent terrain" in r.text
    r = logged_client.post("/admin/terrain", data={"type": "service", "service": "RH"}, follow_redirects=True)
    assert "Ndiaye Moussa" in r.text and ">3<" in r.text  # RH : Sow, Fall, Ba
    assert "introuvable" not in r.text
    bad = logged_client.post("/admin/terrain", data={"type": "employe", "matricule": "ZZZ"}, follow_redirects=True).text
    assert "Aucun employé trouvé" in bad

    r = rows(pg, MON)
    moussa = r[("E002", MON)]  # arrivé à 8h10 : pas de retard ; 8h validées minimum, durée effective mesurée
    assert (moussa.statut, moussa.statut_libelle) == ("TERRAIN", "Sur le terrain") and moussa.terrain
    assert hm(moussa.duree_validee) == "8h00" and hm(moussa.duree_effective) == "7h05" and moussa.retard_min is None
    khady = r[("E005", MON)]  # aucun pointage : pas absente
    assert khady.statut == "TERRAIN" and hm(khady.duree_validee) == "8h00" and khady.duree_effective is None
    assert r[("E004", MON)].statut == "TERRAIN"  # un seul pointage : pas « incomplet »
    assert r[("E001", MON)].statut == "A_L_HEURE" and not r[("E001", MON)].terrain
    assert rows(pg, WED)[("E005", WED)].statut == "NON_OUVRE"
    assert r[("E007", MON)].retard_min == 15  # 7h45 pour un début à 7h30

    page = logged_client.get(f"/suivi?date={MON.isoformat()}").text
    assert "Sur le terrain" in page and 'badge st-field">Sur le terrain' in page

    for entry in pointage.field_entries(pg, pointage.Mapping.from_json(_cfg_data())):
        logged_client.post(f"/admin/terrain/{entry['id']}/delete")
    assert rows(pg, MON)[("E005", MON)].statut == "ABSENT"


def _cfg_data() -> str:
    with SessionLocal() as db:
        return db.query(PointageConfig).one().data


def test_reports_page_and_export(configured, logged_client):
    """Rapports : indicateurs, graphiques, services, alertes, employés, export Excel."""
    import io

    from openpyxl import load_workbook

    url = f"/rapports?p=perso&du={MON.isoformat()}&au={(MON + timedelta(days=4)).isoformat()}"
    page = logged_client.get(url)
    assert page.status_code == 200, page.text[:2000]
    html = page.text
    for part in ("Taux de présence", "Taux d'absentéisme", "Présence par jour", "Selon le jour de la semaine",
                 "Heures d'arrivée au bureau", "Par service", "À suivre", "Par employé", "Absences répétées"):
        assert part in html, part
    assert "Ba Khady" in html and "Production" in html and "RH" in html
    assert "<svg class=\"chart\"" in html and "data-tip=" in html
    # Tri, filtre service et période prédéfinie
    assert logged_client.get(url + "&tri=retards&service=RH").status_code == 200
    assert logged_client.get("/rapports?p=mois-1").status_code == 200
    r = logged_client.get("/rapports/export.xlsx?" + url.split("?", 1)[1])
    assert r.status_code == 200
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames[:5] == ["Employés", "Services", "Statuts du personnel", "Par jour", "Alertes"]
    rows = {row[0]: row for row in wb["Employés"].iter_rows(min_row=2, values_only=True)}
    assert rows["E005"][14] == 4  # Ba Khady : 4 absences (mercredi férié)
    assert any(row[0] == "Absences répétées" for row in wb["Alertes"].iter_rows(min_row=2, values_only=True))


def test_category_filter(configured, pg, logged_client):
    """Catégorie du personnel (via une table de libellés) : colonne, filtre du suivi et des rapports, exports."""
    import io

    from openpyxl import load_workbook

    r = rows(pg, MON)
    assert r[("E001", MON)].categorie == "Cadre" and r[("E002", MON)].categorie == "Non cadre"
    assert r[("99", MON)].categorie is None  # hors liste
    with SessionLocal() as db:
        m = pointage.Mapping.from_json(db.query(PointageConfig).one().data)
    assert pointage.categories(pg, m) == ["Cadre", "Non cadre"]
    page = logged_client.get(f"/suivi?date={MON.isoformat()}&categorie=Cadre").text
    assert 'name="categorie"' in page and "Statut du personnel : Cadre" in page
    assert "Diallo" in page and "Sow" in page and "Ndiaye" not in page
    xlsx = load_workbook(io.BytesIO(logged_client.get(f"/suivi/export.xlsx?date={MON.isoformat()}&categorie=Cadre").content))
    ws = xlsx.active
    assert ws.cell(1, 6).value == "Statut du personnel" and {row[5] for row in ws.iter_rows(min_row=2, values_only=True) if row[0] not in (None, "Moyenne")} == {"Cadre"}
    url = f"/rapports?p=perso&du={MON.isoformat()}&au={(MON + timedelta(days=4)).isoformat()}"
    rep = logged_client.get(url).text
    assert "Par statut du personnel" in rep and "Non cadre" in rep
    assert "Ba Khady" not in logged_client.get(url + "&categorie=Cadre").text
    wb = load_workbook(io.BytesIO(logged_client.get("/rapports/export.xlsx?" + url.split("?", 1)[1]).content))
    assert "Statuts du personnel" in wb.sheetnames
    admin = logged_client.get("/admin/pointage").text
    assert "Statut du personnel" in admin and "<code>Cadre</code>" in admin
    # Choix multiples : plusieurs services et plusieurs statuts du personnel.
    page = logged_client.get(f"/suivi?date={MON.isoformat()}&categorie=Cadre&categorie=Non+cadre&service=RH").text
    assert "Statut du personnel : Cadre, Non cadre" in page and "Service : RH" in page and 'class="ms-more">+1' in page
    assert "E003</td>" in page and "E005</td>" in page and "E001</td>" not in page  # E001 : Production
    both = logged_client.get(f"/suivi?date={MON.isoformat()}&service=RH&service=Production&pop=liste").text
    assert "E001</td>" in both and "E003</td>" in both
    xlsx = load_workbook(io.BytesIO(logged_client.get(
        f"/suivi/export.xlsx?date={MON.isoformat()}&service=RH&service=Production").content))
    assert {row[4] for row in xlsx.active.iter_rows(min_row=2, values_only=True)
            if row[0] not in (None, "Moyenne")} == {"RH", "Production"}
    assert dict(xlsx["Filtres"].iter_rows(values_only=True))["Service"] == "RH, Production"
    multi = logged_client.get(url + "&service=RH&service=Production&categorie=Cadre").text
    assert "Par employé" in multi and "E005</td>" not in multi and "E001</td>" in multi and "E003</td>" in multi
    # Colonne non configurée : le filtre reste visible (désactivé), avec le lien de configuration.
    import app.pointage as pt
    original = pt.categories
    pt.categories = lambda *a, **k: []
    try:
        for url in (f"/suivi?date={MON.isoformat()}", "/rapports?p=7j"):
            html = logged_client.get(url).text
            assert "Statut du personnel" in html and "Non configuré" in html and "#statut-personnel" in html, url
    finally:
        pt.categories = original


def test_badge_mails_test_mode_production_and_no_duplicates(configured, pg, logged_client, monkeypatch):
    """Mails de badge : jamais d'envoi aux employés hors production, un seul mail par pointage, modèle et essai."""
    from app import mails
    from app.models import MailLog, MailSettings, MailSubscriber

    now = datetime.now().replace(second=0, microsecond=0)

    def punch(emp_id, when):
        with pg.begin() as c:
            c.execute(text(f"INSERT INTO {SCHEMA}.punchlog (employe_id, date_pointage, heure_pointage, terminal) "
                           f"VALUES (:e, :d, :h, 'MAIL')"), {"e": emp_id, "d": when.date(), "h": when.strftime("%H%M%S")})

    try:
        # Activation refusée sans adresse de test ; production refusée sans confirmation explicite.
        base = {"enabled": "1", "mode": "test", "smtp_host": "smtp.exemple.com", "smtp_port": "587",
                "smtp_security": "starttls", "from_email": "pointage@exemple.com", "from_name": "Pointage",
                "company": "Société <Test>", "max_per_run": "50"}
        r = logged_client.post("/admin/mails/parametres", data=base, follow_redirects=True)
        assert "au moins une adresse de test" in r.text
        r = logged_client.post("/admin/mails/parametres", data={**base, "test_recipients": "rh@exemple.com"},
                               follow_redirects=True)
        assert "Paramètres des mails enregistrés" in r.text and "Mode test" in r.text
        with SessionLocal() as db:
            s = db.query(MailSettings).one()
            assert s.enabled and s.mode == "test" and s.since is not None
            s.since = now - timedelta(hours=3)  # pour le test : pointages des 3 dernières heures
            db.commit()

        # Abonnés : Awa (adresse saisie ici) et Moussa (sans adresse).
        r = logged_client.post("/admin/mails/abonnes", data={"sub": ["1", "2"], "email_1": "awa@exemple.com",
                                                               "email_2": ""}, follow_redirects=True)
        assert "2 abonné(s) enregistré(s)" in r.text
        assert "awa@exemple.com" in logged_client.get("/admin/mails/abonnes").text

        punch(1, now - timedelta(hours=2))
        punch(2, now - timedelta(hours=2, minutes=5))
        punch(3, now - timedelta(hours=2))            # Fatou : non abonnée
        punch(1, now - timedelta(hours=5))            # avant l'activation : jamais notifié
        sent = []
        result = mails.process(sender=sent.append)
        assert result["sent"] == 2 and result["mode"] == "test"
        # Mode test : tout part vers l'adresse de test, aucun mail vers un employé.
        assert {m["To"] for m in sent} == {"rh@exemple.com"}
        assert all(m["Subject"].startswith("[TEST] ") for m in sent)
        awa = next(m for m in sent if "awa@exemple.com" in m.get_body(("html",)).get_content())
        body = awa.get_body(("html",)).get_content()
        assert "MODE TEST" in body and "Bonjour Awa" in body and "Société &lt;Test&gt;" in body
        assert "awa@exemple.com" not in awa["To"]

        # Deuxième passage : rien de nouveau, aucun doublon.
        assert mails.process(sender=sent.append)["sent"] == 0 and len(sent) == 2

        # Nouveau pointage d'Awa : un seul mail, avec ses pointages du jour (sauf changement de date).
        punch(1, now - timedelta(minutes=30))
        more = []
        assert mails.process(sender=more.append)["sent"] == 1
        assert "Awa" in more[0].get_body(("html",)).get_content()

        # Passage en production : confirmation obligatoire, puis envoi réel à l'adresse de l'employé.
        prod = {**base, "test_recipients": "rh@exemple.com", "mode": "production"}
        r = logged_client.post("/admin/mails/parametres", data=prod, follow_redirects=True)
        assert "cochez la confirmation" in r.text
        r = logged_client.post("/admin/mails/parametres", data={**prod, "confirm_production": "1"}, follow_redirects=True)
        assert "Mode PRODUCTION activé" in r.text
        with SessionLocal() as db:
            s = db.query(MailSettings).one()
            assert s.mode == "production" and s.since >= now - timedelta(minutes=1)
            s.since = now - timedelta(minutes=20)
            db.commit()
        punch(1, now - timedelta(minutes=10))
        punch(2, now - timedelta(minutes=10))
        real = []
        result = mails.process(sender=real.append)
        assert result["sent"] == 1 and result["skipped"] == 1  # Moussa : aucune adresse
        assert real[0]["To"] == "awa@exemple.com" and not real[0]["Subject"].startswith("[TEST]")
        with SessionLocal() as db:
            skipped = db.query(MailLog).filter(MailLog.status == "skipped").one()
            assert skipped.emp_key == "2" and "aucune adresse" in skipped.error
        journal = logged_client.get("/admin/mails/journal").text
        assert "awa@exemple.com" in journal and "production" in journal and "ignoré" in journal

        # Modèle : aperçu avec valeurs échappées, enregistrement, modèle par défaut.
        page = logged_client.get("/admin/mails/modele").text
        assert "tpl_html" in page and "{{prenom}}" in page
        prev = logged_client.post("/admin/mails/apercu", data={"html": "<p>Bonjour {{prenom}} ({{entreprise}})</p>"}).text
        assert prev == "<p>Bonjour Aminata (Société &lt;Test&gt;)</p>"
        r = logged_client.post("/admin/mails/modele", data={"subject": "Badge {{heure}}", "html": "<p>{{prenom}}</p>"},
                               follow_redirects=True)
        assert "Modèle enregistré" in r.text
        r = logged_client.post("/admin/mails/modele", data={"reset": "1"}, follow_redirects=True)
        assert "Modèle par défaut rétabli" in r.text

        # Mail d'essai via le serveur SMTP (simulé).
        delivered = []

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                self.host = host
            def ehlo(self): pass
            def starttls(self, context=None): pass
            def login(self, user, pwd): pass
            def send_message(self, msg): delivered.append(msg)
            def quit(self): pass

        monkeypatch.setattr(mails.smtplib, "SMTP", FakeSMTP)
        r = logged_client.post("/admin/mails/essai", data={"to": "moi@exemple.com"}, follow_redirects=True)
        assert "essai envoyé à moi@exemple.com" in r.text
        assert delivered[0]["To"] == "moi@exemple.com"
        assert delivered[0]["Subject"].startswith("[TEST] ")
        assert "Mails de badge" in logged_client.get("/admin/mails").text
    finally:
        with pg.begin() as c:
            c.execute(text(f"DELETE FROM {SCHEMA}.punchlog WHERE terminal = 'MAIL'"))
        with SessionLocal() as db:
            db.query(MailLog).delete()
            db.query(MailSubscriber).delete()
            s = db.query(MailSettings).first()
            if s:
                s.enabled, s.mode = False, "test"
            db.commit()


def test_field_agents_excel_import(configured, pg, logged_client):
    """Agents terrain importés d'un fichier Excel ou CSV : ajout, remplacement, matricules inconnus, modèle, export."""
    import io

    from openpyxl import Workbook, load_workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Nom employé", "Matricule", "Motif"])          # « Nom employé » ne doit pas être pris pour le matricule
    ws.append(["Ndiaye Moussa", "E002", "Commercial Nord"])
    ws.append(["Fall Ibou", "E004", "Commercial Sud"])
    ws.append(["Inconnu", "ZZZ9", "?"])
    ws.append(["Ndiaye Moussa", "E002", ""])                  # doublon
    buf = io.BytesIO()
    wb.save(buf)
    r = logged_client.post("/admin/terrain/import", files={"fichier": ("commerciaux.xlsx", buf.getvalue())},
                           data={"mode": "ajouter"}, follow_redirects=True)
    assert "2 agent(s) ajouté(s)" in r.text and "1 matricule(s) introuvable(s)" in r.text and "ZZZ9" in r.text
    assert "Commercial Nord" in r.text and "Commercial Sud" in r.text
    assert rows(pg, MON)[("E002", MON)].statut == "TERRAIN"

    # CSV sans en-tête, séparateur « ; », accents Windows : remplacement de la liste des employés.
    csv = "E007;Délégué médical\r\n".encode("cp1252")
    r = logged_client.post("/admin/terrain/import", files={"fichier": ("liste.csv", csv)},
                           data={"mode": "remplacer"}, follow_redirects=True)
    assert "1 agent(s) ajouté(s)" in r.text and "2 retiré(s)" in r.text and "Délégué médical" in r.text
    assert rows(pg, MON)[("E002", MON)].statut == "RETARD" and rows(pg, MON)[("E007", MON)].statut == "TERRAIN"

    bad = logged_client.post("/admin/terrain/import", files={"fichier": ("vieux.xls", b"...")}, follow_redirects=True)
    assert "format .xlsx" in bad.text
    empty = logged_client.post("/admin/terrain/import", files={"fichier": ("vide.csv", b"\r\n")}, follow_redirects=True)
    assert "vide" in empty.text

    model = load_workbook(io.BytesIO(logged_client.get("/admin/terrain/modele.xlsx").content)).active
    assert [c.value for c in model[1]] == ["Matricule", "Motif"]
    export = load_workbook(io.BytesIO(logged_client.get("/admin/terrain/export.xlsx").content)).active
    assert list(export.iter_rows(min_row=2, values_only=True)) == [("E007", "Délégué médical", "Diop Aminata")]
    assert "Importer une liste depuis Excel" in logged_client.get("/admin/terrain").text

    for entry in pointage.field_entries(pg, pointage.Mapping.from_json(_cfg_data())):
        logged_client.post(f"/admin/terrain/{entry['id']}/delete")


def test_average_punches_and_duration_colours(configured, logged_client):
    """Moyennes du 1er et du dernier pointage ; durée validée en vert (objectif atteint) ou rouge ; export."""
    import io

    from openpyxl import load_workbook

    # Lundi, employés de la liste au bureau : 1ers pointages 07h20, 08h10, 07h35, 07h40, 07h45 → 07h42 ;
    # derniers (2 pointages ou plus) 17h10, 16h45, 15h50, 12h00 → 15h26.
    page = logged_client.get(f"/suivi?date={MON.isoformat()}&pop=liste").text
    assert "Pointages moyens" in page and "07h42" in page and "15h26" in page
    assert 'class="avg-row"' in page and "Moyenne" in page
    # Objectif par défaut 8h : Awa (7h30) et Moussa (6h50) en rouge.
    assert page.count('class="dur dur-ko"') >= 2 and 'class="dur dur-ok"' in page  # légende
    values = {k: v for k, _, _, v in pointage.PARAMS if k != "jours_ouvres"}
    r = logged_client.post("/admin/parametres", data={**values, "objectif_duree": "07:00",
                           "jours_ouvres": ["1", "2", "3", "4", "5"], "date_effet": MON.isoformat()}, follow_redirects=True)
    assert "paramètre(s) modifié(s)" in r.text
    page = logged_client.get(f"/suivi?date={MON.isoformat()}&pop=liste&q=E001").text
    assert 'dur dur-ok"' in page.split("<tbody>")[1] and "≥ 7h00" in page
    xlsx = load_workbook(io.BytesIO(logged_client.get(f"/suivi/export.xlsx?date={MON.isoformat()}&pop=liste").content))
    ws = xlsx["Suivi journalier"]
    fills = {ws.cell(i, 2).value: ws.cell(i, 11).fill.fgColor.rgb for i in range(2, ws.max_row + 1) if ws.cell(i, 11).value}
    assert fills["E001"].endswith("C6EFCE") and fills["E002"].endswith("FFC7CE")  # 7h30 ≥ 7h ; 6h50 < 7h
    # Durée effective colorée de la même façon : Awa 8h20 ≥ 7h (vert), Aminata 4h15 < 7h (rouge).
    eff = {ws.cell(i, 2).value: ws.cell(i, 12).fill.fgColor.rgb for i in range(2, ws.max_row + 1) if ws.cell(i, 12).value}
    assert eff["E001"].endswith("C6EFCE") and eff["E007"].endswith("FFC7CE")
    cells = page.split("<tbody>")[1].split("</tbody>")[0]
    assert cells.count('class="dur dur-ok"') == 2  # validée 7h30 et effective 8h20 d'Awa
    last = [c.value for c in ws[ws.max_row]]
    assert last[0] == "Moyenne" and last[6].strftime("%H:%M") == "07:42" and last[7].strftime("%H:%M") == "15:26"
    logged_client.post("/admin/parametres", data={**values, "objectif_duree": "08:00",
                       "jours_ouvres": ["1", "2", "3", "4", "5"], "date_effet": MON.isoformat()})


def test_sick_leave_workflow_and_hr(configured, pg, logged_client):
    """Arrêt maladie : déclaration par l'employé (justificatif obligatoire), validation N+1 puis RH, statut dans les
    calculs ; saisie RH validée d'office ; refus, annulation, droits d'accès au justificatif, circuit paramétrable."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import SickLeave, SickLeaveAction, User

    THU, FRI = date(2026, 9, 24), date(2026, 9, 25)
    pdf = b"%PDF-1.4 arret de travail"
    for data in ({"username": "moussa", "role": "lecteur", "emp_matricule": "E002"},
                 {"username": "awa", "role": "manager", "emp_matricule": "E001"},
                 {"username": "rh1", "role": "lecteur", "sick_leave_hr": "true"},
                 {"username": "fatou", "role": "lecteur", "emp_matricule": "E003"}):
        r = logged_client.post("/admin/users/save", data={**data, "password": "motdepasse1"}, follow_redirects=True)
        assert "enregistré" in r.text, r.text[:500]
    r = logged_client.post("/admin/arrets", data={"type": ["responsable", "rh"], "user": ["", ""]}, follow_redirects=True)
    assert "Responsable N+1 de l&#39;employé → Agent RH habilité" in r.text

    def client(name):
        c = TestClient(app)
        c.post("/login", data={"username": name, "password": "motdepasse1"})
        return c

    try:
        moussa, awa, rh1, fatou = client("moussa"), client("awa"), client("rh1"), client("fatou")
        page = moussa.get("/arrets").text
        assert "Déclarer un arrêt maladie" in page and "Responsable N+1 de l&#39;employé → Agent RH habilité" in page
        r = moussa.post("/arrets", data={"du": THU.isoformat(), "au": FRI.isoformat()}, follow_redirects=True)
        assert "Joignez le justificatif" in r.text
        r = moussa.post("/arrets", data={"du": THU.isoformat(), "au": FRI.isoformat()},
                        files={"justificatif": ("faux.pdf", b"pas un pdf")}, follow_redirects=True)
        assert "ne correspond pas" in r.text
        r = moussa.post("/arrets", data={"du": THU.isoformat(), "au": FRI.isoformat(), "matricule": "E005",
                                         "commentaire": "Grippe"},
                        files={"justificatif": ("arret.pdf", pdf)}, follow_redirects=True)
        assert "transmis pour validation" in r.text and "Ndiaye Moussa" in r.text  # pour lui-même, pas E005
        with SessionLocal() as db:
            leave = db.query(SickLeave).one()
            assert (leave.matricule, leave.manager_matricule, leave.status) == ("E002", "E001", "en_attente")
            lid = leave.id
        assert "Valider" not in moussa.get(f"/arrets/{lid}").text  # pas de validation de son propre arrêt
        assert moussa.post(f"/arrets/{lid}/decision", data={"decision": "valider"}, follow_redirects=True).status_code == 200
        assert rows(pg, THU)[("E002", THU)].statut == "ABSENT"  # en attente : pas encore d'effet

        # Étape 1 : le responsable N+1 (Awa) ; étape 2 : les RH.
        assert "À valider" in awa.get("/arrets").text and 'class="nav-badge"' in awa.get("/arrets").text
        assert "À valider" not in rh1.get("/arrets").text
        r = awa.post(f"/arrets/{lid}/decision", data={"decision": "valider", "commentaire": "OK"}, follow_redirects=True)
        assert "passe à l&#39;étape suivante" in r.text
        assert "À valider" in rh1.get("/arrets").text
        r = rh1.post(f"/arrets/{lid}/decision", data={"decision": "valider"}, follow_redirects=True)
        assert "Arrêt validé" in r.text
        thu = rows(pg, THU)[("E002", THU)]
        assert (thu.statut, thu.statut_libelle, hm(thu.duree_validee)) == ("ARRET_MALADIE", "Arrêt maladie", "8h00")
        assert rows(pg, FRI)[("E002", FRI)].statut == "ARRET_MALADIE"
        assert 'badge st-sick">Arrêt maladie' in logged_client.get(f"/suivi?date={THU.isoformat()}").text

        # Justificatif : l'employé, son responsable et les RH ; pas un autre employé.
        doc = moussa.get(f"/arrets/{lid}/justificatif")
        assert doc.status_code == 200 and doc.content == pdf and doc.headers["content-type"] == "application/pdf"
        assert awa.get(f"/arrets/{lid}/justificatif").status_code == 200
        assert fatou.get(f"/arrets/{lid}/justificatif").status_code == 404
        assert "Ndiaye" not in fatou.get("/arrets").text

        # Chevauchement refusé ; refus motivé obligatoire.
        r = moussa.post("/arrets", data={"du": FRI.isoformat(), "au": FRI.isoformat()},
                        files={"justificatif": ("a.png", b"\x89PNG....")}, follow_redirects=True)
        assert "couvre déjà" in r.text
        r = moussa.post("/arrets", data={"du": "2026-10-05", "au": "2026-10-06"},
                        files={"justificatif": ("b.jpg", b"\xff\xd8\xff....")}, follow_redirects=True)
        with SessionLocal() as db:
            second = db.query(SickLeave).filter(SickLeave.id != lid).one().id
        assert "motif du refus" in awa.post(f"/arrets/{second}/decision", data={"decision": "refuser"},
                                             follow_redirects=True).text
        r = awa.post(f"/arrets/{second}/decision", data={"decision": "refuser", "commentaire": "Dates erronées"},
                     follow_redirects=True)
        assert "Arrêt refusé" in r.text and "Dates erronées" in r.text

        # Saisie RH : n'importe quel employé, sans justificatif, validée d'office.
        r = rh1.post("/arrets", data={"matricule": "E005", "du": THU.isoformat(), "au": THU.isoformat()},
                     follow_redirects=True)
        assert "enregistré et validé" in r.text and rows(pg, THU)[("E005", THU)].statut == "ARRET_MALADIE"

        # Annulation d'un arrêt validé : RH seulement ; les jours redeviennent absents.
        assert "RH" in moussa.post(f"/arrets/{lid}/annuler", data={}, follow_redirects=True).text
        r = rh1.post(f"/arrets/{lid}/annuler", data={"commentaire": "Erreur"}, follow_redirects=True)
        assert "Arrêt annulé" in r.text and rows(pg, THU)[("E002", THU)].statut == "ABSENT"
        with SessionLocal() as db:
            actions = [a.action for a in db.query(SickLeaveAction).filter(SickLeaveAction.leave_id == lid)
                       .order_by(SickLeaveAction.id)]
        assert actions == ["declare", "valide", "valide", "annule"]

        # Circuit : un utilisateur désigné doit être choisi ; sans étape, validé dès la déclaration.
        bad = logged_client.post("/admin/arrets", data={"type": ["utilisateur"], "user": [""]}, follow_redirects=True)
        assert "Choisissez l&#39;utilisateur" in bad.text
        r = logged_client.post("/admin/arrets", data={}, follow_redirects=True)
        assert "aucune étape" in r.text
        r = moussa.post("/arrets", data={"du": "2026-10-12", "au": "2026-10-12"},
                        files={"justificatif": ("c.pdf", pdf)}, follow_redirects=True)
        assert "Validé" in r.text
        for c in (moussa, awa, rh1, fatou):
            c.close()
    finally:
        S = pointage.qi(pointage.Mapping.from_json(_cfg_data()).objs)
        with pg.begin() as c:
            c.execute(text(f"DELETE FROM {S}.pointage_arrets_maladie"))
        with SessionLocal() as db:
            db.query(SickLeaveAction).delete()
            db.query(SickLeave).delete()
            db.query(User).filter(User.username.in_(["moussa", "awa", "rh1", "fatou"])).delete()
            db.commit()
        logged_client.post("/admin/arrets", data={"type": ["responsable", "rh"], "user": ["", ""]})


def _planning_xlsx(days: dict) -> bytes:
    """Planning au format « PLANNING ATF » : légende, ligne MATRICULE, ligne NOM, une ligne par jour."""
    from datetime import time as t

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "PLANNING"
    ws["C1"] = "VEUILLEZ SAISIR LE CODE DE CHAQUE PLANNING HORAIRE DEVANT LE JOUR DE CHAQUE COLLABORATEUR"
    for col, value in zip("BCDFGH", ["Code", "Heure Début", "Heure Fin", "Code", "Heure Début", "Heure Fin"]):
        ws[f"{col}5"] = value
    legend = [("P1", t(6), t(14), "P5", "ASTREINTE"), ("P2", t(14), t(22), "P6", "FERIE PAYE"),
              ("P3", t(22), t(6), "P8", "FORMATION"), ("P4", "REPOS", None, "P9", "FORMATION CONTINUE")]
    for i, (c1, d1, f1, c2, l2) in enumerate(legend, start=6):
        ws[f"B{i}"], ws[f"C{i}"], ws[f"D{i}"], ws[f"F{i}"], ws[f"G{i}"] = c1, d1, f1, c2, l2
    ws["J5"], ws["K5"], ws["L5"] = "Code", "Heure Début", "Heure Fin"
    ws["J6"], ws["K6"], ws["L6"] = "0618", t(6), t(18)
    ws["J7"], ws["K7"], ws["L7"] = "1806", t(18), t(6)
    ws.append([])
    ws.append(["MATRICULE", "E005", "X999"])
    ws.append(["NOM", "BA KHADY     ", "INCONNU"])
    ws.append(["DATE"])
    for day, (code, other) in days.items():
        ws.append([datetime(day.year, day.month, day.day), code, other])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_shift_planning_import_and_calculations(configured, pg, logged_client):
    """Horaires postés : nuit 18h-6h rattachée au jour de début, repos, retard sur l'horaire du poste, absence un
    dimanche planifié, congé du planning ; import du fichier Excel tel quel (618 = 0618, légende, inconnus)."""
    THU, FRI, SUN, MON2 = date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 27), date(2026, 9, 28)
    S = f"{SCHEMA}"
    punches = [(5, THU, "17:55"), (5, FRI, "06:05"), (5, SAT, "15:00"), (5, SAT, "22:00")]
    try:
        with pg.begin() as c:
            for emp, day, hhmm in punches:
                c.execute(text(f"INSERT INTO {S}.punchlog (employe_id, date_pointage, heure_pointage, terminal) "
                               f"VALUES (:e, :d, :h, 'PLAN')"), {"e": emp, "d": day, "h": hhmm.replace(":", "") + "00"})
        # Codes par défaut installés avec les calculs (légende du planning ATF).
        page = logged_client.get("/admin/planning")
        assert page.status_code == 200 and "1806" in page.text and "Aucun planning" in page.text

        data = _planning_xlsx({THU: (1806, "P1"), FRI: ("P4", "P1"), SAT: ("P2", "P1"), SUN: ("P1", None),
                               MON2: ("P11", None), date(2026, 9, 29): ("ZZ", None)})
        r = logged_client.post("/admin/planning/import", files={"fichier": ("PLANNING ATF.xlsx", data)},
                               follow_redirects=True)
        assert "importé du 24/09/2026 au 29/09/2026 : 1 employé(s), 5 jour(s)" in r.text, r.text[:3000]
        assert "P9" in r.text and "Nouveaux codes" in r.text          # code de la légende créé
        assert "« ZZ » (1 j)" in r.text and "X999" in r.text           # code et matricule inconnus signalés
        assert "BA" in r.text.upper() and 'pl pl-nuit">1806' in r.text
        assert "28 h" in r.text  # heures planifiées : 12 + 8 + 8 (repos et congé non comptés)
        # Période libre : longue plage, plage vide (lien vers le planning importé), plage trop longue ramenée à un an.
        page = logged_client.get("/admin/planning?du=2026-01-01&au=2026-12-31").text
        assert "Planning du 01/01/2026 au 31/12/2026" in page and "365 jour(s)" in page and "28 h" in page
        page = logged_client.get("/admin/planning?du=2007-11-21&au=2008-01-22").text
        assert "Aucun planning sur cette période" in page and "du=2026-09-24&au=2026-09-28" in page
        page = logged_client.get("/admin/planning?du=2020-01-01&au=2026-12-31").text
        assert "Période ramenée à 366 jours" in page and "Tout le planning" in page and 'id="pl_expand"' not in page

        got = rows(pg, THU, MON2)
        night = got[("E005", THU)]
        assert night.statut == "A_L_HEURE" and night.nb_pointages == 2 and night.poste == "1806 · 18h00–06h00"
        assert hm(night.premier_pointage) == "17h55" and night.dernier_pointage.date() == FRI
        assert hm(night.duree_validee) == "12h00" and hm(night.duree_effective) == "12h10"
        rest = got[("E005", FRI)]
        assert rest.statut == "REPOS" and rest.nb_pointages == 0 and not rest.jour_ouvre
        assert hm(rest.duree_validee) == "8h00" and rest.duree_effective is None  # repos crédité de 8h validées
        late = got[("E005", SAT)]  # 15h00 pour un poste de 14h (tolérance 45 min) : retard de 60 min
        assert late.statut == "RETARD" and late.retard_min == 60 and hm(late.duree_validee) == "7h00" and late.jour_ouvre
        assert got[("E005", SUN)].statut == "ABSENT" and got[("E005", SUN)].jour_ouvre
        leave = got[("E005", MON2)]
        assert leave.statut == "CONGE_ANNUEL" and hm(leave.duree_validee) == "8h00"
        assert got[("E004", SUN)].statut == "NON_OUVRE" and got[("E004", SUN)].poste is None  # non planifié : bureau

        # Détail de la nuit : le départ du lendemain est affiché ; suivi et rapports affichent le poste.
        detail = logged_client.get(f"/suivi/detail?key=5&jour={THU.isoformat()}").text
        assert "17:55" in detail and "06:05" in detail
        page = logged_client.get(f"/suivi?du={THU.isoformat()}&au={SAT.isoformat()}&q=E005").text
        assert "1806 · 18h00–06h00" in page and "+1 j" in page and "Repos (planning)" in page
        assert logged_client.get(f"/rapports?du={THU.isoformat()}&au={MON2.isoformat()}").status_code == 200

        # Codes de poste : modification, refus de suppression d'un code utilisé, export réimportable.
        r = logged_client.post("/admin/planning/postes", data={"code": "p7", "libelle": "Journée", "type": "travail",
                                                               "debut": "08:00", "fin": "08:00"}, follow_redirects=True)
        assert "heure de début et une heure de fin différentes" in r.text
        r = logged_client.post("/admin/planning/postes", data={"code": "p7", "libelle": "Journée", "type": "travail",
                                                               "debut": "08:00", "fin": "16:30", "pause": "0h30"},
                               follow_redirects=True)
        assert "Code « P7 » enregistré" in r.text
        assert "utilisé par" in logged_client.post("/admin/planning/postes/P4/delete", follow_redirects=True).text
        assert "Code « P7 » supprimé" in logged_client.post("/admin/planning/postes/P7/delete", follow_redirects=True).text
        export = logged_client.get(f"/admin/planning/export.xlsx?du={THU.isoformat()}&au={MON2.isoformat()}")
        plan = pointage.read_planning_file("x.xlsx", export.content)
        assert ("E005", THU, "1806") in plan["entries"] and ("E005", FRI, "P4") in plan["entries"]

        # Suppression : les jours reviennent à l'horaire de bureau.
        r = logged_client.post("/admin/planning/supprimer", data={"du": THU.isoformat(), "au": MON2.isoformat()},
                               follow_redirects=True)
        assert "5 jour(s) planifié(s) supprimé(s)" in r.text
        assert rows(pg, FRI)[("E005", FRI)].statut != "REPOS"
    finally:
        with pg.begin() as c:
            c.execute(text(f"DELETE FROM {S}.punchlog WHERE terminal = 'PLAN'"))
            c.execute(text(f"DELETE FROM {S}.pointage_planning"))
            c.execute(text(f"DELETE FROM {S}.pointage_postes WHERE code IN ('P9', 'P7')"))


def test_parse_planning_rejects_unknown_layout():
    with pytest.raises(pointage.PointageError, match="MATRICULE"):
        pointage.parse_planning([["a", "b"], [1, 2]])
    plan = pointage.parse_planning([["MATRICULE", 590128.0], ["NOM", "KARAMOKO"], ["19/08/2026", 618]])
    assert plan["entries"] == [("590128", date(2026, 8, 19), "618")] and plan["names"] == {"590128": "KARAMOKO"}


def test_sick_leave_email_notifications(configured, pg, logged_client, monkeypatch):
    """Mails des arrêts maladie : valideur de l'étape en cours, puis l'employé à la décision ; mode test redirigé,
    auteur de l'action exclu, adresse du compte ou de la fiche employé, désactivé par défaut."""
    from fastapi.testclient import TestClient

    from app import arrets, mails
    from app.main import app
    from app.models import MailSettings, SickLeave, SickLeaveAction, User

    delivered, results = [], []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None): pass
        def ehlo(self): pass
        def starttls(self, context=None): pass
        def login(self, user, pwd): pass
        def send_message(self, msg): delivered.append(msg)
        def quit(self): pass

    monkeypatch.setattr(mails.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(arrets, "start_notify", lambda *a: results.append(arrets.notify(*a)))
    for data in ({"username": "n_moussa", "emp_matricule": "E002", "email": "moussa@exemple.com"},
                 {"username": "n_awa", "emp_matricule": "E001", "email": "awa@exemple.com"},
                 {"username": "n_rh", "sick_leave_hr": "true", "email": "rh@exemple.com"},
                 {"username": "n_bad", "email": "pas-une-adresse"}):
        r = logged_client.post("/admin/users/save", data={**data, "role": "lecteur", "password": "motdepasse1"},
                               follow_redirects=True)
        assert ("invalide" if data["username"] == "n_bad" else "enregistré") in r.text
    logged_client.post("/admin/arrets", data={"type": ["responsable", "rh"], "user": ["", ""]})
    with SessionLocal() as db:
        s = mails.get_settings(db)
        s.smtp_host, s.from_email, s.mode, s.test_recipients = "smtp.test", "pointage@exemple.com", "test", "qa@exemple.com"
        db.commit()

    def client(name):
        c = TestClient(app)
        c.post("/login", data={"username": name, "password": "motdepasse1"})
        return c

    pdf = b"%PDF-1.4 arret"
    try:
        moussa, awa, rh = client("n_moussa"), client("n_awa"), client("n_rh")
        moussa.post("/arrets", data={"du": "2026-10-05", "au": "2026-10-06"}, files={"justificatif": ("a.pdf", pdf)})
        assert results[-1]["status"] == "disabled" and not delivered  # désactivé par défaut

        page = logged_client.post("/admin/arrets/notifications", data={"notify": "true"}, follow_redirects=True).text
        assert "Notifications par e-mail activées" in page and "smtp.test" in page and "qa@exemple.com" in page
        with SessionLocal() as db:
            db.query(SickLeave).delete()
            db.commit()

        # Déclaration : le responsable N+1 est prévenu ; mode test → redirigé vers l'adresse de test.
        moussa.post("/arrets", data={"du": "2026-10-05", "au": "2026-10-06", "commentaire": "Grippe"},
                    files={"justificatif": ("a.pdf", pdf)})
        assert results[-1] == {"status": "sent", "sent": 1, "to": ["awa@exemple.com"]}
        msg = delivered[-1]
        assert msg["To"] == "qa@exemple.com" and msg["Subject"] == "[TEST] Arrêt maladie à valider — Ndiaye Moussa"
        body = msg.get_body(("html",)).get_content()
        assert "MODE TEST" in body and "awa@exemple.com" in body and "Grippe" in body and "/arrets/" in body
        with SessionLocal() as db:
            lid = db.query(SickLeave).one().id

        # Étape 1 validée par Awa → les RH ; étape 2 validée par les RH → l'employé (pas l'auteur de l'action).
        awa.post(f"/arrets/{lid}/decision", data={"decision": "valider"})
        assert results[-1]["to"] == ["rh@exemple.com"]
        rh.post(f"/arrets/{lid}/decision", data={"decision": "valider"})
        assert results[-1]["to"] == ["moussa@exemple.com"] and "validé" in delivered[-1]["Subject"]

        # Production : le mail part vers la vraie adresse ; refus avec motif.
        with SessionLocal() as db:
            mails.get_settings(db).mode = "production"
            db.commit()
        moussa.post("/arrets", data={"du": "2026-10-12", "au": "2026-10-12"}, files={"justificatif": ("a.pdf", pdf)})
        assert delivered[-1]["To"] == "awa@exemple.com" and not delivered[-1]["Subject"].startswith("[TEST]")
        with SessionLocal() as db:
            lid2 = db.query(SickLeave).filter(SickLeave.id != lid).one().id
        awa.post(f"/arrets/{lid2}/decision", data={"decision": "refuser", "commentaire": "Justificatif illisible"})
        msg = delivered[-1]
        assert msg["To"] == "moussa@exemple.com" and msg["Subject"] == "Arrêt maladie refusé — Ndiaye Moussa"
        assert "Justificatif illisible" in msg.get_body(("html",)).get_content()
        for c in (moussa, awa, rh):
            c.close()
    finally:
        with pg.begin() as c:
            c.execute(text(f"DELETE FROM {SCHEMA}.pointage_arrets_maladie"))
        with SessionLocal() as db:
            db.query(SickLeaveAction).delete()
            db.query(SickLeave).delete()
            db.query(User).filter(User.username.like("n\\_%", escape="\\")).delete(synchronize_session=False)
            s = db.query(MailSettings).first()
            s.smtp_host, s.from_email, s.mode, s.test_recipients = "", "", "test", ""
            arrets.set_notify(db, False, "test")
            db.commit()


def test_missions_from_authorisation_table(configured, pg, logged_client):
    """Autorisations de mission (ex. feuille Smartsheet synchronisée) : « En mission », 8h validées, même si la
    personne a badgé ; seulement les missions approuvées et les jours ouvrés."""
    import dataclasses

    with pg.begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.missions_ss"))
        c.execute(text(f"""CREATE TABLE {SCHEMA}.missions_ss (row_id bigint, matricule text, date_de_depart date,
                          date_de_retour date, statut text)"""))
        c.execute(text(f"""INSERT INTO {SCHEMA}.missions_ss VALUES
            (1, 'E002', '2026-09-21', '2026-09-22', 'Approuvée'),   -- a badgé lundi : en mission quand même
            (2, 'E005', '2026-09-21', NULL, ' APPROUVE '),          -- sans date de retour : un jour ; casse ignorée
            (3, 'E003', '2026-09-21', '2026-09-21', 'En attente'),  -- non approuvée
            (4, 'E004', '2026-09-26', '2026-09-26', 'Validée')      -- samedi : reste non ouvré"""))
    with SessionLocal() as db:
        m = pointage.Mapping.from_json(db.query(PointageConfig).one().data)
    m = dataclasses.replace(m, objects_schema="pt_test_mission", mission_table="missions_ss", mission_emp_col="matricule",
                            mission_start_col="date_de_depart", mission_end_col="date_de_retour",
                            mission_state_col="statut")
    assert m.mission_ref == "matricule" and "Approuvée" in m.mission_state_values
    with pg.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS pt_test_mission CASCADE"))
    pointage.install(pg, m, "test")
    try:
        with pg.connect() as c:
            r = {(x.matricule, x.jour): x for x in c.execute(text(
                "SELECT * FROM pt_test_mission.f_pointage_journalier(:du, :au)"), {"du": MON, "au": SAT}).mappings()}
        moussa = r[("E002", MON)]
        assert (moussa.statut, moussa.statut_libelle) == ("MISSION", "En mission")
        assert hm(moussa.duree_validee) == "8h00" and hm(moussa.duree_effective) == "7h05"  # effective : ses pointages
        assert r[("E002", TUE)].statut == "MISSION" and r[("E002", WED)].statut != "MISSION"
        khady = r[("E005", MON)]
        assert khady.statut == "MISSION" and hm(khady.duree_validee) == "8h00" and hm(khady.duree_effective) == "8h00"
        assert r[("E005", TUE)].statut == "ABSENT"
        assert r[("E003", MON)].statut == "A_L_HEURE" and r[("E004", SAT)].statut == "NON_OUVRE"
        assert r[("E002", MON)].retard_min is None

        # Administration : section Missions avec colonnes proposées.
        admin = logged_client.get(f"/admin/pointage?conn_id={configured}&schema={SCHEMA}&punch_table=punchlog"
                                  f"&emp_table=Employes&mission_table=missions_ss").text
        section = admin.split("Table des autorisations de mission")[1]
        assert '<option value="date_de_depart" selected' in section and '<option value="date_de_retour" selected' in section
        assert '<option value="statut" selected' in section

        # Feuille Smartsheet sans matricule : lien par e-mail (colonne « contact »), plusieurs dates d'aller et de
        # retour par ligne → du premier aller au dernier retour.
        with pg.begin() as c:
            c.execute(text(f'ALTER TABLE {SCHEMA}."Personnel" ADD COLUMN IF NOT EXISTS "Email" text'))
            c.execute(text(f'UPDATE {SCHEMA}."Personnel" SET "Email" = lower("Prenom" || \'.\' || "Nom") || \'@exemple.com\''))
            c.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.missions_email"))
            c.execute(text(f"""CREATE TABLE {SCHEMA}.missions_email (row_id bigint, demandeur text, demandeur_nom text,
                date_aller_1 date, date_retour_1 date, date_aller_2 date, date_retour_2 date, statut text)"""))
            c.execute(text(f"""INSERT INTO {SCHEMA}.missions_email VALUES
                (1, 'fatou.sow@exemple.com', 'Fatou Sow', '2026-09-22', '2026-09-22', '2026-09-24', '2026-09-25', 'Approuvée'),
                (2, 'ibou.fall@exemple.com', 'Ibou Fall', NULL, NULL, '2026-09-23', NULL, 'Approuvée'),
                (3, 'inconnu@exemple.com', 'Inconnu', '2026-09-22', '2026-09-22', NULL, NULL, 'Approuvée'),
                (4, 'aminata.diop@exemple.com', 'Aminata Diop', '2026-09-21', '2026-09-21', NULL, NULL, 'Refusée')"""))
        m2 = dataclasses.replace(m, email_col="Email", email_in="person", mission_table="missions_email",
                                 mission_emp_col="demandeur", mission_ref="email",
                                 mission_start_col="date_aller_1, date_aller_2",
                                 mission_end_col="date_retour_1, date_retour_2", mission_state_col="statut")
        pointage.install(pg, m2, "test")
        with pg.connect() as c:
            r = {(x.matricule, x.jour): x.statut for x in c.execute(text(
                "SELECT * FROM pt_test_mission.f_pointage_journalier(:du, :au)"), {"du": MON, "au": SAT}).mappings()}
        fatou = [r[("E003", d)] for d in (MON, TUE, WED, date(2026, 9, 24), date(2026, 9, 25))]
        assert fatou == ["A_L_HEURE", "MISSION", "MISSION", "MISSION", "MISSION"]  # 22 → 25 (écart inclus)
        assert r[("E004", WED)] == "MISSION" and r[("E004", date(2026, 9, 24))] == "ABSENT"  # seul l'aller 2 renseigné
        assert r[("E007", MON)] == "RETARD"  # mission refusée
        match = pointage.unmatched_requests(pg, m2, "mission")
        assert match == {"total": 3, "sans_employe": 1, "exemples": [("inconnu@exemple.com", 1)]}

        # Administration : dates multiples et lien par e-mail proposés automatiquement.
        admin = logged_client.get(f"/admin/pointage?conn_id={configured}&schema={SCHEMA}&punch_table=punchlog"
                                  f"&emp_table=Employes&email_col=Email&mission_table=missions_email"
                                  f"&mission_ref=matricule").text
        section = admin.split("Table des autorisations de mission")[1].split("</fieldset>")[0]
        assert '<option value="date_aller_1" selected' in section and '<option value="date_aller_2" selected' in section
        assert '<option value="date_retour_2" selected' in section and '<option value="date_retour_1" selected' in section
        assert '<option value="demandeur" selected' in section and '<option value="email" selected' in section
    finally:
        with pg.begin() as c:
            c.execute(text("DROP SCHEMA IF EXISTS pt_test_mission CASCADE"))
            c.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.missions_ss"))
            c.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.missions_email"))
            c.execute(text(f'ALTER TABLE {SCHEMA}."Personnel" DROP COLUMN IF EXISTS "Email"'))


def test_manager_digests(configured, pg, logged_client, monkeypatch):
    """Résumés par mail aux responsables : équipe N-1 (sans le responsable), constats, planification, mode test,
    un seul envoi par période, journal."""
    from app import digests, mails
    from app.models import DigestLog, DigestSubscriber, MailSettings

    # Périodes et heures d'envoi.
    assert digests.period("quotidien", date(2026, 9, 22)) == (MON, MON)
    assert digests.period("hebdomadaire", date(2026, 9, 29)) == (MON, date(2026, 9, 27))
    with SessionLocal() as db:
        s = digests.get_settings(db)
        s.daily_enabled, s.daily_time, s.weekly_enabled, s.weekly_day, s.weekly_time = True, "07:30", True, 1, "08:00"
        db.commit()
        assert digests.due_kinds(s, datetime(2026, 9, 28, 7, 0)) == []
        assert digests.due_kinds(s, datetime(2026, 9, 28, 7, 45)) == ["quotidien"]
        assert digests.due_kinds(s, datetime(2026, 9, 29, 6, 0)) == ["hebdomadaire"]  # lundi manqué : rattrapé mardi

    # Contenu : équipe directe de Diallo (Ndiaye, Sow ; Gueye inactif), sans Diallo elle-même.
    engine = create_engine(POSTGRES_URL)
    with SessionLocal() as db:
        m = pointage.Mapping.from_json(db.query(PointageConfig).one().data)
    data = digests.collect(pg, m, "1", "quotidien", MON, MON)
    assert [r["matricule"] for r in data["rows"]] == ["E002", "E003"]  # retard d'abord, puis par nom
    notes = " ".join(t for _, t in digests.insights(data))
    assert "1 retard(s) : Ndiaye Moussa (40 min)" in notes and "Taux de présence de 100 %" in notes
    subject, body, _ = digests.render(data, "Diallo Awa", "ACME", "http://pointage:8000")
    assert subject == "Pointages de votre équipe — lundi 21 septembre 2026"
    assert "Point du jour — équipe de Diallo Awa" in body and "http://pointage:8000/suivi?du=2026-09-21" in body
    assert "<strong>Diallo Awa</strong>" not in body and "<strong>Ndiaye Moussa</strong>" in body
    week = digests.collect(pg, m, "1", "hebdomadaire", MON, date(2026, 9, 27))
    subject, body, _ = digests.render(week, "Diallo Awa")
    assert subject.startswith("Bilan hebdomadaire des pointages de votre équipe — semaine du 21 septembre au 27")
    assert "Détail par collaborateur" in body
    engine.dispose()

    # Administration : responsables proposés, abonnements, aperçu.
    page = logged_client.get("/admin/resumes").text
    assert "Diallo Awa" in page and "Sow Fatou" in page and 'name="freq_1"' in page
    r = logged_client.post("/admin/resumes/abonnes", data={"freq_1": "les_deux", "email_1": "awa@exemple.com",
                                                          "freq_3": "hebdomadaire", "email_3": "pas-une-adresse"},
                           follow_redirects=True)
    assert "2 responsable(s) abonné(s)" in r.text and "Adresse(s) invalide(s) ignorée(s)" in r.text
    preview = logged_client.get("/admin/resumes/apercu?manager=1&kind=hebdomadaire").text
    assert "Point de la semaine — équipe de Diallo Awa" in preview

    # Envoi planifié : SMTP simulé, mode test → adresses de test ; un seul envoi par période.
    delivered = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None): pass
        def ehlo(self): pass
        def starttls(self, context=None): pass
        def login(self, user, pwd): pass
        def send_message(self, msg): delivered.append(msg)
        def quit(self): pass

    monkeypatch.setattr(mails.smtplib, "SMTP", FakeSMTP)
    with SessionLocal() as db:
        ms = mails.get_settings(db)
        ms.smtp_host, ms.from_email, ms.mode, ms.test_recipients = "smtp.test", "pointage@exemple.com", "test", "qa@exemple.com"
        db.commit()
    try:
        result = digests.run_due(datetime(2026, 9, 28, 8, 30))  # lundi : veille = dimanche, semaine du 21 au 27
        assert result == {"status": "done", "sent": 1, "skipped": 2, "failed": 0}, result
        # Diallo : hebdomadaire envoyé, quotidien du dimanche ignoré (personne attendu) ; Sow : aucune adresse
        # valide → ignoré, comme il le serait en production.
        subjects = [msg["Subject"] for msg in delivered]
        assert len(subjects) == 1 and subjects[0].startswith("[TEST] Bilan hebdomadaire")
        assert all(msg["To"] == "qa@exemple.com" for msg in delivered)
        assert "MODE TEST" in delivered[0].get_body(("html",)).get_content()
        assert digests.run_due(datetime(2026, 9, 28, 9, 0)) == {"status": "idle"}  # déjà traité
        page = logged_client.get("/admin/resumes").text
        assert "Journal des envois" in page and "Envoyé" in page and "personne n&#39;était attendu" in page
        assert "aucune adresse e-mail" in page

        # Essai depuis l'administration, vers une adresse choisie.
        r = logged_client.post("/admin/resumes/essai", data={"manager": "1", "kind": "quotidien", "to": "moi@exemple.com"},
                               follow_redirects=True)
        assert "Résumé quotidien d&#39;essai envoyé à moi@exemple.com" in r.text
        assert delivered[-1]["To"] == "moi@exemple.com" and delivered[-1]["Subject"].startswith("[TEST] Pointages")
    finally:
        with SessionLocal() as db:
            db.query(DigestLog).delete()
            db.query(DigestSubscriber).delete()
            s = digests.get_settings(db)
            s.daily_enabled = s.weekly_enabled = False
            ms = db.query(MailSettings).first()
            ms.smtp_host, ms.from_email, ms.mode, ms.test_recipients = "", "", "test", ""
            db.commit()
