"""Santé interne de Home Assistant : apps (ex add-ons), intégrations, réparations,
journal d'erreurs, taille de la base, batteries."""
import asyncio
import json
import logging
import re
import time
from collections import Counter
from datetime import datetime

from . import config, db, devices, watchdog

log = logging.getLogger("guardian.health")

state: dict = {
    "apps": None,            # liste des apps, ou None si pas encore lue
    "apps_error": None,
    "integrations": None,    # intégrations en échec
    "repairs": None,         # réparations proposées par HA
    "ws_error": None,
    "log": None,             # statistiques du journal d'erreurs
    "batteries": None,       # {"low": [...], "unavailable": [...]}
    "checked": None,
}
_previous_app_states: dict[str, str] = {}
_last_app_restart: dict[str, float] = {}

_SLUG_RE = re.compile(r"^[a-z0-9_\-]+$")


# ================================================================ apps (ex add-ons)
def parse_apps(output: str) -> list[dict] | None:
    """Lit la sortie de « ha addons --raw-json » (ou « ha apps », depuis HA 2026.2)."""
    for part in output.split("@@"):
        part = part.strip()
        if not part.startswith("{"):
            continue
        try:
            data = json.loads(part)
        except ValueError:
            continue
        if data.get("result") != "ok":
            continue
        items = (data.get("data") or {})
        items = items.get("addons") or items.get("apps") or []
        return sorted(
            ({"slug": a.get("slug"), "name": a.get("name") or a.get("slug"),
              "state": a.get("state") or "unknown", "version": a.get("version"),
              "update_available": bool(a.get("update_available"))}
             for a in items if a.get("slug")),
            key=lambda a: a["name"].lower())
    return None


async def fetch_apps() -> list[dict]:
    _, out, _ = await devices.ssh_exec(
        "sh -c 'ha addons --raw-json 2>/dev/null; echo @@; ha apps --raw-json 2>/dev/null'",
        timeout=60)
    apps = parse_apps(out)
    if apps is None:
        raise RuntimeError("Liste des apps illisible (commande « ha addons » / « ha apps »)")
    return apps


async def app_boot_mode(slug: str) -> str | None:
    if not _SLUG_RE.match(slug):
        return None
    _, out, _ = await devices.ssh_exec(
        f"sh -c 'ha addons info {slug} --raw-json 2>/dev/null; echo @@; "
        f"ha apps info {slug} --raw-json 2>/dev/null'", timeout=60)
    for part in out.split("@@"):
        try:
            data = json.loads(part.strip())
        except ValueError:
            continue
        if data.get("result") == "ok":
            return (data.get("data") or {}).get("boot")
    return None


async def start_app(slug: str) -> str:
    if not _SLUG_RE.match(slug):
        raise RuntimeError("Identifiant d'app invalide")
    status, out, err = await devices.ssh_exec(
        f"sh -c 'ha addons start {slug} 2>/dev/null || ha apps start {slug}'", timeout=180)
    if status not in (0, None):
        raise RuntimeError((out + err).strip()[:300] or f"code {status}")
    return f"App « {slug} » démarrée"


def app_transitions(previous: dict[str, str], apps: list[dict]) -> list[dict]:
    """Apps qui tournaient au contrôle précédent et qui sont maintenant arrêtées."""
    return [a for a in apps
            if previous.get(a["slug"]) == "started" and a["state"] != "started"]


async def _check_apps(settings: dict) -> None:
    try:
        apps = await fetch_apps()
    except Exception as exc:  # noqa: BLE001
        state["apps_error"] = str(exc)[:200]
        return
    state["apps"], state["apps_error"] = apps, None

    for app in app_transitions(_previous_app_states, apps):
        db.add_event("warn", f"App « {app['name']} » arrêtée ({app['state']})")
        await _maybe_restart_app(app, settings)
    for app in apps:
        was_down = f"app:{app['slug']}" in watchdog.state["alerts"]
        down = app["state"] != "started" and (
            was_down or _previous_app_states.get(app["slug"]) == "started")
        await watchdog.alert(f"app:{app['slug']}", down, "🧩 App arrêtée",
                             f"{app['name']} ({app['state']})",
                             recovered=f"App « {app['name']} » de nouveau démarrée")
    _previous_app_states.clear()
    _previous_app_states.update({a["slug"]: a["state"] for a in apps})


