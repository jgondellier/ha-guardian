"""Blocage d'IP, sessions, protection CSRF et en-têtes de sécurité."""
import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app import db, main, notify, security  # noqa: E402

main.FAILURE_DELAY = 0
HEADERS = {"X-Requested-With": "HA-Guardian"}
GOOD = {"password": main.config.APP_PASSWORD}  # mot de passe factice de conftest.py
BAD = {"password": "mauvais"}


def make_client(ip="203.0.113.5"):
    return TestClient(main.app, headers=HEADERS, client=(ip, 40000))


def test_ip_bloquee_apres_n_echecs(env):
    db.save_settings({"login_max_attempts": 3, "login_block_minutes": 30})
    c = make_client()
    r = c.post("/api/login", json=BAD)
    assert r.status_code == 401 and "2 essais restants" in r.json()["detail"]
    assert c.post("/api/login", json=BAD).status_code == 401
    r = c.post("/api/login", json=BAD)
    assert r.status_code == 429 and "Retry-After" in r.headers
    # Bloquée partout, même avec le bon mot de passe
    assert c.post("/api/login", json=GOOD).status_code == 429
    assert c.get("/login").status_code == 429
    assert c.get("/healthz").status_code == 200
    # Les autres adresses ne sont pas concernées
    assert make_client("198.51.100.7").post("/api/login", json=GOOD).status_code == 200
    blocks = security.list_blocks()
    assert [b["key"] for b in blocks] == ["203.0.113.5"]
    assert 29 * 60 < blocks[0]["until"] - time.time() <= 30 * 60
    assert any("bloquée" in e["message"] for e in db.events())


def test_blocage_notifie(env):
    db.save_settings({"login_max_attempts": 1})
    with make_client() as c:  # avec lifespan : les tâches de fond s'exécutent
        c.post("/api/login", json=BAD)
        time.sleep(0.1)
    assert any(call[0] == "notify" and "IP bloquée" in call[1] for call in env)


def test_echecs_hors_fenetre_oublies(env, monkeypatch):
    db.save_settings({"login_max_attempts": 2, "login_window_minutes": 10})
    security.register_failure("203.0.113.5")
    real = time.time
    monkeypatch.setattr(security.time, "time", lambda: real() + 11 * 60)
    left, until = security.register_failure("203.0.113.5")
    assert left == 1 and until is None


def test_succes_remet_le_compteur_a_zero(env):
    db.save_settings({"login_max_attempts": 3})
    c = make_client()
    c.post("/api/login", json=BAD)
    c.post("/api/login", json=BAD)
    assert c.post("/api/login", json=GOOD).status_code == 200
    assert security.remaining_attempts("203.0.113.5") == 3


def test_blocage_expire_et_deblocage(env, monkeypatch):
    db.save_settings({"login_max_attempts": 1, "login_block_minutes": 5})
    assert security.register_failure("203.0.113.5")[1] is not None
    assert security.blocked_until("203.0.113.5")
    real = time.time
    monkeypatch.setattr(security.time, "time", lambda: real() + 6 * 60)
    assert security.blocked_until("203.0.113.5") is None
    monkeypatch.setattr(security.time, "time", real)
    security.register_failure("203.0.113.9")
    assert security.unblock("203.0.113.9") and not security.blocked_until("203.0.113.9")


def test_ipv6_comptee_par_prefixe_64(env):
    db.save_settings({"login_max_attempts": 2})
    security.register_failure("2001:db8:1:2::1")
    _, until = security.register_failure("2001:db8:1:2:ffff::42")
    assert until is not None
    assert security.blocked_until("2001:db8:1:2::abcd")
    assert not security.blocked_until("2001:db8:1:3::1")
    assert security.ip_key("::ffff:192.0.2.1") == "192.0.2.1"


def test_debloquer_depuis_l_interface(env):
    db.save_settings({"login_max_attempts": 1})
    security.register_failure("2001:db8::1")
    c = make_client()
    assert c.post("/api/login", json=GOOD).status_code == 200
    data = c.get("/api/security").json()
    assert data["client_ip"] == "203.0.113.5"
    key = data["blocked"][0]["key"]
    assert key == "2001:db8::/64"
    assert c.delete("/api/security/blocked/2001:db8::%2F64").status_code == 200
    assert c.get("/api/security").json()["blocked"] == []


def test_lien_signe_invalide_compte_comme_echec(env):
    db.save_settings({"login_max_attempts": 2})
    c = make_client()
    assert c.get("/api/hook/plug_cycle?exp=9999999999&sig=faux").status_code == 403
    assert security.remaining_attempts("203.0.113.5") == 1


def test_lien_signe_a_usage_unique(env):
    url = notify.hook_url("maintenance_60").replace(main.config.PUBLIC_URL, "")
    c = make_client()
    assert c.get(url).status_code == 200
    assert c.get(url).status_code == 403


def test_csrf_entete_obligatoire(env):
    c = make_client()
    assert c.post("/api/login", json=GOOD).status_code == 200
    bare = TestClient(main.app, client=("203.0.113.5", 40000), cookies=c.cookies)
    assert bare.post("/api/check").status_code == 403
    assert bare.post("/api/login", json=GOOD).status_code == 403
    assert bare.get("/api/status").status_code == 200  # lecture : pas d'en-tête requis
    assert c.post("/api/check", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post("/api/check").status_code == 200


def test_fermer_les_autres_sessions(env):
    a, b = make_client("203.0.113.5"), make_client("203.0.113.6")
    assert a.post("/api/login", json=GOOD).status_code == 200
    assert b.post("/api/login", json=GOOD).status_code == 200
    assert a.post("/api/sessions/revoke").status_code == 200
    assert a.get("/api/status").status_code == 200  # nouvelle session pour l'appareil courant
    assert b.get("/api/status").status_code == 401


def test_jeton_de_session(env, monkeypatch):
    token = security.make_token(7)
    assert security.valid_token(token)
    assert not security.valid_token(token + "0")
    assert not security.valid_token("abc.def")
    assert not security.valid_token(None)
    # Changer le mot de passe invalide les sessions
    monkeypatch.setattr(security.config, "APP_PASSWORD", "autre-mot-de-passe")
    assert not security.valid_token(token)


def test_mot_de_passe_non_ascii_et_json_invalide(env):
    c = make_client()
    assert c.post("/api/login", json={"password": "été€"}).status_code == 401
    assert c.post("/api/login", content=b"pas du json").status_code == 400
    assert security.check_password(main.config.APP_PASSWORD)


def test_cookie_et_en_tetes(env):
    c = make_client()
    r = c.post("/api/login", json=GOOD)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert "max-age=604800" in cookie  # 7 jours par défaut
    https = TestClient(main.app, base_url="https://guardian.test", headers=HEADERS,
                       client=("203.0.113.8", 1))
    r = https.post("/api/login", json=GOOD)
    assert "secure" in r.headers["set-cookie"].lower()
    assert r.headers["strict-transport-security"].startswith("max-age=")
    r = c.get("/login")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert c.get("/api/nope").headers["cache-control"] == "no-store"
    unauth = make_client("203.0.113.9")
    r = unauth.get("/api/status")
    assert r.status_code == 401 and r.headers["x-content-type-options"] == "nosniff"


def test_version_cachee_hors_du_nas(env):
    assert "version" not in make_client().get("/healthz").json()
    assert "version" in make_client("127.0.0.1").get("/healthz").json()
    c = make_client()
    c.post("/api/login", json=GOOD)
    assert c.get("/openapi.json").status_code == 404
