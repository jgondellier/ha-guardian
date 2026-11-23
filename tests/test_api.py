"""Authentification et routes sensibles de l'API web."""
import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.main import app  # noqa: E402

main.FAILURE_DELAY = 0
HEADERS = {"X-Requested-With": "HA-Guardian"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app, headers=HEADERS) as c:
        yield c


def test_routes_publiques(client):
    assert client.get("/healthz").json()["ok"] is True
    assert client.get("/login").status_code == 200


def test_api_protegee_sans_session(client):
    client.cookies.clear()
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/actions/plug_cycle").status_code == 401
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/login"


def test_lien_de_notification_invalide(client):
    client.cookies.clear()
    assert client.post("/api/hook/plug_cycle?exp=9999999999&sig=faux").status_code == 403


def test_connexion(client):
    client.cookies.clear()
    assert client.post("/api/login", json={"password": "mauvais"}).status_code == 401
    r = client.post("/api/login", json={"password": "mot-de-passe-de-test"})
    assert r.status_code == 200 and "guardian_session" in r.cookies
    status = client.get("/api/status")
    assert status.status_code == 200 and status.json()["app"]["version"] == "2.0.0"
    assert client.get("/api/backups/..%2F..%2Fdata%2Fsecret.key").status_code == 404
    assert client.post("/api/actions/inconnue").status_code == 404
