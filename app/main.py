"""Application web HA Guardian : API + interface."""
import asyncio
import hashlib
import hmac
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import backups, config, db, devices, health, network, notify, tasks, updater, watchdog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("guardian")

STATIC = Path(__file__).parent / "static"
COOKIE = "guardian_session"
SESSION_DAYS = 30
PUBLIC_PATHS = {"/login", "/api/login", "/static/style.css", "/healthz"}

if not config.APP_PASSWORD:
    raise SystemExit("APP_PASSWORD est vide : définis un mot de passe dans le fichier .env")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    devices.ensure_ssh_key()
    await updater.on_startup()
    bg = [asyncio.create_task(watchdog.loop()), asyncio.create_task(tasks.loop())]
    yield
    for t in bg:
        t.cancel()


app = FastAPI(title="HA Guardian", lifespan=lifespan, docs_url=None, redoc_url=None)


# ================================================================ authentification
def _sign(value: str) -> str:
    return hmac.new(config.SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


def _make_token() -> str:
    expires = str(int(time.time()) + SESSION_DAYS * 86400)
    return f"{expires}.{_sign(expires)}"


def _valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expires, sig = token.split(".", 1)
    return (hmac.compare_digest(sig, _sign(expires))
            and expires.isdigit() and int(expires) > time.time())


_failed_logins: dict[str, list[float]] = {}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if (path in PUBLIC_PATHS or path.startswith("/api/hook/")
            or _valid(request.cookies.get(COOKIE))):
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Non authentifié"}, status_code=401)
    return RedirectResponse("/login")


@app.get("/login")
async def login_page():
    return FileResponse(STATIC / "login.html")


@app.post("/api/login")
async def login(request: Request):
    ip = request.client.host if request.client else "?"
    recent = [t for t in _failed_logins.get(ip, []) if t > time.time() - 600]
    if len(recent) >= 5:
        raise HTTPException(429, "Trop de tentatives, réessaie dans 10 minutes")
    body = await request.json()
    if not hmac.compare_digest(str(body.get("password", "")), config.APP_PASSWORD):
        recent.append(time.time())
        _failed_logins[ip] = recent
        await asyncio.sleep(1)
        raise HTTPException(401, "Mot de passe incorrect")
    _failed_logins.pop(ip, None)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, _make_token(), max_age=SESSION_DAYS * 86400, httponly=True,
                    samesite="strict", secure=request.url.scheme == "https")
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


# ================================================================ pages
@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": config.APP_VERSION}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ================================================================ état
@app.get("/api/status")
async def status():
    s = watchdog.state
    settings = db.get_settings()
    now = time.time()
    return {
        "now": now,
        "check_interval": config.CHECK_INTERVAL,
        "last_check": s["last_check"],
        "ha": s["ha"], "host": s["host"], "ssh": s["ssh"],
        "slzb": s["slzb"], "plug": s["plug"],
        "sentinels": s["sentinels"], "system": s["system"],
        "alerts": s["alerts"],
        "ha_host": config.HA_HOST,
        "plug_type": config.PLUG_TYPE if config.PLUG_ENABLED else None,
        "wol": config.WOL_ENABLED,
        "watchdog": {
            "enabled": settings["watchdog_enabled"],
            "consecutive_failures": s["consecutive_failures"],
            "incident_since": s["incident_since"],
            "steps_done": s["incident_steps_done"],
            "grace_remaining": max(0, int(s["grace_until"] - now)),
            "last_action": s["last_action"],
            "last_action_ts": s["last_action_ts"] or None,
            "busy": s["busy"],
            "maintenance_until": watchdog.maintenance_until() or None,
        },
        "uptime": {"24h": db.uptime_ratio(24), "7j": db.uptime_ratio(24 * 7)},
        "backup": {**backups.state, "enabled": settings["backup_enabled"]},
        "ha_updates": tasks.ha_updates,
        "app": updater.state,
        "health": health.state,
        "network": {"devices": network.state["devices"], "asus": network.state["asus"],
                    "asus_enabled": config.ASUS_ENABLED},
    }


@app.get("/api/timeline")
async def timeline(hours: int = 24):
    hours = min(max(hours, 1), 24 * config.HISTORY_DAYS)
    return db.timeline(hours=hours, buckets=96)


@app.get("/api/events")
async def events(limit: int = 100):
    return db.events(min(limit, 500))


# ================================================================ actions
@app.get("/api/actions")
async def actions():
    return [{"key": k, "label": v[0], "available": watchdog.action_available(k)}
            for k, v in watchdog.ACTIONS.items()]


def _launch_action(key: str, origin: str = "manuel") -> None:
    async def _bg():
        try:
            await watchdog.run_action(key, origin)
        except Exception:  # noqa: BLE001
            pass  # déjà journalisé
    tasks.spawn(_bg())


@app.post("/api/actions/{key}")
async def run_action(key: str):
    if key not in watchdog.ACTIONS:
        raise HTTPException(404, "Action inconnue")
    if not watchdog.action_available(key):
        raise HTTPException(400, "Action non configurée")
    if watchdog.state["busy"]:
        raise HTTPException(409, f"Action déjà en cours : {watchdog.state['busy']}")
    _launch_action(key)
    await asyncio.sleep(0.2)
    return {"ok": True, "message": f"{watchdog.ACTIONS[key][0]} : lancé"}


