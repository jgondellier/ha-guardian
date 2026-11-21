"""Détection des nouvelles versions publiées sur GitHub et mise à jour via Watchtower.

Une version = une « release » GitHub (v2.1.0…), créée automatiquement quand le
fichier VERSION change sur main. L'image `latest` ne suit que ces versions.
"""
import logging
import re
import time

import httpx

from . import config, db, notify

log = logging.getLogger("guardian.updater")

state: dict = {
    "version": config.APP_VERSION,
    "sha": config.GIT_SHA,
    "latest_version": None,
    "available": False,
    "releases": [],      # versions plus récentes que celle en cours, avec leurs notes
    "checked": None,
    "error": None,
    "updating": False,
    "enabled": bool(config.UPDATE_TOKEN),
    "repo": config.GITHUB_REPO,
}


def parse_version(value: str | None) -> tuple:
    """« v2.10.1 » -> (2, 10, 1) ; valeur non numérique -> ()."""
    m = re.match(r"^v?(\d+(?:\.\d+)*)", (value or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def is_newer(candidate: str | None, current: str | None) -> bool:
    c, cur = parse_version(candidate), parse_version(current)
    return bool(c) and bool(cur) and c > cur


def _client() -> httpx.AsyncClient:
    headers = {"Accept": "application/vnd.github+json"}
    if config.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
    return httpx.AsyncClient(timeout=15, headers=headers)


def _notes(body: str | None) -> list[str]:
    lines = []
    for line in (body or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("**Full Changelog"):
            continue
        lines.append(re.sub(r"^[-*]\s+", "", line)[:200])
    return lines[:15]


async def check() -> dict:
    state["checked"] = time.time()
    if not config.GITHUB_REPO:
        state["error"] = "GITHUB_REPO non défini"
        return state
    try:
        async with _client() as client:
            r = await client.get(f"https://api.github.com/repos/{config.GITHUB_REPO}/releases",
                                 params={"per_page": 20})
            r.raise_for_status()
        releases = [x for x in r.json() if not x.get("draft") and not x.get("prerelease")]
        if not releases:
            state.update(error="Aucune version publiée pour l'instant", available=False,
                         latest_version=None, releases=[])
            return state
        releases.sort(key=lambda x: parse_version(x["tag_name"]), reverse=True)
        latest = releases[0]["tag_name"].lstrip("v")
        state["latest_version"] = latest
        state["error"] = None
        if not parse_version(config.APP_VERSION):
            state.update(available=False, releases=[],
                         error="Version construite localement : mise à jour non suivie")
            return state
        newer = [x for x in releases if is_newer(x["tag_name"], config.APP_VERSION)]
        state["available"] = bool(newer)
        state["releases"] = [
            {"version": x["tag_name"].lstrip("v"), "date": x.get("published_at"),
             "url": x.get("html_url"), "notes": _notes(x.get("body"))}
            for x in newer
        ]
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        state["error"] = ("Dépôt introuvable ou privé (définir GITHUB_TOKEN)" if code == 404
                          else f"GitHub : HTTP {code}")
    except Exception as exc:  # noqa: BLE001
        state["error"] = f"GitHub injoignable ({type(exc).__name__})"
    return state


async def trigger() -> None:
    """Demande à Watchtower de télécharger la nouvelle image et de recréer le conteneur.
    Le conteneur courant est arrêté pendant l'opération : la requête finit souvent coupée."""
    if not config.UPDATE_TOKEN:
        raise RuntimeError("UPDATE_TOKEN non défini dans .env")
    state["updating"] = True
    db.add_event("action", f"Mise à jour de HA Guardian demandée "
                           f"(v{config.APP_VERSION} → v{state['latest_version'] or '?'})")
    try:
        async with httpx.AsyncClient(timeout=900) as client:
            r = await client.get(f"{config.UPDATE_URL}/v1/update",
                                 headers={"Authorization": f"Bearer {config.UPDATE_TOKEN}"})
        if r.status_code == 401:
            raise RuntimeError("Watchtower refuse le jeton (UPDATE_TOKEN différent ?)")
        db.add_event("info", "Watchtower n'a pas trouvé de nouvelle image. Si l'image est "
                             "épinglée sur une version dans docker-compose.yml, remets « latest ».")
    except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout):
        pass  # normal : le conteneur est en train d'être remplacé
    except httpx.ConnectError as exc:
        db.add_event("error", "Watchtower injoignable : vérifier le service "
                              "ha-guardian-watchtower")
        raise RuntimeError("Watchtower injoignable") from exc
    finally:
        state["updating"] = False


async def on_startup() -> None:
    """Journalise un changement de version depuis le dernier démarrage."""
    previous = db.kv_get("running_version")
    current = f"{config.APP_VERSION} ({config.GIT_SHA[:7]})"
    if previous and previous != current:
        msg = f"HA Guardian : v{previous} → v{current}"
        db.add_event("ok", msg)
        await notify.send("⬆️ HA Guardian mis à jour", msg)
    db.kv_set("running_version", current)
