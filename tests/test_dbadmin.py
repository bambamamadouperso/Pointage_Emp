"""Choix et création d'une base depuis le formulaire de connexion."""
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")
MARIADB_URL = os.getenv("TEST_MARIADB_URL")
NEW_DB = "sync_test_nouvelle_base"


def _fields(url, kind):
    u = make_url(url)
    return {"kind": kind, "host": u.host, "port": u.port or (5432 if kind == "postgresql" else 3306),
            "username": u.username, "password": u.password, "database": ""}


def test_requires_login(client):
    r = client.post("/connections/databases", data={"kind": "postgresql"}, follow_redirects=False)
    assert r.status_code == 303


def test_validation(logged_client):
    r = logged_client.post("/connections/databases", data={"kind": "postgresql", "host": "", "username": ""})
    assert r.status_code == 400 and "Renseignez" in r.json()["error"]
    r = logged_client.post("/connections/create-database", data={
        "host": "127.0.0.1", "port": 1, "username": "u", "password": "p", "new_database": "nom invalide!"})
    assert r.status_code == 400 and "Nom invalide" in r.json()["error"]


def test_unreachable_server(logged_client):
    r = logged_client.post("/connections/databases", data={
        "kind": "postgresql", "host": "127.0.0.1", "port": 1, "username": "u", "password": "p"})
    assert r.status_code == 400 and "Connexion au serveur impossible" in r.json()["error"]


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL non définie")
def test_list_and_create_postgres(logged_client):
    admin = create_engine(POSTGRES_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {NEW_DB}"))
    try:
        fields = _fields(POSTGRES_URL, "postgresql")
        r = logged_client.post("/connections/databases", data=fields)
        assert r.status_code == 200, r.text
        names = r.json()["databases"]
        assert make_url(POSTGRES_URL).database in names and "template0" not in names

        r = logged_client.post("/connections/create-database", data={**fields, "new_database": NEW_DB})
        assert r.status_code == 200, r.text
        assert NEW_DB in logged_client.post("/connections/databases", data=fields).json()["databases"]

        r = logged_client.post("/connections/create-database", data={**fields, "new_database": NEW_DB})
        assert r.status_code == 400 and "existe déjà" in r.json()["error"]
    finally:
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {NEW_DB}"))
        admin.dispose()


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL non définie")
def test_saved_password_is_reused(logged_client):
    """Sur une connexion existante, le mot de passe peut rester vide : celui enregistré est utilisé."""
    fields = _fields(POSTGRES_URL, "postgresql")
    r = logged_client.post("/connections/save", data={**fields, "name": "pg-picker", "database": "postgres",
                                                        "action": "save"}, follow_redirects=False)
    assert r.status_code == 303
    from app.database import SessionLocal
    from app.models import Connection

    with SessionLocal() as db:
        conn_id = db.query(Connection).filter_by(name="pg-picker").one().id
    r = logged_client.post("/connections/databases", data={**fields, "password": "", "conn_id": conn_id})
    assert r.status_code == 200, r.text
    logged_client.post(f"/connections/{conn_id}/delete")


@pytest.mark.skipif(not MARIADB_URL, reason="TEST_MARIADB_URL non définie")
def test_list_mariadb(logged_client):
    r = logged_client.post("/connections/databases", data=_fields(MARIADB_URL, "mariadb"))
    assert r.status_code == 200, r.text
    names = r.json()["databases"]
    assert make_url(MARIADB_URL).database in names and "mysql" not in names
