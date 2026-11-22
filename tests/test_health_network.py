"""Santé de HA (apps, batteries, journal, intégrations) et réseau (équipements, Asus)."""
import json
import time
from datetime import datetime

from app import health, network

# ---------------------------------------------------------------- apps
APPS_JSON = json.dumps({"result": "ok", "data": {"addons": [
    {"slug": "core_mosquitto", "name": "Mosquitto broker", "state": "started", "version": "6.5"},
    {"slug": "a0d7b954_zigbee2mqtt", "name": "Zigbee2MQTT", "state": "stopped", "update_available": True},
    {"name": "sans slug"},
]}})


def test_lecture_des_apps_avec_ancienne_ou_nouvelle_commande():
    apps = health.parse_apps(APPS_JSON + "\n@@\n")
    assert [a["slug"] for a in apps] == ["core_mosquitto", "a0d7b954_zigbee2mqtt"]
    assert apps[1]["update_available"] is True
    # « ha addons » inconnu, « ha apps » répond avec la nouvelle clé
    new = json.dumps({"result": "ok", "data": {"apps": [{"slug": "x", "name": "X", "state": "started"}]}})
    assert health.parse_apps("Error: unknown command\n@@\n" + new)[0]["slug"] == "x"
    assert health.parse_apps("@@") is None


def test_transitions_vers_arret():
    apps = health.parse_apps(APPS_JSON)
    prev = {"core_mosquitto": "started", "a0d7b954_zigbee2mqtt": "started"}
    assert [a["slug"] for a in health.app_transitions(prev, apps)] == ["a0d7b954_zigbee2mqtt"]
    assert health.app_transitions({}, apps) == []   # premier contrôle : pas d'alerte


def test_identifiant_d_app_dangereux_refuse():
    import asyncio
    import pytest
    with pytest.raises(RuntimeError):
        asyncio.run(health.start_app("x; rm -rf /"))


# ---------------------------------------------------------------- batteries
def state(eid, value, **attrs):
    return {"entity_id": eid, "state": value, "attributes": attrs}


def test_batteries_faibles_et_injoignables():
    states = [
        state("sensor.porte_battery", "12", device_class="battery", unit_of_measurement="%", friendly_name="Porte"),
        state("sensor.salon_battery", "80", device_class="battery", unit_of_measurement="%"),
        state("sensor.fenetre_battery", "3", device_class="battery", unit_of_measurement="%", friendly_name="Fenêtre"),
        state("sensor.cave_battery", "unavailable", device_class="battery", friendly_name="Cave"),
        state("binary_sensor.fumee_battery_low", "on", device_class="battery", friendly_name="Détecteur fumée"),
        state("binary_sensor.ok_battery_low", "off", device_class="battery"),
        state("sensor.temperature", "21", device_class="temperature"),
    ]
    b = health.parse_batteries(states, 15)
    assert [x["name"] for x in b["low"]] == ["Fenêtre", "Porte", "Détecteur fumée"]
    assert [x["name"] for x in b["unavailable"]] == ["Cave"]


# ---------------------------------------------------------------- journal
def test_journal_compte_la_derniere_heure():
    now = time.time()
    fmt = lambda ago: datetime.fromtimestamp(now - ago).strftime("%Y-%m-%d %H:%M:%S.123")  # noqa: E731
    text = "\n".join([
        f"{fmt(7200)} ERROR (MainThread) [homeassistant.components.vieux] trop ancien",
        f"{fmt(600)} ERROR (MainThread) [homeassistant.components.zha] Échec 1",
        f"{fmt(300)} ERROR (MainThread) [homeassistant.components.zha] Échec 2",
        f"{fmt(200)} WARNING (MainThread) [homeassistant.components.mqtt] lent",
        f"{fmt(100)} ERROR (SyncWorker_3) [custom_components.meteo] HTTP 500",
        "Traceback (most recent call last):",
        "  File \"x.py\", line 1",
    ])
    s = health.parse_error_log(text, now)
    assert s["errors_hour"] == 3 and s["warnings_hour"] == 1
    assert s["top_sources"][0] == ("homeassistant.components.zha", 2)
    assert s["last"][0]["source"] == "custom_components.meteo"


