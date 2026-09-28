"""Parcours du tableau de bord (sans base MariaDB/PostgreSQL réelle)."""
from app.database import SessionLocal
from app.models import Connection, SyncJob


def test_login_required(client):
    for url in ("/", "/jobs", "/connections", "/logs", "/runs"):
        r = client.get(url, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", url
    assert client.get("/health").json()["status"] == "ok"


def test_bad_login(client):
    r = client.post("/login", data={"username": "admin", "password": "faux"})
    assert "Identifiants incorrects" in r.text


def test_pages_render(logged_client):
    for url in ("/", "/jobs", "/jobs/new", "/connections", "/connections/new", "/logs", "/runs",
                "/logs?job_id=&level=WARNING&q=&table=", "/runs?job_id=&status="):
        r = logged_client.get(url)
        assert r.status_code == 200, url


def test_connection_and_job_crud(logged_client):
    c = logged_client
    for name, kind, port in (("web-src", "mariadb", 3306), ("web-dst", "postgresql", 5432)):
        r = c.post("/connections/save", data={
            "name": name, "kind": kind, "host": "db.invalid", "port": port,
            "database": "x", "username": "u", "password": "p", "action": "save",
        }, follow_redirects=False)
        assert r.status_code == 303
    with SessionLocal() as db:
        src = db.query(Connection).filter_by(name="web-src").one()
        dst = db.query(Connection).filter_by(name="web-dst").one()
        assert src.password_enc and src.password_enc != "p"

    # Modification sans mot de passe : l'ancien est conservé.
    old_enc = src.password_enc
    c.post("/connections/save", data={
        "conn_id": src.id, "name": "web-src", "kind": "mariadb", "host": "db2.invalid", "port": 3307,
        "database": "x", "username": "u", "password": "", "action": "save",
    })
    with SessionLocal() as db:
        src = db.get(Connection, src.id)
        assert src.host == "db2.invalid" and src.password_enc == old_enc

    # Mauvais sens (cible PostgreSQL en source) refusé.
    r = c.post("/jobs/save", data={
        "name": "bad", "source_id": dst.id, "target_id": src.id, "target_schema": "public",
        "interval_value": 5, "interval_unit": "minutes",
    }, follow_redirects=False)
    assert r.headers["location"] == "/jobs/new"

    r = c.post("/jobs/save", data={
        "name": "web-job", "source_id": src.id, "target_id": dst.id, "target_schema": "public",
        "interval_value": 2, "interval_unit": "hours", "enabled": "true",
    }, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        job = db.query(SyncJob).filter_by(name="web-job").one()
        assert job.interval_seconds == 7200 and job.enabled

    # La source est injoignable : la page s'affiche quand même avec l'erreur.
    r = c.get(f"/jobs/{job.id}")
    assert r.status_code == 200 and "Source injoignable" in r.text

    r = c.post(f"/jobs/{job.id}/tables", data={"source_table": "t", "mode": "incremental"})
    assert "nécessite une colonne de suivi" in r.text
    c.post(f"/jobs/{job.id}/tables", data={"source_table": "t", "mode": "incremental", "incremental_column": "id"})
    c.post(f"/jobs/{job.id}/toggle")
    with SessionLocal() as db:
        job = db.get(SyncJob, job.id)
        assert not job.enabled and len(job.tables) == 1

    # Exécution manuelle : échec de connexion journalisé et visible dans les logs.
    c.post(f"/jobs/{job.id}/run")
    r = c.get(f"/logs?job_id={job.id}&level=ERROR")
    assert "Job interrompu" in r.text
    assert c.get("/runs").status_code == 200
    assert "Job interrompu" in c.get(f"/logs/export.csv?job_id={job.id}").text

    # Une connexion utilisée ne peut pas être supprimée.
    r = c.post(f"/connections/{src.id}/delete")
    assert "Supprimez d&#39;abord ces jobs" in r.text or "Supprimez d'abord ces jobs" in r.text
    c.post(f"/jobs/{job.id}/delete")
    c.post(f"/connections/{src.id}/delete")
    with SessionLocal() as db:
        assert db.get(SyncJob, job.id) is None and db.get(Connection, src.id) is None


def test_gsheet_connection_form(logged_client, monkeypatch):
    from app import gsheet

    c = logged_client
    assert "Lien du classeur Google Sheets" in c.get("/connections/new?kind=gsheet").text

    # Compte de service sans clé : refusé.
    r = c.post("/connections/save", data={
        "name": "gs-web", "kind": "gsheet", "sheet_link": "https://docs.google.com/spreadsheets/d/ABC123/edit",
        "sheet_auth": "service_account", "sa_json": "", "action": "save",
    })
    assert "Collez la clé JSON" in r.text
    r = c.post("/connections/save", data={
        "name": "gs-web", "kind": "gsheet", "sheet_link": "x/spreadsheets/d/ABC123/edit",
        "sheet_auth": "service_account", "sa_json": "{pas du json", "action": "save",
    })
    assert "Clé JSON invalide" in r.text

    key = '{"type": "service_account", "client_email": "sa@p.iam.gserviceaccount.com", "private_key": "k"}'
    r = c.post("/connections/save", data={
        "name": "gs-web", "kind": "gsheet", "sheet_link": "https://docs.google.com/spreadsheets/d/ABC123/edit",
        "sheet_auth": "service_account", "sa_json": key, "action": "save",
    }, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        conn = db.query(Connection).filter_by(name="gs-web").one()
        assert conn.database == "ABC123" and conn.username == "service_account"
        assert "private_key" not in conn.password_enc  # chiffrée
        assert gsheet.service_account_email(conn) == "sa@p.iam.gserviceaccount.com"

    # Modification sans ressaisir la clé : conservée ; l'e-mail du compte est affiché.
    r = c.get(f"/connections/{conn.id}/edit")
    assert "sa@p.iam.gserviceaccount.com" in r.text
    c.post("/connections/save", data={
        "conn_id": conn.id, "name": "gs-web", "kind": "gsheet", "sheet_link": "ABC123",
        "sheet_auth": "service_account", "sa_json": "", "action": "save",
    })
    with SessionLocal() as db:
        assert gsheet.service_account_email(db.get(Connection, conn.id)) == "sa@p.iam.gserviceaccount.com"

    # Test de connexion : le résumé du classeur est affiché.
    monkeypatch.setattr(gsheet, "describe", lambda conn: "classeur accessible : 2 onglet(s) (A, B)")
    r = c.post("/connections/save", data={
        "conn_id": conn.id, "name": "gs-web", "kind": "gsheet", "sheet_link": "ABC123",
        "sheet_auth": "public", "action": "test",
    })
    assert "classeur accessible" in r.text
    assert "Google Sheets" in c.get("/connections").text
    assert "gs-web" in c.get("/jobs/new").text
    c.post(f"/connections/{conn.id}/delete")


def test_reload_route_validation(logged_client):
    r = logged_client.post("/jobs/999999/reload", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/jobs"


def test_connection_test_without_name(logged_client, monkeypatch):
    """« Tester » sans nom : pas d'erreur 422, le test s'exécute ; « Enregistrer » demande un nom."""
    import app.routers.connections as routes

    monkeypatch.setattr(routes, "test_connection", lambda conn: "HFSQL 28 — 12 table(s)")
    data = {"name": "", "kind": "hfsql", "host": "h", "port": "4900", "database": "d",
            "username": "admin", "password": "", "action": "test"}
    r = logged_client.post("/connections/save", data=data)
    assert r.status_code == 200 and "Connexion réussie : HFSQL 28" in r.text
    r = logged_client.post("/connections/save", data={**data, "action": "save"})
    assert r.status_code == 200 and "Donnez un nom à la connexion" in r.text


def test_invalid_form_is_readable(logged_client):
    # Navigateur : message lisible et retour à la page précédente.
    r = logged_client.post("/jobs/save", data={"name": "x"}, headers={"accept": "text/html", "referer": "/jobs/new"},
                           follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/jobs/new"
    assert "Formulaire incomplet" in logged_client.get("/jobs/new").text
    # Appel JavaScript : réponse JSON avec un message.
    r = logged_client.post("/jobs/save", data={"name": "x"}, headers={"accept": "*/*"})
    assert r.status_code == 422 and "Formulaire incomplet" in r.json()["error"]


def test_stop_forced_and_orphan_runs(logged_client):
    """Exécution bloquée qui ne répond pas (arrêt forcé) et exécution restée « En cours » sans tâche."""
    from app import sync
    from app.database import SessionLocal
    from app.models import Connection, JobRun, SyncJob

    with SessionLocal() as db:
        s = Connection(name="stop-src", kind="mariadb", host="h", port=3306, database="d", username="u")
        d = Connection(name="stop-dst", kind="postgresql", host="h", port=5432, database="d", username="u")
        db.add_all([s, d])
        db.flush()
        job = SyncJob(name="stop-job", source_id=s.id, target_id=d.id, interval_seconds=60)
        db.add(job)
        db.flush()
        run = JobRun(job_id=job.id, trigger="schedule", status="running")
        db.add(run)
        db.commit()
        job_id, run_id = job.id, run.id

    # Tâche active qui ne rend jamais la main.
    control = sync.RunControl(job_id)
    control.run_id = run_id
    sync._active[job_id] = control
    assert sync.is_running(job_id)
    assert sync.cancel_job(job_id, "test", wait=0.5) == "forced"
    assert not sync.is_running(job_id) and control.stop.is_set()
    with SessionLocal() as db:
        assert db.get(JobRun, run_id).status == "cancelled"

    # Exécution orpheline (tâche disparue) : clôturée par le bouton de la page du job.
    with SessionLocal() as db:
        db.add(JobRun(job_id=job_id, trigger="schedule", status="running"))
        db.commit()
    page = logged_client.get(f"/jobs/{job_id}")
    assert "■ Arrêter" in page.text
    r = logged_client.post(f"/jobs/{job_id}/stop", follow_redirects=True)
    assert "clôturée" in r.text
    with SessionLocal() as db:
        assert not db.query(JobRun).filter_by(job_id=job_id, status="running").count()
    assert "Arrêté" in logged_client.get("/runs").text
