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
