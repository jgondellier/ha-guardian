"""Surveillance du réseau : équipements (ping / port / URL) et routeur Asus AiMesh."""
import asyncio
import base64
import json
import logging
import re
import ssl
import time

import httpx

from . import config, db, devices

log = logging.getLogger("guardian.network")

state: dict = {
    "devices": [],      # résultats du dernier contrôle
    "asus": None,       # état du routeur principal et des nœuds AiMesh
}
_fails: dict[str, int] = {}
_asus_prev_cpu: dict = {}

ASUS_UA = "asusrouter-Android-DUTUtil-1.0.0.245"
ASUS_HOOKS = ("nvram_get(productid);nvram_get(firmver);nvram_get(buildno);nvram_get(extendno);"
              "uptime();cpu_usage(appobj);memory_usage(appobj);get_cfg_clientlist();get_clientlist()")


# ================================================================ équipements
def parse_devices(text: str) -> list[dict]:
    """Une ligne par équipement : « Nom ; hôte [; port ou URL] [; critique] ».
    « critique » = équipement indispensable au réseau (routeur principal) : s'il tombe,
    HA Guardian n'intervient pas sur Home Assistant (la panne vient du réseau)."""
    out, seen = [], set()
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        name, host = parts[0][:60], parts[1]
        if not re.match(r"^[A-Za-z0-9.\-:\[\]]+$", host) or name in seen:
            continue
        dev = {"name": name, "host": host, "port": None, "url": None, "critical": False}
        for extra in parts[2:]:
            low = extra.lower()
            if low in ("critique", "critical"):
                dev["critical"] = True
            elif low.isdigit() and 0 < int(low) < 65536:
                dev["port"] = int(low)
            elif low.startswith(("http://", "https://")):
                dev["url"] = extra
        seen.add(name)
        out.append(dev)
    return out[:30]


async def check_device(dev: dict) -> dict:
    start = time.perf_counter()
    if dev["url"]:
        try:
            async with httpx.AsyncClient(timeout=6) as client:
                r = await client.get(dev["url"])
            ok, how = r.status_code < 500, f"HTTP {r.status_code}"
        except httpx.ConnectError as exc:
            # Certificat non reconnu (auto-signé, courant sur le réseau local) : l'équipement
            # répond, ce qui suffit pour un contrôle de présence. Rien n'est envoyé.
            ok = _cert_error(exc)
            how = "HTTPS (certificat non reconnu)" if ok else type(exc).__name__
        except Exception as exc:  # noqa: BLE001
            ok, how = False, type(exc).__name__
    elif dev["port"]:
        ok, how = await devices.tcp_open(dev["host"], dev["port"]), f"port {dev['port']}"
    else:
        ok, how = await devices.ping(dev["host"]), "ping"
    return {**dev, "ok": ok, "how": how,
            "latency_ms": round((time.perf_counter() - start) * 1000) if ok else None}


def _cert_error(exc: BaseException) -> bool:
    while exc is not None:
        if isinstance(exc, ssl.SSLCertVerificationError):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


async def check_all(settings: dict) -> list[dict]:
    devs = parse_devices(settings["network_devices"])
    results = list(await asyncio.gather(*(check_device(d) for d in devs)))
    names = {d["name"] for d in devs}
    for name in list(_fails):
        if name not in names:
            _fails.pop(name)
    for r in results:
        _fails[r["name"]] = 0 if r["ok"] else _fails.get(r["name"], 0) + 1
        r["fails"] = _fails[r["name"]]
    state["devices"] = results
    return results


def critical_down() -> list[str]:
    """Équipements critiques en échec au dernier contrôle."""
    return [d["name"] for d in state["devices"] if d["critical"] and not d["ok"]]


async def alerts(settings: dict) -> None:
    from . import watchdog
    n = max(1, settings["network_fail_after"])
    for d in state["devices"]:
        await watchdog.alert(
            f"net:{d['name']}", d["fails"] >= n, "📶 Équipement réseau injoignable",
            f"{d['name']} ({d['host']}) ne répond plus depuis {d['fails']} contrôles.",
            recovered=f"{d['name']} de nouveau joignable",
            priority=5 if d["critical"] else 4)


# ================================================================ routeur Asus
def _lenient_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        # Certaines versions renvoient des virgules en trop
        return json.loads(re.sub(r",\s*([}\]])", r"\1", text))


def _find_nodes(obj) -> list[dict]:
    """La liste AiMesh est imbriquée différemment selon les firmwares : on cherche
    tous les dictionnaires qui décrivent un nœud (adresse MAC + état)."""
    found = []
    if isinstance(obj, dict):
        if "mac" in obj and ("online" in obj or "ip" in obj):
            found.append(obj)
        else:
            for v in obj.values():
                found += _find_nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            found += _find_nodes(v)
    return found


