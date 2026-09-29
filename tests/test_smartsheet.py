"""Source Smartsheet : lecture des feuilles par l'API (simulée), typage, erreurs et synchronisation."""
import io
import json
import os
import urllib.error
from datetime import date

import pytest
from sqlalchemy import create_engine, text

from app import smartsheet
from app.crypto import encrypt
from app.database import SessionLocal
from app.models import Connection, JobRun, SyncJob, TableMapping
from app.sync import run_job

COLUMNS = [
    {"id": 1, "title": "Matricule", "type": "TEXT_NUMBER", "primary": True},
    {"id": 2, "title": "Employé", "type": "CONTACT_LIST"},
    {"id": 3, "title": "Date de départ", "type": "DATE"},
    {"id": 4, "title": "Date de retour", "type": "DATE"},
    {"id": 5, "title": "Statut", "type": "PICKLIST"},
    {"id": 6, "title": "Urgent", "type": "CHECKBOX"},
]


def row(row_id, mat, name, start, end, status, urgent=False):
    return {"id": row_id, "cells": [
        {"columnId": 1, "value": mat}, {"columnId": 2, "value": f"{name.lower()}@exemple.com", "displayValue": name},
        {"columnId": 3, "value": start}, {"columnId": 4, "value": end}, {"columnId": 5, "value": status},
        {"columnId": 6, "value": urgent}]}


SHEET = {"id": 4583173393803140, "name": "Autorisations de mission", "totalRowCount": 3, "columns": COLUMNS,
         "rows": [row(11, 590394, "Awa Diallo", "2026-09-21", "2026-09-23", "Approuvée", True),
                  row(12, "E002", "Moussa Ndiaye", "2026-09-22", "2026-09-22", "En attente"),
                  {"id": 13, "cells": [{"columnId": 1}, {"columnId": 3}]}]}  # ligne vide : ignorée


def fake_api(sheets=None):
    sheets = sheets or {"4583173393803140": SHEET}

    def api(conn, path, params=None):
        if path == "/sheets":
            return {"totalPages": 1, "data": [{"id": int(k), "name": v["name"]} for k, v in sheets.items()]}
        sheet_id = path.rsplit("/", 1)[-1]
        if sheet_id not in sheets:
            raise smartsheet.SmartsheetError("Feuille introuvable : vérifiez son identifiant.")
        return sheets[sheet_id]
    return api


def conn(database="4583173393803140"):
    return Connection(name="ss", kind="smartsheet", host="api.smartsheet.com", port=443, database=database,
                      username="token", password_enc=encrypt("jeton-secret"))


def test_parse_sheet_types_and_row_id():
    data = smartsheet.parse_sheet(SHEET)
    assert data.title == "Autorisations de mission"
    assert data.columns == ["row_id", "matricule", "employe", "date_de_depart", "date_de_retour", "statut", "urgent"]
    assert data.headers[1:3] == ["Matricule", "Employé"]
    assert data.rows == [[11, 590394, "Awa Diallo", date(2026, 9, 21), date(2026, 9, 23), "Approuvée", True],
                         [12, "E002", "Moussa Ndiaye", date(2026, 9, 22), date(2026, 9, 22), "En attente", False]]


def test_load_by_name_describe_and_errors(monkeypatch):
    monkeypatch.setattr(smartsheet, "_api", fake_api())
    sheets = smartsheet.load_sheets(conn("Autorisations de mission"))
    assert list(sheets) == ["Autorisations de mission"] and len(sheets["Autorisations de mission"].rows) == 2
    assert smartsheet.describe(conn("")).startswith("jeton valide : 1 feuille(s) accessible(s) — Autorisations")
    assert "(2 ligne(s))" in smartsheet.describe(conn())
    with pytest.raises(smartsheet.SmartsheetError, match="Aucune feuille nommée"):
        smartsheet.load_sheets(conn("Inconnue"))


def test_http_errors_are_explained(monkeypatch):
    def urlopen(request, timeout=None):
        assert request.headers["Authorization"] == "Bearer jeton-secret"
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(
            json.dumps({"message": "Your Access Token is invalid."}).encode()))
    monkeypatch.setattr(smartsheet.urllib.request, "urlopen", urlopen)
    with pytest.raises(smartsheet.SmartsheetError, match="Jeton d'accès refusé"):
        smartsheet.list_sheets(conn())


