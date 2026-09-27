"""Source HFSQL (ODBC).

Le pilote HFSQL n'existe que chez PC SOFT (Windows) : la synchronisation de bout en bout est testée
avec un autre pilote ODBC (PostgreSQL ODBC) qui emprunte exactement le même chemin de code.
"""
import datetime as dt
import os
from decimal import Decimal

import pytest
from sqlalchemy import BigInteger, Date, DateTime, Numeric, Text, create_engine, text
from sqlalchemy.engine import make_url

from app import hfsql
from app.crypto import encrypt
from app.database import SessionLocal
from app.models import Connection, JobRun, SyncJob, TableMapping
from app.sync import run_job


class FakePyodbc:
    def __init__(self, drivers):
        self._drivers = drivers

    def drivers(self):
        return self._drivers


def _conn(**kw):
    base = dict(kind="hfsql", host="srv-hf", port=4900, database="Pointage", username="admin",
                password_enc=encrypt("p;w"), options=None)
    base.update(kw)
    return Connection(**base)


def test_connection_string(monkeypatch):
    monkeypatch.setattr(hfsql, "_pyodbc", lambda: FakePyodbc(["SQL Server", "HFSQL"]))
    cs = hfsql.connection_string(_conn())
    assert cs == "DRIVER={HFSQL};Server Name=srv-hf;Server Port=4900;Database=Pointage;UID=admin;PWD={p;w};"


def test_connection_string_options(monkeypatch):
    monkeypatch.setattr(hfsql, "_pyodbc", lambda: FakePyodbc(["HFSQL"]))
    cs = hfsql.connection_string(_conn(options="DRIVER=HyperFileSQL; Password=fichiers"))
    assert cs.startswith("DRIVER={HyperFileSQL};") and "Password=fichiers;" in cs


def test_missing_driver(monkeypatch):
    monkeypatch.setattr(hfsql, "_pyodbc", lambda: FakePyodbc(["SQL Server"]))
    with pytest.raises(hfsql.HfsqlError, match="Pilote ODBC HFSQL introuvable.*SQL Server"):
        hfsql.connection_string(_conn())


def test_map_type_and_clean():
    assert isinstance(hfsql.map_type(int, 10, 0), BigInteger)
    assert isinstance(hfsql.map_type(Decimal, 10, 2), Numeric)
    assert isinstance(hfsql.map_type(dt.date, 0, 0), Date)
    assert isinstance(hfsql.map_type(str, 50, 0), Text)
    assert hfsql.clean("", Date()) is None
    assert hfsql.clean("00000000", Date()) is None
    assert hfsql.clean("2024-01-31", Date()) == dt.date(2024, 1, 31)
    assert hfsql.clean("a\x00b", Text()) == "ab"
    assert hfsql.clean(dt.datetime(2024, 1, 1, 8), DateTime()) == dt.datetime(2024, 1, 1, 8)


# --------------------------------------------------------------------------- bout en bout (ODBC réel)

POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")
SCHEMA = "it_hfsql"


def _odbc_available():
    try:
        import pyodbc
        return any("postgresql" in d.lower() for d in pyodbc.drivers())
    except Exception:
        return False


