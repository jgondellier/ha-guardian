"""Détection des nouvelles versions à partir des releases GitHub (réponses simulées)."""
import httpx
import pytest

from app import updater
from conftest import run

pytestmark = pytest.mark.skipif(not hasattr(httpx, "MockTransport"), reason="httpx réel requis")

RELEASES = [
    {"tag_name": "v2.0.0", "draft": False, "prerelease": False, "body": "- Première", "published_at": "2026-11-21T10:00:00Z"},
    {"tag_name": "v2.2.0", "draft": False, "prerelease": False, "body": "## Nouveautés\n- Ajout A\n* Ajout B", "published_at": "2026-12-01T10:00:00Z"},
    {"tag_name": "v2.1.0", "draft": False, "prerelease": False, "body": "- Correctif", "published_at": "2026-11-25T10:00:00Z"},
    {"tag_name": "v3.0.0-beta", "draft": False, "prerelease": True, "body": "", "published_at": None},
]


def use(monkeypatch, status=200, payload=None):
    def handler(request):
        return httpx.Response(status, json=RELEASES if payload is None else payload)
    monkeypatch.setattr(updater, "_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_nouvelles_versions_listees_de_la_plus_recente(monkeypatch):
    use(monkeypatch)
    s = run(updater.check())
    assert s["available"] and s["latest_version"] == "2.2.0" and s["error"] is None
    assert [r["version"] for r in s["releases"]] == ["2.2.0", "2.1.0"]
    assert s["releases"][0]["notes"] == ["Ajout A", "Ajout B"]


def test_deja_a_jour(monkeypatch):
    use(monkeypatch, payload=RELEASES[:1])
    s = run(updater.check())
    assert not s["available"] and s["releases"] == []


def test_aucune_release(monkeypatch):
    use(monkeypatch, payload=[])
    assert "Aucune version" in run(updater.check())["error"]


def test_depot_prive_sans_jeton(monkeypatch):
    use(monkeypatch, status=404, payload={"message": "Not Found"})
    assert "GITHUB_TOKEN" in run(updater.check())["error"]
