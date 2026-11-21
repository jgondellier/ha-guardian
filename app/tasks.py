"""Tâches planifiées : santé du mini-PC, sauvegardes, rapport, signal de vie,
mises à jour HA et vérification de nouvelle version."""
import asyncio
import logging
import time
from datetime import datetime

import httpx

from . import backups, config, db, devices, notify, updater, watchdog

log = logging.getLogger("guardian.tasks")

ha_updates: dict = {"items": [], "checked": None, "error": None, "installing": None}
_last: dict[str, float] = {}
_background: set[asyncio.Task] = set()

DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def _due(name: str, every: float) -> bool:
    if time.time() - _last.get(name, 0) >= every:
        _last[name] = time.time()
        return True
    return False


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


# ---------------------------------------------------------------- sous-tâches
async def heartbeat() -> None:
    if not config.HEARTBEAT_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.get(config.HEARTBEAT_URL)
    except Exception as exc:  # noqa: BLE001
        log.warning("Signal de vie : %s", exc)


async def refresh_system(settings: dict) -> None:
    host = watchdog.state.get("host") or {}
    if not config.SSH_ENABLED or not host.get("ok"):
        return
    try:
        stats = await devices.system_stats()
        watchdog.state["system"] = stats
    except Exception as exc:  # noqa: BLE001
        watchdog.state["system"] = {"error": str(exc)[:200], "ts": time.time()}
        return
    disk = stats.get("disk_percent")
    if disk is not None:
        await watchdog.alert(
            "disk", disk >= settings["disk_alert_percent"], "💾 Disque du mini-PC presque plein",
            f"{disk} % utilisés ({stats.get('disk_free_gb')} Go libres).",
            recovered="Espace disque du mini-PC revenu à la normale")
    temp = stats.get("temp_c")
    if temp is not None:
        await watchdog.alert(
            "temp", temp >= settings["temp_alert_celsius"], "🌡️ Mini-PC trop chaud",
            f"Température : {temp} °C.", recovered="Température du mini-PC revenue à la normale")


async def refresh_ha_updates() -> None:
    if not (watchdog.state.get("ha") or {}).get("ok"):
        return
    try:
        ha_updates["items"] = await devices.ha_updates()
        ha_updates["error"] = None
    except Exception as exc:  # noqa: BLE001
        ha_updates["error"] = str(exc)[:200]
    ha_updates["checked"] = time.time()


async def install_ha_update(entity_id: str, with_backup: bool) -> None:
    """Sauvegarde (facultative) puis installation d'une mise à jour HA, en mode maintenance."""
    item = next((u for u in ha_updates["items"] if u["entity_id"] == entity_id), None)
    title = item["title"] if item else entity_id
    ha_updates["installing"] = title
    watchdog.set_maintenance(45, origin="mise à jour")
    try:
        if with_backup:
            await backups.run(origin=f"avant mise à jour {title}")
        db.add_event("action", f"Installation de la mise à jour : {title}")
        msg = await devices.ha_service("update", "install", {"entity_id": entity_id},
                                       timeout=1800)
        db.add_event("ok", f"Mise à jour {title} : {msg}")
    except Exception as exc:  # noqa: BLE001
        db.add_event("error", f"Mise à jour {title} échouée : {exc}")
        await notify.send("❌ Mise à jour échouée", f"{title} : {exc}", priority=4)
    finally:
        ha_updates["installing"] = None
        await asyncio.sleep(60)
        await refresh_ha_updates()


async def scheduled_backup(settings: dict) -> None:
    if not settings["backup_enabled"] or not config.SSH_ENABLED:
        return
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if (now.hour == settings["backup_hour"] and db.kv_get("last_backup_try") != today
            and not backups.state["running"]
            and (watchdog.state.get("ha") or {}).get("ok")):
        db.kv_set("last_backup_try", today)
        try:
            await backups.run(origin="auto")
        except Exception as exc:  # noqa: BLE001
            await notify.send("❌ Sauvegarde échouée", str(exc)[:300], priority=4)
    age = backups.latest_age_hours()
    too_old = age is None or age > settings["backup_max_age_hours"]
    await watchdog.alert(
        "backup_age", too_old, "🗄️ Sauvegarde trop ancienne",
        "Aucune sauvegarde sur le NAS" if age is None
        else f"Dernière sauvegarde il y a {int(age)} h.",
        recovered="Sauvegarde récente présente sur le NAS", priority=3)


async def weekly_report(settings: dict, force: bool = False) -> str:
    now = datetime.now()
    week = now.strftime("%G-W%V")
    if not force:
        if (not settings["report_enabled"] or now.weekday() != settings["report_day"]
                or now.hour < settings["report_hour"] or db.kv_get("last_report") == week):
            return ""
        db.kv_set("last_report", week)
    since = time.time() - 7 * 86400
    up = db.uptime_ratio(24 * 7)
    incidents = db.count_events_since("Home Assistant ne répond pas", since)
    autos = db.count_events_since("[auto]", since, level="action") // 2
    lines = [f"Disponibilité : {up if up is not None else '?'} %",
             f"Pannes : {incidents} · actions automatiques : {autos}"]
    files = backups.list_local()
    if files:
        lines.append(f"Dernière sauvegarde : {datetime.fromtimestamp(files[0]['mtime']):%d/%m %H:%M}"
                     f" ({len(files)} conservées)")
    sysinfo = watchdog.state.get("system") or {}
    if sysinfo.get("disk_percent") is not None:
        lines.append(f"Disque : {sysinfo['disk_percent']} % · RAM : {sysinfo.get('mem_percent', '?')} %"
                     + (f" · {sysinfo['temp_c']} °C" if sysinfo.get("temp_c") else ""))
    if ha_updates["items"]:
        lines.append(f"Mises à jour HA en attente : {len(ha_updates['items'])}")
    if updater.state.get("available"):
        lines.append("Nouvelle version de HA Guardian disponible")
    text = "\n".join(lines)
    await notify.send("📊 Rapport hebdomadaire", text, priority=2)
    db.add_event("info", "Rapport hebdomadaire envoyé")
    return text


# ---------------------------------------------------------------- boucle
async def loop() -> None:
    await asyncio.sleep(20)  # laisse le 1er contrôle se terminer
    while True:
        settings = db.get_settings()
        jobs = []
        if _due("heartbeat", 300):
            jobs.append(heartbeat())
        if _due("system", config.SYSTEM_INTERVAL):
            jobs.append(refresh_system(settings))
        if _due("ha_updates", 1800):
            jobs.append(refresh_ha_updates())
        if _due("schedule", 60):
            jobs.append(scheduled_backup(settings))
            jobs.append(weekly_report(settings))
        if _due("app_update", 6 * 3600):
            jobs.append(updater.check())
        for job in jobs:
            spawn(_safe(job))
        await asyncio.sleep(20)


async def _safe(coro) -> None:
    try:
        await coro
    except Exception:  # noqa: BLE001
        log.exception("Erreur dans une tâche planifiée")