async def _maybe_restart_app(app: dict, settings: dict) -> None:
    """Redémarrage automatique : respecte maintenance, période de grâce,
    délai entre actions et limite quotidienne."""
    if not settings["app_autorestart"] or watchdog.maintenance_until():
        return
    now = time.time()
    if now < watchdog.state["grace_until"]:
        return
    if now - _last_app_restart.get(app["slug"], 0) < settings["cooldown_minutes"] * 60:
        return
    prefix = f"Redémarrage auto de l'app « {app['name']} »"
    if db.count_events_since(prefix, now - 86400) >= settings["app_max_restarts_per_day"]:
        db.add_event("error", f"App « {app['name']} » : limite quotidienne de redémarrages atteinte")
        return
    try:
        if await app_boot_mode(app["slug"]) != "auto":
            return  # app en démarrage manuel : arrêt probablement volontaire
        _last_app_restart[app["slug"]] = now
        db.add_event("action", prefix)
        await start_app(app["slug"])
        db.add_event("ok", f"App « {app['name']} » redémarrée")
    except Exception as exc:  # noqa: BLE001
        db.add_event("error", f"{prefix} : échec — {exc}")


# ================================================================ batteries
def parse_batteries(states: list[dict], threshold: int) -> dict:
    low, unavailable = [], []
    for st in states:
        attrs = st.get("attributes") or {}
        if attrs.get("device_class") != "battery":
            continue
        name = attrs.get("friendly_name") or st["entity_id"]
        value = st.get("state")
        if value in ("unavailable", "unknown", None):
            unavailable.append({"entity_id": st["entity_id"], "name": name})
            continue
        if st["entity_id"].startswith("binary_sensor."):
            if value == "on":
                low.append({"entity_id": st["entity_id"], "name": name, "level": None})
            continue
        try:
            level = float(value)
        except (TypeError, ValueError):
            continue
        if attrs.get("unit_of_measurement", "%") == "%" and level <= threshold:
            low.append({"entity_id": st["entity_id"], "name": name, "level": round(level)})
    low.sort(key=lambda b: (b["level"] is None, b["level"] or 0))
    unavailable.sort(key=lambda b: b["name"].lower())
    return {"low": low, "unavailable": unavailable}


# ================================================================ journal d'erreurs
_LOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)? (ERROR|WARNING|CRITICAL) "
    r"\([^)]*\) \[([^\]]+)\] ?(.*)$")


def parse_error_log(text: str, now: float | None = None, window: int = 3600) -> dict:
    now = now or time.time()
    errors_hour = warnings_hour = 0
    sources: Counter = Counter()
    last: list[dict] = []
    for line in text.splitlines():
        m = _LOG_RE.match(line)
        if not m:
            continue
        stamp, level, source, message = m.groups()
        try:
            ts = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        if now - ts > window:
            continue
        if level == "WARNING":
            warnings_hour += 1
            continue
        errors_hour += 1
        sources[source] += 1
        last.append({"ts": ts, "source": source, "message": message[:200]})
    return {"errors_hour": errors_hour, "warnings_hour": warnings_hour,
            "top_sources": sources.most_common(5), "last": last[-5:][::-1]}


# ================================================================ intégrations et réparations
FAILED_ENTRY_STATES = {"setup_error", "setup_retry", "migration_error", "failed_unload"}


def parse_config_entries(entries: list[dict]) -> list[dict]:
    return sorted(
        ({"domain": e.get("domain"), "title": e.get("title") or e.get("domain"),
          "state": e.get("state"), "reason": (e.get("reason") or "")[:200]}
         for e in entries
         if e.get("state") in FAILED_ENTRY_STATES and not e.get("disabled_by")),
        key=lambda e: e["title"].lower())


