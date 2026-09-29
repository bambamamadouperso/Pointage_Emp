import os
import tempfile

# La configuration est lue à l'import : on isole la base interne avant d'importer l'application.
_tmp = tempfile.mkdtemp(prefix="sync-tests-")
os.environ["APP_DB_URL"] = f"sqlite:///{_tmp}/app.db"
os.environ["ARRETS_DIR"] = f"{_tmp}/arrets"
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "secret"
os.environ.setdefault("LOG_LEVEL", "DEBUG")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import init_db  # noqa: E402

init_db()


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def logged_client(client):
    r = client.post("/login", data={"username": "admin", "password": "secret"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    return client
