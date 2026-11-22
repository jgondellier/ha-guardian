"""Boucle de surveillance, escalade automatique par paliers et alertes."""
import asyncio
import logging
import time

from . import config, db, devices, network, notify

log = logging.getLogger("guardian.watchdog")

state: dict = {
    "last_check": None,
    "ha": None, "host": None, "ssh": None, "slzb": None, "plug": None,
    "sentinels": [],
    "system": None,
    "consecutive_failures": 0,
    "incident_since": None,
    "incident_steps_done": [],
    "last_action_ts": 0.0,
    "last_action": None,
    "grace_until": 0.0,
    "busy": None,
    "alerts": {},      # alertes actives (clé -> message), pour ne prévenir qu'une fois
}

_check_now = asyncio.Event()
_action_lock = asyncio.Lock()


async def _plug(on: bool) -> str:
    await devices.plug_set(on)
    return "Prise allumée" if on else "Prise éteinte"


async def _wol() -> str:
    return devices.wake_on_lan()


# Actions disponibles : clé -> (libellé, fonction)
ACTIONS = {
    "ha_reload_automations": ("Recharger les automatisations",
                              lambda: devices.ha_service("automation", "reload")),
    "ha_restart": ("Redémarrer Home Assistant (API)",
                   lambda: devices.ha_service("homeassistant", "restart")),
    "ha_host_reboot_api": ("Redémarrer le mini-PC (via HA)",
                           lambda: devices.ha_service("hassio", "host_reboot")),
    "ssh_core_restart": ("Redémarrer HA Core (SSH)",
                         lambda: devices.ssh_run("ha core restart")),
    "ssh_host_reboot": ("Redémarrer le mini-PC (SSH)",
                        lambda: devices.ssh_run("ha host reboot", timeout=60)),
    "wol": ("Réveiller le mini-PC (Wake-on-LAN)", _wol),
    "plug_cycle": ("Couper / rallumer la prise", devices.plug_cycle),
    "plug_off": ("Éteindre la prise", lambda: _plug(False)),
    "plug_on": ("Allumer la prise", lambda: _plug(True)),
}

REBOOTING = {"ha_restart", "ha_host_reboot_api", "ssh_core_restart",
             "ssh_host_reboot", "plug_cycle", "plug_on", "wol"}


def action_available(key: str) -> bool:
    if key.startswith("ssh_"):
        return config.SSH_ENABLED
    if key.startswith("plug_"):
        return config.PLUG_ENABLED
    if key == "wol":
        return config.WOL_ENABLED
    return True


def trigger_check() -> None:
    _check_now.set()


# ---------------------------------------------------------------- maintenance
def maintenance_until() -> float:
    until = db.kv_get("maintenance_until", 0) or 0
    return until if until > time.time() else 0


def set_maintenance(minutes: int, origin: str = "manuel") -> float:
    if minutes <= 0:
        db.kv_set("maintenance_until", 0)
        db.add_event("info", f"[{origin}] Fin du mode maintenance")
        return 0
    until = time.time() + minutes * 60
    db.kv_set("maintenance_until", until)
    db.add_event("info", f"[{origin}] Mode maintenance pendant {minutes} min "
                         f"(jusqu'à {time.strftime('%H:%M', time.localtime(until))})")
    return until


# ---------------------------------------------------------------- alertes
async def alert(key: str, active: bool, title: str, message: str,
                recovered: str | None = None, priority: int = 4) -> None:
    """Prévient une seule fois à l'apparition d'un problème, puis à sa disparition."""
    alerts = state["alerts"]
    if active and key not in alerts:
        alerts[key] = message
        db.add_event("warn", f"{title} : {message}")
        if not maintenance_until():
            await notify.send(title, message, priority=priority)
    elif not active and key in alerts:
        alerts.pop(key)
        if recovered:
            db.add_event("ok", recovered)
            if not maintenance_until():
                await notify.send("✅ Rétabli", recovered, priority=2)