@app.api_route("/api/hook/{key}", methods=["GET", "POST"])
async def hook(key: str, exp: int = 0, sig: str = ""):
    """Boutons des notifications ntfy (liens signés, valables 12 h)."""
    if not notify.verify_hook(key, exp, sig):
        raise HTTPException(403, "Lien invalide ou expiré")
    if key == "maintenance_60":
        watchdog.set_maintenance(60, origin="notification")
        return {"ok": True, "message": "Maintenance 1 h"}
    if watchdog.state["busy"]:
        raise HTTPException(409, "Action déjà en cours")
    _launch_action(key, origin="notification")
    return {"ok": True, "message": f"{watchdog.ACTIONS[key][0]} : lancé"}


@app.post("/api/check")
async def check_now():
    watchdog.trigger_check()
    return {"ok": True}


@app.post("/api/maintenance")
async def maintenance(request: Request):
    body = await request.json()
    until = watchdog.set_maintenance(int(body.get("minutes", 0)))
    return {"ok": True, "until": until or None}


# ================================================================ sauvegardes
@app.get("/api/backups")
async def list_backups():
    return {"files": backups.list_local(), "state": backups.state,
            "dir": str(config.BACKUP_DIR)}


@app.post("/api/backups")
async def create_backup():
    if backups.state["running"]:
        raise HTTPException(409, "Une sauvegarde est déjà en cours")
    if not config.SSH_ENABLED:
        raise HTTPException(400, "Les sauvegardes nécessitent le SSH")

    async def _bg():
        try:
            await backups.run()
        except Exception:  # noqa: BLE001
            pass
    tasks.spawn(_bg())
    await asyncio.sleep(0.2)
    return {"ok": True, "message": "Sauvegarde lancée"}


@app.get("/api/backups/{name}")
async def download_backup(name: str):
    path = backups.local_path(name)
    if not path:
        raise HTTPException(404, "Sauvegarde introuvable")
    return FileResponse(path, filename=name, media_type="application/x-tar")


# ================================================================ mises à jour HA
@app.post("/api/ha-updates/refresh")
async def refresh_ha_updates():
    await tasks.refresh_ha_updates()
    return tasks.ha_updates


@app.post("/api/ha-updates/install")
async def install_ha_update(request: Request):
    body = await request.json()
    entity_id = str(body.get("entity_id", ""))
    if not entity_id.startswith("update."):
        raise HTTPException(400, "Entité invalide")
    if tasks.ha_updates["installing"]:
        raise HTTPException(409, "Une mise à jour est déjà en cours")
    with_backup = bool(body.get("backup", True)) and config.SSH_ENABLED
    tasks.spawn(tasks.install_ha_update(entity_id, with_backup))
    return {"ok": True, "message": "Installation lancée"
            + (" (sauvegarde d'abord)" if with_backup else "")}


# ================================================================ mise à jour de l'appli
@app.post("/api/app/check")
async def app_check():
    return await updater.check()


@app.post("/api/app/update")
async def app_update():
    if updater.state["updating"]:
        raise HTTPException(409, "Mise à jour déjà en cours")
    if not config.UPDATE_TOKEN:
        raise HTTPException(400, "UPDATE_TOKEN non défini dans .env")

    async def _bg():
        try:
            await updater.trigger()
        except Exception:  # noqa: BLE001
            pass
    tasks.spawn(_bg())
    await asyncio.sleep(0.5)
    return {"ok": True, "message": "Mise à jour lancée : l'interface va redémarrer"}


# ================================================================ réglages
@app.get("/api/settings")
async def get_settings():
    return {
        "settings": db.get_settings(),
        "ssh_public_key": devices.ensure_ssh_key(),
        "ssh_fingerprint": devices.known_fingerprint(),
        "ssh": {"enabled": config.SSH_ENABLED, "user": config.SSH_USER,
                "port": config.SSH_PORT, "host": config.HA_HOST},
        "notifications": notify.enabled(),
        "notification_buttons": bool(config.PUBLIC_URL),
        "heartbeat": bool(config.HEARTBEAT_URL),
        "plug": config.PLUG_TYPE if config.PLUG_ENABLED else None,
        "wol": config.WOL_ENABLED,
        "asus": config.ASUS_ENABLED,
        "backup_dir": str(config.BACKUP_DIR),
        "version": {"version": config.APP_VERSION, "sha": config.GIT_SHA,
                    "repo": config.GITHUB_REPO},
    }


@app.put("/api/settings")
async def put_settings(request: Request):
    body = await request.json()
    before = db.get_settings()
    after = db.save_settings(body)
    if before["watchdog_enabled"] != after["watchdog_enabled"]:
        db.add_event("info", "Chien de garde " + ("activé" if after["watchdog_enabled"]
                                                  else "désactivé"))
    elif before != after:
        db.add_event("info", "Réglages modifiés")
    return after


@app.post("/api/ssh/reset-fingerprint")
async def reset_fingerprint():
    devices.reset_fingerprint()
    db.add_event("info", "Empreinte SSH du mini-PC réinitialisée")
    return {"ok": True}


@app.post("/api/test-notification")
async def test_notification():
    if not notify.enabled():
        raise HTTPException(400, "Aucune notification configurée dans .env")
    await notify.send("🔔 Test HA Guardian", "Les notifications fonctionnent.",
                      actions=["maintenance_60"])
    return {"ok": True}


@app.post("/api/report")
async def send_report():
    if not notify.enabled():
        raise HTTPException(400, "Aucune notification configurée dans .env")
    text = await tasks.weekly_report(db.get_settings(), force=True)
    return {"ok": True, "text": text}
