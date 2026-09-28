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
        c.execute(text(f"""CREATE TABLE {SCHEMA}."Personnel" ("IDPersonnel" int PRIMARY KEY, "Nom" text, "Prenom" text)"""))
        c.execute(text(f"""CREATE TABLE {SCHEMA}.hierarchie (matricule_employe text, matricule_responsable text)"""))
        c.execute(text(f"CREATE TABLE {SCHEMA}.punchlog (id serial PRIMARY KEY, employe_id int, "
                       f"date_pointage date, heure_pointage text, terminal text)"))
        c.execute(text(f"INSERT INTO {SCHEMA}.services VALUES (1, 'Production'), (2, 'RH')"))
        c.execute(text(f"""INSERT INTO {SCHEMA}."Employes" VALUES
            (1, 'E001', 101, 1, '1'), (2, 'E002', 102, 1, '1'), (3, 'E003', 103, 2, '1'), (4, 'E004', 104, 2, '1'),
            (5, 'E005', 105, 2, '1'), (6, 'E006', 106, 1, '0'), (7, 'E007', 107, 1, '1')"""))
        c.execute(text(f"""INSERT INTO {SCHEMA}."Personnel" VALUES
            (101, 'Diallo', 'Awa'), (102, 'Ndiaye', 'Moussa'), (103, 'Sow', 'Fatou'), (104, 'Fall', 'Ibou'),
            (105, 'Ba', 'Khady'), (106, 'Gueye', 'Ousmane'), (107, 'Diop', 'Aminata')"""))
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
    assert hm(awa.heure_validee) == "7h30" and hm(awa.duree_effective) == "8h20" and awa.nb_pointages == 3
    moussa = r[("E002", MON)]
    assert moussa.statut == "RETARD" and hm(moussa.heure_validee) == "6h50" and hm(moussa.duree_effective) == "7h05"
    fatou = r[("E003", MON)]
    assert fatou.statut == "A_L_HEURE" and hm(fatou.heure_validee) == "6h50" and hm(fatou.duree_effective) == "6h45"
    ibou = r[("E004", MON)]
    assert ibou.statut == "INCOMPLET" and ibou.heure_validee is None and ibou.duree_effective is None
    assert r[("E005", MON)].statut == "ABSENT" and r[("E005", MON)].service == "RH"
    assert ("E006", MON) not in r  # inactif : jamais absent
    aminata = r[("E007", MON)]  # 7h45 pile = retard ; départ 12h00 < fin de pause : pas de déduction
    assert aminata.statut == "RETARD" and hm(aminata.heure_validee) == "4h15" and hm(aminata.duree_effective) == "4h15"
    assert r[("99", MON)].nom == "(employé inconnu)"
    assert r[("E001", MON)].service == "Production" and r[("E001", MON)].statut_libelle == "À l'heure"
    # Noms venant de la table Personnel, responsable venant de la table hierarchie.
    assert (awa.nom, awa.prenom, awa.responsable) == ("Diallo", "Awa", None)
    assert moussa.responsable == "Diallo Awa" and moussa.responsable_key == "1"
    assert r[("E004", MON)].responsable == "Sow Fatou" and aminata.responsable is None


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
    assert ("E005", WED) not in week                  # jour férié
    assert ("E005", SAT) not in week                  # samedi non ouvré
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
    assert "Taux de ponctualité" in html and "Moyenne heure validée" in html
    only_late = logged_client.get(f"/suivi?date={MON.isoformat()}&statut=RETARD").text
    assert "E002</td>" in only_late and "E001</td>" not in only_late
    search = logged_client.get(f"/suivi?date={MON.isoformat()}&q=E003").text
    assert "E003</td>" in search and "E002</td>" not in search
    service = logged_client.get(f"/suivi?date={MON.isoformat()}&service=RH").text
    assert "E003</td>" in service and "E001</td>" not in service
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
    statuts = {ws.cell(r, 9).value for r in range(2, ws.max_row + 1)}
    assert statuts == {"En retard", "Absent"}
    assert ws.cell(1, 10).value == "Heure validée"


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
    assert {ws.cell(r, 2).value for r in range(2, ws.max_row + 1)} == {"E003", "E004", "E005"}
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