def parse_repairs(result: dict | list) -> list[dict]:
    issues = result.get("issues", []) if isinstance(result, dict) else result
    return [{"domain": i.get("domain"), "issue_id": i.get("issue_id"),
             "severity": i.get("severity"), "key": i.get("translation_key") or i.get("issue_id")}
            for i in issues if not i.get("ignored") and not i.get("dismissed_version")]


async def ws_query(commands: list[str]) -> list:
    """Envoie quelques commandes à l'API WebSocket de HA et renvoie leurs résultats."""
    import websockets  # import local : seul ce module en a besoin

    url = re.sub(r"^http", "ws", config.HA_URL) + "/api/websocket"
    ssl_ctx = devices.ha_tls_context() if url.startswith("wss") else None
    async with websockets.connect(url, ssl=ssl_ctx, open_timeout=10, max_size=2**23) as ws:
        await asyncio.wait_for(ws.recv(), 10)                       # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": config.HA_TOKEN}))
        auth = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if auth.get("type") != "auth_ok":
            raise RuntimeError("Authentification WebSocket refusée")
        results = []
        for i, cmd in enumerate(commands, start=1):
            await ws.send(json.dumps({"id": i, "type": cmd}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 20))
                if msg.get("id") == i and msg.get("type") == "result":
                    results.append(msg.get("result") if msg.get("success") else None)
                    break
        return results


# ================================================================ orchestration
async def refresh(settings: dict) -> None:
    ha = watchdog.state.get("ha") or {}
    if not ha.get("ok"):
        return
    jobs = [_check_ws(), _check_log(settings), _check_batteries(settings)]
    if config.SSH_ENABLED:
        jobs.append(_check_apps(settings))
    await asyncio.gather(*jobs, return_exceptions=True)
    state["checked"] = time.time()


async def _check_ws() -> None:
    try:
        entries, repairs = await ws_query(["config_entries/get", "repairs/list_issues"])
        state["integrations"] = parse_config_entries(entries or [])
        state["repairs"] = parse_repairs(repairs or {})
        state["ws_error"] = None
    except Exception as exc:  # noqa: BLE001
        state["ws_error"] = f"{type(exc).__name__} : {str(exc)[:150]}"
        return
    failed = state["integrations"]
    await watchdog.alert(
        "integrations", bool(failed), "🔌 Intégrations en échec",
        ", ".join(f"{e['title']} ({e['state']})" for e in failed),
        recovered="Toutes les intégrations sont de nouveau chargées", priority=3)


async def _check_log(settings: dict) -> None:
    try:
        text = await devices.ha_error_log()
    except Exception as exc:  # noqa: BLE001
        state["log"] = {"error": str(exc)[:150]}
        return
    stats = parse_error_log(text)
    state["log"] = stats
    limit = settings["log_errors_per_hour"]
    top = ", ".join(f"{s} ({n})" for s, n in stats["top_sources"][:3])
    await watchdog.alert(
        "log_errors", limit > 0 and stats["errors_hour"] >= limit, "📜 Beaucoup d'erreurs dans HA",
        f"{stats['errors_hour']} erreurs en 1 h. Principales sources : {top}",
        recovered="Le journal de HA est redevenu calme", priority=3)


async def _check_batteries(settings: dict) -> None:
    try:
        states = await devices.ha_states()
    except Exception:  # noqa: BLE001
        return
    previous = {b["entity_id"] for b in (state["batteries"] or {}).get("low", [])}
    result = parse_batteries(states, settings["battery_alert_percent"])
    state["batteries"] = result
    new_low = [b for b in result["low"] if b["entity_id"] not in previous]
    if new_low and previous is not None and state["checked"] is not None:
        names = ", ".join(f"{b['name']}" + (f" ({b['level']} %)" if b["level"] is not None else "")
                          for b in new_low)
        db.add_event("warn", f"Batteries faibles : {names}")
        if not watchdog.maintenance_until():
            from . import notify
            await notify.send("🔋 Batteries à changer", names, priority=2)
