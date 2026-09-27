"""Tests de bout en bout sur de vraies bases.

Définir TEST_MARIADB_URL et TEST_POSTGRES_URL, par exemple :
  TEST_MARIADB_URL=mysql+pymysql://tester:testpw@127.0.0.1:3306/sync_test
  TEST_POSTGRES_URL=postgresql+psycopg://tester:testpw@127.0.0.1:5432/sync_test
"""
import json
import os
from datetime import date, time
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.crypto import encrypt
from app.database import SessionLocal
from app.models import Connection, JobRun, LogEntry, SyncJob, TableMapping
from app.sync import run_job

MARIADB_URL = os.getenv("TEST_MARIADB_URL")
POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not (MARIADB_URL and POSTGRES_URL), reason="TEST_MARIADB_URL / TEST_POSTGRES_URL non définies"
)
SCHEMA = "it_sync"


def _conn(url: str, kind: str, name: str) -> Connection:
    u = make_url(url)
    return Connection(name=name, kind=kind, host=u.host, port=u.port or (3306 if kind == "mariadb" else 5432),
                      database=u.database, username=u.username, password_enc=encrypt(u.password or ""))


@pytest.fixture(scope="module")
def engines():
    src = create_engine(MARIADB_URL + "?charset=utf8mb4")
    dst = create_engine(POSTGRES_URL)
    yield src, dst
    src.dispose()
    dst.dispose()


@pytest.fixture
def job(engines):
    src, dst = engines
    with src.begin() as c:
        c.execute(text("SET SESSION sql_mode = ''"))
        for t in ("employes", "pointages", "sans_cle", "types_divers"):
            c.execute(text(f"DROP TABLE IF EXISTS {t}"))
        c.execute(text("""
            CREATE TABLE employes (
              id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
              nom VARCHAR(100) NOT NULL,
              service ENUM('RH','Production') NOT NULL,
              salaire DECIMAL(10,2),
              updated_at DATETIME NOT NULL
            ) CHARACTER SET utf8mb4"""))
        c.execute(text("""
            CREATE TABLE pointages (
              id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
              employe_id INT UNSIGNED NOT NULL,
              jour DATE NOT NULL,
              arrivee TIME,
              type SET('normal','nuit') DEFAULT 'normal'
            )"""))
        c.execute(text("CREATE TABLE sans_cle (seq INT NOT NULL, libelle VARCHAR(50))"))
        c.execute(text("""
            CREATE TABLE types_divers (
              id INT PRIMARY KEY,
              annee YEAR,
              drapeau BIT(1),
              grand BIGINT UNSIGNED,
              doc JSON,
              binaire BLOB,
              date_zero DATETIME,
              texte LONGTEXT
            )"""))
        c.execute(text("""INSERT INTO employes (nom, service, salaire, updated_at) VALUES
            ('Awa Diallo', 'RH', 450000.00, '2024-01-01 08:00:00'),
            ('Moussa Ndiaye', 'Production', 320000.00, '2024-01-01 08:00:00')"""))
        c.execute(text("""INSERT INTO pointages (employe_id, jour, arrivee, type) VALUES
            (1, '2024-01-02', '08:02:00', 'normal'), (2, '2024-01-02', '22:00:00', 'normal,nuit')"""))
        c.execute(text("INSERT INTO sans_cle VALUES (1, 'un'), (2, 'deux')"))
        c.execute(text("""INSERT INTO types_divers VALUES
            (1, 2024, b'1', 18446744073709551615, '{"a": [1, 2]}', x'00FF', '0000-00-00 00:00:00', 'é\\0à')"""))
    with dst.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))

    with SessionLocal() as db:
        db.query(SyncJob).filter_by(name="it-job").delete()
        db.query(Connection).filter(Connection.name.in_(["it-src", "it-dst"])).delete()
        db.commit()
        s, d = _conn(MARIADB_URL, "mariadb", "it-src"), _conn(POSTGRES_URL, "postgresql", "it-dst")
        db.add_all([s, d])
        db.flush()
        j = SyncJob(name="it-job", source_id=s.id, target_id=d.id, target_schema=SCHEMA, interval_seconds=60)
        j.tables = [
            TableMapping(source_table="employes", target_table="employes", mode="incremental",
                         incremental_column="updated_at"),
            TableMapping(source_table="pointages", target_table="pointages_copie", mode="incremental",
                         incremental_column="id"),
            TableMapping(source_table="sans_cle", target_table="sans_cle", mode="incremental",
                         incremental_column="seq"),
            TableMapping(source_table="types_divers", target_table="types_divers", mode="full"),
        ]
        db.add(j)
        db.commit()
        return j.id