# ---------------------------------------------------------------- actions
async def run_action(key: str, origin: str = "manuel") -> str:
    if key not in ACTIONS:
        raise KeyError(key)
    label, func = ACTIONS[key]
    if _action_lock.locked():
        raise RuntimeError(f"Une action est déjà en cours : {state['busy']}")
    async with _action_lock:
        state["busy"] = label
        db.add_event("action", f"[{origin}] {label} : lancement")
        try:
            msg = await func()
            db.add_event("action", f"[{origin}] {label} : {msg}")
            if key in REBOOTING:
                grace = db.get_settings()["boot_grace_minutes"] * 60
                state["grace_until"] = time.time() + grace
            state["last_action_ts"] = time.time()
            state["last_action"] = f"{label} ({origin})"
            if key == "plug_cycle":
                db.add_event("power", "Coupure prise effectuée")
            return msg
        except Exception as exc:  # noqa: BLE001
            db.add_event("error", f"[{origin}] {label} : échec — {exc}")
            raise
        finally:
            state["busy"] = None
            trigger_check()


# ---------------------------------------------------------------- contrôles
def _sentinel_list(settings: dict) -> list[str]:
    raw = settings["sentinel_entities"].replace(",", "\n")
    return [e.strip() for e in raw.splitlines() if "." in e.strip()][:20]


async def _run_checks(settings: dict) -> None:
    ssh_coro = (devices.tcp_open(config.HA_HOST, config.SSH_PORT) if config.SSH_ENABLED
                else asyncio.sleep(0, result=None))
    ha, host_ping, ssh_open, slzb, plug, _net = await asyncio.gather(
        devices.check_ha(), devices.ping(config.HA_HOST), ssh_coro,
        devices.check_slzb(), devices.plug_status(), network.check_all(settings),
    )
    # Double contrôle : on confirme un échec avant de le compter
    if not ha["ok"] and settings["retry_seconds"] > 0:
        await asyncio.sleep(settings["retry_seconds"])
        second = await devices.check_ha()
        if second["ok"]:
            ha = second
    ha["slow"] = bool(ha["ok"] and ha["latency_ms"] and ha["latency_ms"] > settings["slow_ms"])

    host_ok = host_ping or bool(ssh_open) or ha["ok"] or await devices.tcp_open(
        config.HA_HOST, 8123)

    sentinels = []
    entities = _sentinel_list(settings)
    if ha["ok"] and entities:
        sentinels = await devices.check_sentinels(entities, settings["sentinel_max_minutes"])

    state.update(
        last_check=time.time(), ha=ha, host={"ok": host_ok, "ping": host_ping},
        ssh=None if ssh_open is None else {"ok": ssh_open},
        slzb=slzb, plug=plug, sentinels=sentinels,
    )
    db.add_check(ha["ok"], host_ok, ssh_open, slzb["ok"] if slzb else None, ha["latency_ms"])

    # Alertes secondaires (sans action automatique)
    bad = [s for s in sentinels if not s["ok"]]
    await alert("sentinels", bool(bad), "📡 Capteurs muets",
                ", ".join(f"{s.get('name', s['entity_id'])}"
                          + (f" ({s['age_min']} min)" if s.get("age_min") is not None else "")
                          for s in bad) + " — réseau Zigbee à vérifier",
                recovered="Les capteurs témoins donnent de nouveau signe de vie")
    await network.alerts(settings)
    if slzb is not None:
        await alert("slzb", not slzb["ok"], "📡 SLZB-06 injoignable",
                    "Le coordinateur Zigbee ne répond plus.",
                    recovered="SLZB-06 de nouveau joignable")