# ---------------------------------------------------------------- intégrations / réparations
def test_integrations_en_echec_et_reparations():
    entries = [
        {"domain": "zha", "title": "Zigbee", "state": "loaded"},
        {"domain": "netatmo", "title": "Netatmo", "state": "setup_retry", "reason": "timeout"},
        {"domain": "hue", "title": "Hue", "state": "setup_error", "disabled_by": "user"},
    ]
    assert [e["title"] for e in health.parse_config_entries(entries)] == ["Netatmo"]
    issues = {"issues": [
        {"domain": "hassio", "issue_id": "a", "translation_key": "unhealthy", "ignored": False},
        {"domain": "x", "issue_id": "b", "ignored": True},
    ]}
    assert [i["key"] for i in health.parse_repairs(issues)] == ["unhealthy"]


# ---------------------------------------------------------------- réseau
def test_declaration_des_equipements():
    text = """
    Routeur principal ; 192.168.1.1 ; critique
    Routeur étage (AiMesh) ; 192.168.1.2
    Imprimante ; 192.168.1.50 ; 631   # commentaire
    NAS web ; 192.168.1.5 ; https://192.168.1.5:5001
    ligne invalide
    Injection ; 1.2.3.4;rm -rf
    Routeur principal ; 10.0.0.1
    """
    d = network.parse_devices(text)
    assert [x["name"] for x in d] == ["Routeur principal", "Routeur étage (AiMesh)", "Imprimante", "NAS web", "Injection"]
    assert d[0]["critical"] and not d[1]["critical"]
    assert d[2]["port"] == 631 and d[3]["url"].startswith("https://")
    assert d[4]["port"] is None and d[4]["url"] is None
    assert network.parse_devices("Bad ; 1.2.3.4 && reboot") == []


def test_lecture_routeur_asus():
    data = {
        "productid": "RT-AX88U", "firmver": "3.0.0.6", "buildno": "102", "extendno": "34791",
        "uptime": "Sun, 22 Nov 2026 14:00:00 +0100(864000 secs since boot)",
        "cpu_usage": {"cpu1_total": "2000", "cpu1_usage": "500", "cpu2_total": "2000", "cpu2_usage": "300"},
        "memory_usage": {"mem_total": "1048576", "mem_free": "524288", "mem_used": "524288"},
        "get_cfg_clientlist": [[
            {"alias": "Salon", "model_name": "RT-AX88U", "ip": "192.168.1.1", "mac": "AA:AA:AA:AA:AA:01", "online": "1"},
            {"alias": "", "ui_model_name": "RT-AX58U", "ip": "192.168.1.2", "mac": "AA:AA:AA:AA:AA:02", "online": "0"},
        ]],
        "get_clientlist": {"maclist": ["11:11:11:11:11:11", "22:22:22:22:22:22"],
                           "11:11:11:11:11:11": {"isOnline": "1"}, "22:22:22:22:22:22": {"isOnline": "0"}},
    }
    first = network.parse_asus(data)
    assert first["model"] == "RT-AX88U" and first["firmware"] == "3.0.0.6.102.34791"
    assert first["uptime_s"] == 864000 and first["mem_percent"] == 50.0 and first["clients"] == 1
    assert first["cpu_percent"] is None   # il faut deux lectures
    assert [(n["name"], n["online"]) for n in first["nodes"]] == [("Salon", True), ("RT-AX58U", False)]
    data2 = dict(data, cpu_usage={"cpu1_total": "3000", "cpu1_usage": "750", "cpu2_total": "3000", "cpu2_usage": "550"})
    assert network.parse_asus(data2, first["cpu_counters"])["cpu_percent"] == 25.0


def test_json_asus_avec_virgules_en_trop():
    assert network._lenient_json('{"a": [1, 2,], "b": {"c": 1,},}') == {"a": [1, 2], "b": {"c": 1}}
