"""Menu « Données » : liste des tables, comptages, filtres, tri, export."""
import os
from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.crypto import encrypt
from app.database import SessionLocal
from app.models import Connection

POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL non définie")
SCHEMA = "it_data"


@pytest.fixture
def pg_conn():
    engine = create_engine(POSTGRES_URL)
    with engine.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        c.execute(text(f"CREATE TABLE {SCHEMA}.pointages (id int PRIMARY KEY, matricule text, jour date, "
                       f"heures numeric, present boolean, commentaire text)"))
        c.execute(text(f"""INSERT INTO {SCHEMA}.pointages VALUES
            (1, 'E001', '2024-01-02', 8.5, true, 'RAS'),
            (2, 'E002', '2024-01-03', 7, false, NULL),
            (3, 'E001', '2024-02-10', 9, true, 'Retard 50%'),
            (4, 'E003', '2024-02-11', 8, true, '')"""))
    u = make_url(POSTGRES_URL)
    with SessionLocal() as db:
        db.query(Connection).filter_by(name="data-pg").delete()
        conn = Connection(name="data-pg", kind="postgresql", host=u.host, port=u.port or 5432,
                          database=u.database, username=u.username, password_enc=encrypt(u.password or ""))
        db.add(conn)
        db.commit()
        conn_id = conn.id
    yield conn_id
    with SessionLocal() as db:
        db.query(Connection).filter_by(id=conn_id).delete()
        db.commit()
    with engine.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
    engine.dispose()


def _count(html: str) -> str:
    return html.split("<h2>", 1)[1].split(" ligne", 1)[0].strip()


def test_overview_lists_tables_with_counts(logged_client, pg_conn):
    r = logged_client.get(f"/data?conn_id={pg_conn}&q={SCHEMA}")
    assert r.status_code == 200
    assert 'data-target-rows="4"' in r.text and "pointages" in r.text


def test_filters_sort_and_export(logged_client, pg_conn):
    base = f"/data/{pg_conn}/{SCHEMA}/pointages"
    assert _count(logged_client.get(base).text) == "4"
    assert _count(logged_client.get(base + "?fc=matricule&fo=eq&fv=E001").text) == "2"
    assert _count(logged_client.get(base + "?fc=jour&fo=gte&fv=2024-02-01").text) == "2"
    assert _count(logged_client.get(base + "?fc=heures&fo=gt&fv=8").text) == "2"
    assert _count(logged_client.get(base + "?fc=present&fo=eq&fv=false").text) == "1"
    assert _count(logged_client.get(base + "?fc=commentaire&fo=empty").text) == "2"
    assert _count(logged_client.get(base + "?fc=commentaire&fo=notempty").text) == "2"
    # « % » est cherché littéralement, pas comme joker.
    assert _count(logged_client.get(base + "?q=50%25").text) == "1"
    # Plusieurs filtres combinés (ET).
    assert _count(logged_client.get(base + "?fc=matricule&fo=eq&fv=E001&fc=jour&fo=lt&fv=2024-02-01").text) == "1"
    # Valeur incompatible : message clair, pas d'erreur serveur.
    r = logged_client.get(base + "?fc=jour&fo=gte&fv=hier")
    assert r.status_code == 200 and "incompatible avec le type" in r.text
    # Colonne inconnue ignorée (pas d'injection possible).
    assert _count(logged_client.get(base + "?fc=id%3B+drop&fo=eq&fv=1").text) == "4"
    # Tri décroissant + pagination.
    r = logged_client.get(base + "?sort=jour&dir=desc&size=25")
    assert r.text.index("11/02/2024") < r.text.index("02/01/2024")
    csv_text = logged_client.get(base + "/export.csv?fc=matricule&fo=eq&fv=E001&sort=id").text
    lines = csv_text.lstrip("﻿").strip().splitlines()
    assert lines[0].startswith("id;matricule;jour") and len(lines) == 3
    assert "2024-01-02" in lines[1]


def test_unknown_table(logged_client, pg_conn):
    r = logged_client.get(f"/data/{pg_conn}/{SCHEMA}/inexistante")
    assert r.status_code == 200 and "introuvable" in r.text