def _run(job_id: int) -> JobRun:
    run_id = run_job(job_id, "manual")
    with SessionLocal() as db:
        return db.get(JobRun, run_id)


def _rows(dst, sql):
    with dst.connect() as c:
        return c.execute(text(sql)).all()


def test_full_cycle(engines, job):
    src, dst = engines
    run = _run(job)
    assert run.status == "success", run.message
    assert run.tables_ok == 4 and run.rows_written == 2 + 2 + 2 + 1

    assert _rows(dst, f"SELECT id, nom, service, salaire FROM {SCHEMA}.employes ORDER BY id") == [
        (1, "Awa Diallo", "RH", Decimal("450000.00")), (2, "Moussa Ndiaye", "Production", Decimal("320000.00"))]
    assert _rows(dst, f"SELECT id, arrivee, type FROM {SCHEMA}.pointages_copie ORDER BY id") == [
        (1, time(8, 2), "normal"), (2, time(22, 0), "normal,nuit")]
    row = _rows(dst, f"SELECT annee, drapeau, grand, doc, binaire, date_zero, texte FROM {SCHEMA}.types_divers")[0]
    # JSON est un alias de LONGTEXT dans MariaDB : il arrive sous forme de texte JSON valide.
    assert json.loads(row[3]) == {"a": [1, 2]}
    assert row[:3] + row[4:] == (2024, 1, Decimal("18446744073709551615"), b"\x00\xff", None, "éà")

    # Modifications côté source : mise à jour, insertion, nouvelle colonne.
    with src.begin() as c:
        c.execute(text("UPDATE employes SET salaire = 500000, updated_at = '2024-02-01 09:00:00' WHERE id = 1"))
        c.execute(text("INSERT INTO employes (nom, service, salaire, updated_at) "
                       "VALUES ('Fatou Sow', 'RH', 1, '2024-02-01 09:00:00')"))
        c.execute(text("ALTER TABLE pointages ADD COLUMN depart TIME NULL"))
        c.execute(text("INSERT INTO pointages (employe_id, jour, arrivee, depart) VALUES (3, '2024-02-01', '07:00', '15:00')"))
        c.execute(text("INSERT INTO sans_cle VALUES (3, 'trois')"))
        c.execute(text("UPDATE types_divers SET texte = 'modifié'"))

    run = _run(job)
    assert run.status == "success", run.message
    assert _rows(dst, f"SELECT id, salaire FROM {SCHEMA}.employes ORDER BY id") == [
        (1, Decimal("500000.00")), (2, Decimal("320000.00")), (3, Decimal("1.00"))]
    assert _rows(dst, f"SELECT id, depart FROM {SCHEMA}.pointages_copie ORDER BY id") == [
        (1, None), (2, None), (3, time(15, 0))]
    # Sans clé : ajout strict des nouvelles lignes, sans doublon.
    assert _rows(dst, f"SELECT seq FROM {SCHEMA}.sans_cle ORDER BY seq") == [(1,), (2,), (3,)]
    assert _rows(dst, f"SELECT texte FROM {SCHEMA}.types_divers") == [("modifié",)]

    # Une relance sans changement est idempotente.
    run = _run(job)
    assert run.status == "success"
    assert _rows(dst, f"SELECT count(*) FROM {SCHEMA}.employes") == [(3,)]
    assert _rows(dst, f"SELECT count(*) FROM {SCHEMA}.sans_cle") == [(3,)]

    with SessionLocal() as db:
        m = db.query(TableMapping).filter_by(job_id=job, source_table="pointages").one()
        assert m.last_value_display == "3"
        messages = [l.message for l in db.query(LogEntry).filter_by(job_id=job)]
    assert any("Colonnes ajoutées dans la cible : depart" in msg for msg in messages)


def test_table_error_is_isolated(engines, job):
    with SessionLocal() as db:
        db.add(TableMapping(job_id=job, source_table="inexistante", target_table="inexistante"))
        db.commit()
    run = _run(job)
    assert run.status == "partial"
    assert run.tables_ok == 4 and run.tables_failed == 1
    with SessionLocal() as db:
        errors = db.query(LogEntry).filter_by(run_id=run.id, level="ERROR").all()
    assert [e.table_name for e in errors] == ["inexistante"]