def parse_asus(data: dict, prev_cpu: dict | None = None) -> dict:
    res: dict = {
        "model": data.get("productid"),
        "firmware": ".".join(str(x) for x in (data.get("firmver"), data.get("buildno"),
                                                data.get("extendno")) if x) or None,
    }
    m = re.search(r"\((\d+) secs? since boot\)", str(data.get("uptime", "")))
    res["uptime_s"] = int(m.group(1)) if m else None

    mem = data.get("memory_usage") or {}
    try:
        total, used = float(mem["mem_total"]), float(mem["mem_used"])
        res["mem_percent"] = round(used / total * 100, 1) if total else None
    except (KeyError, TypeError, ValueError):
        res["mem_percent"] = None

    # cpu_usage donne des compteurs cumulés : le % se calcule entre deux lectures
    cpu = data.get("cpu_usage") or {}
    counters = {}
    for key, value in cpu.items():
        m2 = re.match(r"cpu(\d+)_(total|usage)$", key)
        if m2:
            try:
                counters.setdefault(m2.group(1), {})[m2.group(2)] = float(value)
            except (TypeError, ValueError):
                pass
    res["cpu_counters"] = counters
    res["cpu_percent"] = None
    if prev_cpu:
        dt = du = 0.0
        for core, c in counters.items():
            p = prev_cpu.get(core)
            if p and "total" in c and "usage" in c:
                dt += c["total"] - p.get("total", 0)
                du += c["usage"] - p.get("usage", 0)
        if dt > 0:
            res["cpu_percent"] = round(max(0.0, min(100.0, du / dt * 100)), 1)

    nodes = []
    for n in _find_nodes(data.get("get_cfg_clientlist")):
        online = str(n.get("online", "1")) == "1"
        nodes.append({
            "name": n.get("alias") or n.get("ui_model_name") or n.get("model_name") or n.get("ip") or n["mac"],
            "model": n.get("ui_model_name") or n.get("model_name"),
            "ip": n.get("ip"), "mac": n.get("mac"), "online": online,
            "firmware": n.get("fwver"),
        })
    res["nodes"] = nodes

    clients = data.get("get_clientlist") or {}
    if isinstance(clients, dict):
        macs = clients.get("maclist") or [k for k in clients if re.match(r"^[0-9A-F:]{17}$", k, re.I)]
        res["clients"] = sum(1 for mac in macs
                             if str((clients.get(mac) or {}).get("isOnline", "1")) == "1")
    else:
        res["clients"] = None
    return res


async def asus_fetch() -> dict:
    auth = base64.b64encode(f"{config.ASUS_USER}:{config.ASUS_PASSWORD}".encode()).decode()
    async with httpx.AsyncClient(timeout=12, verify=config.ASUS_VERIFY_TLS,
                                 headers={"User-Agent": ASUS_UA}) as client:
        r = await client.post(f"{config.ASUS_URL}/login.cgi",
                              data={"login_authorization": auth},
                              headers={"Referer": f"{config.ASUS_URL}/Main_Login.asp"})
        try:
            body = _lenient_json(r.text)
        except ValueError:
            body = {}
        token = body.get("asus_token") or client.cookies.get("asus_token")
        if not token:
            code = str(body.get("error_status", ""))
            raise RuntimeError({
                "3": "identifiant ou mot de passe refusé",
                "7": "connexion temporairement bloquée par le routeur (trop d'essais)",
                "10": "le routeur demande un captcha (trop d'essais)",
            }.get(code, f"connexion refusée ({code or r.status_code})"))
        client.cookies.set("asus_token", token)
        try:
            r = await client.get(f"{config.ASUS_URL}/appGet.cgi", params={"hook": ASUS_HOOKS})
            r.raise_for_status()
            return _lenient_json(r.text)
        finally:
            try:
                await client.get(f"{config.ASUS_URL}/Logout.asp")
            except Exception:  # noqa: BLE001
                pass


async def refresh_asus() -> None:
    from . import watchdog
    if not config.ASUS_ENABLED:
        return
    global _asus_prev_cpu
    try:
        parsed = parse_asus(await asus_fetch(), _asus_prev_cpu)
    except Exception as exc:  # noqa: BLE001
        state["asus"] = {"error": str(exc)[:200], "ts": time.time()}
        if "refusé" in str(exc) or "bloquée" in str(exc):
            db.add_event("warn", f"Routeur Asus : {exc}")
        return
    _asus_prev_cpu = parsed.pop("cpu_counters")
    parsed["ts"] = time.time()
    state["asus"] = parsed
    for node in parsed["nodes"]:
        await watchdog.alert(
            f"aimesh:{node['mac']}", not node["online"], "📶 Nœud AiMesh hors ligne",
            f"{node['name']} ({node.get('ip') or node['mac']}) n'est plus connecté au réseau maillé.",
            recovered=f"Nœud AiMesh {node['name']} reconnecté")