async def _evaluate(settings: dict) -> None:
    s = settings
    ha_ok = state["ha"]["ok"]
    now = time.time()
    in_maintenance = bool(maintenance_until())

    if ha_ok:
        if state["incident_since"]:
            duration = int((now - state["incident_since"]) / 60)
            db.add_event("ok", f"Home Assistant rétabli (panne d'environ {duration} min)")
            if state["consecutive_failures"] >= s["notify_after"] and not in_maintenance:
                await notify.send("✅ Home Assistant rétabli",
                                  f"De nouveau joignable après ~{duration} min.")
        state.update(consecutive_failures=0, incident_since=None, incident_steps_done=[])
        return

    state["consecutive_failures"] += 1
    fails = state["consecutive_failures"]
    if state["incident_since"] is None:
        state["incident_since"] = now
        db.add_event("warn", f"Home Assistant ne répond pas : {state['ha'].get('error')}"
                     + (" (mode maintenance)" if in_maintenance else ""))
    if in_maintenance:
        return

    # Si un équipement réseau critique (routeur principal) est en panne, le problème
    # vient du réseau : redémarrer HA ou couper le mini-PC n'y changerait rien.
    # Le compteur repart de zéro pour que l'escalade reprenne normalement ensuite.
    net_down = network.critical_down()
    if net_down:
        state["consecutive_failures"] = 0
        if "network" not in state["incident_steps_done"]:
            state["incident_steps_done"].append("network")
            db.add_event("warn", "Panne réseau (" + ", ".join(net_down) + ") : "
                         "aucune intervention sur Home Assistant")
        return

    host_ok = state["host"]["ok"]
    if fails == s["notify_after"]:
        buttons = []
        if config.SSH_ENABLED and host_ok:
            buttons.append("ssh_core_restart")
        if config.WOL_ENABLED and not host_ok:
            buttons.append("wol")
        if config.PLUG_ENABLED:
            buttons.append("plug_cycle")
        buttons.append("maintenance_60")
        await notify.send("⚠️ Home Assistant ne répond pas",
                          f"{fails} échecs consécutifs ({state['ha'].get('error')}). "
                          f"Mini-PC joignable : {'oui' if host_ok else 'non'}.",
                          priority=4, actions=buttons)

    if not s["watchdog_enabled"]:
        return
    if now < state["grace_until"]:
        return
    if now - state["last_action_ts"] < s["cooldown_minutes"] * 60:
        return
    if (state["ha"].get("error") or "").startswith("Jeton refusé"):
        return  # problème de configuration, pas une panne

    done = state["incident_steps_done"]
    ssh_ok = bool(state["ssh"] and state["ssh"]["ok"])
    # Paliers du plus doux au plus brutal ; un palier indisponible est sauté
    ladder = [
        ("wol", s["fail_core_restart"], config.WOL_ENABLED and not host_ok),
        ("ssh_core_restart", s["fail_core_restart"], ssh_ok),
        ("ssh_host_reboot", s["fail_host_reboot"], ssh_ok),
        ("plug_cycle", s["fail_power_cycle"], config.PLUG_ENABLED),
    ]
    step = next((k for k, threshold, available in ladder
                 if available and fails >= threshold and k not in done), None)
    if step == "plug_cycle":
        if db.count_events_since("Coupure prise effectuée", now - 86400) >= \
                s["max_power_cycles_per_day"]:
            if "limit" not in done:
                done.append("limit")
                db.add_event("error", "Limite quotidienne de coupures atteinte : "
                                      "intervention manuelle requise")
                await notify.send("🛑 Intervention manuelle requise",
                                  "Limite quotidienne de coupures de la prise atteinte.",
                                  priority=5)
            return

    if step:
        done.append(step)
        label = ACTIONS[step][0]
        await notify.send("🔧 Action automatique", f"{label} après {fails} échecs.",
                          priority=4)
        try:
            await run_action(step, origin="auto")
        except Exception as exc:  # noqa: BLE001
            await notify.send("❌ Action automatique échouée", f"{label} : {exc}",
                              priority=5)


async def loop() -> None:
    db.add_event("info", f"Surveillance démarrée (v{config.APP_VERSION})")
    last_prune = 0.0
    while True:
        try:
            settings = db.get_settings()
            await _run_checks(settings)
            await _evaluate(settings)
            if time.time() - last_prune > 3600:
                db.prune()
                last_prune = time.time()
        except Exception:  # noqa: BLE001
            log.exception("Erreur dans la boucle de surveillance")
        try:
            await asyncio.wait_for(_check_now.wait(), config.CHECK_INTERVAL)
        except asyncio.TimeoutError:
            pass
        _check_now.clear()