@pytest.fixture
def odbc_job(monkeypatch):
    if not POSTGRES_URL or not _odbc_available():
        pytest.skip("TEST_POSTGRES_URL ou pilote ODBC PostgreSQL absent")
    u = make_url(POSTGRES_URL)
    # La source « HFSQL » est simulée par PostgreSQL via ODBC.
    monkeypatch.setattr(hfsql, "connection_string", lambda conn: (
        f"DRIVER={{PostgreSQL Unicode}};Servername={u.host};Port={u.port or 5432};Database={u.database};"
        f"UID={u.username};PWD={u.password};"))
    engine = create_engine(POSTGRES_URL)
    with engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS hf_pointage, hf_service"))
        c.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        c.execute(text("CREATE TABLE hf_pointage (IDPointage int PRIMARY KEY, Matricule varchar(10), "
                       "DateJour date, Arrivee timestamp, Heures numeric(6,2), Commentaire text)"))
        c.execute(text("""INSERT INTO hf_pointage VALUES
            (1, 'E001', '2024-01-02', '2024-01-02 08:05', 8.5, 'RAS'),
            (2, 'E002', '2024-01-02', '2024-01-02 07:55', 7.25, NULL)"""))
        c.execute(text("CREATE TABLE hf_service (Code varchar(5), Libelle text)"))
        c.execute(text("INSERT INTO hf_service VALUES ('RH', 'Ressources humaines'), ('PRD', 'Production')"))
    with SessionLocal() as db:
        db.query(SyncJob).filter_by(name="hf-job").delete()
        db.query(Connection).filter(Connection.name.in_(["hf-src", "hf-dst"])).delete()
        db.commit()
        # Port réel du serveur simulé : la connexion vérifie d'abord que le port répond.
        src = Connection(name="hf-src", kind="hfsql", host=u.host, port=u.port or 5432, database=u.database,
                         username=u.username, password_enc=encrypt(u.password or ""))
        tgt = Connection(name="hf-dst", kind="postgresql", host=u.host, port=u.port or 5432, database=u.database,
                         username=u.username, password_enc=encrypt(u.password or ""))
        db.add_all([src, tgt])
        db.flush()
        job = SyncJob(name="hf-job", source_id=src.id, target_id=tgt.id, target_schema=SCHEMA, interval_seconds=60)
        job.tables = [
            TableMapping(source_table="hf_pointage", target_table="pointage", mode="incremental",
                         incremental_column="idpointage"),
            TableMapping(source_table="hf_service", target_table="service", mode="full"),
        ]
        db.add(job)
        db.commit()
        job_id, src_id = job.id, src.id
    yield job_id, src_id, engine
    hfsql.reset_pool()
    with engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS hf_pointage, hf_service"))
        c.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
    engine.dispose()


def _run(job_id, **kw):
    with SessionLocal() as db:
        return db.get(JobRun, run_job(job_id, "manual", **kw))


def test_odbc_sync_end_to_end(odbc_job):
    job_id, src_id, engine = odbc_job
    from app.sync import list_columns, list_tables, test_connection

    with SessionLocal() as db:
        src = db.get(Connection, src_id)
        assert "hf_pointage" in list_tables(src)
        cols = {c["name"]: c for c in list_columns(src, "hf_pointage")}
        assert cols["idpointage"]["pk"] and "NUMERIC(6, 2)" in cols["heures"]["type"]
        assert test_connection(src)

    run = _run(job_id)
    assert run.status == "success", run.message
    assert run.rows_written == 4
    with engine.connect() as c:
        rows = c.execute(text(f"SELECT idpointage, matricule, datejour, heures FROM {SCHEMA}.pointage ORDER BY 1")).all()
        assert rows == [(1, "E001", dt.date(2024, 1, 2), Decimal("8.50")), (2, "E002", dt.date(2024, 1, 2), Decimal("7.25"))]

    with engine.begin() as c:
        c.execute(text("INSERT INTO hf_pointage VALUES (3, 'E003', '2024-01-03', NULL, 9, 'nouveau')"))
        c.execute(text("UPDATE hf_service SET libelle = 'RH & paie' WHERE code = 'RH'"))
    run = _run(job_id)
    assert run.status == "success", run.message
    with SessionLocal() as db:
        m = db.query(TableMapping).filter_by(job_id=job_id, source_table="hf_pointage").one()
        assert m.last_rows == 1 and m.last_value_display == "3"  # seule la nouvelle ligne est lue
    with engine.connect() as c:
        assert c.execute(text(f"SELECT count(*) FROM {SCHEMA}.pointage")).scalar() == 3
        assert c.execute(text(f"SELECT libelle FROM {SCHEMA}.service WHERE code = 'RH'")).scalar() == "RH & paie"

    # Réimport complet : fonctionne aussi pour une source HFSQL.
    run = _run(job_id, reset=True)
    assert run.status == "success" and run.rows_written == 3 + 2