def test_connection_form_saves_smartsheet(logged_client, monkeypatch):
    monkeypatch.setattr(smartsheet, "_api", fake_api())
    page = logged_client.get("/connections/new?kind=smartsheet").text
    assert "Jeton d'accès API" in page and "api.smartsheet.eu" in page
    r = logged_client.post("/connections/save", data={"name": "ss-form", "kind": "smartsheet",
                                                      "ss_host": "api.smartsheet.com", "ss_sheets": "",
                                                      "ss_token": "jeton", "action": "test"}, follow_redirects=True)
    assert "1 feuille(s) accessible(s)" in r.text and "4583173393803140" in r.text
    r = logged_client.post("/connections/save", data={"name": "ss-form", "kind": "smartsheet", "ss_sheets": "",
                                                      "ss_token": "jeton"}, follow_redirects=True)
    assert "Indiquez la ou les feuilles" in r.text
    r = logged_client.post("/connections/save", data={"name": "ss-form", "kind": "smartsheet",
                                                      "ss_sheets": "4583173393803140", "ss_token": "jeton"},
                           follow_redirects=True)
    with SessionLocal() as db:
        saved = db.query(Connection).filter_by(name="ss-form").one()
        assert (saved.kind, saved.database, saved.is_sheet) == ("smartsheet", "4583173393803140", True)
        # Modification sans ressaisir le jeton : il est conservé.
        conn_id, token = saved.id, saved.password_enc
    logged_client.post("/connections/save", data={"conn_id": conn_id, "name": "ss-form", "kind": "smartsheet",
                                                  "ss_sheets": "4583173393803140, Autre", "ss_token": ""})
    with SessionLocal() as db:
        saved = db.get(Connection, conn_id)
        assert saved.password_enc == token and saved.database == "4583173393803140, Autre"
        db.delete(saved)
        db.commit()


POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")
SCHEMA = "it_smartsheet"


def test_smartsheet_sync_end_to_end(monkeypatch):
    if not POSTGRES_URL:
        pytest.skip("TEST_POSTGRES_URL non définie")
    from sqlalchemy.engine import make_url

    monkeypatch.setattr(smartsheet, "_api", fake_api())
    dst = create_engine(POSTGRES_URL)
    with dst.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
    u = make_url(POSTGRES_URL)
    with SessionLocal() as db:
        db.query(SyncJob).filter_by(name="ss-job").delete()
        db.query(Connection).filter(Connection.name.in_(["ss-src", "ss-dst"])).delete()
        db.commit()
        src = conn()
        src.name = "ss-src"
        tgt = Connection(name="ss-dst", kind="postgresql", host=u.host, port=u.port or 5432, database=u.database,
                         username=u.username, password_enc=encrypt(u.password or ""))
        db.add_all([src, tgt])
        db.flush()
        job = SyncJob(name="ss-job", source_id=src.id, target_id=tgt.id, target_schema=SCHEMA, interval_seconds=60)
        job.tables = [TableMapping(source_table="Autorisations de mission", target_table="missions", mode="full")]
        db.add(job)
        db.commit()
        job_id = job.id
    try:
        with SessionLocal() as db:
            run = db.get(JobRun, run_job(job_id, "manual"))
            assert run.status == "success", run.message
        with dst.connect() as c:
            rows = c.execute(text(f"SELECT row_id, matricule, employe, date_de_depart, date_de_retour, statut, urgent "
                                  f"FROM {SCHEMA}.missions ORDER BY row_id")).all()
        # Matricules mixtes (nombre et texte) : colonne texte ; dates et case à cocher typées.
        assert rows == [(11, "590394", "Awa Diallo", date(2026, 9, 21), date(2026, 9, 23), "Approuvée", True),
                        (12, "E002", "Moussa Ndiaye", date(2026, 9, 22), date(2026, 9, 22), "En attente", False)]
    finally:
        with SessionLocal() as db:
            db.query(SyncJob).filter_by(name="ss-job").delete()
            db.query(Connection).filter(Connection.name.in_(["ss-src", "ss-dst"])).delete()
            db.commit()
        dst.dispose()
