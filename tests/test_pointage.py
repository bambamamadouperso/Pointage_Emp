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
            (105, 'Ba', 'Khady'), (106, 'Gueye', 'Ousmane'), (107, 'Diop', 'Aminata'),
            (108, 'Kane', 'Omar')"""))  # Kane : dans Personnel, sans fiche dans Employes
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
    assert "Taux de ponctualité" in html and "Moyenne durée validée" in html
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
    statuts = {ws.cell(r, 9).value for r in range(2, ws.max_row + 1)}
    assert statuts == {"En retard", "Absent"}
    assert ws.cell(1, 10).value == "Durée validée"


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
            ('E002', '2026-09-22 00:00', '2026-09-22 00:00', 'Approuvée')    -- a badgé : garde son statut"""))
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

    # Écran : carte « En congé », badge vert clair, moyennes hors congés.
    from app.routers import suivi

    monkey_cfg = suivi.load_config
    cfg, _ = monkey_cfg(SessionLocal())
    suivi.load_config = lambda db: (cfg, m)
    try:
        page = logged_client.get(f"/suivi?date={TUE.isoformat()}").text
    finally:
        suivi.load_config = monkey_cfg
    assert "En congé" in page and 'badge st-leave">Congé Annuel' in page and "Congé exceptionnel" in page
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