def test_unreachable_port_fails_fast(monkeypatch):
    import time

    monkeypatch.setattr(hfsql, "_pyodbc", lambda: FakePyodbc(["HFSQL"]))
    start = time.monotonic()
    with pytest.raises(hfsql.HfsqlError, match="Connexion refusée par 127.0.0.1:1"):
        hfsql.connect(_conn(host="127.0.0.1", port=1))
    assert time.monotonic() - start < 5


def test_hanging_driver_times_out(monkeypatch):
    import threading
    import time

    class HangingPyodbc(FakePyodbc):
        Error = Exception

        def connect(self, *a, **kw):
            time.sleep(5)

    monkeypatch.setattr(hfsql, "_pyodbc", lambda: HangingPyodbc(["HFSQL"]))
    monkeypatch.setattr(hfsql, "check_port", lambda host, port: None)
    monkeypatch.setattr(hfsql, "CONNECT_TIMEOUT", 1)
    start = time.monotonic()
    with pytest.raises(hfsql.HfsqlError, match="ne répond pas après 1 s"):
        hfsql.connect(_conn())
    assert time.monotonic() - start < 3


def test_port_check_messages():
    from app.netcheck import NetError, check_port

    with pytest.raises(NetError, match="refusée"):
        check_port("127.0.0.1", 1)
    with pytest.raises(NetError, match="inconnu"):
        check_port("hote-inexistant.invalid", 4900)


def test_connection_string_dsn():
    cs = hfsql.connection_string(_conn(options="DSN=HRsmart", password_enc=encrypt("")))
    assert cs == "DSN=HRsmart;UID=admin;PWD=;"
    assert hfsql.masked("DSN=x;UID=a;PWD={p;w};") == "DSN=x;UID=a;PWD=*****;"


def test_missing_dsn_lists_visible_sources(monkeypatch):
    class Err(Exception):
        pass

    class DsnPyodbc(FakePyodbc):
        Error = Err

        def connect(self, *a, **kw):
            raise Err("IM002", "[IM002] Source de données introuvable et nom de pilote non spécifié")

        def dataSources(self):
            return {"Autre": "SQL Server", "HRsmart32": "HFSQL"}

    monkeypatch.setattr(hfsql, "_pyodbc", lambda: DsnPyodbc(["HFSQL"]))
    monkeypatch.setattr(hfsql, "check_port", lambda host, port: None)
    with pytest.raises(hfsql.HfsqlError, match="DSN système.*Sources visibles par l'application : Autre, HRsmart32"):
        hfsql.connect(_conn(options="DSN=HRsmart"))


def test_connection_is_reused_between_runs(monkeypatch):
    """L'ouverture HFSQL peut prendre > 1 min : la connexion est gardée d'une exécution à l'autre."""
    opened = []

    class FakeCnx:
        closed = False

        def getinfo(self, code):
            return '"'

        def close(self):
            self.closed = True

    class PoolPyodbc(FakePyodbc):
        Error = Exception

        def connect(self, *a, **kw):
            opened.append(FakeCnx())
            return opened[-1]

    hfsql.reset_pool()
    monkeypatch.setattr(hfsql, "_pyodbc", lambda: PoolPyodbc(["HFSQL"]))
    monkeypatch.setattr(hfsql, "check_port", lambda host, port: None)
    conn = _conn()
    with hfsql.Source(conn):
        pass
    with hfsql.Source(conn) as second:
        # Utilisation simultanée : une seconde connexion indépendante est ouverte.
        with hfsql.Source(conn):
            pass
    assert len(opened) == 2 and not opened[0].closed
    # Après une erreur, la connexion gardée est fermée et la suivante est neuve.
    with pytest.raises(RuntimeError):
        with hfsql.Source(conn):
            raise RuntimeError("échec pendant le job")
    assert opened[0].closed
    with hfsql.Source(conn):
        pass
    assert len(opened) == 3
    hfsql.reset_pool()
