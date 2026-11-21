"""Détection des nouvelles versions sur GitHub et mise à jour via Watchtower."""
import logging
import time

import httpx

from . import config, db, notify

log = logging.getLogger("guardian.updater")

WORKFLOW = "docker.yml"

state: dict = {
    "version": config.APP_VERSION,
    "sha": config.GIT_SHA,
    "latest_sha": None,
    "available": False,
    "commits": [],
    "checked": None,
    "error": None,
    "updating": False,
    "enabled": bool(config.UPDATE_TOKEN),
    "repo": config.GITHUB_REPO,
}


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json"}
    if config.GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
    return h


async def check() -> dict:
    """Compare la version en cours à la dernière image construite avec succès."""
    state["checked"] = time.time()
    if not config.GITHUB_REPO:
        state["error"] = "GITHUB_REPO non défini"
        return state
    try:
        async with httpx.AsyncClient(timeout=15, headers=_headers()) as client:
            r = await client.get(
                f"https://api.github.com/repos/{config.GITHUB_REPO}/actions/workflows/"
                f"{WORKFLOW}/runs",
                params={"branch": "main", "status": "success", "per_page": 1},
            )
            r.raise_for_status()
            runs = r.json().get("workflow_runs", [])
            if not runs:
                state.update(error="Aucune image construite pour l'instant", available=False)
                return state
            latest = runs[0]["head_sha"]
            state["latest_sha"] = latest
            state["error"] = None
            if config.GIT_SHA in ("", "dev"):
                state.update(available=False, commits=[],
                             error="Version construite localement : mise à jour non suivie")
                return state
            state["available"] = latest != config.GIT_SHA
            state["commits"] = []
            if state["available"]:
                c = await client.get(
                    f"https://api.github.com/repos/{config.GITHUB_REPO}/compare/"
                    f"{config.GIT_SHA}...{latest}")
                if c.status_code == 200:
                    state["commits"] = [
                        {"sha": x["sha"][:7],
                         "message": x["commit"]["message"].splitlines()[0][:140],
                         "date": x["commit"]["committer"]["date"]}
                        for x in reversed(c.json().get("commits", [])[-20:])
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
                           f"({config.GIT_SHA[:7]} → {(state['latest_sha'] or '?')[:7]})")
    try:
        async with httpx.AsyncClient(timeout=900) as client:
            r = await client.get(f"{config.UPDATE_URL}/v1/update",
                                 headers={"Authorization": f"Bearer {config.UPDATE_TOKEN}"})
        if r.status_code == 401:
            raise RuntimeError("Watchtower refuse le jeton (UPDATE_TOKEN différent ?)")
        # Si on arrive ici, aucune nouvelle image n'a été trouvée par Watchtower
        db.add_event("info", "Watchtower n'a pas trouvé de nouvelle image "
                             "(construction GitHub peut-être encore en cours)")
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
    previous = db.kv_get("running_sha")
    if previous and previous != config.GIT_SHA:
        msg = f"HA Guardian mis à jour : v{config.APP_VERSION} ({config.GIT_SHA[:7]})"
        db.add_event("ok", msg)
        await notify.send("⬆️ HA Guardian mis à jour", msg)
    db.kv_set("running_sha", config.GIT_SHA)
